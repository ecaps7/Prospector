"""SSE framing for persisted research events."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def encode_event(event: Mapping[str, Any]) -> str:
    data = {
        "event_type": str(event["event_type"]),
        "payload": dict(event.get("payload") or {}),
        "task_id": event.get("task_id"),
        "decision_round": event.get("decision_round"),
        "created_at": event.get("created_at"),
    }
    payload = json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))
    return f"id: {int(event['id'])}\nevent: {event['event_type']}\ndata: {payload}\n\n"


def heartbeat() -> str:
    return ": heartbeat\n\n"
