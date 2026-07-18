"""Wire format for sentence-level Report Writer revision patches."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from prospector.schemas.report import ReportStatement, WriterSnapshot, excerpt_alias_map
from prospector.schemas.report_stream import ReportStreamError


class PatchStatementRecord(ReportStatement):
    """Wire patch: candidate_excerpt_ids carries short aliases, not UUIDs."""

    record: Literal["patch_statement"] = "patch_statement"
    candidate_excerpt_ids: list[str] = Field(default_factory=list)

    def to_statement(self, alias_to_id: dict[str, UUID]) -> ReportStatement:
        payload = self.model_dump(exclude={"record"})
        payload["candidate_excerpt_ids"] = [
            alias_to_id[alias] for alias in self.candidate_excerpt_ids
        ]
        return ReportStatement.model_validate(payload)


class PatchEndRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record: Literal["end"]


PatchRecord = Annotated[PatchStatementRecord | PatchEndRecord, Field(discriminator="record")]
_PATCH_ADAPTER: TypeAdapter[PatchRecord] = TypeAdapter(PatchRecord)


@dataclass(frozen=True, slots=True)
class PatchTurnOutcome:
    accepted: int
    error: str | None


@dataclass
class ReportPatchAssembler:
    """Fold patch_statement records into a list of ReportStatement replacements."""

    snapshot: WriterSnapshot
    allowed_statement_ids: set[str]
    patches: list[ReportStatement] = field(default_factory=list)
    done: bool = False
    last_accepted: str = "（尚未接受任何补丁）"

    def __post_init__(self) -> None:
        alias_map = excerpt_alias_map(self.snapshot)
        self._alias_to_id: dict[str, UUID] = {
            alias: excerpt_id for excerpt_id, alias in alias_map.items()
        }
        self._allowed_excerpt_aliases: set[str] = {
            alias_map[excerpt.excerpt_id]
            for card in self.snapshot.evidence_cards
            for excerpt in card.excerpts
        }
        self._seen: set[str] = set()

    def consume(self, content: str) -> PatchTurnOutcome:
        payloads, parse_error = self._parse_payloads(content)
        accepted = 0
        for payload in payloads:
            if self.done:
                break
            try:
                record = _PATCH_ADAPTER.validate_python(payload)
                self._apply(record)
            except (ValidationError, ReportStreamError) as exc:
                rejected = json.dumps(payload, ensure_ascii=False, default=str)[:300]
                return PatchTurnOutcome(accepted, f"记录 {rejected} 被拒绝：{exc}")
            accepted += 1
            self.last_accepted = (
                f"patch {record.statement_id}"
                if isinstance(record, PatchStatementRecord)
                else "end"
            )
        return PatchTurnOutcome(accepted, parse_error)

    def _parse_payloads(self, content: str) -> tuple[list[object], str | None]:
        lines = [line.strip() for line in content.splitlines()]
        lines = [line for line in lines if line and not line.startswith("```")]
        payloads: list[object] = []
        for index, line in enumerate(lines):
            try:
                payloads.append(json.loads(line))
            except json.JSONDecodeError as exc:
                if index == len(lines) - 1:
                    break
                return payloads, f"第 {index + 1} 行不是合法的单行 JSON：{exc}"
        return payloads, None

    def _apply(self, record: PatchRecord) -> None:
        if isinstance(record, PatchEndRecord):
            if not self.patches:
                raise ReportStreamError("end 之前至少要有一条 patch_statement")
            self.done = True
            return
        if record.statement_id not in self.allowed_statement_ids:
            raise ReportStreamError(
                f"statement_id {record.statement_id} 不在审稿失败列表中，禁止修改"
            )
        if record.statement_id in self._seen:
            raise ReportStreamError(f"statement_id {record.statement_id} 已打过补丁")
        unknown_excerpts = set(record.candidate_excerpt_ids) - self._allowed_excerpt_aliases
        if unknown_excerpts:
            raise ReportStreamError(
                "candidate_excerpt_ids 引用了研究材料之外的 excerpt："
                + ", ".join(sorted(unknown_excerpts))
            )
        self.patches.append(record.to_statement(self._alias_to_id))
        self._seen.add(record.statement_id)
