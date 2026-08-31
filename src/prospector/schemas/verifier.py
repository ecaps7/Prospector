"""Research Verifier contracts and deterministic decision invariants."""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

GapKind = Literal[
    "plan_coverage",
    "brief_alignment",
    "conflict",
    "source_credibility",
]
GapSeverity = Literal["minor", "major"]
VerifierTrigger = Literal["planner_finish", "budget_exhausted", "synthesis_gap"]
TaskRef = Annotated[str, StringConstraints(pattern=r"^t[1-9][0-9]*$")]
AssertionRef = Annotated[str, StringConstraints(pattern=r"^a[1-9][0-9]*$")]


class VerifierGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: GapKind
    severity: GapSeverity
    related_task_ids: list[UUID] = Field(default_factory=list)
    related_assertion_ids: list[UUID] = Field(default_factory=list)
    description: str = Field(..., min_length=1)
    evidence_needed: str = Field(
        default="",
        description="重大缺口仍需要什么证据；不规定 Planner 如何拆分或执行任务",
    )

    @model_validator(mode="after")
    def _major_gap_names_needed_evidence(self) -> VerifierGap:
        self.description = self.description.strip()
        self.evidence_needed = self.evidence_needed.strip()
        if not self.description:
            raise ValueError("gap description must not be blank")
        if self.severity == "major" and not self.evidence_needed:
            raise ValueError("major gap requires evidence_needed")
        return self


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
    """Evidence-usability judgement for one assertion (LLM and persisted shape).

    ``granularity`` records an Assertion that packs several separately checkable facts
    into one row.  That is a real cost -- a Claim bound to a bundle says less about which
    fact it rests on -- but it is a packaging cost, not a truth one, and it used to be
    answered with deletion: of 187 disqualifications across this project's Jobs, 122 were
    merged facts that the bound Excerpt fully supported, and one Job lost 47 of its 51
    disqualified Assertions that way. The material stays usable and the note travels;
    attribution binds Claims to Excerpts as well as Assertions, so a bundled statement
    still points a reader at the passage it came from.
    """

    model_config = ConfigDict(extra="forbid")

    assertion_id: UUID
    status: Literal["unusable", "granularity", "restored"]
    reason: str = Field(..., min_length=1)


def _decision_matches_gaps(
    release_decision: Literal["pass", "needs_research"],
    gaps: list[VerifierGap],
) -> None:
    major_gaps = [gap for gap in gaps if gap.severity == "major"]
    if release_decision == "pass" and major_gaps:
        raise ValueError("pass must not contain major gaps")
    if release_decision == "needs_research" and not major_gaps:
        raise ValueError("needs_research requires at least one major gap")


DERIVED_UNUSABLE_REASON = (
    "由代码推导：该断言被列入 major source_credibility 缺口，按定义不可用于成文。"
)


def _validate_assertion_dispositions(dispositions: list[AssertionDisposition]) -> None:
    seen: set[UUID] = set()
    for disposition in dispositions:
        if disposition.assertion_id in seen:
            raise ValueError("duplicate assertion disposition in one Verifier decision")
        seen.add(disposition.assertion_id)


def _validate_credibility_gaps_name_assertions(gaps: list[VerifierGap]) -> None:
    """Every credibility gap must say *which* assertions it is about, at any severity.

    Naming is all the model is asked for here, and it can always comply. Whether the named
    assertions are then discarded depends on severity and is decided by code
    (``derive_credibility_dispositions``), not by the model's own bookkeeping.
    """
    for gap in gaps:
        if gap.kind != "source_credibility":
            continue
        if not gap.related_assertion_ids:
            raise ValueError(
                "source_credibility gap must cite related_assertion_ids "
                "(cannot point at toxic evidence via excerpts alone)"
            )


def _validate_major_credibility_is_discarded(
    gaps: list[VerifierGap],
    dispositions: list[AssertionDisposition],
) -> None:
    """Post-derivation invariant: evidence called fatally unreliable must be unusable.

    Scoped to ``major`` on purpose. A *minor* credibility gap is the disclosable-but-not
    -blocking case the Verifier prompt defines, so requiring disposals for it would make
    "this source is weak, disclose it, but the finding stands" an illegal judgement --
    which is the most ordinary credibility call there is.
    """
    unusable = {item.assertion_id for item in dispositions if item.status == "unusable"}
    for gap in gaps:
        if gap.kind != "source_credibility" or gap.severity != "major":
            continue
        missing = [
            assertion_id
            for assertion_id in gap.related_assertion_ids
            if assertion_id not in unusable
        ]
        if missing:
            raise ValueError(
                "major source_credibility gap assertions must be marked unusable "
                "in assertion_dispositions"
            )


def derive_credibility_dispositions(
    gaps: list[VerifierGap],
    dispositions: list[AssertionDisposition],
) -> list[AssertionDisposition]:
    """Force every assertion named in a *major* credibility gap to ``unusable``.

    The model owns the open judgement -- is this source unreliable, and how badly. The
    mechanical consequence -- fatally unreliable therefore unusable -- is code's, so a
    Verifier that names toxic evidence and then forgets to discard it gets corrected
    instead of rejected. Deriving beats validating here: no model round trip, no run lost
    to bookkeeping, and no way to name toxic evidence in a major gap and still keep it.

    Minor gaps are left exactly as the model wrote them.
    """
    forced: list[UUID] = []
    for gap in gaps:
        if gap.kind != "source_credibility" or gap.severity != "major":
            continue
        for assertion_id in gap.related_assertion_ids:
            if assertion_id not in forced:
                forced.append(assertion_id)
    if not forced:
        return list(dispositions)

    forced_set = set(forced)
    derived = [
        AssertionDisposition(
            assertion_id=item.assertion_id,
            status="unusable" if item.assertion_id in forced_set else item.status,
            reason=(
                f"{item.reason}（{DERIVED_UNUSABLE_REASON}）"
                if item.assertion_id in forced_set and item.status != "unusable"
                else item.reason
            ),
        )
        for item in dispositions
    ]
    already_disposed = {item.assertion_id for item in dispositions}
    derived.extend(
        AssertionDisposition(
            assertion_id=assertion_id,
            status="unusable",
            reason=DERIVED_UNUSABLE_REASON,
        )
        for assertion_id in forced
        if assertion_id not in already_disposed
    )
    return derived


class SourceCredibilityFinding(BaseModel):
    """A usable finding whose source still constrains how strongly it may be used."""

    model_config = ConfigDict(extra="forbid")

    related_assertion_ids: list[UUID] = Field(..., min_length=1)
    description: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _normalize(self) -> SourceCredibilityFinding:
        self.description = self.description.strip()
        if not self.description:
            raise ValueError("source credibility finding description must not be blank")
        if len(set(self.related_assertion_ids)) != len(self.related_assertion_ids):
            raise ValueError("source credibility finding assertion ids must be unique")
        return self


class VerifierEvidenceReview(BaseModel):
    """First-pass evidence qualification; it deliberately has no release decision."""

    model_config = ConfigDict(extra="forbid")

    source_credibility_findings: list[SourceCredibilityFinding] = Field(default_factory=list)
    conflicts: list[ConflictJudgement] = Field(default_factory=list)
    assertion_dispositions: list[AssertionDisposition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_review(self) -> VerifierEvidenceReview:
        _validate_assertion_dispositions(self.assertion_dispositions)
        return self


class CoreAnswerabilityCheck(BaseModel):
    """One explicit Brief requirement and the qualified evidence that answers it."""

    model_config = ConfigDict(extra="forbid")

    requirement: str = Field(..., min_length=1)
    status: Literal["answered", "blocked"]
    answer: str = Field(
        default="",
        description="The bounded answer supported by the cited assertions; empty when blocked",
    )
    supporting_assertion_ids: list[UUID] = Field(default_factory=list)
    evidence_bridge: str = Field(
        default="",
        description=(
            "Why the cited assertions answer this requirement rather than merely relate to it"
        ),
    )
    evidence_needed: str = Field(
        default="",
        description="What evidence is missing when this requirement is blocked",
    )

    @model_validator(mode="after")
    def _status_matches_evidence(self) -> CoreAnswerabilityCheck:
        self.requirement = self.requirement.strip()
        self.answer = self.answer.strip()
        self.evidence_bridge = self.evidence_bridge.strip()
        self.evidence_needed = self.evidence_needed.strip()
        if not self.requirement:
            raise ValueError("answerability requirement must not be blank")
        if len(set(self.supporting_assertion_ids)) != len(self.supporting_assertion_ids):
            raise ValueError("answerability supporting assertion ids must be unique")
        if self.status == "answered":
            if not self.answer:
                raise ValueError("answered requirement requires an answer")
            if not self.supporting_assertion_ids:
                raise ValueError("answered requirement requires supporting_assertion_ids")
            if not self.evidence_bridge:
                raise ValueError("answered requirement requires an evidence_bridge")
            if self.evidence_needed:
                raise ValueError("answered requirement must not include evidence_needed")
        else:
            if self.answer or self.evidence_bridge:
                raise ValueError("blocked requirement must not claim an answer or evidence_bridge")
            if not self.evidence_needed:
                raise ValueError("blocked requirement requires evidence_needed")
        return self


class VerifierCoverageDecision(BaseModel):
    """Second-pass answerability decision over the qualified evidence projection."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["pass", "needs_research"]
    reason: str = Field(..., min_length=1, description="为何放行或返回 Planner")
    answerability_checks: list[CoreAnswerabilityCheck] = Field(..., min_length=1)
    gaps: list[VerifierGap] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decision_matches_gaps(self) -> VerifierCoverageDecision:
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("Verifier reason must not be blank")
        _decision_matches_gaps(self.decision, self.gaps)
        _validate_credibility_gaps_name_assertions(self.gaps)
        blocked = [check for check in self.answerability_checks if check.status == "blocked"]
        if self.decision == "pass" and blocked:
            raise ValueError("pass must not contain blocked answerability checks")
        if self.decision == "needs_research" and not blocked:
            raise ValueError("needs_research requires a blocked answerability check")
        return self


class VerifierGapRefs(BaseModel):
    """Model-facing gap contract in the frozen snapshot's local namespace."""

    model_config = ConfigDict(extra="forbid")

    kind: GapKind
    severity: GapSeverity
    related_task_refs: list[TaskRef] = Field(default_factory=list)
    related_assertion_refs: list[AssertionRef] = Field(default_factory=list)
    description: str = Field(..., min_length=1)
    evidence_needed: str = ""

    @model_validator(mode="after")
    def _major_gap_names_needed_evidence(self) -> VerifierGapRefs:
        self.description = self.description.strip()
        self.evidence_needed = self.evidence_needed.strip()
        if not self.description:
            raise ValueError("gap description must not be blank")
        if self.severity == "major" and not self.evidence_needed:
            raise ValueError("major gap requires evidence_needed")
        return self


class ConflictJudgementRefs(BaseModel):
    """Model-facing conflict whose Assertions use local ``aN`` references."""

    model_config = ConfigDict(extra="forbid")

    disputed_point: str = Field(..., min_length=1)
    assertion_refs: list[AssertionRef] = Field(..., min_length=2)
    decision: Literal["present_both", "adjudicated"]
    winning_assertion_refs: list[AssertionRef] = Field(default_factory=list)
    rationale: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _decision_matches_winners(self) -> ConflictJudgementRefs:
        assertions = set(self.assertion_refs)
        winners = set(self.winning_assertion_refs)
        if len(assertions) != len(self.assertion_refs):
            raise ValueError("conflict assertion_refs must be unique")
        if self.decision == "adjudicated" and not winners:
            raise ValueError("adjudicated conflict requires winning_assertion_refs")
        if self.decision == "present_both" and winners:
            raise ValueError("present_both conflict must not select winners")
        if not winners.issubset(assertions):
            raise ValueError("winning assertions must belong to the conflict")
        return self


class AssertionDispositionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_ref: AssertionRef
    status: Literal["unusable", "granularity", "restored"]
    reason: str = Field(..., min_length=1)


class SourceCredibilityFindingRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    related_assertion_refs: list[AssertionRef] = Field(..., min_length=1)
    description: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _normalize(self) -> SourceCredibilityFindingRefs:
        self.description = self.description.strip()
        if not self.description:
            raise ValueError("source credibility finding description must not be blank")
        if len(set(self.related_assertion_refs)) != len(self.related_assertion_refs):
            raise ValueError("source credibility finding assertion refs must be unique")
        return self


class VerifierEvidenceReviewRefs(BaseModel):
    """Qualification-pass wire contract; storage UUIDs never reach model output."""

    model_config = ConfigDict(extra="forbid")

    source_credibility_findings: list[SourceCredibilityFindingRefs] = Field(default_factory=list)
    conflicts: list[ConflictJudgementRefs] = Field(default_factory=list)
    assertion_dispositions: list[AssertionDispositionRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dispositions_are_unique(self) -> VerifierEvidenceReviewRefs:
        refs = [item.assertion_ref for item in self.assertion_dispositions]
        if len(refs) != len(set(refs)):
            raise ValueError("duplicate assertion disposition in one Verifier decision")
        return self


class CoreAnswerabilityCheckRefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement: str = Field(..., min_length=1)
    status: Literal["answered", "blocked"]
    answer: str = ""
    supporting_assertion_refs: list[AssertionRef] = Field(default_factory=list)
    evidence_bridge: str = ""
    evidence_needed: str = ""

    @model_validator(mode="after")
    def _status_matches_evidence(self) -> CoreAnswerabilityCheckRefs:
        self.requirement = self.requirement.strip()
        self.answer = self.answer.strip()
        self.evidence_bridge = self.evidence_bridge.strip()
        self.evidence_needed = self.evidence_needed.strip()
        if not self.requirement:
            raise ValueError("answerability requirement must not be blank")
        if len(set(self.supporting_assertion_refs)) != len(self.supporting_assertion_refs):
            raise ValueError("answerability supporting assertion refs must be unique")
        if self.status == "answered":
            if not self.answer:
                raise ValueError("answered requirement requires an answer")
            if not self.supporting_assertion_refs:
                raise ValueError("answered requirement requires supporting_assertion_refs")
            if not self.evidence_bridge:
                raise ValueError("answered requirement requires an evidence_bridge")
            if self.evidence_needed:
                raise ValueError("answered requirement must not include evidence_needed")
        else:
            if self.answer or self.evidence_bridge:
                raise ValueError("blocked requirement must not claim an answer or evidence_bridge")
            if not self.evidence_needed:
                raise ValueError("blocked requirement requires evidence_needed")
        return self


class VerifierCoverageDecisionRefs(BaseModel):
    """Coverage-pass wire contract in the same local namespace as qualification."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["pass", "needs_research"]
    reason: str = Field(..., min_length=1)
    answerability_checks: list[CoreAnswerabilityCheckRefs] = Field(..., min_length=1)
    gaps: list[VerifierGapRefs] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decision_matches_gaps(self) -> VerifierCoverageDecisionRefs:
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("Verifier reason must not be blank")
        major_gaps = [gap for gap in self.gaps if gap.severity == "major"]
        if self.decision == "pass" and major_gaps:
            raise ValueError("pass must not contain major gaps")
        if self.decision == "needs_research" and not major_gaps:
            raise ValueError("needs_research requires at least one major gap")
        for gap in self.gaps:
            if gap.kind == "source_credibility" and not gap.related_assertion_refs:
                raise ValueError(
                    "source_credibility gap must cite related_assertion_refs "
                    "(cannot point at toxic evidence via excerpts alone)"
                )
        blocked = [check for check in self.answerability_checks if check.status == "blocked"]
        if self.decision == "pass" and blocked:
            raise ValueError("pass must not contain blocked answerability checks")
        if self.decision == "needs_research" and not blocked:
            raise ValueError("needs_research requires a blocked answerability check")
        return self


class VerifierDecision(BaseModel):
    """Persisted / graph-facing decision after conflict excerpt binding."""

    model_config = ConfigDict(extra="forbid")

    release_decision: Literal["pass", "needs_research"]
    decision_reason: str = Field(..., min_length=1, description="为何放行或返回 Planner")
    gaps: list[VerifierGap] = Field(default_factory=list)
    conflict_resolutions: list[ConflictResolution] = Field(default_factory=list)
    assertion_dispositions: list[AssertionDisposition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _decision_matches_gaps(self) -> VerifierDecision:
        # Persisted shape: the full invariant, which now holds by construction because
        # derive_credibility_dispositions ran first. It is a genuine assertion about
        # system state rather than a bet on how the model filled in two fields.
        self.decision_reason = self.decision_reason.strip()
        if not self.decision_reason:
            raise ValueError("Verifier decision_reason must not be blank")
        _decision_matches_gaps(self.release_decision, self.gaps)
        _validate_assertion_dispositions(self.assertion_dispositions)
        _validate_credibility_gaps_name_assertions(self.gaps)
        _validate_major_credibility_is_discarded(self.gaps, self.assertion_dispositions)
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
            # Named, not just diagnosed: this message is handed straight back to the Verifier
            # as its retry instruction, and "one of your judgements is illegal" is not
            # something a model can act on when it submitted several.
            shared = ", ".join(str(value) for value in excerpt_ids) or "no excerpt at all"
            raise ValueError(
                f"Conflict judgement {judgement.disputed_point!r} does not resolve to at "
                "least two distinct excerpts: assertions "
                f"{', '.join(str(value) for value in judgement.assertion_ids)} "
                f"all rest on {shared}. Assertions that contradict each other while resting "
                "on the same excerpt are a transcription error inside one source, not a "
                "conflict between sources; drop the judgement and mark the wrong assertion "
                "unusable in assertion_dispositions instead."
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
    evidence_review: VerifierEvidenceReview,
    coverage_decision: VerifierCoverageDecision,
    assertion_excerpts: dict[UUID, list[UUID]],
) -> VerifierDecision:
    """Combine the two model passes into the persisted graph-facing decision."""
    return VerifierDecision(
        release_decision=coverage_decision.decision,
        decision_reason=coverage_decision.reason,
        gaps=coverage_decision.gaps,
        conflict_resolutions=materialize_conflict_resolutions(
            evidence_review.conflicts,
            assertion_excerpts,
        ),
        assertion_dispositions=derive_credibility_dispositions(
            coverage_decision.gaps,
            evidence_review.assertion_dispositions,
        ),
    )


def effective_unusable_assertion_ids(
    dispositions_by_plan_version: list[tuple[int, list[AssertionDisposition]]],
) -> set[UUID]:
    """Fold versioned dispositions: last write wins; never-seen → usable."""
    status_by_id: dict[UUID, Literal["unusable", "granularity", "restored"]] = {}
    for _plan_version, dispositions in sorted(
        dispositions_by_plan_version, key=lambda item: item[0]
    ):
        for disposition in dispositions:
            status_by_id[disposition.assertion_id] = disposition.status
    return {assertion_id for assertion_id, status in status_by_id.items() if status == "unusable"}


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


def verifier_reference_ids_from_snapshot(
    snapshot: dict[str, object],
) -> tuple[set[UUID], set[UUID], set[UUID]]:
    """Return the task, assertion, and excerpt ids the Verifier was allowed to name."""

    def ids(rows: object, key: str) -> set[UUID]:
        if not isinstance(rows, list):
            return set()
        return {
            UUID(str(row[key]))
            for row in rows
            if isinstance(row, dict) and row.get(key) is not None
        }

    return (
        ids(snapshot.get("tasks"), "task_id"),
        ids(snapshot.get("assertions"), "assertion_id"),
        ids(snapshot.get("excerpts"), "excerpt_id"),
    )


def validate_evidence_review_references(
    review: VerifierEvidenceReview,
    *,
    assertion_ids: set[UUID],
) -> None:
    """Reject qualification-pass references outside the frozen Job snapshot."""
    for finding in review.source_credibility_findings:
        if not set(finding.related_assertion_ids).issubset(assertion_ids):
            raise ValueError(
                "Source credibility finding references an assertion outside the current Job"
            )
    for conflict in review.conflicts:
        if not set(conflict.assertion_ids).issubset(assertion_ids):
            raise ValueError("Conflict judgement references an assertion outside the current Job")
    for disposition in review.assertion_dispositions:
        if disposition.assertion_id not in assertion_ids:
            raise ValueError(
                "Assertion disposition references an assertion outside the current Job"
            )


def validate_coverage_references(
    decision: VerifierCoverageDecision,
    *,
    task_ids: set[UUID],
    usable_assertion_ids: set[UUID],
) -> None:
    """Restrict coverage gaps to tasks and evidence present in its own projection."""
    for check in decision.answerability_checks:
        if not set(check.supporting_assertion_ids).issubset(usable_assertion_ids):
            raise ValueError(
                "Answerability check references an assertion outside the usable evidence projection"
            )
    for gap in decision.gaps:
        if not set(gap.related_task_ids).issubset(task_ids):
            raise ValueError("Verifier gap references a task outside the current Job")
        if not set(gap.related_assertion_ids).issubset(usable_assertion_ids):
            raise ValueError(
                "Verifier gap references an assertion outside the usable evidence projection"
            )


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
    for resolution in decision.conflict_resolutions:
        if not set(resolution.excerpt_ids).issubset(excerpt_ids):
            raise ValueError("Conflict resolution references an excerpt outside the current Job")
    for disposition in decision.assertion_dispositions:
        if disposition.assertion_id not in assertion_ids:
            raise ValueError(
                "Assertion disposition references an assertion outside the current Job"
            )
