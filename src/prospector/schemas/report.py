"""Structured Report Writer input, draft, and validation contracts."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

StatementKind = Literal["evidence", "derived", "elaboration", "limitation"]


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
    source: WriterSource


class WriterConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_key: str
    disputed_point: str
    excerpt_ids: list[UUID] = Field(..., min_length=2)
    decision: Literal["present_both", "adjudicated"]
    winning_excerpt_ids: list[UUID] = Field(default_factory=list)
    rationale: str


class WriterEvidenceCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: UUID
    assertion_statement: str = Field(..., min_length=1)
    excerpts: list[WriterExcerptRef] = Field(..., min_length=1)


class WriterSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    brief: dict[str, Any]
    final_plan_summary: list[dict[str, Any]]
    evidence_cards: list[WriterEvidenceCard] = Field(..., min_length=1)
    conflicts: list[WriterConflict] = Field(default_factory=list)
    minor_gaps: list[dict[str, Any]] = Field(default_factory=list)


def excerpt_alias_map(snapshot: WriterSnapshot) -> dict[UUID, str]:
    """Deterministic short aliases (e_01, e_02, ...) for every excerpt id shown to the Writer.

    The Writer prompt and wire stream use these aliases instead of raw UUIDs, which
    the model reliably corrupts when transcribing; drafts store the real UUIDs.
    """
    aliases: dict[UUID, str] = {}
    referenced = [
        excerpt.excerpt_id for card in snapshot.evidence_cards for excerpt in card.excerpts
    ]
    for conflict in snapshot.conflicts:
        referenced.extend(conflict.excerpt_ids)
        referenced.extend(conflict.winning_excerpt_ids)
    for excerpt_id in referenced:
        if excerpt_id not in aliases:
            aliases[excerpt_id] = f"e_{len(aliases) + 1:02d}"
    return aliases


class ReportStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement_id: str = Field(..., pattern=r"^s_[A-Za-z0-9_-]+$")
    text: str = Field(..., min_length=1)
    kind: StatementKind
    candidate_excerpt_ids: list[UUID] = Field(default_factory=list)
    premise_statement_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references_match_kind(self) -> ReportStatement:
        if self.kind == "evidence":
            if not self.candidate_excerpt_ids:
                raise ValueError("evidence statement requires candidate_excerpt_ids")
            if self.premise_statement_ids:
                raise ValueError("evidence statement cannot cite premise statements")
        elif self.kind == "derived":
            if not self.premise_statement_ids:
                raise ValueError("derived statement requires premise_statement_ids")
            if self.candidate_excerpt_ids:
                raise ValueError("derived statement cannot cite excerpts directly")
        elif self.candidate_excerpt_ids or self.premise_statement_ids:
            raise ValueError(f"{self.kind} statement cannot carry evidence or premises")
        return self


class ReportParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paragraph_id: str = Field(..., pattern=r"^p_[A-Za-z0-9_-]+$")
    statements: list[ReportStatement] = Field(..., min_length=1)


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(..., pattern=r"^sec_[A-Za-z0-9_-]+$")
    title: str = Field(..., min_length=1)
    paragraphs: list[ReportParagraph] = Field(..., min_length=1)


class ReportDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    introduction: str = Field(..., min_length=1)
    sections: list[ReportSection] = Field(..., min_length=1)
    conclusion: list[ReportParagraph] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _references_are_consistent(self) -> ReportDraft:
        section_ids: set[str] = set()
        paragraph_ids: set[str] = set()
        prior_statement_ids: set[str] = set()
        for section in self.sections:
            if section.section_id in section_ids:
                raise ValueError("section_id values must be unique")
            section_ids.add(section.section_id)
        paragraphs = [
            paragraph for section in self.sections for paragraph in section.paragraphs
        ] + list(self.conclusion)
        for paragraph in paragraphs:
            if paragraph.paragraph_id in paragraph_ids:
                raise ValueError("paragraph_id values must be unique")
            paragraph_ids.add(paragraph.paragraph_id)
            for statement in paragraph.statements:
                if statement.statement_id in prior_statement_ids:
                    raise ValueError("statement_id values must be unique")
                unknown = set(statement.premise_statement_ids) - prior_statement_ids
                if unknown:
                    raise ValueError(
                        "derived premises must reference earlier statements: "
                        + ", ".join(sorted(unknown))
                    )
                prior_statement_ids.add(statement.statement_id)
        return self

    def statements(self) -> list[ReportStatement]:
        section_statements = [
            statement
            for section in self.sections
            for paragraph in section.paragraphs
            for statement in paragraph.statements
        ]
        conclusion_statements = [
            statement for paragraph in self.conclusion for statement in paragraph.statements
        ]
        return section_statements + conclusion_statements

    def body_char_count(self) -> int:
        return len(self.introduction.strip()) + sum(
            len(statement.text.strip()) for statement in self.statements()
        )


def validate_writer_draft(snapshot: WriterSnapshot, draft: ReportDraft) -> None:
    """Validate that Writer-owned candidate references stay inside its input."""
    allowed_excerpt_ids: set[UUID] = set()
    for card in snapshot.evidence_cards:
        for excerpt in card.excerpts:
            allowed_excerpt_ids.add(excerpt.excerpt_id)

    for statement in draft.statements():
        unknown = set(statement.candidate_excerpt_ids) - allowed_excerpt_ids
        if unknown:
            raise ValueError(
                "statement references excerpts outside Writer input: "
                + ", ".join(sorted(str(value) for value in unknown))
            )
