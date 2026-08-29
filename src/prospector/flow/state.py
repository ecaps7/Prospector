"""Serializable state for the bounded Planner-Worker research graph."""

from __future__ import annotations

import json
from typing import Any, Literal, TypedDict


class ResearchState(TypedDict):
    job_id: str
    phase: str
    brief_id: str
    plan_version: int
    # decision_round is the monotonic storage key used for idempotent replay; it advances
    # on every Planner turn including malformed output. research_decisions_used is the
    # budget: it counts only genuine research decisions, so a formatting failure never
    # eats a round the model could have spent researching.
    decision_round: int
    research_decisions_used: int
    consecutive_schema_errors: int
    decision_round_limit: int
    active_task_ids: list[str]
    last_verifier_run_id: str | None
    synthesis_run_id: str | None
    report_id: str | None
    report_markdown_ref: str | None
    report_json_ref: str | None
    outcome: str | None
    error_code: str | None
    planner_messages: list[dict[str, Any]]
    verifier_trigger: Literal["planner_finish", "budget_exhausted", "synthesis_gap"] | None
    route: Literal[
        "planner",
        "workers",
        "verifier",
        "synthesis",
        "writer",
        "attribution",
        "review",
        "render",
        "end",
    ]


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
        "research_decisions_used": 0,
        "consecutive_schema_errors": 0,
        "decision_round_limit": 0,
        "active_task_ids": [],
        "last_verifier_run_id": None,
        "synthesis_run_id": None,
        "report_id": None,
        "report_markdown_ref": None,
        "report_json_ref": None,
        "outcome": None,
        "error_code": None,
        "planner_messages": [],
        "verifier_trigger": None,
        "route": "planner",
    }
