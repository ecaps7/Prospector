"""Planner and Worker hard gates expressed as pure functions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PlannerRejection(StrEnum):
    OVER_CONCURRENCY = "over_concurrency"
    SCHEMA_ERROR = "schema_error"
    EMPTY_FINISH = "empty_finish"


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
