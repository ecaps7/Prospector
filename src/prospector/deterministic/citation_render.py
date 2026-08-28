"""Deterministic citation rendering from verified claim support relations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from prospector.schemas.report import ReportDraft, WriterExcerptRef, WriterSnapshot

VerificationStatus = Literal["pending", "verified", "partial"]

# Appended to every statement that did not pass verification. The report keeps such
# sentences (they are the Writer's judgement, not a fabrication) but must never let
# them read as checked; the top banner alone cannot say *which* sentences they are.
UNVERIFIED_MARKER = "〔未通过核对〕"


@dataclass(frozen=True, slots=True)
class RenderedReport:
    markdown: str
    json_text: str


def render_verified_report(
    snapshot: WriterSnapshot,
    draft: ReportDraft,
    *,
    citation_map: dict[str, list[UUID]],
    verification_status: VerificationStatus,
    failed_statement_ids: list[str] | None = None,
    requirement_failures: list[dict[str, Any]] | None = None,
    quality_reminders: list[dict[str, Any]] | None = None,
) -> RenderedReport:
    """Render a frozen draft; citations come only from verified support excerpts."""
    excerpt_by_id = {
        excerpt.excerpt_id: excerpt for card in snapshot.evidence_cards for excerpt in card.excerpts
    }
    source_numbers: dict[tuple[str, int], int] = {}
    source_excerpts: dict[tuple[str, int], WriterExcerptRef] = {}
    source_cited_excerpt_ids: dict[tuple[str, int], list[UUID]] = {}
    statement_citations: dict[str, list[int]] = {}

    for statement in draft.statements():
        numbers: list[int] = []
        for excerpt_id in citation_map.get(statement.statement_id, []):
            excerpt = excerpt_by_id[excerpt_id]
            key = (excerpt.source.source_uri, excerpt.source.document_version)
            number = source_numbers.setdefault(key, len(source_numbers) + 1)
            source_excerpts.setdefault(key, excerpt)
            cited_ids = source_cited_excerpt_ids.setdefault(key, [])
            if excerpt_id not in cited_ids:
                cited_ids.append(excerpt_id)
            if number not in numbers:
                numbers.append(number)
        statement_citations[statement.statement_id] = numbers

    failed = set(failed_statement_ids or [])

    lines: list[str] = []
    if verification_status == "pending":
        lines.extend(["> **草稿预览：正文和引用尚未逐句验证。**", ""])
    elif verification_status == "partial" and failed:
        requirement_note = " 报告同时仍有未解决的整篇质量问题。" if requirement_failures else ""
        lines.extend(
            [
                f"> **部分句子未通过逐句验证；这些句子保留原文，"
                f"在句末标注 {UNVERIFIED_MARKER}，且不附已验证引用角标。"
                f"{requirement_note}**",
                "",
            ]
        )
    elif verification_status == "partial":
        lines.extend(
            [
                "> **报告在修订次数用尽后仍有未解决的整篇质量问题，"
                "因此不能标记为已验证。引用角标只表示相关句子的事实依据已通过核对。**",
                "",
            ]
        )

    lines.extend([f"# {draft.title}", ""])

    def append_paragraph(paragraph: Any) -> None:
        rendered_statements: list[str] = []
        for statement in paragraph.statements:
            prefix = "**信息局限：**" if statement.kind == "limitation" else ""
            if statement.kind == "derived":
                prefix = ""
            citations = "".join(
                f"[^{number}]" for number in statement_citations[statement.statement_id]
            )
            # Withholding the footnote is not a signal a reader can read: premise-only
            # derived statements carry no footnote either, so an unverified claim is
            # otherwise indistinguishable from a sound one. Marking it is the only way
            # the page tells the truth about which sentences were checked.
            mark = UNVERIFIED_MARKER if statement.statement_id in failed else ""
            rendered_statements.append(f"{prefix}{statement.text}{citations}{mark}")
        lines.extend(["".join(rendered_statements), ""])

    lines.extend(["## 引言", ""])
    for paragraph in draft.introduction:
        append_paragraph(paragraph)

    for section in draft.sections:
        lines.extend([f"## {section.title}", ""])
        for paragraph in section.paragraphs:
            append_paragraph(paragraph)

    lines.extend(["## 综合结论", ""])
    for paragraph in draft.conclusion:
        append_paragraph(paragraph)

    lines.extend(["## 来源", ""])
    for key, number in sorted(source_numbers.items(), key=lambda item: item[1]):
        lines.append(f"[^{number}]: {_source_label(source_excerpts[key])}")
    lines.append("")

    payload: dict[str, Any] = {
        "verification_status": verification_status,
        "failed_statement_ids": list(failed_statement_ids or []),
        "requirement_failures": list(requirement_failures or []),
        "quality_reminders": list(quality_reminders or []),
        "job_id": str(snapshot.job_id),
        "draft": draft.model_dump(mode="json"),
        "statement_citations": statement_citations,
        "citation_excerpt_ids": {
            statement_id: [str(value) for value in excerpt_ids]
            for statement_id, excerpt_ids in citation_map.items()
        },
        "sources": [
            {
                "citation_number": number,
                "source_uri": key[0],
                "document_version": key[1],
                "title": source_excerpts[key].source.title,
                "author": source_excerpts[key].source.author,
                "published_at": source_excerpts[key].source.published_at,
                "excerpt_ids": [str(excerpt_id) for excerpt_id in source_cited_excerpt_ids[key]],
            }
            for key, number in sorted(source_numbers.items(), key=lambda item: item[1])
        ],
    }
    return RenderedReport(
        markdown="\n".join(lines),
        json_text=json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
    )


def _source_label(excerpt: WriterExcerptRef) -> str:
    source = excerpt.source
    parts = [source.title or source.source_uri]
    if source.author:
        parts.append(source.author)
    if source.published_at:
        parts.append(source.published_at)
    parts.append(f"{source.source_uri}（版本 {source.document_version}）")
    return "，".join(parts)


def _json_default(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")
