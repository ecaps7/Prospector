"""Report composition contracts for the post-attribution pipeline."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from prospector.schemas.brief import ResearchBrief

ResearchAssertionRef = Annotated[str, StringConstraints(pattern=r"^a[1-9][0-9]*$")]
ResearchConflictRef = Annotated[str, StringConstraints(pattern=r"^x[1-9][0-9]*$")]


class WriterSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    author: str | None = None
    published_at: str | None = None
    source_uri: str
    document_version: int = Field(..., ge=1)


class WriterExcerptRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    excerpt_id: UUID
    text: str = Field(..., min_length=1)
    source: WriterSource


class WriterEvidenceCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: UUID
    task_id: UUID
    assertion_statement: str = Field(..., min_length=1)
    excerpts: list[WriterExcerptRef] = Field(..., min_length=1)


class ResearchSynthesisResult(BaseModel):
    """Domain result after local model refs have been resolved."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["ready", "needs_research"]
    synthesis: str = Field(..., min_length=1)
    assertion_ids: list[UUID] = Field(default_factory=list)
    material_conflict_keys: list[str] = Field(default_factory=list)
    reason: str | None = None
    evidence_needed: str | None = None

    @model_validator(mode="after")
    def _validate_decision_fields(self) -> ResearchSynthesisResult:
        self.synthesis = self.synthesis.strip()
        if not self.synthesis:
            raise ValueError("synthesis must not be blank")
        if self.decision == "needs_research":
            if not (self.reason or "").strip() or not (self.evidence_needed or "").strip():
                raise ValueError("needs_research requires reason and evidence_needed")
        elif self.reason is not None or self.evidence_needed is not None:
            raise ValueError("ready must not carry reason or evidence_needed")
        return self


SynthesisDefectKind = Literal[
    "evidence_catalog",
    "missing_relationships",
    "missing_selection",
    "unsupported_overreach",
    "brief_not_answered",
]


class SynthesisReviewDefect(BaseModel):
    """One analytical failure found by the independent synthesis review."""

    model_config = ConfigDict(extra="forbid")

    kind: SynthesisDefectKind
    reason: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _reason_is_present(self) -> SynthesisReviewDefect:
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("defect reason must not be blank")
        return self


class ResearchSynthesisReview(BaseModel):
    """Independent check of a first synthesis draft.

    The model lists analytical defects; whether the draft is accepted is computed
    from that list, not from a separately predicted ``accept`` / ``revise`` flag.
    """

    model_config = ConfigDict(extra="forbid")

    defects: list[SynthesisReviewDefect] = Field(default_factory=list)
    reason: str = Field(..., min_length=1)
    revised_result: ResearchSynthesisResult | None = None

    @property
    def decision(self) -> Literal["accept", "revise"]:
        return "revise" if self.defects else "accept"

    @model_validator(mode="after")
    def _validate_revision(self) -> ResearchSynthesisReview:
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("review reason must not be blank")
        if self.defects and self.revised_result is None:
            raise ValueError("defects require revised_result")
        if not self.defects and self.revised_result is not None:
            raise ValueError("a defect-free review must not carry revised_result")
        return self


class ResearchSynthesisModelResult(BaseModel):
    """Model-facing synthesis result using the frozen material's local refs."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["ready", "needs_research"]
    synthesis: str = Field(..., min_length=1)
    assertion_refs: list[ResearchAssertionRef] = Field(default_factory=list)
    material_conflict_refs: list[ResearchConflictRef] = Field(default_factory=list)
    reason: str | None = None
    evidence_needed: str | None = None

    @model_validator(mode="after")
    def _validate_decision_fields(self) -> ResearchSynthesisModelResult:
        self.synthesis = self.synthesis.strip()
        if not self.synthesis:
            raise ValueError("synthesis must not be blank")
        if self.decision == "needs_research":
            if not (self.reason or "").strip() or not (self.evidence_needed or "").strip():
                raise ValueError("needs_research requires reason and evidence_needed")
        elif self.reason is not None or self.evidence_needed is not None:
            raise ValueError("ready must not carry reason or evidence_needed")
        return self


class ResearchSynthesisModelReview(BaseModel):
    """Independent review wire contract; revisions keep the same short refs."""

    model_config = ConfigDict(extra="forbid")

    defects: list[SynthesisReviewDefect] = Field(default_factory=list)
    reason: str = Field(..., min_length=1)
    revised_result: ResearchSynthesisModelResult | None = None

    @property
    def decision(self) -> Literal["accept", "revise"]:
        return "revise" if self.defects else "accept"

    @model_validator(mode="after")
    def _validate_revision(self) -> ResearchSynthesisModelReview:
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("review reason must not be blank")
        if self.defects and self.revised_result is None:
            raise ValueError("defects require revised_result")
        if not self.defects and self.revised_result is not None:
            raise ValueError("a defect-free review must not carry revised_result")
        return self


class ResearchSynthesisRun(ResearchSynthesisResult):
    synthesis_run_id: UUID
    job_id: UUID
    version: int = Field(..., ge=1)
    # The verifier run this analysis was built from.  Optional only for runs written
    # before the link existed.
    verifier_run_id: UUID | None = None
    status: Literal["prompted", "completed", "failed"] = "completed"
    raw_output: Any | None = None
    contract_error: str | None = None


class WriterSnapshot(BaseModel):
    """All and only usable research material available to synthesis and writing."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    brief: ResearchBrief
    final_plan_summary: list[dict[str, Any]] = Field(default_factory=list)
    evidence_cards: list[WriterEvidenceCard] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    minor_gaps: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def usable_assertion_ids(self) -> set[UUID]:
        return {card.assertion_id for card in self.evidence_cards}

    @property
    def excerpt_ids(self) -> set[UUID]:
        return {excerpt.excerpt_id for card in self.evidence_cards for excerpt in card.excerpts}


class MarkdownBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(..., pattern=r"^b_[0-9]{4}$")
    kind: Literal["heading", "paragraph", "list_item", "blockquote", "table_cell"]
    text: str = Field(..., min_length=1)
    text_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    # Absolute position of ``text`` inside the frozen Markdown, or -1 when the visible
    # text could not be located verbatim.  Citations are spliced by offset because block
    # texts repeat across table cells and a search would hit the wrong occurrence.
    source_start: int = Field(-1, ge=-1)
    source_end: int = Field(-1, ge=-1)


class BlockReplacement(BaseModel):
    """One contiguous block range the Writer wants to replace during revision."""

    model_config = ConfigDict(extra="forbid")

    start_block_id: str = Field(..., pattern=r"^b_[0-9]{4}$")
    end_block_id: str = Field(..., pattern=r"^b_[0-9]{4}$")
    # Empty markdown deletes the range.  A fix whose evidence does not exist is sometimes
    # only fixable by removing the passage and whatever rested on it.
    markdown: str = ""
    reason: str = Field(..., min_length=1)


class ReportRevisionPatch(BaseModel):
    """The Writer's whole revision output: what to replace, and nothing else.

    The Writer chooses the range, so it can widen a replacement to carry the neighbours a
    fix would otherwise strand -- a dangling "这一转变", a "因此" whose premise moved.
    What it cannot do is quietly rewrite a passage nobody asked about.
    """

    model_config = ConfigDict(extra="forbid")

    replacements: list[BlockReplacement] = Field(default_factory=list)
    audit_note: str | None = None


class ReportRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report_id: UUID
    job_id: UUID
    revision: int = Field(..., ge=1)
    synthesis_run_id: UUID
    full_prompt: list[dict[str, str]] = Field(default_factory=list)
    raw_output: Any | None = None
    markdown: str = Field(..., min_length=1)
    markdown_hash: str = Field(..., pattern=r"^sha256:[0-9a-f]{64}$")
    parsed_blocks: list[MarkdownBlock] = Field(default_factory=list)
    status: Literal["prompted", "generated", "attributed", "reviewed", "rendered", "failed"]
