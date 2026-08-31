# ruff: noqa: E501
"""Post-writing Claim Attribution adapter."""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast
from uuid import UUID, uuid4

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from prospector.agents.llm import get_openai_client, strong_model, thinking_extra_body
from prospector.deterministic.excerpt_text import clip_excerpt_text, writer_excerpt_limit
from prospector.deterministic.markdown_report import (
    MARKER_LEXICON_VERSION,
    AttributionBatchSpec,
    build_entity_whitelist,
    parse_markdown,
    partition_attribution_batches,
    retrieval_candidate_spans,
    scan_markers,
    text_hash,
    validate_claim_span,
)
from prospector.schemas.claims import (
    AttributionBatchClaim,
    AttributionBatchSelection,
    AttributionBatchVerification,
    AttributionFinding,
    AttributionRun,
    AttributionSummary,
    BlockAssessment,
    ClaimEvidence,
    ClaimMarker,
    ClaimPremise,
    ClaimSpan,
    ReportReviewRun,
    RevisionFailureDisposition,
    core_attribution_finding_ids,
)
from prospector.schemas.report import MarkdownBlock, WriterSnapshot

# A single malformed span is a formatting slip, not a reason to discard a whole Job's
# research: the offending claim is dropped and recorded instead.  The ratio guard keeps
# that leniency from covering an output that is broken end to end.
MAX_INVALID_CLAIM_RATIO = 0.2
ATTRIBUTION_ATTEMPTS = 2
SchemaT = TypeVar("SchemaT", bound=BaseModel)


class ClaimAttributionOutputError(ValueError):
    def __init__(self, message: str, raw_output: object) -> None:
        super().__init__(message)
        self.raw_output = raw_output


@dataclass(frozen=True, slots=True)
class ClaimAttributionResult:
    full_prompt: list[dict[str, str]]
    raw_output: str
    run: AttributionRun


@dataclass(frozen=True, slots=True)
class AttributionPlan:
    blocks: tuple[MarkdownBlock, ...]
    candidates: tuple[dict[str, Any], ...]
    batches: tuple[AttributionBatchSpec, ...]
    catalog: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "marker_lexicon_version": MARKER_LEXICON_VERSION,
            "batches": [
                {
                    "batch_index": item.batch_index,
                    "block_ids": list(item.block_ids),
                    "candidate_refs": list(item.candidate_refs),
                }
                for item in self.batches
            ],
        }


class ClaimAttributionModel(Protocol):
    def select_materials(
        self, prompt: list[dict[str, str]]
    ) -> tuple[AttributionBatchSelection, str]: ...

    def verify_batch(
        self, prompt: list[dict[str, str]]
    ) -> tuple[AttributionBatchVerification, str]: ...

    def summarize(self, prompt: list[dict[str, str]]) -> tuple[AttributionSummary, str]: ...


class AttributionPersistence(Protocol):
    def begin_attribution_run(
        self, report_id: UUID, revision: int, plan: dict[str, Any]
    ) -> UUID: ...

    def list_attribution_batches(self, run_id: UUID) -> list[dict[str, Any]]: ...

    def begin_attribution_batch(
        self,
        run_id: UUID,
        batch_index: int,
        *,
        block_ids: Sequence[str],
        candidate_refs: Sequence[str],
        selection_prompt: list[dict[str, str]] | None,
    ) -> dict[str, Any]: ...

    def save_attribution_batch_selection(
        self,
        run_id: UUID,
        batch_index: int,
        *,
        raw_output: object,
        result: dict[str, Any],
        verify_prompt: list[dict[str, str]],
    ) -> None: ...

    def complete_attribution_batch(
        self,
        run_id: UUID,
        batch_index: int,
        *,
        raw_output: object,
        result: dict[str, Any],
    ) -> None: ...

    def fail_attribution_batch(
        self,
        run_id: UUID,
        batch_index: int,
        *,
        raw_output: object,
        error: str,
    ) -> None: ...

    def get_attribution_attempt(self, report_id: UUID, revision: int) -> dict[str, Any] | None: ...

    def begin_attribution_summary(
        self, run_id: UUID, prompt: list[dict[str, str]]
    ) -> dict[str, Any]: ...

    def complete_attribution_summary(
        self, run_id: UUID, *, raw_output: object, result: dict[str, Any]
    ) -> None: ...

    def fail_attribution_run(self, run_id: UUID, *, raw_output: object, error: str) -> None: ...

    def complete_attribution_run(self, run: AttributionRun) -> None: ...


def entity_names(snapshot: WriterSnapshot) -> set[str]:
    return build_entity_whitelist(
        value
        for card in snapshot.evidence_cards
        for value in (card.assertion_statement, *(excerpt.text for excerpt in card.excerpts))
    )


def _source_caveats(snapshot: WriterSnapshot) -> dict[str, list[str]]:
    caveats: dict[str, list[str]] = {}
    for gap in snapshot.minor_gaps:
        if gap.get("kind") != "source_credibility":
            continue
        for assertion_id in gap.get("related_assertion_ids", []):
            caveats.setdefault(str(assertion_id), []).append(str(gap["description"]))
    return caveats


class EvidenceRefs:
    """Short, stable names for this Job's material, and the way back to real ids.

    Every model-facing mention of an Assertion or an Excerpt goes through here.  The refs
    are positional in snapshot order, so they are identical in every prompt of a Job and
    an Excerpt ref carries its Assertion inside it ("a17e2"), which makes the
    Excerpt-belongs-to-Assertion rule visible to the model rather than only enforced
    after the fact.
    """

    def __init__(self, snapshot: WriterSnapshot) -> None:
        self.assertion_by_ref: dict[str, UUID] = {}
        self.ref_by_assertion: dict[UUID, str] = {}
        self.excerpt_by_ref: dict[str, UUID] = {}
        self.ref_by_excerpt: dict[UUID, str] = {}
        self.excerpt_refs_by_assertion: dict[UUID, list[str]] = {}
        self.conflict_by_ref: dict[str, str] = {}
        self.ref_by_conflict: dict[str, str] = {}
        for position, card in enumerate(snapshot.evidence_cards, start=1):
            ref = f"a{position}"
            self.assertion_by_ref[ref] = card.assertion_id
            self.ref_by_assertion[card.assertion_id] = ref
            refs: list[str] = []
            for index, excerpt in enumerate(card.excerpts, start=1):
                excerpt_ref = f"{ref}e{index}"
                self.excerpt_by_ref[excerpt_ref] = excerpt.excerpt_id
                # One Excerpt can back several Assertions; the first ref wins so the
                # mapping stays single-valued in both directions.
                self.ref_by_excerpt.setdefault(excerpt.excerpt_id, excerpt_ref)
                refs.append(excerpt_ref)
            self.excerpt_refs_by_assertion[card.assertion_id] = refs
        for index, item in enumerate(snapshot.conflicts, start=1):
            key = str(item.get("conflict_key") or "")
            if not key:
                continue
            ref = f"x{index}"
            self.conflict_by_ref[ref] = key
            self.ref_by_conflict[key] = ref

    def assertions(self, refs: Sequence[str]) -> list[UUID]:
        return [self.assertion_by_ref[ref] for ref in refs if ref in self.assertion_by_ref]

    def excerpts(self, refs: Sequence[str]) -> list[UUID]:
        return [self.excerpt_by_ref[ref] for ref in refs if ref in self.excerpt_by_ref]

    def unknown(self, refs: Sequence[str], *, excerpts: bool = False) -> list[str]:
        known = self.excerpt_by_ref if excerpts else self.assertion_by_ref
        return [ref for ref in refs if ref not in known]

    def conflicts(self, refs: Sequence[str]) -> list[str]:
        unknown = [ref for ref in refs if ref not in self.conflict_by_ref]
        if unknown:
            raise ValueError(f"unknown Conflict refs: {', '.join(dict.fromkeys(unknown))}")
        return [self.conflict_by_ref[ref] for ref in refs]


def assertion_catalog(snapshot: WriterSnapshot) -> list[dict[str, Any]]:
    """Statement directory only; Excerpt text is expanded after the model chooses."""

    caveats = _source_caveats(snapshot)
    refs = EvidenceRefs(snapshot)
    return [
        {
            "ref": refs.ref_by_assertion[card.assertion_id],
            "statement": card.assertion_statement,
            "source_caveats": caveats.get(str(card.assertion_id), []),
        }
        for card in snapshot.evidence_cards
    ]


def expand_selected_sources(
    snapshot: WriterSnapshot, assertion_ids: Sequence[UUID]
) -> list[dict[str, Any]]:
    """Expand the chosen Assertions into source cards under the same Excerpt budget.

    The batch character budget only sizes the report side of the prompt. Selection can
    legitimately pick most of the catalog, so unclipped Excerpt bodies -- an Exa
    highlight runs to several thousand characters -- put the whole evidence pool back
    into one request and blow past the context window, which is what turns a written
    report into an attribution contract failure. Clipping uses the same deterministic
    rule as Synthesis and the Writer so the same passage reads the same everywhere.
    """
    refs = EvidenceRefs(snapshot)
    wanted = list(dict.fromkeys(assertion_ids))
    cards = {card.assertion_id: card for card in snapshot.evidence_cards}
    caveats = _source_caveats(snapshot)
    limit = writer_excerpt_limit(sum(len(cards[item].excerpts) for item in wanted))
    sources: list[dict[str, Any]] = []
    for assertion_id in wanted:
        card = cards[assertion_id]
        excerpt_refs = refs.excerpt_refs_by_assertion[assertion_id]
        sources.append(
            {
                "ref": refs.ref_by_assertion[assertion_id],
                "statement": card.assertion_statement,
                "source_caveats": caveats.get(str(card.assertion_id), []),
                "excerpts": [
                    {
                        "ref": excerpt_ref,
                        "text": clip_excerpt_text(excerpt.text, limit),
                        "source": excerpt.source.model_dump(mode="json"),
                    }
                    for excerpt_ref, excerpt in zip(excerpt_refs, card.excerpts, strict=True)
                ],
            }
        )
    return sources


def candidate_specs(
    blocks: Sequence[MarkdownBlock], snapshot: WriterSnapshot
) -> list[dict[str, Any]]:
    names = entity_names(snapshot)
    candidates: list[dict[str, Any]] = []
    for block in blocks:
        markers = scan_markers(block.text, names, block_kind=block.kind)
        for start, end in retrieval_candidate_spans(block, markers):
            relevant = [marker for marker in markers if start <= marker.start_offset < end]
            candidates.append(
                {
                    "candidate_ref": f"k{len(candidates) + 1}",
                    "block_id": block.block_id,
                    "text": block.text[start:end],
                    "start_offset": start,
                    "end_offset": end,
                    "requires_evidence": any(marker.family == "retrieval" for marker in relevant),
                    "markers": [item.model_dump(mode="json") for item in relevant],
                }
            )
    return candidates


def prepare_attribution_plan(
    markdown: str,
    snapshot: WriterSnapshot,
    *,
    only_blocks: Collection[str] | None = None,
) -> AttributionPlan:
    """Plan attribution over the whole report, or over just the blocks that changed.

    ``only_blocks`` is what makes a repair affordable.  A revision that replaces three
    paragraphs leaves the other ninety-eight byte-identical, and re-earning verdicts for
    text nobody touched is the cost that forced the repair budget down to two rounds.
    """
    blocks = parse_markdown(markdown)
    planned = (
        blocks if only_blocks is None else [item for item in blocks if item.block_id in only_blocks]
    )
    candidates = candidate_specs(planned, snapshot)
    return AttributionPlan(
        blocks=tuple(planned),
        candidates=tuple(candidates),
        batches=tuple(partition_attribution_batches(planned, candidates)),
        catalog=tuple(assertion_catalog(snapshot)),
    )


@dataclass(frozen=True, slots=True)
class CarriedAttribution:
    """Verdicts that survive a revision untouched, and the blocks that must be redone."""

    claims: tuple[ClaimSpan, ...]
    claim_evidence: tuple[ClaimEvidence, ...]
    claim_premises: tuple[ClaimPremise, ...]
    blocking_findings: tuple[AttributionFinding, ...]
    # Notes and span-level findings belong to text, not to a claim.  Leaving them behind
    # made a revision look healthier than the report it revised: the health summary lost
    # every unchecked quantity and every unanswered span the moment one paragraph moved.
    audit_notes: tuple[dict[str, Any], ...]
    dirty_block_ids: frozenset[str]


def plan_incremental_attribution(
    previous: AttributionRun,
    previous_blocks: Sequence[MarkdownBlock],
    blocks: Sequence[MarkdownBlock],
) -> CarriedAttribution:
    """Split a revised report into verdicts that still hold and blocks that need redoing.

    A block whose text is byte-identical keeps its verdicts: the claim spans, their
    offsets and their markers are all functions of that text.  What does not survive is
    reasoning that rested on a passage the revision rewrote -- if the fact a sentence was
    derived from is gone, the sentence has to be judged again rather than inherited on
    the strength of a premise that no longer exists.
    """
    unmatched = list(previous_blocks)
    carried_block: dict[str, str] = {}
    dirty: set[str] = set()
    cursor = 0
    for block in blocks:
        # Order-preserving match: identical text may repeat across a report, so scanning
        # forward keeps a later paragraph from inheriting an earlier one's verdicts.
        found = next(
            (
                position
                for position in range(cursor, len(unmatched))
                if unmatched[position].text_hash == block.text_hash
            ),
            None,
        )
        if found is None:
            dirty.add(block.block_id)
            continue
        carried_block[unmatched[found].block_id] = block.block_id
        cursor = found + 1

    premise_by_claim = {item.claim_id: item for item in previous.claim_premises}
    block_of_claim = {claim.claim_id: claim.block_id for claim in previous.claims}
    invalid: set[UUID] = set()
    while True:
        grew = False
        for claim in previous.claims:
            if claim.claim_id in invalid:
                continue
            premise = premise_by_claim.get(claim.claim_id)
            rests_on_rewritten = premise is not None and any(
                parent in invalid or block_of_claim.get(parent) not in carried_block
                for parent in premise.premise_claim_ids
            )
            if claim.block_id not in carried_block or rests_on_rewritten:
                invalid.add(claim.claim_id)
                # The block holding an invalidated claim has to be re-judged as a whole:
                # its other spans were answered in the same pass and under the same
                # reading of the passage.
                if (moved := carried_block.get(claim.block_id)) is not None:
                    dirty.add(moved)
                grew = True
        if not grew:
            break
    dirty |= {
        moved for original, moved in carried_block.items() if moved in dirty or original in dirty
    }

    kept = [
        claim
        for claim in previous.claims
        if claim.claim_id not in invalid and carried_block.get(claim.block_id) not in dirty
    ]
    kept_ids = {claim.claim_id for claim in kept}

    def survives(block_id: str | None) -> bool:
        moved = carried_block.get(str(block_id))
        return moved is not None and moved not in dirty

    return CarriedAttribution(
        claims=tuple(
            claim.model_copy(update={"block_id": carried_block[claim.block_id]}) for claim in kept
        ),
        claim_evidence=tuple(item for item in previous.claim_evidence if item.claim_id in kept_ids),
        claim_premises=tuple(item for item in previous.claim_premises if item.claim_id in kept_ids),
        blocking_findings=tuple(
            item.model_copy(update={"block_id": carried_block[item.block_id]})
            for item in previous.blocking_findings
            # A finding with no claim -- an unanswered span -- is still true of text the
            # revision did not touch, so it carries on the strength of its block alone.
            if (item.claim_id in kept_ids or item.claim_id is None) and survives(item.block_id)
        ),
        audit_notes=tuple(
            {**note, "block_id": carried_block[str(note["block_id"])]}
            for note in previous.audit_notes
            if note.get("block_id") is not None and survives(str(note["block_id"]))
        ),
        dirty_block_ids=frozenset(dirty),
    )


def _prior_failure_specs(
    previous: AttributionRun | None, previous_review: ReportReviewRun | None
) -> list[dict[str, Any]]:
    if previous is None:
        return []
    if previous_review is None:
        raise ValueError("a previous AttributionRun requires its Whole-report Review")
    core_ids = core_attribution_finding_ids(previous, previous_review)
    return [
        {
            "prior_ref": f"p{index}",
            "finding_id": str(item.finding_id),
            "block_id": item.block_id,
            "text": item.text,
            "reason": item.reason,
        }
        for index, item in enumerate(
            (item for item in previous.blocking_findings if item.finding_id in core_ids), start=1
        )
    ]


def _batch_blocks(plan: AttributionPlan, spec: AttributionBatchSpec) -> list[MarkdownBlock]:
    wanted = set(spec.block_ids)
    return [block for block in plan.blocks if block.block_id in wanted]


def _batch_candidates(plan: AttributionPlan, spec: AttributionBatchSpec) -> list[dict[str, Any]]:
    wanted = set(spec.candidate_refs)
    return [item for item in plan.candidates if item["candidate_ref"] in wanted]


def _block_payload(
    blocks: Sequence[MarkdownBlock], snapshot: WriterSnapshot
) -> list[dict[str, Any]]:
    names = entity_names(snapshot)
    return [
        {
            "block_id": block.block_id,
            "text": block.text,
            "markers": [
                marker.model_dump(mode="json")
                for marker in scan_markers(block.text, names, block_kind=block.kind)
            ],
        }
        for block in blocks
    ]


def selection_messages(
    plan: AttributionPlan,
    spec: AttributionBatchSpec,
    snapshot: WriterSnapshot,
) -> list[dict[str, str]]:
    blocks = _batch_blocks(plan, spec)
    candidates = _batch_candidates(plan, spec)
    system = """你负责为这一批报告正文选择可能相关的研究材料。

代码已经给出本批候选位置和一份精简的 Assertion 目录。目录只有断言文本，没有 Excerpt。每条材料带一个短编号 ref（形如 a17）。选出可能支持、限制或反驳本批候选的 Assertion，只写它们的 ref；不要为了覆盖目录而全选。

不得改写正文、使用目录之外的 ref，或根据训练知识补材料。最终只输出符合 output_schema 的单个 JSON 对象。"""
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "blocks": _block_payload(blocks, snapshot),
                    "candidates": candidates,
                    "assertion_catalog": list(plan.catalog),
                    "output_schema": AttributionBatchSelection.model_json_schema(),
                },
                ensure_ascii=False,
            ),
        },
    ]


def report_index(plan: AttributionPlan, spec: AttributionBatchSpec) -> list[dict[str, Any]]:
    """Every candidate position in the report, so a premise can point outside this batch.

    Batches are answered independently, so a batch cannot be handed the verdicts another
    batch reached.  It can be handed the report's own positions: a conclusion may declare
    that it rests on the passage at k12, and code resolves that position to whatever
    claims covered it once every batch is in.  Without this, a third of the analysis in
    the first real report -- the claims resting on facts established in earlier sections
    -- would have lost its grounding the moment the batches stopped running in order.
    """
    inside = set(spec.candidate_refs)
    return [
        {"candidate_ref": item["candidate_ref"], "block_id": item["block_id"], "text": item["text"]}
        for item in plan.candidates
        if item["candidate_ref"] not in inside
    ]


def verification_messages(
    plan: AttributionPlan,
    spec: AttributionBatchSpec,
    snapshot: WriterSnapshot,
    sources: Sequence[dict[str, Any]],
) -> list[dict[str, str]]:
    blocks = _batch_blocks(plan, spec)
    candidates = _batch_candidates(plan, spec)
    refs = EvidenceRefs(snapshot)
    known_conflicts = []
    for item in snapshot.conflicts:
        key = str(item.get("conflict_key") or "")
        if not key:
            continue
        known_conflicts.append(
            {
                "conflict_ref": refs.ref_by_conflict[key],
                "disputed_point": item.get("disputed_point"),
                "excerpt_refs": [
                    refs.ref_by_excerpt[UUID(str(value))]
                    for value in item.get("excerpt_ids") or []
                    if UUID(str(value)) in refs.ref_by_excerpt
                ],
                "decision": item.get("decision"),
                "winning_excerpt_refs": [
                    refs.ref_by_excerpt[UUID(str(value))]
                    for value in item.get("winning_excerpt_ids") or []
                    if UUID(str(value)) in refs.ref_by_excerpt
                ],
                "rationale": item.get("rationale"),
            }
        )
    system = """你负责冻结这一批报告正文的后置归因。

代码已经给出本批需要检查的候选位置，以及你刚选中的 Assertion 和对应 Excerpt。对每个候选位置，提取其中最小的、可以从研究材料中独立核对的具体陈述，并核对主体、动作、数字、时间、范围、口径和来源归属。

只核对具体事实锚点，不评价相邻的解释、意义判断或文章写法。分析内容使用 analysis，并记录它实际依赖的本轮 Claim、已完成批次中的 Claim、Assertion 或材料冲突；不要求 Excerpt 原文表达相同观点。专名只表示附近可能有具体事实，不自动使整个分析判断接受出处核对；requires_evidence=true 的候选必须由 verified 或 failed 的事实 Claim 覆盖。

材料用短编号引用：assertion_refs 写 Assertion 的 ref（形如 a17），excerpt_refs 写 Excerpt 的 ref（形如 a17e2，前缀就是它所属的 Assertion），known_conflict_refs 写材料冲突的 ref（形如 x1）。只使用输入中出现过的 ref，不要写完整 id 或 conflict key。

verified 必须绑定足以支持该陈述的给定 Assertion 和对应 Excerpt，并且**只绑定真正承载这句话的那几条**。把所有沾边的材料都列上不等于更严谨：它会让读者无法判断这句话到底出自哪里。一条陈述通常一到两条就够，超过三条说明这个 span 划得太宽，应该拆成更小的具体陈述。failed 必须说明正文写了什么、材料实际支持什么以及差异在哪里。每个 candidate_ref 都必须由至少一个覆盖相应标记的 Claim 引用。

claim_ref 是本批 span 的短编号。premise_claim_refs 可以引用本批其他 claim_ref；如果这段分析依赖的是本批之外的正文，就引用 report_index 里那处的 candidate_ref（形如 k12），代码会在全部批次完成后把它解析成对应判定。

不得改写正文、使用输入之外的知识补证据或提出文风建议。最终只输出符合 output_schema 的单个 JSON 对象。"""
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "blocks": _block_payload(blocks, snapshot),
                    "candidates": candidates,
                    "sources": list(sources),
                    "known_conflicts": known_conflicts,
                    "report_index": report_index(plan, spec),
                    "output_schema": AttributionBatchVerification.model_json_schema(),
                },
                ensure_ascii=False,
            ),
        },
    ]


def summary_messages(
    plan: AttributionPlan,
    snapshot: WriterSnapshot,
    claims: Sequence[dict[str, Any]],
    previous: AttributionRun | None,
    previous_review: ReportReviewRun | None,
) -> list[dict[str, str]]:
    system = """你核对本轮修订里上一版核心失败片段的去向。

previous_failed_claims 必须按 prior_ref 逐条说明它在新版中是 corrected、replaced_source、removed，还是仍以笼统表述保留原内容的 in_place_downgrade；最后一种必须给出新版文字的位置。按内容比对，不按位置比对。

不得改写正文或重做出处检索。最终只输出符合 output_schema 的单个 JSON 对象。"""
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "blocks": _block_payload(plan.blocks, snapshot),
                    "claims": list(claims),
                    "previous_failed_claims": [
                        {key: value for key, value in item.items() if key != "finding_id"}
                        for item in _prior_failure_specs(previous, previous_review)
                    ],
                    "output_schema": AttributionSummary.model_json_schema(),
                },
                ensure_ascii=False,
            ),
        },
    ]


class _SkippedClaim(ValueError):
    """A single claim that cannot be trusted; dropped and recorded, never fatal."""


@dataclass
class AcceptedClaim:
    """A batch claim that passed validation, with its refs resolved to real ids.

    Refs are the model's vocabulary and UUIDs are storage's; resolution happens once,
    here, so nothing downstream has to know that the model never saw a UUID.
    """

    claim_ref: str
    block_id: str
    status: str
    candidate_refs: list[str]
    excerpt_ids: list[UUID]
    assertion_ids: list[UUID]
    premise_claim_refs: list[str]
    known_conflict_keys: list[str]
    reason: str | None
    audit_note: str | None


def prefix_batch_claim_refs(
    batch_index: int, output: AttributionBatchVerification
) -> AttributionBatchVerification:
    """Give each batch a stable global claim_ref so later batches can cite it."""

    mapping = {
        item.claim_ref: item.claim_ref
        if item.claim_ref.startswith(f"b{batch_index}_")
        else f"b{batch_index}_{item.claim_ref}"
        for item in output.claims
    }
    rewritten: list[AttributionBatchClaim] = []
    for item in output.claims:
        premises: list[str] = []
        for ref in item.premise_claim_refs:
            premises.append(mapping.get(ref, ref))
        rewritten.append(
            item.model_copy(
                update={"claim_ref": mapping[item.claim_ref], "premise_claim_refs": premises}
            )
        )
    return output.model_copy(update={"claims": rewritten})


def _accept_claims(
    output: AttributionBatchVerification,
    blocks: Sequence[MarkdownBlock],
    snapshot: WriterSnapshot,
    candidates: Sequence[dict[str, Any]],
    raw: object,
) -> tuple[list[tuple[AcceptedClaim, ClaimSpan]], list[dict[str, Any]]]:
    by_id = {block.block_id: block for block in blocks}
    candidate_by_ref = {item["candidate_ref"]: item for item in candidates}
    names = entity_names(snapshot)
    marker_cache = {
        block.block_id: scan_markers(block.text, names, block_kind=block.kind) for block in blocks
    }
    refs = EvidenceRefs(snapshot)
    excerpt_ids_by_assertion = {
        card.assertion_id: {excerpt.excerpt_id for excerpt in card.excerpts}
        for card in snapshot.evidence_cards
    }
    claims: list[tuple[AcceptedClaim, ClaimSpan]] = []
    audit_notes = list(output.audit_notes)
    seen_refs: set[str] = set()

    def note(reason: str, item: AttributionBatchClaim) -> None:
        audit_notes.append(
            {
                "kind": "skipped_claim",
                "block_id": item.block_id,
                "claim_ref": item.claim_ref,
                "reason": reason,
            }
        )

    for item in output.claims:
        unchecked: list[ClaimMarker] = []
        try:
            if item.claim_ref in seen_refs:
                raise _SkippedClaim("duplicate claim_ref")
            block = by_id.get(item.block_id)
            if block is None or item.end_offset > len(block.text):
                raise _SkippedClaim("span references an invalid block or offset")
            text = block.text[item.start_offset : item.end_offset]
            markers = [
                marker
                for marker in marker_cache[item.block_id]
                if item.start_offset <= marker.start_offset < item.end_offset
            ]
            claim = ClaimSpan(
                claim_id=uuid4(),
                block_id=item.block_id,
                start_offset=item.start_offset,
                end_offset=item.end_offset,
                text=text,
                text_hash=text_hash(text),
                markers=markers,
            )
            validate_claim_span(claim, by_id)
            if len(item.candidate_refs) != len(set(item.candidate_refs)):
                raise _SkippedClaim("candidate_refs must be unique")
            if any(
                len(values) != len(set(values))
                for values in (
                    item.excerpt_refs,
                    item.assertion_refs,
                    item.premise_claim_refs,
                    item.known_conflict_refs,
                )
            ):
                raise _SkippedClaim("claim references must be unique")
            for ref in item.candidate_refs:
                candidate = candidate_by_ref.get(ref)
                if candidate is None:
                    raise _SkippedClaim("claim references an unknown candidate")
                if candidate["block_id"] != item.block_id or not (
                    candidate["start_offset"] <= item.start_offset
                    and item.end_offset <= candidate["end_offset"]
                ):
                    raise _SkippedClaim("claim does not stay inside its candidate")
            if item.status in {"verified", "failed"} and not item.candidate_refs:
                raise _SkippedClaim("evidence claim must reference a candidate")
            if item.status == "analysis":
                unchecked = [marker for marker in markers if marker.family == "retrieval"]
            if refs.unknown(item.assertion_refs) or refs.unknown(item.excerpt_refs, excerpts=True):
                raise _SkippedClaim("attribution used unavailable evidence")
            excerpt_ids = refs.excerpts(item.excerpt_refs)
            assertion_ids = refs.assertions(item.assertion_refs)
            known_conflict_keys = refs.conflicts(item.known_conflict_refs)
            if item.status == "verified":
                if not excerpt_ids or not assertion_ids:
                    raise _SkippedClaim("verified claim requires Assertion and Excerpt evidence")
                selected_excerpts = {
                    excerpt_id
                    for assertion_id in assertion_ids
                    for excerpt_id in excerpt_ids_by_assertion[assertion_id]
                }
                if set(excerpt_ids) - selected_excerpts:
                    raise _SkippedClaim("verified Excerpt does not belong to its Assertion")
            if item.status == "failed" and not item.reason:
                raise _SkippedClaim("failed attribution carries no actionable reason")
            if item.status == "analysis" and excerpt_ids:
                raise _SkippedClaim("analysis links use Claims or Assertions, not Excerpt verdicts")
        except (_SkippedClaim, ValidationError, ValueError) as exc:
            note(str(exc), item)
            continue
        seen_refs.add(item.claim_ref)
        claims.append(
            (
                AcceptedClaim(
                    claim_ref=item.claim_ref,
                    block_id=item.block_id,
                    status=item.status,
                    candidate_refs=list(item.candidate_refs),
                    excerpt_ids=excerpt_ids,
                    assertion_ids=assertion_ids,
                    premise_claim_refs=list(item.premise_claim_refs),
                    known_conflict_keys=known_conflict_keys,
                    reason=item.reason,
                    audit_note=item.audit_note,
                ),
                claim,
            )
        )
        if unchecked:
            # The marker lexicon is a surface scan: it cannot tell a source quotation from
            # a Chinese emphasis quote, a fact anchor from the report's own topic year, or
            # a quantity from a list ordinal.  Its disagreement with the model about what
            # counts as a fact is an observation for the health summary, never a defect in
            # the model's answer -- treating it as one discarded correct verdicts and, at
            # thirteen of fifty-one, killed the Job that produced them.
            audit_notes.append(
                {
                    "kind": "unchecked_marker_in_analysis",
                    "block_id": item.block_id,
                    "claim_ref": item.claim_ref,
                    "markers": [{"kind": marker.kind, "text": marker.text} for marker in unchecked],
                }
            )

    attempted = len(output.claims)
    invalid = attempted - len(claims)
    if attempted and (invalid / attempted > MAX_INVALID_CLAIM_RATIO or not claims):
        raise ClaimAttributionOutputError(
            f"Attribution dropped {invalid}/{attempted} claims as malformed", raw
        )
    return claims, audit_notes


def uncovered_candidates(
    accepted: Sequence[tuple[AcceptedClaim, ClaimSpan]],
    candidates: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Report the candidate spans that came back without any verdict.

    A span the model never answered for is a real hole -- the reader gets prose with no
    statement about whether it was checked -- but it is a hole in one span, not a reason
    to discard the report those spans came from.  It travels as a finding so the verdict
    and the repair loop can weigh it against everything else.
    """
    covered = {ref for item, _claim in accepted for ref in item.candidate_refs}
    return [item for item in candidates if item["candidate_ref"] not in covered]


def _resolve_premises(
    accepted: Sequence[tuple[AcceptedClaim, ClaimSpan]],
    prior_claim_refs: set[str],
    candidate_refs: Collection[str] = (),
) -> list[dict[str, Any]]:
    """Drop premise references that point nowhere, and say which ones were dropped.

    Cross-batch premise refs are copied by the model from a prior-claim index, so an
    invented ref is the same class of slip as a spliced Assertion id.  The claim itself
    still stands on whatever premises did resolve; if none of them do, the grounding
    check downstream is what decides the claim is unsupported.
    """
    known = {item.claim_ref for item, _claim in accepted} | prior_claim_refs | set(candidate_refs)
    notes: list[dict[str, Any]] = []
    for item, _claim in accepted:
        unresolved = [
            ref for ref in item.premise_claim_refs if ref not in known or ref == item.claim_ref
        ]
        if not unresolved:
            continue
        item.premise_claim_refs = [ref for ref in item.premise_claim_refs if ref not in unresolved]
        notes.append(
            {
                "kind": "dropped_premise_refs",
                "claim_ref": item.claim_ref,
                "block_id": item.block_id,
                "premise_claim_refs": unresolved,
            }
        )
    return notes


def _advisory_notes(
    blocks: Sequence[MarkdownBlock], snapshot: WriterSnapshot
) -> list[dict[str, Any]]:
    names = entity_names(snapshot)
    notes: list[dict[str, Any]] = []
    for block in blocks:
        markers = scan_markers(block.text, names, block_kind=block.kind)
        if any(marker.kind == "number" for marker in markers):
            continue
        notes.extend(
            {
                "kind": "advisory_without_quantification",
                "block_id": block.block_id,
                "text": marker.text,
            }
            for marker in markers
            if marker.family == "advisory"
        )
    return notes


def _build_dispositions(
    summary: AttributionSummary,
    previous: AttributionRun | None,
    previous_review: ReportReviewRun | None,
    blocks: Sequence[MarkdownBlock],
    raw: object,
) -> tuple[list[RevisionFailureDisposition], list[AttributionFinding]]:
    by_id = {block.block_id: block for block in blocks}
    findings: list[AttributionFinding] = []
    dispositions: list[RevisionFailureDisposition] = []
    if previous is not None:
        prior_specs = _prior_failure_specs(previous, previous_review)
        prior_by_ref = {
            item["prior_ref"]: next(
                finding
                for finding in previous.blocking_findings
                if str(finding.finding_id) == item["finding_id"]
            )
            for item in prior_specs
        }
        output_refs = [item.prior_ref for item in summary.dispositions]
        if len(output_refs) != len(set(output_refs)) or set(output_refs) != set(prior_by_ref):
            raise ClaimAttributionOutputError(
                "Attribution must account for every prior core failure exactly once", raw
            )
        for item in summary.dispositions:
            prior = prior_by_ref[item.prior_ref]
            dispositions.append(
                RevisionFailureDisposition(
                    prior_finding_id=prior.finding_id,
                    outcome=item.outcome,
                    reason=item.reason,
                )
            )
            if item.outcome == "in_place_downgrade":
                if (
                    item.current_block_id is None
                    or item.current_start_offset is None
                    or item.current_end_offset is None
                ):
                    raise ClaimAttributionOutputError(
                        "in_place_downgrade requires the current text location", raw
                    )
                block = by_id.get(item.current_block_id)
                if block is None or not (
                    0 <= item.current_start_offset < item.current_end_offset <= len(block.text)
                ):
                    raise ClaimAttributionOutputError(
                        "in_place_downgrade references invalid current text", raw
                    )
                current_text = block.text[item.current_start_offset : item.current_end_offset]
                findings.append(
                    AttributionFinding(
                        kind="in_place_downgrade",
                        block_id=item.current_block_id,
                        start_offset=item.current_start_offset,
                        end_offset=item.current_end_offset,
                        text=current_text,
                        reason=item.reason,
                    )
                )
    elif summary.dispositions:
        raise ClaimAttributionOutputError("initial attribution cannot contain dispositions", raw)
    return dispositions, findings


def ungrounded_claim_ids(
    accepted: Sequence[tuple[AcceptedClaim, ClaimSpan]],
    premises: Sequence[ClaimPremise],
) -> set[UUID]:
    """Return the analysis claims whose reasoning never reaches checked material.

    This is the check that replaces marker policing.  Analysis is the one verdict a model
    can award itself, so it needs a cost, and the cost is structural rather than
    linguistic: an analysis claim has to name what it rests on, and following those names
    has to arrive at a verified span or at an Assertion.  Marking everything analysis is
    then self-defeating instead of merely unpunished -- nothing bottoms out, so nothing
    stands.  Code can decide this exactly; it never has to judge whether a sentence
    "sounds like" a fact.
    """
    status_by_id = {claim.claim_id: item.status for item, claim in accepted}
    premise_by_id = {item.claim_id: item for item in premises}
    grounded: dict[UUID, bool] = {}

    def walk(claim_id: UUID, seen: frozenset[UUID]) -> bool:
        if claim_id in grounded:
            return grounded[claim_id]
        if claim_id in seen:
            # A premise cycle proves nothing; treat it as reaching no ground rather than
            # recursing forever.
            return False
        if status_by_id.get(claim_id) == "verified":
            grounded[claim_id] = True
            return True
        premise = premise_by_id.get(claim_id)
        if premise is None:
            grounded[claim_id] = False
            return False
        if premise.direct_assertion_ids:
            grounded[claim_id] = True
            return True
        result = any(walk(parent, seen | {claim_id}) for parent in premise.premise_claim_ids)
        # Only cycle-free answers are cached: a False produced inside a cycle depends on
        # the path taken to reach it.
        if claim_id not in seen:
            grounded[claim_id] = result
        return result

    return {
        claim.claim_id
        for item, claim in accepted
        if item.status == "analysis" and not walk(claim.claim_id, frozenset())
    }


def assemble_attribution_run(
    report_id: UUID,
    revision: int,
    accepted: Sequence[tuple[AcceptedClaim, ClaimSpan]],
    audit_notes: Sequence[dict[str, Any]],
    summary: AttributionSummary,
    blocks: Sequence[MarkdownBlock],
    snapshot: WriterSnapshot,
    previous: AttributionRun | None,
    previous_review: ReportReviewRun | None,
    raw: object,
    *,
    attribution_run_id: UUID | None = None,
    uncovered: Sequence[dict[str, Any]] = (),
    carried: CarriedAttribution | None = None,
) -> AttributionRun:
    claim_id_by_ref = {item.claim_ref: claim.claim_id for item, claim in accepted}
    claim_ids_by_candidate: dict[str, list[UUID]] = {}
    for item, claim in accepted:
        for ref in item.candidate_refs:
            claim_ids_by_candidate.setdefault(ref, []).append(claim.claim_id)
    premises: list[ClaimPremise] = []
    evidence: list[ClaimEvidence] = []
    findings: list[AttributionFinding] = []
    claims = [claim for _item, claim in accepted]
    for item, claim in accepted:
        premise_ids: list[UUID] = []
        for ref in item.premise_claim_refs:
            target = claim_id_by_ref.get(ref)
            if target is not None:
                if target != claim.claim_id:
                    premise_ids.append(target)
                continue
            # A whole-report position: everything that ended up covering it.
            premise_ids.extend(
                other for other in claim_ids_by_candidate.get(ref, []) if other != claim.claim_id
            )
        evidence.extend(
            ClaimEvidence(claim_id=claim.claim_id, excerpt_id=value)
            for value in item.excerpt_ids
            if item.status == "verified"
        )
        premises.append(
            ClaimPremise(
                claim_id=claim.claim_id,
                premise_claim_ids=list(dict.fromkeys(premise_ids)),
                direct_assertion_ids=list(dict.fromkeys(item.assertion_ids)),
                known_conflict_keys=list(dict.fromkeys(item.known_conflict_keys)),
                audit_note=item.audit_note,
            )
        )
        if item.status == "failed":
            findings.append(
                AttributionFinding(
                    kind="attribution",
                    claim_id=claim.claim_id,
                    block_id=claim.block_id,
                    start_offset=claim.start_offset,
                    end_offset=claim.end_offset,
                    text=claim.text,
                    reason=cast(str, item.reason),
                )
            )

    if carried is not None:
        premises.extend(carried.claim_premises)
        evidence.extend(carried.claim_evidence)
        findings.extend(carried.blocking_findings)
        claims = [*carried.claims, *claims]
        audit_notes = [*carried.audit_notes, *audit_notes]
    for claim_id in ungrounded_claim_ids(accepted, premises):
        claim = next(item for item in claims if item.claim_id == claim_id)
        findings.append(
            AttributionFinding(
                kind="attribution",
                claim_id=claim_id,
                block_id=claim.block_id,
                start_offset=claim.start_offset,
                end_offset=claim.end_offset,
                text=claim.text,
                reason="这段分析没有落到任何已核对的事实或材料上：它声明的依据要么为空，要么本身也没有依据。",
            )
        )
    for candidate in uncovered:
        findings.append(
            AttributionFinding(
                kind="attribution",
                block_id=candidate["block_id"],
                start_offset=candidate["start_offset"],
                end_offset=candidate["end_offset"],
                text=candidate["text"],
                reason="这处正文没有得到任何核对结论。",
            )
        )
    dispositions, downgrades = _build_dispositions(summary, previous, previous_review, blocks, raw)
    findings.extend(downgrades)
    notes = [*audit_notes, *_advisory_notes(blocks, snapshot), *summary.audit_notes]
    assessed_blocks = {claim.block_id for claim in claims}
    assessments = [
        BlockAssessment(
            block_id=block.block_id,
            status="assessed" if block.block_id in assessed_blocks else "no_claims",
        )
        for block in blocks
    ]
    return AttributionRun(
        attribution_run_id=attribution_run_id or uuid4(),
        report_id=report_id,
        revision=revision,
        block_assessments=assessments,
        claims=claims,
        claim_evidence=evidence,
        claim_premises=premises,
        blocking_findings=findings,
        dispositions=dispositions,
        audit_notes=notes,
        marker_lexicon_version=MARKER_LEXICON_VERSION,
        raw_output=raw,
    )


def build_attribution_run(
    report_id: UUID,
    revision: int,
    output: AttributionBatchVerification,
    blocks: list[MarkdownBlock],
    snapshot: WriterSnapshot,
    previous: AttributionRun | None,
    previous_review: ReportReviewRun | None,
    raw: str,
    *,
    summary: AttributionSummary | None = None,
) -> AttributionRun:
    """Assemble a complete run from one already-merged verification payload.

    Unit tests use this helper; the live path validates each batch first, then
    assembles after the global summary.
    """
    candidates = candidate_specs(blocks, snapshot)
    accepted, notes = _accept_claims(output, blocks, snapshot, candidates, raw)
    notes.extend(_resolve_premises(accepted, set()))
    return assemble_attribution_run(
        report_id,
        revision,
        accepted,
        notes,
        summary or AttributionSummary(),
        blocks,
        snapshot,
        previous,
        previous_review,
        raw,
        uncovered=uncovered_candidates(accepted, candidates),
    )


def _prior_claim_index(
    accepted: Sequence[tuple[AcceptedClaim, ClaimSpan]],
) -> list[dict[str, Any]]:
    return [
        {
            "claim_ref": item.claim_ref,
            "block_id": claim.block_id,
            "text": claim.text,
            "status": item.status,
        }
        for item, claim in accepted
    ]


def _filter_selection(
    selection: AttributionBatchSelection, snapshot: WriterSnapshot
) -> tuple[AttributionBatchSelection, list[dict[str, Any]]]:
    """Drop Assertion ids the model transcribed wrong, and record that it happened.

    Selection asks the model to copy dozens of UUIDs verbatim, so an occasional spliced
    id is a transcription slip, not a broken answer -- the observed shape is the head of
    one real id joined to the tail of another.  Killing a Job over one of seventy ids
    discards a finished report to punish a typo; the surviving ids still describe what
    the model wanted.
    """
    refs = EvidenceRefs(snapshot)
    unknown = refs.unknown(selection.assertion_refs)
    if not unknown:
        return selection, []
    kept = [ref for ref in selection.assertion_refs if ref not in unknown]
    note = {
        "kind": "dropped_selection_ids",
        "assertion_refs": sorted(unknown),
        "reason": "selected catalog references do not exist in this Job",
    }
    return selection.model_copy(update={"assertion_refs": kept}), [note]


def _run_one_batch(
    model: ClaimAttributionModel,
    plan: AttributionPlan,
    spec: AttributionBatchSpec,
    snapshot: WriterSnapshot,
    store: AttributionPersistence | None,
    run_id: UUID,
) -> tuple[list[tuple[AcceptedClaim, ClaimSpan]], list[dict[str, Any]], object]:
    stored = None
    if store is not None:
        existing = {int(row["batch_index"]): row for row in store.list_attribution_batches(run_id)}
        stored = existing.get(spec.batch_index)
        if (
            stored is not None
            and stored.get("status") == "completed"
            and stored.get("verify_result")
        ):
            verification = AttributionBatchVerification.model_validate(stored["verify_result"])
            accepted, notes = _accept_claims(
                verification,
                _batch_blocks(plan, spec),
                snapshot,
                _batch_candidates(plan, spec),
                stored.get("verify_raw"),
            )
            return accepted, notes, stored.get("verify_raw")
    if not spec.candidate_refs:
        verification = AttributionBatchVerification()
        if store is not None:
            store.begin_attribution_batch(
                run_id,
                spec.batch_index,
                block_ids=spec.block_ids,
                candidate_refs=spec.candidate_refs,
                selection_prompt=None,
            )
            store.complete_attribution_batch(
                run_id,
                spec.batch_index,
                raw_output=None,
                result=verification.model_dump(mode="json"),
            )
        return [], [], None

    select_prompt = selection_messages(plan, spec, snapshot)
    selection: AttributionBatchSelection | None = None
    selection_raw: object = None
    selection_notes: list[dict[str, Any]] = []
    verify_prompt: list[dict[str, str]]
    if stored is not None and (
        stored.get("status") == "selected"
        or (stored.get("status") == "failed" and stored.get("selection_result"))
    ):
        selection = AttributionBatchSelection.model_validate(stored["selection_result"])
        selection, selection_notes = _filter_selection(selection, snapshot)
        selection_raw = stored.get("selection_raw")
        stored_prompt = stored.get("verify_prompt")
        if stored_prompt:
            verify_prompt = cast(list[dict[str, str]], stored_prompt)
        else:
            verify_prompt = verification_messages(
                plan,
                spec,
                snapshot,
                expand_selected_sources(
                    snapshot, EvidenceRefs(snapshot).assertions(selection.assertion_refs)
                ),
            )
    else:
        if store is not None:
            store.begin_attribution_batch(
                run_id,
                spec.batch_index,
                block_ids=spec.block_ids,
                candidate_refs=spec.candidate_refs,
                selection_prompt=select_prompt,
            )
        try:
            selection, selection_raw = model.select_materials(select_prompt)
        except ClaimAttributionOutputError as exc:
            if store is not None:
                store.fail_attribution_batch(
                    run_id, spec.batch_index, raw_output=exc.raw_output, error=str(exc)
                )
            raise
        selection, selection_notes = _filter_selection(selection, snapshot)
        sources = expand_selected_sources(
            snapshot, EvidenceRefs(snapshot).assertions(selection.assertion_refs)
        )
        verify_prompt = verification_messages(plan, spec, snapshot, sources)
        if store is not None:
            store.save_attribution_batch_selection(
                run_id,
                spec.batch_index,
                raw_output=selection_raw,
                result=selection.model_dump(mode="json"),
                verify_prompt=verify_prompt,
            )

    try:
        verification, verify_raw = model.verify_batch(verify_prompt)
        verification = prefix_batch_claim_refs(spec.batch_index, verification)
        raw = {"selection": selection_raw, "verify": verify_raw}
        accepted, notes = _accept_claims(
            verification,
            _batch_blocks(plan, spec),
            snapshot,
            _batch_candidates(plan, spec),
            raw,
        )
        notes = [*selection_notes, *notes]
    except ClaimAttributionOutputError as exc:
        if store is not None:
            store.fail_attribution_batch(
                run_id, spec.batch_index, raw_output=exc.raw_output, error=str(exc)
            )
        raise
    if store is not None:
        store.complete_attribution_batch(
            run_id,
            spec.batch_index,
            raw_output=raw,
            result=verification.model_dump(mode="json"),
        )
    return accepted, notes, raw


def run_attribution(
    model: ClaimAttributionModel,
    report_id: UUID,
    revision: int,
    markdown: str,
    snapshot: WriterSnapshot,
    previous: AttributionRun | None = None,
    previous_review: ReportReviewRun | None = None,
    store: AttributionPersistence | None = None,
    carried: CarriedAttribution | None = None,
) -> ClaimAttributionResult:
    """Run deterministic batching, per-batch retrieval, then a global completeness check."""

    all_blocks = parse_markdown(markdown)
    plan = prepare_attribution_plan(
        markdown,
        snapshot,
        only_blocks=None if carried is None else carried.dirty_block_ids,
    )
    prompt = [{"role": "system", "content": json.dumps(plan.as_dict(), ensure_ascii=False)}]
    run_id = uuid4()
    if store is not None:
        run_id = store.begin_attribution_run(report_id, revision, plan.as_dict())
    accepted: list[tuple[AcceptedClaim, ClaimSpan]] = []
    notes: list[dict[str, Any]] = []
    raw_parts: list[object] = []
    all_candidate_refs = {item["candidate_ref"] for item in plan.candidates}
    try:
        # Batches are independent now that a premise can name a position instead of
        # another batch's verdict, and running them in order was the single largest cost
        # in the pipeline: five batches took 2589s in sequence and 627s at their slowest.
        # Each batch still makes its two calls in order; only the batches overlap.
        results: list[tuple[list[tuple[AcceptedClaim, ClaimSpan]], list[dict[str, Any]], object]]
        if len(plan.batches) <= 1:
            # No changed blocks means no batch calls, not a completed attribution run:
            # prior failures, carried results and persistence still need processing below.
            results = [
                _run_one_batch(model, plan, spec, snapshot, store, run_id) for spec in plan.batches
            ]
        else:
            with ThreadPoolExecutor(max_workers=len(plan.batches)) as pool:
                futures = [
                    pool.submit(_run_one_batch, model, plan, spec, snapshot, store, run_id)
                    for spec in plan.batches
                ]
                # Collected in submission order so the merged run does not depend on which
                # batch happened to finish first.
                results = [future.result() for future in futures]
        for batch_accepted, batch_notes, raw in results:
            raw_parts.append(raw)
            accepted.extend(batch_accepted)
            notes.extend(batch_notes)
        notes.extend(_resolve_premises(accepted, set(), all_candidate_refs))
        uncovered = uncovered_candidates(accepted, plan.candidates)
        summary = AttributionSummary()
        prior_specs = _prior_failure_specs(previous, previous_review)
        if prior_specs:
            summary_prompt = summary_messages(
                plan, snapshot, _prior_claim_index(accepted), previous, previous_review
            )
            stored_summary = None
            if store is not None:
                stored_summary = store.begin_attribution_summary(run_id, summary_prompt)
            if stored_summary and stored_summary.get("summary_result"):
                summary = AttributionSummary.model_validate(stored_summary["summary_result"])
                raw_parts.append(stored_summary.get("summary_raw"))
            else:
                summary, summary_raw = model.summarize(summary_prompt)
                raw_parts.append(summary_raw)
                if store is not None:
                    store.complete_attribution_summary(
                        run_id,
                        raw_output=summary_raw,
                        result=summary.model_dump(mode="json"),
                    )
        run = assemble_attribution_run(
            report_id,
            revision,
            accepted,
            notes,
            summary,
            all_blocks,
            snapshot,
            previous,
            previous_review,
            raw_parts,
            attribution_run_id=run_id,
            uncovered=uncovered,
            carried=carried,
        )
    except ClaimAttributionOutputError as exc:
        if store is not None:
            store.fail_attribution_run(run_id, raw_output=exc.raw_output, error=str(exc))
        raise
    if store is not None:
        store.complete_attribution_run(run)
    return ClaimAttributionResult(
        full_prompt=prompt, raw_output=json.dumps(raw_parts, default=str), run=run
    )


class OpenAIClaimAttribution:
    def __init__(self, client: OpenAI | None = None, model: str | None = None) -> None:
        self.client = client or get_openai_client()
        self.model = model or strong_model()

    def select_materials(
        self, prompt: list[dict[str, str]]
    ) -> tuple[AttributionBatchSelection, str]:
        return self._complete(prompt, AttributionBatchSelection, "invalid Attribution selection")

    def verify_batch(
        self, prompt: list[dict[str, str]]
    ) -> tuple[AttributionBatchVerification, str]:
        return self._complete(prompt, AttributionBatchVerification, "invalid Attribution batch")

    def summarize(self, prompt: list[dict[str, str]]) -> tuple[AttributionSummary, str]:
        return self._complete(prompt, AttributionSummary, "invalid Attribution summary")

    def attribute(
        self,
        report_id: UUID,
        revision: int,
        markdown: str,
        snapshot: WriterSnapshot,
        previous: AttributionRun | None = None,
        previous_review: ReportReviewRun | None = None,
        store: AttributionPersistence | None = None,
    ) -> ClaimAttributionResult:
        return run_attribution(
            self, report_id, revision, markdown, snapshot, previous, previous_review, store
        )

    def _request(self, prompt: list[dict[str, str]]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=cast(Any, prompt),
            temperature=0.0,
            extra_body=thinking_extra_body(self.model),
        )
        return response.choices[0].message.content or ""

    def _complete(
        self, prompt: list[dict[str, str]], schema: type[SchemaT], label: str
    ) -> tuple[SchemaT, str]:
        last: ClaimAttributionOutputError | None = None
        for _ in range(ATTRIBUTION_ATTEMPTS):
            raw = self._request(prompt)
            try:
                return schema.model_validate_json(raw), raw
            except (ValidationError, ValueError) as exc:
                last = ClaimAttributionOutputError(f"{label}: {exc}", raw)
        raise cast(ClaimAttributionOutputError, last)
