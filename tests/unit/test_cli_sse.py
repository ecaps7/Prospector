from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from prospector.cli.client import CliConnectionError
from prospector.cli.sse import follow_events, parse_event_stream


def _frame(event_id: int, event_type: str, payload: dict[str, Any]) -> list[str]:
    data = json.dumps(
        {
            "event_type": event_type,
            "payload": payload,
            "task_id": None,
            "decision_round": None,
            "created_at": "2026-07-19T12:00:00+00:00",
        }
    )
    return [f"id: {event_id}", f"event: {event_type}", f"data: {data}", ""]


def test_parse_event_stream_ignores_heartbeat_and_accepts_multiline_data() -> None:
    payload = {
        "event_type": "job.phase_changed",
        "payload": {"phase": "research"},
        "task_id": None,
        "decision_round": 1,
        "created_at": None,
    }
    encoded = json.dumps(payload)
    split_at = encoded.index(",") + 1
    events = list(
        parse_event_stream(
            [
                ": heartbeat",
                "",
                "id: 7",
                "event: job.phase_changed",
                f"data: {encoded[:split_at]}",
                f"data: {encoded[split_at:]}",
                "",
            ]
        )
    )
    assert len(events) == 1
    assert events[0].id == 7
    assert events[0].event_type == "job.phase_changed"
    assert events[0].payload == {"phase": "research"}


def test_follow_events_reconnects_with_last_complete_event_id() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.cursors: list[int | None] = []

        def stream_events(
            self, _job_id: object, *, last_event_id: int | None
        ) -> Iterator[str]:
            self.cursors.append(last_event_id)
            if len(self.cursors) == 1:
                yield from _frame(10, "brief.confirmed", {"effort": "standard"})
                raise CliConnectionError("socket closed")
            yield from _frame(
                11,
                "job.stopped",
                {
                    "status": "completed",
                    "phase": "draft_rendered",
                    "outcome": "draft_rendered",
                    "error_code": None,
                },
            )

    client = FakeClient()
    sleeps: list[float] = []
    reconnects: list[float] = []
    events = list(
        follow_events(
            client,  # type: ignore[arg-type]
            uuid4(),
            sleep=sleeps.append,
            on_reconnect=reconnects.append,
        )
    )
    assert [event.id for event in events] == [10, 11]
    assert client.cursors == [None, 10]
    assert sleeps == [1.0]
    assert reconnects == [1.0]

