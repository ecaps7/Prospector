"""Deterministic citation insertion for verified Claim spans."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from prospector.schemas.claims import AttributionRun, ReportReviewRun
from prospector.schemas.report import MarkdownBlock, WriterSnapshot

UNVERIFIED_MARKER = "〔未获事实支持〕"
# A footnote exists so a reader can check one statement against one source.  Past a
# handful the marks stop being navigable and start burying the prose: the first real
# report came back with 519 marks, one span carrying 21, and a title carrying 5.  The
# full chain is never lost -- every binding stays in the audit JSON.
MAX_INLINE_CITATIONS = 3
# A mark may sit anywhere except inside a Latin word or number.  Claim spans end where a
# checkable statement ends, which is occasionally mid-token ("agentic AI foundat|ion").
#
# The rule covers only what a renderer can decide: Chinese has no word boundaries, so a
# mark landing inside "2026年|初" cannot be repaired here.  Nudging it one character was
# tried and made things worse -- it moved 12 marks to fix 3, and the other 9 landed
# inside a different word ("2025年逐|步", "2025年提|供").  That is a span-choice problem
# and belongs upstream, not in presentation.


def _splits_a_word(text: str, at: int) -> bool:
    if not 0 < at < len(text):
        return False
    before, after = text[at - 1], text[at]
    return before.isascii() and before.isalnum() and after.isascii() and after.isalnum()


def _readable_offset(text: str, at: int) -> int:
    """Move an insertion point past a Latin word or number it would otherwise split."""
    while _splits_a_word(text, at):
        at += 1
    return at


@dataclass(frozen=True, slots=True)
class ReportHealth:
    """What the checks actually established, counted rather than asserted.

    Every number here is derived from records the pipeline already holds, so none of it
    depends on a model's summary of its own work.  It exists because the chain measured
    overclaiming in fine detail and measured omission not at all, and because a reader
    handed a report with no idea how much of it was checked cannot weigh it.
    """

    blocks: int
    checked_blocks: int
    reasoned_blocks: int
    failed_claims: int
    unchecked_spans: int
    assertions_collected: int
    assertions_used: int
    unused_assertion_ids: tuple[UUID, ...]
    quantities_in_checked_text: int
    quantities_in_reasoning: int
    # Spans bound to more sources than are shown inline.  A high count is the report
    # citing everything related instead of what actually carries the statement.
    spans_over_citation_cap: int = 0


def report_health(
    attribution: AttributionRun,
    snapshot: WriterSnapshot,
    blocks: Sequence[MarkdownBlock],
) -> ReportHealth:
    evidence_claims = {item.claim_id for item in attribution.claim_evidence}
    failed = {item.claim_id for item in attribution.blocking_findings if item.claim_id}
    # The two sets overlap on purpose: a paragraph normally states a checked fact and
    # then reasons from it.  Subtracting one from the other reported zero reasoning for a
    # report that carried twenty-six reasoning spans.
    checked_blocks = {
        claim.block_id for claim in attribution.claims if claim.claim_id in evidence_claims
    }
    reasoned_blocks = {
        claim.block_id
        for claim in attribution.claims
        if claim.claim_id not in evidence_claims and claim.claim_id not in failed
    }
    used = {
        assertion_id
        for premise in attribution.claim_premises
        for assertion_id in premise.direct_assertion_ids
    }
    collected = [card.assertion_id for card in snapshot.evidence_cards]
    unchecked_markers = [
        note
        for note in attribution.audit_notes
        if note.get("kind") == "unchecked_marker_in_analysis"
    ]
    return ReportHealth(
        blocks=len(blocks),
        checked_blocks=len(checked_blocks),
        reasoned_blocks=len(reasoned_blocks),
        failed_claims=len(failed),
        unchecked_spans=sum(1 for item in attribution.blocking_findings if item.claim_id is None),
        assertions_collected=len(collected),
        assertions_used=len([item for item in collected if item in used]),
        unused_assertion_ids=tuple(item for item in collected if item not in used),
        # Counted by position, not per claim: claim spans nest, and summing their markers
        # reported 314 quantities in a report that contains 165.
        quantities_in_checked_text=len(
            {
                (claim.block_id, marker.start_offset, marker.end_offset)
                for claim in attribution.claims
                if claim.claim_id in evidence_claims
                for marker in claim.markers
                if marker.family == "retrieval"
            }
        ),
        quantities_in_reasoning=sum(len(note.get("markers", [])) for note in unchecked_markers),
        spans_over_citation_cap=sum(
            1
            for claim_id, items in _evidence_by_claim(attribution).items()
            if claim_id in evidence_claims and len(set(items)) > MAX_INLINE_CITATIONS
        ),
    )


def _evidence_by_claim(attribution: AttributionRun) -> dict[UUID, list[UUID]]:
    grouped: dict[UUID, list[UUID]] = {}
    for item in attribution.claim_evidence:
        grouped.setdefault(item.claim_id, []).append(item.excerpt_id)
    return grouped


def health_summary_markdown(health: ReportHealth, status: str) -> str:
    """The reader-facing version of the same counts, in one short paragraph."""

    verdict = {
        "verified": "全部核对通过",
        "partial": "部分核对未通过",
        "failed": "核心内容核对未通过",
    }.get(status, "核对进行中")
    lines = [
        f"> **核对情况（{verdict}）**：全文 {health.blocks} 段，"
        f"其中 {health.checked_blocks} 段含有已核对的具体事实，"
        f"{health.reasoned_blocks} 段含有基于这些事实的推理。"
    ]
    if health.failed_claims:
        lines.append(f"有 {health.failed_claims} 处核对未通过，正文中已标注。")
    if health.unchecked_spans:
        lines.append(f"有 {health.unchecked_spans} 处未能给出核对结论。")
    lines.append(
        f"共收集研究材料 {health.assertions_collected} 条，本报告使用 {health.assertions_used} 条。"
    )
    if health.quantities_in_reasoning:
        lines.append(
            f"另有 {health.quantities_in_reasoning} 处数字或时间出现在推理段落中，未逐项核对。"
        )
    return " ".join(lines)


@dataclass(frozen=True, slots=True)
class RenderedReport:
    markdown: str
    json_text: str


def render_final_report(
    markdown: str,
    blocks: list[MarkdownBlock],
    attribution: AttributionRun,
    review: ReportReviewRun,
    snapshot: WriterSnapshot,
    *,
    status: str,
    readthrough: dict[str, Any] | None = None,
) -> RenderedReport:
    """Attach footnotes only to verified spans; preserve frozen visible text otherwise."""
    excerpts = {
        excerpt.excerpt_id: excerpt for card in snapshot.evidence_cards for excerpt in card.excerpts
    }
    assertion_ids_by_excerpt: dict[UUID, list[UUID]] = {}
    for card in snapshot.evidence_cards:
        for excerpt in card.excerpts:
            assertion_ids_by_excerpt.setdefault(excerpt.excerpt_id, []).append(card.assertion_id)
    source_caveats: dict[UUID, list[str]] = {}
    for gap in snapshot.minor_gaps:
        if gap.get("kind") != "source_credibility":
            continue
        for assertion_id in gap.get("related_assertion_ids", []):
            source_caveats.setdefault(UUID(str(assertion_id)), []).append(str(gap["description"]))
    evidence_by_claim: dict[UUID, list[UUID]] = {}
    for item in attribution.claim_evidence:
        evidence_by_claim.setdefault(item.claim_id, []).append(item.excerpt_id)
    failed_claims = {item.claim_id for item in attribution.blocking_findings if item.claim_id}
    source_numbers: dict[tuple[str, int], int] = {}
    source_refs: dict[tuple[str, int], object] = {}
    block_starts = {
        block.block_id: block.source_start for block in blocks if block.source_start >= 0
    }
    # Citations are spliced at absolute document offsets.  Block texts repeat freely --
    # table cells like "是" occur many times -- so locating a block by searching its text
    # would attach a footnote to the first match anywhere in the document instead.
    # A heading is a label whose content the section restates; citing it duplicates the
    # body's marks on text that carries no checkable statement of its own.
    heading_blocks = {block.block_id for block in blocks if block.kind == "heading"}
    # Marks are gathered by the position they land on rather than by the claim that
    # produced them.  Claim spans nest and often end together, so a per-claim cap still
    # let three claims stack nine marks against one full stop.
    marks_at: dict[int, list[str]] = {}
    unverified_at: set[int] = set()
    block_text = {block.block_id: block.text for block in blocks}
    for claim in attribution.claims:
        block_start = block_starts.get(claim.block_id)
        if block_start is None or claim.block_id in heading_blocks:
            continue
        at = block_start + _readable_offset(block_text[claim.block_id], claim.end_offset)
        for excerpt_id in evidence_by_claim.get(claim.claim_id, []):
            excerpt = excerpts[excerpt_id]
            key = (excerpt.source.source_uri, excerpt.source.document_version)
            number = source_numbers.setdefault(key, len(source_numbers) + 1)
            source_refs.setdefault(key, excerpt)
            marks_at.setdefault(at, []).append(f"[^{number}]")
        if claim.claim_id in failed_claims:
            unverified_at.add(at)
    for finding in attribution.blocking_findings:
        if finding.claim_id is not None or finding.end_offset is None:
            continue
        block_start = block_starts.get(finding.block_id)
        if block_start is not None and finding.block_id not in heading_blocks:
            unverified_at.add(
                block_start + _readable_offset(block_text[finding.block_id], finding.end_offset)
            )
    inserts: list[tuple[int, str]] = []
    for at in sorted(marks_at.keys() | unverified_at):
        marks = list(dict.fromkeys(marks_at.get(at, [])))[:MAX_INLINE_CITATIONS]
        if at in unverified_at:
            marks.append(UNVERIFIED_MARKER)
        if marks:
            inserts.append((at, "".join(marks)))
    health = report_health(attribution, snapshot, blocks)
    result = markdown
    for offset, insert in sorted(inserts, key=lambda item: item[0], reverse=True):
        result = result[:offset] + insert + result[offset:]
    # The health line goes at the top, ahead of the prose: a reader deciding how much
    # weight to put on the report needs it before reading, not after.
    result = health_summary_markdown(health, status) + "\n\n" + result
    if source_numbers:
        result += "\n\n## 来源\n\n"
        for key, number in sorted(source_numbers.items(), key=lambda item: item[1]):
            excerpt = source_refs[key]
            source = excerpt.source  # type: ignore[union-attr]
            label = source.title or source.source_uri
            result += (
                f"[^{number}]: {label}，{source.source_uri}（版本 {source.document_version}）\n"
            )
    audit = {
        "verification_status": status,
        "inline_citation_cap": MAX_INLINE_CITATIONS,
        "health": {
            **asdict(health),
            "unused_assertion_ids": [str(item) for item in health.unused_assertion_ids],
        },
        "readthrough": readthrough,
        "claims": [claim.model_dump(mode="json") for claim in attribution.claims],
        "claim_evidence": [
            {
                "claim_id": str(item.claim_id),
                "excerpt": excerpts[item.excerpt_id].model_dump(mode="json"),
                "assertion_ids": [
                    str(value) for value in assertion_ids_by_excerpt.get(item.excerpt_id, [])
                ],
                "source_caveats": [
                    caveat
                    for assertion_id in assertion_ids_by_excerpt.get(item.excerpt_id, [])
                    for caveat in source_caveats.get(assertion_id, [])
                ],
            }
            for item in attribution.claim_evidence
        ],
        "claim_premises": [item.model_dump(mode="json") for item in attribution.claim_premises],
        "blocking_findings": [
            item.model_dump(mode="json") for item in attribution.blocking_findings
        ],
        "audit_notes": attribution.audit_notes,
        "whole_report_review": {
            "blocking_findings": [
                item.model_dump(mode="json") for item in review.blocking_findings
            ],
            "key_block_ids": review.key_block_ids,
            "audit_notes": review.audit_notes,
        },
        "conflicts": snapshot.conflicts,
        "minor_gaps": snapshot.minor_gaps,
        "retrieval_claims": sum(
            claim.claim_id in evidence_by_claim or claim.claim_id in failed_claims
            for claim in attribution.claims
        ),
        "verified_claims": len(evidence_by_claim),
        "failed_claims": len(attribution.blocking_findings),
        "verified_claims_in_key_blocks": sum(
            claim.claim_id in evidence_by_claim and claim.block_id in set(review.key_block_ids)
            for claim in attribution.claims
        ),
    }
    return RenderedReport(
        markdown=result, json_text=json.dumps(audit, ensure_ascii=False, indent=2, default=str)
    )
