"""Effort mapping and runtime task-budget injection."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from prospector.schemas.brief import EffortLevel
from prospector.schemas.plan import ResearchTask, ResearchTaskDraft, TaskBudget


@dataclass(frozen=True, slots=True)
class ResearchLimits:
    decision_round_limit: int
    max_concurrency: int
    max_tool_calls: int


EFFORT_LIMITS: dict[EffortLevel, ResearchLimits] = {
    "quick": ResearchLimits(5, 3, 12),
    "standard": ResearchLimits(8, 3, 20),
    "deep": ResearchLimits(16, 5, 36),
}


def limits_for_effort(effort: EffortLevel) -> ResearchLimits:
    return EFFORT_LIMITS[effort]


def inject_task_budget(draft: ResearchTaskDraft, effort: EffortLevel) -> ResearchTask:
    limits = limits_for_effort(effort)
    return ResearchTask(
        **draft.model_dump(),
        task_id=uuid4(),
        budget=TaskBudget(max_tool_calls=limits.max_tool_calls),
    )
