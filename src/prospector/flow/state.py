"""Serializable state for the bounded Planner-Worker research graph."""

from __future__ import annotations

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
    # A passing Verifier's own evidence_needed is handed back to the Planner at most once
    # per Job. finish_withheld marks that one round, in which the Planner chooses how to
    # cover the named evidence but not whether to; it is separate from the once-per-Job
    # marker so a malformed decision retried inside that round cannot restore finish.
    follow_up_research_used: bool
    finish_withheld: bool
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
        "follow_up_research_used": False,
        "finish_withheld": False,
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
