"""Research Verifier contracts and deterministic decision invariants."""

from __future__ import annotations

import hashlib
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

GapKind = Literal[
    "plan_coverage",
    "brief_alignment",
    "conflict",
    "source_credibility",
]
GapSeverity = Literal["minor", "major"]
VerifierTrigger = Literal["planner_finish", "budget_exhausted"]


class VerifierGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: GapKind
    severity: GapSeverity
    related_task_ids: list[UUID] = Field(default_factory=list)
    related_assertion_ids: list[UUID] = Field(default_factory=list)
    related_excerpt_ids: list[UUID] = Field(default_factory=list)
    description: str = Field(..., min_length=1)
    attempted_paths: list[str] = Field(default_factory=list)
    why_insufficient: str = Field(..., min_length=1)
    recommended_research: str = Field(..., min_length=1)


class ConflictJudgement(BaseModel):
    """LLM-facing conflict card: model points at assertions; code binds excerpts."""

    model_config = ConfigDict(extra="forbid")

    disputed_point: str = Field(..., min_length=1)
    assertion_ids: list[UUID] = Field(..., min_length=2)
    decision: Literal["present_both", "adjudicated"]
    winning_assertion_ids: list[UUID] = Field(default_factory=list)
    rationale: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _decision_matches_winners(self) -> ConflictJudgement:
        assertion_ids = set(self.assertion_ids)
        winners = set(self.winning_assertion_ids)
        if len(assertion_ids) != len(self.assertion_ids):
            raise ValueError("conflict assertion_ids must be unique")
        if self.decision == "adjudicated" and not winners:
            raise ValueError("adjudicated conflict requires winning_assertion_ids")
        if self.decision == "present_both" and winners:
            raise ValueError("present_both conflict must not select winners")
        if not winners.issubset(assertion_ids):
            raise ValueError("winning assertions must belong to the conflict")
        return self


class ConflictResolution(BaseModel):
    """Persisted conflict card with code-bound excerpt IDs."""

    model_config = ConfigDict(extra="forbid")

    disputed_point: str = Field(..., min_length=1)
    excerpt_ids: list[UUID] = Field(..., min_length=2)
    decision: Literal["present_both", "adjudicated"]
    winning_excerpt_ids: list[UUID] = Field(default_factory=list)
    rationale: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _decision_matches_winners(self) -> ConflictResolution:
        excerpt_ids = set(self.excerpt_ids)
        winners = set(self.winning_excerpt_ids)
        if len(excerpt_ids) != len(self.excerpt_ids):
            raise ValueError("conflict excerpt_ids must be unique")
        if self.decision == "adjudicated" and not winners:
            raise ValueError("adjudicated conflict requires winning_excerpt_ids")
        if self.decision == "present_both" and winners:
            raise ValueError("present_both conflict must not select winners")
        if not winners.issubset(excerpt_ids):
            raise ValueError("winning excerpts must belong to the conflict")
        return self


class AssertionDisposition(BaseModel):
    """Evidence-usability judgement for one assertion (LLM and persisted shape)."""

    model_config = ConfigDict(extra="forbid")

    assertion_id: UUID
    status: Literal["unusable", "restored"]
    reason: str = Field(..., min_length=1)


def _decision_matches_gaps(
    release_decision: Literal["pass", "needs_research"],
    brief_alignment: Literal["aligned", "misaligned"],
    gaps: list[VerifierGap],
) -> None:
    major_gaps = [gap for gap in gaps if gap.severity == "major"]
    if release_decision == "pass" and major_gaps:
        raise ValueError("pass must not contain major gaps")
    if release_decision == "needs_research" and not major_gaps:
        raise ValueError("needs_research requires at least one major gap")
    if brief_alignment == "misaligned" and not any(
        gap.kind == "brief_alignment" and gap.severity == "major" for gap in gaps
    ):
        raise ValueError("misaligned requires a major brief_alignment gap")


def _validate_assertion_dispositions(
    gaps: list[VerifierGap],
    dispositions: list[AssertionDisposition],
) -> None:
    seen: set[UUID] = set()
    for disposition in dispositions:
        if disposition.assertion_id in seen:
            raise ValueError("duplicate assertion disposition in one Verifier decision")
        seen.add(disposition.assertion_id)

    unusable = {
        item.assertion_id for item in dispositions if item.status == "unusable"
    }
    for gap in gaps:
        if gap.kind != "source_credibility":
            continue
        if not gap.related_assertion_ids:
            raise ValueError(
                "source_credibility gap must cite related_assertion_ids "
                "(cannot point at toxic evidence via excerpts alone)"
            )
        missing = [
            assertion_id
            for assertion_id in gap.related_assertion_ids
            if assertion_id not in unusable
        ]
        if missing:
            raise ValueError(
                "source_credibility gap assertions must be marked unusable "
                "in assertion_dispositions"
            )


class VerifierLlmDecision(BaseModel):
    """Model output schema: conflicts cite assertion_ids only."""

    model_config = ConfigDict(extra="forbid")

    release_decision: Literal["pass", "needs_research"]
    decision_reason: str = Field(
        ...,
        min_length=1,
        description="一句极短中文：为何作出当前放行判断",
    )
    brief_alignment: Literal["aligned", "misaligned"]
    coverage_rationale: str = Field(..., min_length=1)
    brief_alignment_rationale: str = Field(..., min_length=1)
    credibility_rationale: str = Field(..., min_length=1)
    gaps: list[VerifierGap] = Field(default_factory=list)
    conflict_judgements: list[ConflictJudgement] = Field(default_factory=list)
    assertion_dispositions: list[AssertionDisposition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decision_matches_gaps(self) -> VerifierLlmDecision:
        _decision_matches_gaps(self.release_decision, self.brief_alignment, self.gaps)
        _validate_assertion_dispositions(self.gaps, self.assertion_dispositions)
        return self


class VerifierDecision(BaseModel):
    """Persisted / graph-facing decision after conflict excerpt binding."""

    model_config = ConfigDict(extra="forbid")

    release_decision: Literal["pass", "needs_research"]
    decision_reason: str = Field(
        ...,
        min_length=1,
        description="一句极短中文：为何作出当前放行判断",
    )
    brief_alignment: Literal["aligned", "misaligned"]
    coverage_rationale: str = Field(..., min_length=1)
    brief_alignment_rationale: str = Field(..., min_length=1)
    credibility_rationale: str = Field(..., min_length=1)
    gaps: list[VerifierGap] = Field(default_factory=list)
    conflict_resolutions: list[ConflictResolution] = Field(default_factory=list)
    assertion_dispositions: list[AssertionDisposition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decision_matches_gaps(self) -> VerifierDecision:
        _decision_matches_gaps(self.release_decision, self.brief_alignment, self.gaps)
        _validate_assertion_dispositions(self.gaps, self.assertion_dispositions)
        return self


def conflict_key(excerpt_ids: list[UUID]) -> str:
    """Return the stable identity of one disputed excerpt set."""
    normalized = "\n".join(sorted(str(value) for value in set(excerpt_ids)))
    return "sha256:" + hashlib.sha256(normalized.encode()).hexdigest()


def _stable_unique(ids: list[UUID]) -> list[UUID]:
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for value in ids:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def materialize_conflict_resolutions(
    judgements: list[ConflictJudgement],
    assertion_excerpts: dict[UUID, list[UUID]],
) -> list[ConflictResolution]:
    """Bind model assertion references to authoritative excerpt IDs."""
    resolutions: list[ConflictResolution] = []
    for judgement in judgements:
        unknown = [aid for aid in judgement.assertion_ids if aid not in assertion_excerpts]
        if unknown:
            raise ValueError(
                "Conflict judgement references an assertion outside the current Job: "
                + ", ".join(str(value) for value in unknown)
            )
        excerpt_ids = _stable_unique(
            [
                excerpt_id
                for assertion_id in judgement.assertion_ids
                for excerpt_id in assertion_excerpts[assertion_id]
            ]
        )
        if len(excerpt_ids) < 2:
            raise ValueError(
                "Conflict judgement does not resolve to at least two distinct excerpts"
            )
        winning_excerpt_ids = _stable_unique(
            [
                excerpt_id
                for assertion_id in judgement.winning_assertion_ids
                for excerpt_id in assertion_excerpts[assertion_id]
            ]
        )
        resolutions.append(
            ConflictResolution(
                disputed_point=judgement.disputed_point,
                excerpt_ids=excerpt_ids,
                decision=judgement.decision,
                winning_excerpt_ids=winning_excerpt_ids,
                rationale=judgement.rationale,
            )
        )
    return resolutions


def materialize_verifier_decision(
    llm_decision: VerifierLlmDecision,
    assertion_excerpts: dict[UUID, list[UUID]],
) -> VerifierDecision:
    """Convert LLM output into the persisted VerifierDecision shape."""
    return VerifierDecision(
        release_decision=llm_decision.release_decision,
        decision_reason=llm_decision.decision_reason,
        brief_alignment=llm_decision.brief_alignment,
        coverage_rationale=llm_decision.coverage_rationale,
        brief_alignment_rationale=llm_decision.brief_alignment_rationale,
        credibility_rationale=llm_decision.credibility_rationale,
        gaps=llm_decision.gaps,
        conflict_resolutions=materialize_conflict_resolutions(
            llm_decision.conflict_judgements,
            assertion_excerpts,
        ),
        assertion_dispositions=list(llm_decision.assertion_dispositions),
    )


def effective_unusable_assertion_ids(
    dispositions_by_plan_version: list[tuple[int, list[AssertionDisposition]]],
) -> set[UUID]:
    """Fold versioned dispositions: last write wins; never-seen → usable."""
    status_by_id: dict[UUID, Literal["unusable", "restored"]] = {}
    for _plan_version, dispositions in sorted(
        dispositions_by_plan_version, key=lambda item: item[0]
    ):
        for disposition in dispositions:
            status_by_id[disposition.assertion_id] = disposition.status
    return {
        assertion_id
        for assertion_id, status in status_by_id.items()
        if status == "unusable"
    }


def assertion_excerpt_map_from_snapshot(snapshot: dict[str, object]) -> dict[UUID, list[UUID]]:
    """Build assertion_id → excerpt_ids from a verifier snapshot."""
    mapping: dict[UUID, list[UUID]] = {}
    assertions = snapshot.get("assertions") or []
    if not isinstance(assertions, list):
        return mapping
    for row in assertions:
        if not isinstance(row, dict):
            continue
        assertion_id = UUID(str(row["assertion_id"]))
        raw_ids = row.get("excerpt_ids") or []
        mapping[assertion_id] = [UUID(str(value)) for value in raw_ids]
    return mapping


def validate_verifier_references(
    decision: VerifierDecision,
    *,
    task_ids: set[UUID],
    assertion_ids: set[UUID],
    excerpt_ids: set[UUID],
) -> None:
    """Reject model-authored references outside the evaluated Job snapshot."""
    for gap in decision.gaps:
        if not set(gap.related_task_ids).issubset(task_ids):
            raise ValueError("Verifier gap references a task outside the current Job")
        if not set(gap.related_assertion_ids).issubset(assertion_ids):
            raise ValueError("Verifier gap references an assertion outside the current Job")
        if not set(gap.related_excerpt_ids).issubset(excerpt_ids):
            raise ValueError("Verifier gap references an excerpt outside the current Job")
    for resolution in decision.conflict_resolutions:
        if not set(resolution.excerpt_ids).issubset(excerpt_ids):
            raise ValueError("Conflict resolution references an excerpt outside the current Job")
    for disposition in decision.assertion_dispositions:
        if disposition.assertion_id not in assertion_ids:
            raise ValueError(
                "Assertion disposition references an assertion outside the current Job"
            )
