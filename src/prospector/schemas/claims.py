"""Contracts for statement-level Report Verifier decisions and claim persistence."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from prospector.schemas.brief import UserConstraints

ClaimType = Literal["fact", "number", "causal", "opinion_attributed"]
ClaimGrounding = Literal["evidence", "derived"]
EvidenceRelation = Literal["support", "contradict", "partial", "irrelevant"]
VerdictStatus = Literal["pass", "unsupported", "conflicted", "overreach", "miscalibrated"]
QualityReminderKind = Literal[
    "evidence_listing",
    "repetition",
    "section_without_judgement",
    "long_reasoning_chain",
]
ReportRequirementKind = Literal[
    "core_answer",
    "user_constraint",
    "conclusion_support",
    "internal_consistency",
    "material_omission",
    "overall_calibration",
]
RepairScope = Literal["paragraph", "report"]
MAX_REPORT_REVISION_ROUNDS = 2


class EvidencePairDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    excerpt_id: UUID
    relation: EvidenceRelation


class EvidenceStatementDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_id: str
    kind: Literal["evidence"] = "evidence"
    claim_type: ClaimType
    pairs: list[EvidencePairDecision] = Field(..., min_length=1)
    conflict_keys: list[str] = Field(default_factory=list)
    status: VerdictStatus
    reason: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _status_matches_pairs(self) -> EvidenceStatementDecision:
        supports = [pair for pair in self.pairs if pair.relation == "support"]
        contradicts = [pair for pair in self.pairs if pair.relation == "contradict"]
        if self.status == "pass" and not supports:
            raise ValueError("pass requires at least one support relation")
        if self.status == "conflicted" and not contradicts and not self.conflict_keys:
            raise ValueError("conflicted requires a contradict relation or known conflict")
        if self.status != "conflicted" and self.conflict_keys:
            raise ValueError("conflict_keys are only valid for conflicted decisions")
        return self


class DerivedStatementDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_id: str
    kind: Literal["derived"] = "derived"
    claim_type: ClaimType = "causal"
    inference_note: str = Field(..., min_length=1)
    # Direct Excerpts may support a judgement alongside, or instead of, premise claims.
    # Empty for a premise-only judgement.
    pairs: list[EvidencePairDecision] = Field(default_factory=list)
    conflict_keys: list[str] = Field(default_factory=list)
    status: VerdictStatus
    reason: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _status_matches_conflicts(self) -> DerivedStatementDecision:
        contradicts = [pair for pair in self.pairs if pair.relation == "contradict"]
        if self.status == "conflicted" and not contradicts and not self.conflict_keys:
            raise ValueError("conflicted requires a contradict relation or known conflict")
        if self.status != "conflicted" and self.conflict_keys:
            raise ValueError("conflict_keys are only valid for conflicted decisions")
        return self


class BridgeStatementDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_id: str
    kind: Literal["elaboration", "limitation"]
    contains_factual_claim: bool
    reason: str = Field(..., min_length=1)

    @property
    def status(self) -> VerdictStatus:
        """A bridge may organise prose, but facts must use an auditable statement kind."""
        return "pass" if not self.contains_factual_claim else "unsupported"


class StatementFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_id: str
    kind: Literal["evidence", "derived", "elaboration", "limitation"]
    status: VerdictStatus
    reason: str = Field(..., min_length=1)
    allowed_excerpt_ids: list[UUID] = Field(default_factory=list)


class ReportQualityReminder(BaseModel):
    """A report-level writing issue that is recorded but never enters sentence revision."""

    model_config = ConfigDict(extra="forbid")

    kind: QualityReminderKind
    location: str = Field(..., min_length=1)
    statement_ids: list[str] = Field(default_factory=list)
    reason: str = Field(..., min_length=1)


class ReportRequirementFailure(BaseModel):
    """A whole-report contract failure that must trigger another Writer revision."""

    model_config = ConfigDict(extra="forbid")

    kind: ReportRequirementKind
    repair_scope: RepairScope = "report"
    paragraph_ids: list[str] = Field(default_factory=list)
    statement_ids: list[str] = Field(default_factory=list)
    reason: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _repair_scope_has_matching_locations(self) -> ReportRequirementFailure:
        self.paragraph_ids = list(dict.fromkeys(self.paragraph_ids))
        if self.repair_scope == "paragraph" and not self.paragraph_ids:
            raise ValueError("paragraph repair requires paragraph_ids")
        if self.repair_scope == "report" and self.paragraph_ids:
            raise ValueError("report repair must not name paragraph_ids")
        return self


class ReportQualityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement_failures: list[ReportRequirementFailure] = Field(default_factory=list)
    reminders: list[ReportQualityReminder] = Field(default_factory=list)


class ReportVerifierFindings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round: int = Field(..., ge=1)
    revision: int = Field(..., ge=1)
    failures: list[StatementFailure] = Field(default_factory=list)
    requirement_failures: list[ReportRequirementFailure] = Field(default_factory=list)
    passed_statement_ids: list[str] = Field(default_factory=list)
    quality_reminders: list[ReportQualityReminder] = Field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return not self.failures and not self.requirement_failures

    @property
    def report_rewrite_required(self) -> bool:
        return any(item.repair_scope == "report" for item in self.requirement_failures)

    @property
    def paragraph_repair_ids(self) -> set[str]:
        return {
            paragraph_id
            for item in self.requirement_failures
            if item.repair_scope == "paragraph"
            for paragraph_id in item.paragraph_ids
        }


class ReportVerifierStatementInput(BaseModel):
    """One statement plus only the evidence and premises it explicitly declares."""

    model_config = ConfigDict(extra="forbid")

    statement_id: str
    text: str
    kind: Literal["evidence", "derived", "elaboration", "limitation"]
    candidate_excerpts: list[dict[str, Any]] = Field(default_factory=list)
    premises: list[dict[str, Any]] = Field(default_factory=list)
    premises_all_passed: bool = True
    premise_depth: int = Field(default=0, ge=0)
    known_conflicts: list[dict[str, Any]] = Field(default_factory=list)


StatementDecision = EvidenceStatementDecision | DerivedStatementDecision | BridgeStatementDecision


class ReportVerifierSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    report_id: UUID
    revision: int = Field(..., ge=1)
    round: int = Field(..., ge=1)
    brief_question: str
    user_constraints: UserConstraints = Field(default_factory=UserConstraints)
    statements: list[ReportVerifierStatementInput] = Field(default_factory=list)
    allowed_excerpt_ids: list[UUID] = Field(default_factory=list)
    # Present on full-report verification runs. It is omitted only by isolated
    # statement-level evaluations that intentionally do not run the report quality pass.
    report_context: dict[str, Any] = Field(default_factory=dict)
    # Set when the previous revision already passed every sentence and is being
    # rewritten only for whole-report requirement failures. Stage one is not rerun.
    skip_statement_verification: bool = False
    reused_statement_decisions: list[StatementDecision] = Field(default_factory=list)

    @model_validator(mode="after")
    def _stage_inputs_match_verification_mode(self) -> ReportVerifierSnapshot:
        if self.skip_statement_verification:
            if not self.report_context:
                raise ValueError("stage-two-only verification requires report_context")
            return self
        if not self.statements:
            raise ValueError("stage-one verification requires statements")
        return self
