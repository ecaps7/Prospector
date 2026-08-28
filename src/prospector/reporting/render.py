"""Deterministically render one structured report draft to Markdown and JSON."""

from __future__ import annotations

import json
import os
import re
import textwrap
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from prospector.schemas.claims import ReportVerifierFindings, ReportVerifierSnapshot
from prospector.schemas.report import ReportDraft, WriterExcerptRef, WriterSnapshot


@dataclass(frozen=True, slots=True)
class RenderedReportDraft:
    markdown: str
    json_text: str


def _source_label(excerpt: WriterExcerptRef) -> str:
    source = excerpt.source
    parts = [source.title or source.source_uri]
    if source.author:
        parts.append(source.author)
    if source.published_at:
        parts.append(source.published_at)
    parts.append(f"{source.source_uri}（版本 {source.document_version}）")
    return "，".join(parts)


def render_report_draft(
    snapshot: WriterSnapshot,
    draft: ReportDraft,
    *,
    verified: bool = False,
) -> RenderedReportDraft:
    """Render citations by first source appearance without changing statement text."""
    excerpt_by_id = {
        excerpt.excerpt_id: excerpt for card in snapshot.evidence_cards for excerpt in card.excerpts
    }
    source_numbers: dict[tuple[str, int], int] = {}
    source_excerpts: dict[tuple[str, int], WriterExcerptRef] = {}
    source_cited_excerpt_ids: dict[tuple[str, int], list[UUID]] = {}
    statement_citations: dict[str, list[int]] = {}

    for statement in draft.statements():
        numbers: list[int] = []
        for excerpt_id in statement.candidate_excerpt_ids:
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

    lines: list[str] = []
    if not verified:
        lines.extend(
            [
                "> **草稿预览：正文和引用尚未逐句验证。**",
                "",
            ]
        )
    lines.extend([f"# {draft.title}", ""])

    def append_paragraph(paragraph: Any) -> None:
        rendered_statements: list[str] = []
        for statement in paragraph.statements:
            prefix = ""
            if statement.kind == "limitation":
                prefix = "**信息局限：**"
            citations = "".join(
                f"[^{number}]" for number in statement_citations[statement.statement_id]
            )
            rendered_statements.append(f"{prefix}{statement.text}{citations}")
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
        "verification_status": "pending",
        "failed_statement_ids": [],
        "requirement_failures": [],
        "quality_reminders": [],
        "job_id": str(snapshot.job_id),
        "draft": draft.model_dump(mode="json"),
        "statement_citations": statement_citations,
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
    return RenderedReportDraft(
        markdown="\n".join(lines),
        json_text=json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
    )


def _short_uuid(value: str | UUID) -> str:
    """Render a UUID as its first 4 and last 4 hex digits."""
    s = str(value).replace("-", "")
    return f"{s[:4]}…{s[-4:]}"


_RELATION_ICON = {"support": "↗", "contradict": "↘", "partial": "~", "irrelevant": "–"}
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _truncate_uuids(text: str) -> str:
    """Replace full UUIDs in running text with their short form."""
    return _UUID_RE.sub(lambda m: _short_uuid(m.group()), text)


def _render_findings_excerpt_context(
    failure: Any,
    statement_input: Any,
) -> list[str]:
    """Render per-excerpt context lines for an evidence failure."""
    if failure.kind != "evidence" or not statement_input:
        return []
    excerpt_meta = {str(exc["excerpt_id"]): exc for exc in statement_input.candidate_excerpts}
    if not excerpt_meta:
        return []
    lines: list[str] = []
    for exc_id, meta in excerpt_meta.items():
        title = meta.get("title") or meta.get("url") or "unknown"
        lines.append(f"   [{_short_uuid(exc_id)}] {title}")
    return lines


def _terminal_width() -> int:
    """Return the current terminal width, defaulting to 100 if unavailable."""
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 100


def render_findings(
    snapshot: ReportVerifierSnapshot,
    findings: ReportVerifierFindings,
    *,
    line_width: int | None = None,
) -> str:
    """Render ReportVerifierFindings as a compact, human-readable report.

    Parameters
    ----------
    snapshot:
        The input snapshot used to cross-reference statement text and excerpt
        metadata (titles, authors).
    findings:
        The verifier findings to render.
    line_width:
        Max width for wrapping. If *None* (default), auto-detects the
        terminal width.
    """
    w = line_width or _terminal_width()
    stmt_index = {s.statement_id: s for s in snapshot.statements}

    # Classify failures
    llm_failures = []
    cascade_failures = []
    for failure in findings.failures:
        if "硬闸门" in failure.reason:
            cascade_failures.append(failure)
        else:
            llm_failures.append(failure)

    n_passed = len(findings.passed_statement_ids)
    n_llm = len(llm_failures)
    n_cascade = len(cascade_failures)
    n_requirements = len(findings.requirement_failures)
    n_quality = len(findings.quality_reminders)

    sep = "═" * w

    lines: list[str] = [
        sep,
        f" Report Verifier Findings · round={findings.round} · revision={findings.revision}",
        sep,
        f" ✓ {n_passed} passed   ✗ {n_llm} failed (LLM)   "
        f"⛓ {n_cascade} blocked (cascade)   ! {n_requirements} requirement failures   "
        f"◇ {n_quality} quality reminders",
        "",
    ]

    if findings.requirement_failures:
        lines.append(f"─── 报告任务未履行 ({n_requirements} 条) {'─' * max(1, w - 22)}")
        lines.append("")
        for failure in findings.requirement_failures:
            locations = failure.paragraph_ids or failure.statement_ids
            location = ", ".join(locations) or "整份报告"
            lines.append(f" ! [{failure.kind}/{failure.repair_scope}] ← {location}")
            lines.append(
                textwrap.fill(
                    failure.reason,
                    width=w,
                    initial_indent="   │ ",
                    subsequent_indent="   │ ",
                )
            )
            lines.append("")

    # ── LLM judged failures ──
    if llm_failures:
        lines.append(f"─── LLM 判定失败 {'─' * (w - 18)}")
        lines.append("")
        for failure in llm_failures:
            si = stmt_index.get(failure.statement_id)
            lines.append(f" ✗ {failure.statement_id} [{failure.kind}] → {failure.status}")
            if si:
                lines.append(f"   「{si.text}」")
            # Excerpt context
            lines.extend(_render_findings_excerpt_context(failure, si))
            # Wrapped reason (truncate UUIDs first)
            clean_reason = _truncate_uuids(failure.reason)
            wrapped = textwrap.fill(
                clean_reason, width=w, initial_indent="   │ ", subsequent_indent="   │ "
            )
            lines.append(wrapped)
            lines.append("")

    # Build failure lookup for cross-referencing
    failure_index = {f.statement_id: f for f in findings.failures}

    # ── Cascade failures ──
    if cascade_failures:
        lines.append(f"─── 硬闸门级联拦截 {'─' * (w - 18)}")
        lines.append("")
        for failure in cascade_failures:
            si = stmt_index.get(failure.statement_id)
            lines.append(f" ⛓ {failure.statement_id} [{failure.kind}]")
            if si:
                lines.append(f"   「{si.text}」")
                # Show which premises failed
                failed_premises = [p for p in si.premises if p["statement_id"] in failure_index]
                if failed_premises:
                    for p in failed_premises:
                        p_status = failure_index[p["statement_id"]].status
                        lines.append(f"   ← {p['statement_id']} ({p_status}): {p.get('text', '')}")
                else:
                    # premises_all_passed=False but individual premise not in
                    # failures list — show all premises for context
                    for p in si.premises:
                        lines.append(f"   ← {p['statement_id']}: {p.get('text', '')}")
            lines.append("")

    # ── Passed summary ──
    if findings.passed_statement_ids:
        lines.append(f"─── 通过 ({n_passed} 条) {'─' * (w - 12 - len(str(n_passed)))}")
        lines.append("")

    if findings.quality_reminders:
        lines.append(f"─── 报告质量提醒 ({n_quality} 条) {'─' * max(1, w - 22)}")
        lines.append("")
        for reminder in findings.quality_reminders:
            statement_ids = ", ".join(reminder.statement_ids) or "整段/整章"
            lines.append(f" ◇ {reminder.location} [{reminder.kind}] ← {statement_ids}")
            lines.append(
                textwrap.fill(
                    reminder.reason,
                    width=w,
                    initial_indent="   │ ",
                    subsequent_indent="   │ ",
                )
            )
            lines.append("")
        row: list[str] = []
        for sid in findings.passed_statement_ids:
            row.append(f"{sid:<8}")
            if len(row) == 8:
                lines.append("  " + "".join(row))
                row = []
        if row:
            lines.append("  " + "".join(row))
        lines.append("")

    lines.append(sep)
    return "\n".join(lines)


def _json_default(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")
