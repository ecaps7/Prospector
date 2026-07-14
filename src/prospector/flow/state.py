"""Serializable graph state for the M0 empty/probe flow."""

from __future__ import annotations

import json
import operator
from typing import Annotated, TypedDict


class EmptyFlowState(TypedDict):
    job_id: str
    step: int
    notes: Annotated[list[str], operator.add]


def empty_flow_state_roundtrip(state: EmptyFlowState) -> EmptyFlowState:
    """Prove JSON round-trip (D7 serializability discipline)."""
    raw = json.dumps(
        {
            "job_id": state["job_id"],
            "step": state["step"],
            "notes": list(state["notes"]),
        }
    )
    loaded = json.loads(raw)
    return EmptyFlowState(
        job_id=loaded["job_id"],
        step=loaded["step"],
        notes=loaded["notes"],
    )
