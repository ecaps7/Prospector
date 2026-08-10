"""Contracts for statement-level Report Verifier decisions and claim persistence."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from prospector.schemas.report import MAX_PREMISE_DEPTH

ClaimType = Literal["fact", "number", "causal", "opinion_attributed"]
ClaimGrounding = Literal["evidence", "derived"]
EvidenceRelation = Literal["support", "contradict", "partial"]
VerdictStatus = Literal[
    "pass", "unsupported", "conflicted", "overreach", "miscalibrated"
]
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
    status: VerdictStatus
    reason: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _status_matches_pairs(self) -> EvidenceStatementDecision:
        supports = [pair for pair in self.pairs if pair.relation == "support"]
        contradicts = [pair for pair in self.pairs if pair.relation == "contradict"]
        if self.status == "pass" and not supports:
            raise ValueError("pass requires at least one support relation")
        if self.status == "unsupported" and supports:
            raise ValueError("unsupported cannot retain support relations")
        if self.status == "conflicted" and not contradicts:
            raise ValueError("conflicted requires a contradict relation")
        return self


class DerivedStatementDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_id: str
    kind: Literal["derived"] = "derived"
    claim_type: ClaimType = "causal"
    inference_note: str = Field(..., min_length=1)
    status: VerdictStatus
    reason: str = Field(..., min_length=1)


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


class ReportVerifierFindings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round: int = Field(..., ge=1)
    revision: int = Field(..., ge=1)
    failures: list[StatementFailure] = Field(default_factory=list)
    passed_statement_ids: list[str] = Field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return not self.failures


class ReportVerifierStatementInput(BaseModel):
    """One statement plus the evidence/premise context the model may see."""

    model_config = ConfigDict(extra="forbid")

    statement_id: str
    text: str
    kind: Literal["evidence", "derived", "elaboration", "limitation"]
    candidate_excerpts: list[dict[str, Any]] = Field(default_factory=list)
    premises: list[dict[str, Any]] = Field(default_factory=list)
    premises_all_passed: bool = True
    premise_depth: int = Field(default=0, ge=0, le=MAX_PREMISE_DEPTH)


class ReportVerifierSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    report_id: UUID
    revision: int = Field(..., ge=1)
    round: int = Field(..., ge=1)
    brief_question: str
    statements: list[ReportVerifierStatementInput] = Field(..., min_length=1)
    allowed_excerpt_ids: list[UUID] = Field(default_factory=list)
