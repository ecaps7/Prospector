"""Planner and Worker hard gates expressed as pure functions."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from prospector.schemas.verifier import VerifierGap


class PlannerRejection(StrEnum):
    OVER_CONCURRENCY = "over_concurrency"
    SCHEMA_ERROR = "schema_error"
    EMPTY_FINISH = "empty_finish"
    FINISH_WITHHELD = "finish_withheld"


def dispatch_rejection(task_count: int, max_concurrency: int) -> PlannerRejection | None:
    if task_count > max_concurrency:
        return PlannerRejection.OVER_CONCURRENCY
    return None


def finish_rejection(
    excerpt_count: int,
    *,
    finish_withheld: bool = False,
) -> PlannerRejection | None:
    if excerpt_count == 0:
        return PlannerRejection.EMPTY_FINISH
    if finish_withheld:
        # Withholding finish has to be a gate rather than a line in the runtime state:
        # available_decisions only advises the model, and the round exists precisely
        # because the Planner had already talked itself into finishing.
        return PlannerRejection.FINISH_WITHHELD
    return None


@dataclass(slots=True)
class InformationGainCounter:
    consecutive_empty_saves: int = 0

    def record_save(self, inserted_rows: int) -> bool:
        self.consecutive_empty_saves = self.consecutive_empty_saves + 1 if inserted_rows == 0 else 0
        return self.consecutive_empty_saves >= 2


# Three identical rounds is the point where repetition stops looking like a Worker
# narrowing in and starts looking like one that cannot tell it has already failed.
REPEATED_ROUND_LIMIT = 3


def round_fingerprint(calls: Iterable[tuple[str, dict[str, object]]]) -> str:
    """Identify a round by the actions it took, independent of their order.

    Only the model's own calls belong here.  Runtime fan-out -- the automatic fetch of a
    search's top results -- is derived from them, so including it would count the same
    decision twice.
    """
    return json.dumps(
        sorted((name, json.dumps(args, sort_keys=True, default=str)) for name, args in calls),
        ensure_ascii=False,
    )


@dataclass(slots=True)
class RepeatedRoundCounter:
    """Stop a Worker that keeps making the same calls and saving nothing.

    The round cap was the only thing that ended such a task, and it ended it far too
    late: one Worker in a real run repeated the same two searches and three fetches for
    27 minutes, saving no Assertion, while every other task in the Job had long finished.
    A cap cannot tell that apart from slow progress -- identical actions with nothing
    stored can, and it costs no model call to notice.
    """

    fingerprint: str | None = field(default=None)
    repeats: int = 0

    def record_round(self, fingerprint: str, *, inserted_rows: int) -> bool:
        if inserted_rows > 0 or fingerprint != self.fingerprint:
            self.fingerprint = fingerprint
            self.repeats = 1 if inserted_rows == 0 else 0
            return False
        self.repeats += 1
        return self.repeats >= REPEATED_ROUND_LIMIT


# A passing Verifier still writes down what it could not find. For gaps it judged minor
# that note had exactly one destination -- the report's stated limits -- so nobody ever
# went after the evidence it named: one Job closed research after a single dispatch with
# a minor gap whose evidence_needed read as a ready-made task. The Verifier's own
# evidence_needed is the signal here, because a gap it cannot say how to close is a
# disclosure rather than a task, and the budget test keeps the extra round to the runs
# where it is nearly free.
FOLLOW_UP_BUDGET_DIVISOR = 3


def follow_up_research_gaps(
    gaps: Sequence[VerifierGap],
    *,
    research_decisions_used: int,
    decision_round_limit: int,
    already_used: bool,
) -> list[VerifierGap]:
    """Gaps a passing Verifier named evidence for, while another round still costs little."""
    if already_used:
        return []
    if research_decisions_used * FOLLOW_UP_BUDGET_DIVISOR > decision_round_limit:
        return []
    return [gap for gap in gaps if gap.evidence_needed]
