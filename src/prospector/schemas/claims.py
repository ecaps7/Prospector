"""Claim-attribution and whole-report-review contracts."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

MarkerFamily = Literal["retrieval", "candidate", "advisory"]
FindingKind = Literal[
    "brief_response",
    "user_constraint",
    "material_omission",
    "conclusion_integrity",
]
MAX_WRITER_REPAIRS = 2


class ClaimMarker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: MarkerFamily
    kind: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    start_offset: int = Field(..., ge=0)
    end_offset: int = Field(..., gt=0)


class ClaimSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: UUID
    block_id: str
    start_offset: int = Field(..., ge=0)
    end_offset: int = Field(..., gt=0)
    text_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    text: str = Field(..., min_length=1)
    markers: list[ClaimMarker] = Field(default_factory=list)

    @model_validator(mode="after")
    def _span_is_nonempty(self) -> ClaimSpan:
        if self.end_offset <= self.start_offset:
            raise ValueError("claim span must be non-empty")
        return self


class ClaimEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: UUID
    excerpt_id: UUID


class ClaimPremise(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: UUID
    premise_claim_ids: list[UUID] = Field(default_factory=list)
    direct_assertion_ids: list[UUID] = Field(default_factory=list)
    known_conflict_keys: list[str] = Field(default_factory=list)
    audit_note: str | None = None


class AttributionFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: UUID = Field(default_factory=uuid4)
    kind: Literal["attribution", "in_place_downgrade"]
    claim_id: UUID | None = None
    block_id: str
    start_offset: int | None = Field(default=None, ge=0)
    end_offset: int | None = Field(default=None, gt=0)
    text: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _optional_span_is_complete(self) -> AttributionFinding:
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("finding span offsets must be provided together")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("finding span must be non-empty")
        return self


class BlockAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str
    status: Literal["assessed", "no_claims"]


class RevisionFailureDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prior_finding_id: UUID
    outcome: Literal["corrected", "replaced_source", "removed", "in_place_downgrade"]
    reason: str = Field(..., min_length=1)


class AttributionBatchSelection(BaseModel):
    """First-stage batch output: choose related Assertions from the statement catalog.

    Material is named by short catalog reference ("a17"), never by UUID.  Asking a model
    to transcribe seventy 36-character identifiers produced both a quarter of the output
    volume and a recurring failure where the head of one real id was joined to the tail
    of another -- an id that exists nowhere, from a model that had picked correctly.
    """

    model_config = ConfigDict(extra="forbid")

    assertion_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _assertion_refs_are_unique(self) -> AttributionBatchSelection:
        if len(self.assertion_refs) != len(set(self.assertion_refs)):
            raise ValueError("selected assertion_refs must be unique")
        return self


class AttributionBatchClaim(BaseModel):
    """One span verdict inside a single attribution batch."""

    model_config = ConfigDict(extra="forbid")

    claim_ref: str = Field(..., min_length=1, max_length=96)
    block_id: str
    start_offset: int = Field(..., ge=0)
    end_offset: int = Field(..., gt=0)
    status: Literal["verified", "failed", "analysis"]
    candidate_refs: list[str] = Field(default_factory=list)
    excerpt_refs: list[str] = Field(default_factory=list)
    assertion_refs: list[str] = Field(default_factory=list)
    premise_claim_refs: list[str] = Field(default_factory=list)
    known_conflict_keys: list[str] = Field(default_factory=list)
    reason: str | None = None
    audit_note: str | None = None


class AttributionBatchVerification(BaseModel):
    """Second-stage batch output: fact-check only this batch's candidates."""

    model_config = ConfigDict(extra="forbid")

    claims: list[AttributionBatchClaim] = Field(default_factory=list)
    audit_notes: list[dict[str, Any]] = Field(default_factory=list)


class AttributionDisposition(BaseModel):
    """Model-facing prior-failure outcome; code rewrites it into RevisionFailureDisposition."""

    model_config = ConfigDict(extra="forbid")

    prior_ref: str = Field(..., min_length=1, max_length=32)
    outcome: Literal["corrected", "replaced_source", "removed", "in_place_downgrade"]
    reason: str = Field(..., min_length=1)
    current_block_id: str | None = None
    current_start_offset: int | None = Field(default=None, ge=0)
    current_end_offset: int | None = Field(default=None, ge=0)


class AttributionSummary(BaseModel):
    """Final model pass: prior-failure destinations that cannot be decided inside one batch."""

    model_config = ConfigDict(extra="forbid")

    dispositions: list[AttributionDisposition] = Field(default_factory=list)
    audit_notes: list[dict[str, Any]] = Field(default_factory=list)


class AttributionRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribution_run_id: UUID
    report_id: UUID
    revision: int = Field(..., ge=1)
    status: Literal["prompted", "completed", "failed"] = "completed"
    block_assessments: list[BlockAssessment] = Field(default_factory=list)
    claims: list[ClaimSpan] = Field(default_factory=list)
    claim_evidence: list[ClaimEvidence] = Field(default_factory=list)
    claim_premises: list[ClaimPremise] = Field(default_factory=list)
    blocking_findings: list[AttributionFinding] = Field(default_factory=list)
    dispositions: list[RevisionFailureDisposition] = Field(default_factory=list)
    audit_notes: list[dict[str, Any]] = Field(default_factory=list)
    marker_lexicon_version: str = Field(..., min_length=1)
    raw_output: Any | None = None
    contract_error: str | None = None

    @model_validator(mode="after")
    def _blocks_are_complete(self) -> AttributionRun:
        block_ids = [item.block_id for item in self.block_assessments]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("block assessment ids must be unique")
        return self


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: FindingKind
    reason: str = Field(..., min_length=1)
    block_ids: list[str] = Field(..., min_length=1)


class ReportReviewRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_run_id: UUID
    report_id: UUID
    revision: int = Field(..., ge=1)
    synthesis_run_id: UUID
    status: Literal["prompted", "completed", "failed"] = "completed"
    blocking_findings: list[ReviewFinding] = Field(default_factory=list)
    key_block_ids: list[str] = Field(default_factory=list)
    audit_notes: list[dict[str, Any]] = Field(default_factory=list)
    raw_output: Any | None = None
    contract_error: str | None = None


def core_attribution_finding_ids(attribution: AttributionRun, review: ReportReviewRun) -> set[UUID]:
    """Return the attribution failures that carry the report's main reasoning."""
    claim_by_id = {claim.claim_id: claim for claim in attribution.claims}
    premise_claim_ids = {
        premise_id
        for premise in attribution.claim_premises
        for premise_id in premise.premise_claim_ids
    }
    core_blocks = set(review.key_block_ids)
    return {
        item.finding_id
        for item in attribution.blocking_findings
        if item.kind == "in_place_downgrade"
        or (
            item.claim_id is not None
            and (
                item.claim_id in premise_claim_ids
                or (
                    (claim := claim_by_id.get(item.claim_id)) is not None
                    and claim.block_id in core_blocks
                )
            )
        )
    }


def has_core_problem(attribution: AttributionRun, review: ReportReviewRun) -> bool:
    """Whether anything load-bearing is still wrong.

    Whole-report findings and in-place downgrades are core by construction; a span
    failure is core when another claim rests on it or it sits in a block the review
    marked as carrying the report's main reading.
    """
    if review.blocking_findings:
        return True
    return bool(core_attribution_finding_ids(attribution, review))


def final_report_status(
    attribution: AttributionRun,
    review: ReportReviewRun,
    *,
    repairs_used: int,
) -> Literal["verified", "partial", "failed", "revising"]:
    """Compute the report state without a model judgement at finalization time.

    A full rewrite re-exposes the whole report to every check.  Only a problem that affects
    the report's actual answer is allowed to spend that budget; peripheral failed anchors
    remain visible and produce a partial report without teaching the Writer to avoid detail.
    """
    if not attribution.blocking_findings and not review.blocking_findings:
        return "verified"
    core = has_core_problem(attribution, review)
    if repairs_used < MAX_WRITER_REPAIRS and core:
        return "revising"
    return "failed" if core else "partial"
