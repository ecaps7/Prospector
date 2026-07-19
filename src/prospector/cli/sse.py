"""SSE parsing and reconnect semantics for CLI attach."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from prospector.cli.client import CliConnectionError, CliProtocolError, ProspectorClient


@dataclass(frozen=True, slots=True)
class ServerEvent:
    id: int
    event_type: str
    payload: dict[str, Any]
    task_id: str | None
    decision_round: int | None
    created_at: str | None

    def as_timeline_event(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "payload": self.payload,
            "task_id": self.task_id,
            "decision_round": self.decision_round,
            "created_at": self.created_at,
        }


def parse_event_stream(lines: Iterable[str]) -> Iterator[ServerEvent]:
    """Parse complete SSE frames; comments and incomplete trailing frames are ignored."""
    event_id: str | None = None
    event_name: str | None = None
    data_lines: list[str] = []

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line == "":
            if not data_lines:
                event_id = None
                event_name = None
                continue
            if event_id is None:
                raise CliProtocolError("SSE event is missing id")
            try:
                parsed_id = int(event_id)
                if parsed_id < 0:
                    raise ValueError
                data = json.loads("\n".join(data_lines))
                if not isinstance(data, dict):
                    raise TypeError
                event_type = str(data.get("event_type") or event_name or "")
                payload = data.get("payload") or {}
                if not event_type or not isinstance(payload, dict):
                    raise TypeError
                decision_round = data.get("decision_round")
                yield ServerEvent(
                    id=parsed_id,
                    event_type=event_type,
                    payload=payload,
                    task_id=None if data.get("task_id") is None else str(data["task_id"]),
                    decision_round=(None if decision_round is None else int(decision_round)),
                    created_at=(
                        None if data.get("created_at") is None else str(data["created_at"])
                    ),
                )
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise CliProtocolError("Invalid SSE event") from exc
            event_id = None
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)


def follow_events(
    client: ProspectorClient,
    job_id: UUID,
    *,
    sleep: Callable[[float], None] = time.sleep,
    on_reconnect: Callable[[float], None] | None = None,
    on_connected: Callable[[], None] | None = None,
) -> Iterator[ServerEvent]:
    """Follow a Job until job.stopped, reconnecting from the last committed event id."""
    cursor: int | None = None
    delay = 1.0
    while True:
        try:
            received = False
            stream = (
                client.stream_events(job_id, last_event_id=cursor)
                if on_connected is None
                else client.stream_events(
                    job_id,
                    last_event_id=cursor,
                    on_open=on_connected,
                )
            )
            for event in parse_event_stream(stream):
                if cursor is not None and event.id <= cursor:
                    raise CliProtocolError("SSE event ids are not strictly increasing")
                received = True
                cursor = event.id
                delay = 1.0
                yield event
                if event.event_type == "job.stopped":
                    return
            if received:
                delay = 1.0
        except CliConnectionError:
            pass
        if on_reconnect is not None:
            on_reconnect(delay)
        sleep(delay)
        delay = min(delay * 2.0, 30.0)
