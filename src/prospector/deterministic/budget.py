"""Effort mapping and runtime-owned task-budget injection."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from prospector.schemas.brief import EffortLevel
from prospector.schemas.plan import ResearchTask, ResearchTaskDraft, TaskBudget


@dataclass(frozen=True, slots=True)
class ResearchLimits:
    decision_round_limit: int
    max_concurrency: int
    max_worker_rounds: int


# Worker rounds cap sequential depth; max_concurrency and the runtime's per-round
# parallel tool-call limit cap breadth. Research strategy stays in the task book rather
# than being encoded as a resource profile.
#
# The round caps are sized from 137 finished tasks: median 4 rounds, 95th percentile 16,
# and only two of them ever stopped because the cap was reached. The cap therefore does
# not shape ordinary research -- it exists for the task that stops making progress and
# keeps trying. At 48 it was three times the 95th percentile, which let one task repeat
# the same two searches and three fetches for 27 minutes without saving a single new
# Assertion while every other task in the Job had long finished.
EFFORT_LIMITS: dict[EffortLevel, ResearchLimits] = {
    "quick": ResearchLimits(8, max_concurrency=6, max_worker_rounds=12),
    "standard": ResearchLimits(12, max_concurrency=5, max_worker_rounds=20),
    "deep": ResearchLimits(24, max_concurrency=6, max_worker_rounds=32),
}


def limits_for_effort(effort: EffortLevel) -> ResearchLimits:
    return EFFORT_LIMITS[effort]


def inject_task_budget(
    draft: ResearchTaskDraft,
    effort: EffortLevel,
) -> ResearchTask:
    limits = limits_for_effort(effort)
    return ResearchTask(
        **draft.model_dump(),
        task_id=uuid4(),
        budget=TaskBudget(max_worker_rounds=limits.max_worker_rounds),
    )
