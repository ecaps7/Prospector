"""Planner and Worker hard gates expressed as pure functions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class PlannerRejection(StrEnum):
    OVER_CONCURRENCY = "over_concurrency"
    MIXED_STAGE = "mixed_stage"
    STAGE_ORDER = "stage_order"
    SCHEMA_ERROR = "schema_error"
    EMPTY_FINISH = "empty_finish"


# A batch that mixes stages, or that skips scout, is a planning mistake rather than a
# formatting one: both are checked here so they consume research budget and come back
# as a readable rejection, instead of surfacing as a schema error the model must guess at.
def mixed_stage_rejection(stages: Sequence[str]) -> PlannerRejection | None:
    if len(set(stages)) > 1:
        return PlannerRejection.MIXED_STAGE
    return None


def stage_order_rejection(stage: str, *, scout_dispatched: bool) -> PlannerRejection | None:
    if stage != "scout" and not scout_dispatched:
        return PlannerRejection.STAGE_ORDER
    return None


def dispatch_rejection(task_count: int, max_concurrency: int) -> PlannerRejection | None:
    if task_count > max_concurrency:
        return PlannerRejection.OVER_CONCURRENCY
    return None


def finish_rejection(excerpt_count: int) -> PlannerRejection | None:
    if excerpt_count == 0:
        return PlannerRejection.EMPTY_FINISH
    return None


@dataclass(slots=True)
class InformationGainCounter:
    consecutive_empty_saves: int = 0

    def record_save(self, inserted_rows: int) -> bool:
        self.consecutive_empty_saves = self.consecutive_empty_saves + 1 if inserted_rows == 0 else 0
        return self.consecutive_empty_saves >= 2
