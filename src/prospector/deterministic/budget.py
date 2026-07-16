"""Effort mapping and runtime task-budget injection, differentiated by research stage."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from prospector.schemas.brief import EffortLevel
from prospector.schemas.plan import ResearchStage, ResearchTask, ResearchTaskDraft, TaskBudget


@dataclass(frozen=True, slots=True)
class StageBudget:
    max_concurrency: int
    max_worker_rounds: int


@dataclass(frozen=True, slots=True)
class ResearchLimits:
    decision_round_limit: int
    stages: dict[ResearchStage, StageBudget]


# Worker rounds are the budget: they cap sequential depth (how long a worker keeps
# digging), while breadth is bounded by the task's subjects cap plus the runtime's
# per-round parallel tool-call limit. scout is cheap-and-wide (bounded candidate
# screening, high concurrency), deep_dive is expensive-and-narrow (one subject, one
# mechanism), verify is small and targeted.
EFFORT_LIMITS: dict[EffortLevel, ResearchLimits] = {
    "quick": ResearchLimits(
        8,
        {
            "scout": StageBudget(max_concurrency=6, max_worker_rounds=16),
            "deep_dive": StageBudget(max_concurrency=3, max_worker_rounds=32),
            "verify": StageBudget(max_concurrency=3, max_worker_rounds=12),
        },
    ),
    "standard": ResearchLimits(
        12,
        {
            "scout": StageBudget(max_concurrency=6, max_worker_rounds=24),
            "deep_dive": StageBudget(max_concurrency=3, max_worker_rounds=64),
            "verify": StageBudget(max_concurrency=3, max_worker_rounds=18),
        },
    ),
    "deep": ResearchLimits(
        24,
        {
            "scout": StageBudget(max_concurrency=8, max_worker_rounds=32),
            "deep_dive": StageBudget(max_concurrency=5, max_worker_rounds=81),
            "verify": StageBudget(max_concurrency=5, max_worker_rounds=24),
        },
    ),
}


def limits_for_effort(effort: EffortLevel) -> ResearchLimits:
    return EFFORT_LIMITS[effort]


def stage_budget(effort: EffortLevel, stage: ResearchStage) -> StageBudget:
    return limits_for_effort(effort).stages[stage]


def inject_task_budget(draft: ResearchTaskDraft, effort: EffortLevel) -> ResearchTask:
    budget = stage_budget(effort, draft.research_stage)
    return ResearchTask(
        **draft.model_dump(),
        task_id=uuid4(),
        budget=TaskBudget(max_worker_rounds=budget.max_worker_rounds),
    )
