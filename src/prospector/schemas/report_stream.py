"""Flat line-record wire format and incremental assembly for the Report Writer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from prospector.schemas.report import (
    ReportDraft,
    ReportStatement,
    StatementKind,
    WriterSnapshot,
    excerpt_alias_map,
    premise_depth,
)


class ReportStreamError(ValueError):
    """A wire record violates the stream contract."""


class TitleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: Literal["title"]
    text: str = Field(..., min_length=1)


class IntroductionRecord(BaseModel):
    """Opens the introduction; its prose arrives as ordinary statement records.

    The introduction used to be one free-text field, which meant the report's
    opening answer was rendered without ever passing statement-level verification.
    """

    model_config = ConfigDict(extra="forbid")

    record: Literal["introduction"]


class SectionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: Literal["section"]
    title: str = Field(..., min_length=1)


class ParagraphRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: Literal["paragraph"]


class StatementRecord(ReportStatement):
    """Wire statement: candidate_excerpt_ids carries short aliases (e_01...), not UUIDs."""

    record: Literal["statement"]
    candidate_excerpt_ids: list[str] = Field(default_factory=list)

    def to_statement(self, alias_to_id: dict[str, UUID]) -> ReportStatement:
        payload = self.model_dump(exclude={"record"})
        payload["candidate_excerpt_ids"] = [
            alias_to_id[alias] for alias in self.candidate_excerpt_ids
        ]
        return ReportStatement.model_validate(payload)


class ConclusionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: Literal["conclusion"]


class EndRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: Literal["end"]


WriterRecord = Annotated[
    TitleRecord
    | IntroductionRecord
    | SectionRecord
    | ParagraphRecord
    | StatementRecord
    | ConclusionRecord
    | EndRecord,
    Field(discriminator="record"),
]

_RECORD_ADAPTER: TypeAdapter[WriterRecord] = TypeAdapter(WriterRecord)


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    accepted: int
    error: str | None


@dataclass
class _SectionDraft:
    title: str
    paragraphs: list[list[ReportStatement]] = field(default_factory=list)

    def statement_count(self) -> int:
        return sum(len(paragraph) for paragraph in self.paragraphs)


def _describe(record: WriterRecord) -> str:
    if isinstance(record, StatementRecord):
        return f"statement {record.statement_id}"
    if isinstance(record, SectionRecord):
        return f"section「{record.title}」"
    return f"{record.record} 记录"


class ReportStreamAssembler:
    """Fold the model's flat record stream into a ReportDraft, one turn at a time."""

    def __init__(self, snapshot: WriterSnapshot) -> None:
        alias_map = excerpt_alias_map(snapshot)
        self._alias_to_id: dict[str, UUID] = {
            alias: excerpt_id for excerpt_id, alias in alias_map.items()
        }
        # Only excerpts backing evidence cards are citable; conflict-only ids are not.
        self._allowed_excerpt_aliases: set[str] = {
            alias_map[excerpt.excerpt_id]
            for card in snapshot.evidence_cards
            for excerpt in card.excerpts
        }
        self._title: str | None = None
        self._sections: list[_SectionDraft] = []
        self._introduction_paragraphs: list[list[ReportStatement]] = []
        self._conclusion_paragraphs: list[list[ReportStatement]] = []
        self._in_introduction = False
        self._in_conclusion = False
        self._statement_kinds: dict[str, StatementKind] = {}
        self._statement_depths: dict[str, int] = {}
        self.done = False
        self.last_accepted = "（尚未接受任何记录，请从头开始输出）"

    def consume(self, content: str) -> TurnOutcome:
        """Apply one turn of model output; earlier accepted records are never rolled back."""
        payloads, parse_error = self._parse_payloads(content)
        accepted = 0
        for payload in payloads:
            if self.done:
                break
            try:
                record = _RECORD_ADAPTER.validate_python(payload)
                self._apply(record)
            except (ValidationError, ReportStreamError) as exc:
                rejected = json.dumps(payload, ensure_ascii=False, default=str)[:300]
                return TurnOutcome(accepted, f"记录 {rejected} 被拒绝：{exc}")
            accepted += 1
            self.last_accepted = _describe(record)
        return TurnOutcome(accepted, parse_error)

    def _parse_payloads(self, content: str) -> tuple[list[object], str | None]:
        lines = [line.strip() for line in content.splitlines()]
        lines = [line for line in lines if line and not line.startswith("```")]
        joined = "\n".join(lines).strip()
        if joined.startswith("["):
            try:
                data = json.loads(joined)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, list):
                return data, None
        payloads: list[object] = []
        for index, line in enumerate(lines):
            try:
                payloads.append(json.loads(line))
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    break  # a truncated tail is resumed next turn, not an error
                return payloads, f"第 {index + 1} 行不是合法的单行 JSON：{exc}"
        return payloads, None

    def _apply(self, record: WriterRecord) -> None:
        if isinstance(record, TitleRecord):
            if self._title is not None:
                raise ReportStreamError("title 已经输出过，不能重复")
            self._title = record.text
        elif isinstance(record, IntroductionRecord):
            if self._in_introduction or self._sections or self._in_conclusion:
                raise ReportStreamError("introduction 只能在正文章节之前输出一次")
            self._in_introduction = True
        elif isinstance(record, SectionRecord):
            if self._in_conclusion:
                raise ReportStreamError("conclusion 之后不能再开启新章节")
            self._in_introduction = False
            self._sections.append(_SectionDraft(title=record.title))
        elif isinstance(record, ParagraphRecord):
            self._open_paragraph()
        elif isinstance(record, StatementRecord):
            self._apply_statement(record)
        elif isinstance(record, ConclusionRecord):
            if self._in_conclusion:
                raise ReportStreamError("conclusion 已经开始，不能重复输出")
            self._in_conclusion = True
        elif isinstance(record, EndRecord):
            self._apply_end()

    def _current_paragraphs(self) -> list[list[ReportStatement]]:
        if self._in_conclusion:
            return self._conclusion_paragraphs
        if self._sections:
            return self._sections[-1].paragraphs
        if self._in_introduction:
            return self._introduction_paragraphs
        raise ReportStreamError(
            "statement/paragraph 之前必须先输出 introduction、section 或 conclusion"
        )

    def _open_paragraph(self) -> None:
        self._current_paragraphs().append([])

    def _apply_statement(self, record: StatementRecord) -> None:
        if record.statement_id in self._statement_kinds:
            raise ReportStreamError(f"statement_id {record.statement_id} 已经出现过，必须全文唯一")
        unknown_excerpts = set(record.candidate_excerpt_ids) - self._allowed_excerpt_aliases
        if unknown_excerpts:
            raise ReportStreamError(
                "candidate_excerpt_ids 引用了研究材料之外的 excerpt："
                + ", ".join(sorted(unknown_excerpts))
                + "。只能逐字复制材料中的 excerpt_id 短编号"
            )
        unknown_premises = set(record.premise_statement_ids) - set(self._statement_kinds)
        if unknown_premises:
            raise ReportStreamError(
                "premise_statement_ids 只能引用之前已输出的 statement，未知："
                + ", ".join(sorted(unknown_premises))
            )
        # Reject an ungrounded or over-deep chain here rather than at build(), so the
        # model can repair one statement instead of losing an entire generation.
        try:
            depth = premise_depth(record, self._statement_kinds, self._statement_depths)
        except ValueError as exc:
            raise ReportStreamError(str(exc)) from exc
        paragraphs = self._current_paragraphs()
        if not paragraphs:
            paragraphs.append([])
        paragraphs[-1].append(record.to_statement(self._alias_to_id))
        self._statement_kinds[record.statement_id] = record.kind
        self._statement_depths[record.statement_id] = depth

    def _apply_end(self) -> None:
        if self._title is None:
            raise ReportStreamError("end 之前必须输出 title")
        if not any(self._introduction_paragraphs):
            raise ReportStreamError("end 之前必须先输出 introduction 及其 statement")
        if not self._sections:
            raise ReportStreamError("end 之前至少要有一个章节")
        empty = [section.title for section in self._sections if section.statement_count() == 0]
        if empty:
            raise ReportStreamError("以下章节还没有任何 statement：" + "、".join(empty))
        if not any(self._conclusion_paragraphs):
            raise ReportStreamError("end 之前必须先输出 conclusion 及其内容")
        self.done = True

    def build(self) -> ReportDraft:
        """Assemble the final draft; section and paragraph ids are runtime-assigned."""
        if not self.done:
            raise ReportStreamError("记录流尚未收到 end，报告未完成")
        paragraph_counter = 0

        def paragraph_payload(statements: list[ReportStatement]) -> dict[str, object]:
            nonlocal paragraph_counter
            paragraph_counter += 1
            return {
                "paragraph_id": f"p_{paragraph_counter:03d}",
                "statements": [statement.model_dump(mode="json") for statement in statements],
            }

        # Paragraph ids are handed out in document order, so introduction comes first.
        introduction = [
            paragraph_payload(paragraph) for paragraph in self._introduction_paragraphs if paragraph
        ]
        sections = [
            {
                "section_id": f"sec_{index:03d}",
                "title": section.title,
                "paragraphs": [
                    paragraph_payload(paragraph) for paragraph in section.paragraphs if paragraph
                ],
            }
            for index, section in enumerate(self._sections, start=1)
        ]
        conclusion = [
            paragraph_payload(paragraph) for paragraph in self._conclusion_paragraphs if paragraph
        ]
        return ReportDraft.model_validate(
            {
                "title": self._title,
                "introduction": introduction,
                "sections": sections,
                "conclusion": conclusion,
            }
        )
