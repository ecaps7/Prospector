"""Structured Report Writer input, draft, and validation contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

StatementKind = Literal["evidence", "derived", "elaboration", "limitation"]

# Longest chain of inference allowed above cited material: evidence → derived → derived.
# Defined and enforced here because it is a property of the draft's shape; every other
# layer reads this constant instead of re-deciding the ceiling for itself.
MAX_PREMISE_DEPTH = 2


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


def premise_depth(
    statement: ReportStatement,
    kinds: Mapping[str, StatementKind],
    depths: Mapping[str, int],
) -> int:
    """How many inference steps *statement* sits above cited material.

    ``kinds`` and ``depths`` cover the statements already accepted before this one.
    Raises ValueError when a premise cannot ground a chain — elaboration and
    limitation carry no evidence at all, so resting a derived statement on one
    would let a reasoning chain bottom out in nothing — or when the chain runs
    deeper than MAX_PREMISE_DEPTH. The wire assembler and the draft validator both
    call this, so a shape rejected at the end is also rejected as it streams in.
    """
    groundless = sorted(
        premise_id
        for premise_id in statement.premise_statement_ids
        if kinds[premise_id] not in {"evidence", "derived"}
    )
    if groundless:
        raise ValueError(
            f"{statement.statement_id} rests on premises that carry no evidence "
            f"({', '.join(groundless)}); premises must be evidence or derived statements"
        )
    if statement.kind != "derived":
        return 0
    depth = 1 + max(depths[premise_id] for premise_id in statement.premise_statement_ids)
    if depth > MAX_PREMISE_DEPTH:
        raise ValueError(
            f"{statement.statement_id} builds a reasoning chain of depth {depth}, "
            f"deeper than the allowed {MAX_PREMISE_DEPTH}"
        )
    return depth


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
    introduction: list[ReportParagraph] = Field(..., min_length=1)
    sections: list[ReportSection] = Field(..., min_length=1)
    conclusion: list[ReportParagraph] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _references_are_consistent(self) -> ReportDraft:
        section_ids: set[str] = set()
        for section in self.sections:
            if section.section_id in section_ids:
                raise ValueError("section_id values must be unique")
            section_ids.add(section.section_id)
        paragraph_ids: set[str] = set()
        kinds: dict[str, StatementKind] = {}
        depths: dict[str, int] = {}
        for paragraph in self.paragraphs():
            if paragraph.paragraph_id in paragraph_ids:
                raise ValueError("paragraph_id values must be unique")
            paragraph_ids.add(paragraph.paragraph_id)
            for statement in paragraph.statements:
                if statement.statement_id in kinds:
                    raise ValueError("statement_id values must be unique")
                unknown = set(statement.premise_statement_ids) - set(kinds)
                if unknown:
                    raise ValueError(
                        "derived premises must reference earlier statements: "
                        + ", ".join(sorted(unknown))
                    )
                kinds[statement.statement_id] = statement.kind
                depths[statement.statement_id] = premise_depth(statement, kinds, depths)
        return self

    def paragraphs(self) -> list[ReportParagraph]:
        """Every paragraph in document order, introduction through conclusion."""
        return [
            *self.introduction,
            *(paragraph for section in self.sections for paragraph in section.paragraphs),
            *self.conclusion,
        ]

    def statement_groups(self) -> list[list[ReportStatement]]:
        """Statements grouped by top-level scope: introduction, each section, conclusion.

        Bridge statements are verified against the material their own scope cites,
        so the grouping is part of the contract rather than a caller-side detail.
        """
        groups: list[list[ReportParagraph]] = [list(self.introduction)]
        groups.extend(list(section.paragraphs) for section in self.sections)
        groups.append(list(self.conclusion))
        return [
            [statement for paragraph in group for statement in paragraph.statements]
            for group in groups
        ]

    def statements(self) -> list[ReportStatement]:
        return [statement for paragraph in self.paragraphs() for statement in paragraph.statements]

    def body_char_count(self) -> int:
        return sum(len(statement.text.strip()) for statement in self.statements())


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
