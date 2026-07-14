"""Serializable state for the bounded Planner-Worker research graph."""

from __future__ import annotations

import json
from typing import Any, Literal, TypedDict


class ResearchState(TypedDict):
    job_id: str
    phase: str
    brief_id: str
    plan_version: int
    decision_round: int
    decision_round_limit: int
    max_concurrency: int
    max_tool_calls: int
    active_task_ids: list[str]
    last_verifier_run_id: str | None
    outcome: str | None
    error_code: str | None
    planner_messages: list[dict[str, Any]]
    route: Literal["planner", "workers", "end"]


def research_state_roundtrip(state: ResearchState) -> ResearchState:
    """Assert that checkpoint state contains only JSON-serializable values."""
    return ResearchState(**json.loads(json.dumps(state, ensure_ascii=False)))


def initial_research_state(*, job_id: str, brief_id: str) -> ResearchState:
    return {
        "job_id": job_id,
        "phase": "initialize",
        "brief_id": brief_id,
        "plan_version": 0,
        "decision_round": 0,
        "decision_round_limit": 0,
        "max_concurrency": 0,
        "max_tool_calls": 0,
        "active_task_ids": [],
        "last_verifier_run_id": None,
        "outcome": None,
        "error_code": None,
        "planner_messages": [],
        "route": "planner",
    }
