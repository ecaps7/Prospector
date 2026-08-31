"""Deterministic Markdown parsing, marker scanning, and span validation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from markdown_it import MarkdownIt

from prospector.schemas.claims import ClaimMarker, ClaimSpan
from prospector.schemas.report import BlockReplacement, MarkdownBlock

MARKER_LEXICON_VERSION = "v6"
# Serialized size of one batch's blocks + candidates.  A later excerpt expansion is
# bounded by the model's selection, so this budget is what actually splits the job.
ATTRIBUTION_BATCH_CHAR_BUDGET = 24_000
ADVISORY_WORDS = ("显著", "大幅", "全面", "根本性", "标志着", "转折点")
SCOPE_WORDS = ("所有", "全部", "多数", "大部分", "普遍", "主流", "无一例外")
_NUMBER_RE = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})*|\d+(?:\.\d+)?)(?:%|％|万|亿|美元|元|倍)?")
# Writers space out Chinese dates ("2024 年 10 月 22 日"), and without tolerating that the
# pattern stops at the year: the date degrades to a bare year and the orphaned month and
# day digits are picked up separately as quantities, so one real date becomes three wrong
# markers.  Every date in the first real report went through this path.
_DATE_RE = re.compile(
    r"(?:19|20)\d{2}\s*(?:年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?|年|[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?"
)
_QUOTE_RE = re.compile(r"[“\"「][^”\"」]+[”\"」]")
# A quotation is a retrieval anchor only when something in the same clause says it is a
# quotation.  Chinese uses the same marks for emphasis, and in the first real report 39
# of 52 quoted spans were the writer's own framing -- "从“会回答”走向“会做事”" -- each of
# which demanded a source for a phrase no source contains.
_ATTRIBUTION_CUE_RE = re.compile(
    r"称|表示|写道|指出|援引|引述|引用|所谓|定义为|据|报道|声明|回应|承认|命名为|叫做|原文|白皮书"
)
# A bare year is the period a sentence is about; a year with a month or a day is a fact
# anchor someone can look up.  Every date in the first report was a bare year under the
# old pattern, so the distinction only became usable once dates parsed correctly.
_FULL_DATE_RE = re.compile(r"^(?:19|20)\d{2}\s*(?:年\s*\d{1,2}|[-/.]\d{1,2})")
_LIST_ORDINAL_RE = re.compile(r"^\s*\d{1,2}\s*[.、)）]")
_HTML_RE = re.compile(r"<[/!]?[A-Za-z][^>]*>")
_FOOTNOTE_RE = re.compile(r"\[\^[^\]]+\]|^\[\^[^\]]+\]:", re.MULTILINE)

# Entity whitelist construction.  A named entity is the one retrieval marker family that
# regex alone cannot recognise, so the list is built from the Job's own material.  The
# rule is deliberately narrow: a missed entity costs one unchecked clause, while a bogus
# entity drags plain analytical prose into source retrieval and taxes exactly the writing
# the report exists to produce.  Chinese has no word boundaries, so entities are admitted
# only where a structural cue proves the boundary -- an organisation suffix or a title
# bracket -- never by slicing a fixed-width window out of running text.
ORG_SUFFIXES = (
    "公司",
    "集团",
    "银行",
    "证券",
    "保险",
    "基金",
    "科技",
    "控股",
    "实业",
    "资本",
    "研究院",
    "研究所",
    "实验室",
    "大学",
    "学院",
    "医院",
    "出版社",
    "通讯社",
    "协会",
    "学会",
    "委员会",
    "交易所",
    "基金会",
    "联盟",
    "工作组",
    "中心",
    "部",
    "局",
    "署",
    "厅",
    "办公室",
)
_ORG_RE = re.compile(r"[一-鿿]{2,6}(?:" + "|".join(ORG_SUFFIXES) + r")")
_TITLE_RE = re.compile(r"[《〈]([^》〉\n]{2,40})[》〉]")
_LATIN_ENTITY_RE = re.compile(r"[A-Z][A-Za-z0-9.+&-]{1,}")
_LATIN_STOPWORDS = frozenset(
    {
        "The",
        "This",
        "That",
        "These",
        "Those",
        "And",
        "But",
        "For",
        "With",
        "From",
        "However",
        "In",
        "On",
        "At",
        "As",
        "It",
        "Its",
        "Their",
        "Our",
        "We",
        "You",
    }
)
# Function words that cannot occur inside a real organisation name; their presence proves
# the regex swallowed surrounding prose rather than an entity.
_STOP_CHARS = frozenset(
    "的是和与在了等或该其这那最多大小为对从被把并且但也都很更再又已将会能可不没"
    "有个些之而于以及所它他她们就还只即则因此由向到过着当同新旧上下前后内外"
)
MAX_ENTITY_NAMES = 400


def text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class MarkdownContractError(ValueError):
    pass


def build_entity_whitelist(texts: Iterable[str]) -> set[str]:
    """Collect only entities whose boundary is proven by a structural cue."""
    names: set[str] = set()
    for value in texts:
        for match in _LATIN_ENTITY_RE.finditer(value):
            token = match.group()
            if token not in _LATIN_STOPWORDS:
                names.add(token)
        for match in _TITLE_RE.finditer(value):
            names.add(match.group(1).strip())
        for match in _ORG_RE.finditer(value):
            token = match.group()
            if not any(char in _STOP_CHARS for char in token):
                names.add(token)
    names = {name for name in names if len(name.strip()) >= 2}
    if len(names) <= MAX_ENTITY_NAMES:
        return names
    # Ties on length are broken by the name itself.  Sorting a set by length alone left
    # the cut to set iteration order, which varies with the interpreter's hash seed: the
    # same report produced 185 to 190 candidates across processes, and 52 candidate refs
    # pointed at different sentences depending on which process computed the plan.  For a
    # module whose whole contract is determinism that is not a tuning detail.
    return set(sorted(names, key=lambda name: (-len(name), name))[:MAX_ENTITY_NAMES])


def parse_markdown(markdown: str) -> list[MarkdownBlock]:
    """Parse visible GFM text into stable blocks and reject unsafe writer output."""
    if not markdown.strip():
        raise MarkdownContractError("Markdown must not be blank")
    if _HTML_RE.search(markdown):
        raise MarkdownContractError("raw HTML is not allowed in report Markdown")
    if _FOOTNOTE_RE.search(markdown):
        raise MarkdownContractError("Writer must not generate citation footnotes")
    parser = MarkdownIt("commonmark").enable("table")
    tokens = parser.parse(markdown)
    blocks: list[MarkdownBlock] = []
    pending_kind: str | None = None
    table_cell = False
    # Blocks are located with a forward-only cursor.  Short block texts repeat constantly
    # in tables ("是", "未披露"), so a document-wide search would resolve a later block to
    # an earlier occurrence and splice its citation into unrelated prose.
    cursor = 0
    for token in tokens:
        if token.type == "heading_open":
            pending_kind = "heading"
        elif token.type == "paragraph_open":
            pending_kind = "paragraph"
        elif token.type == "list_item_open":
            pending_kind = "list_item"
        elif token.type == "blockquote_open":
            pending_kind = "blockquote"
        elif token.type in {"th_open", "td_open"}:
            table_cell = True
        elif token.type in {"th_close", "td_close"}:
            table_cell = False
        elif token.type == "inline":
            value = token.content.strip()
            if not value:
                continue
            kind = "table_cell" if table_cell else pending_kind
            if kind is None:
                continue
            start = markdown.find(value, cursor)
            if start >= 0:
                cursor = start + len(value)
            blocks.append(
                MarkdownBlock(
                    block_id=f"b_{len(blocks) + 1:04d}",
                    kind=kind,
                    text=value,
                    text_hash=text_hash(value),
                    source_start=start,
                    source_end=start + len(value) if start >= 0 else -1,
                )
            )
            pending_kind = None
    if not blocks:
        raise MarkdownContractError("Markdown has no visible text blocks")
    return blocks


@dataclass(frozen=True, slots=True)
class AppliedPatch:
    """The result of splicing a revision patch into a frozen report."""

    markdown: str
    # Character ranges in ``markdown`` that hold newly written text.  Everything outside
    # them is byte-identical to the previous revision, which is what lets the next
    # attribution round inherit verdicts instead of re-earning them.
    new_regions: tuple[tuple[int, int], ...]
    rejected: tuple[dict[str, Any], ...]


def _line_bounds(markdown: str, start: int, end: int) -> tuple[int, int]:
    """Widen a visible-text span to the whole lines that contain it.

    Block offsets cover inline text only, so a heading's ``## `` and a list item's ``- ``
    sit outside them.  Replacing the inline span alone would leave the old marker in
    front of new prose that brings its own.
    """
    line_start = markdown.rfind("\n", 0, start) + 1
    line_end = markdown.find("\n", end)
    return line_start, len(markdown) if line_end < 0 else line_end


def apply_block_replacements(
    markdown: str,
    blocks: Sequence[MarkdownBlock],
    replacements: Sequence[BlockReplacement],
) -> AppliedPatch:
    """Splice whole-block ranges into *markdown* and report what was actually rewritten.

    Revision is expressed as replacements over block ranges rather than as a fresh copy
    of the whole report.  Two things follow that a full rewrite cannot offer: a block
    nobody named is byte-identical afterwards, so its verdicts stay earned; and the diff
    between revisions is the patch itself rather than something a reader has to
    reconstruct.  A replacement the code cannot apply safely is rejected and reported --
    never applied approximately.
    """
    index = {block.block_id: position for position, block in enumerate(blocks)}
    rejected: list[dict[str, Any]] = []
    spans: list[tuple[int, int, str, BlockReplacement]] = []
    for item in replacements:
        start_at = index.get(item.start_block_id)
        end_at = index.get(item.end_block_id)
        if start_at is None or end_at is None or end_at < start_at:
            rejected.append({"replacement": item.model_dump(mode="json"), "reason": "块编号无效"})
            continue
        covered = blocks[start_at : end_at + 1]
        if any(block.source_start < 0 for block in covered):
            rejected.append(
                {"replacement": item.model_dump(mode="json"), "reason": "这些块无法在原文中定位"}
            )
            continue
        line_start, line_end = _line_bounds(
            markdown, covered[0].source_start, covered[-1].source_end
        )
        # A table row holds several blocks on one line.  Widening to whole lines must not
        # swallow a block outside the requested range, so the replacement is refused
        # rather than silently deleting a neighbouring cell.
        intruders = [
            block.block_id
            for position, block in enumerate(blocks)
            if not (start_at <= position <= end_at)
            and block.source_start >= 0
            and block.source_start < line_end
            and block.source_end > line_start
        ]
        if intruders:
            rejected.append(
                {
                    "replacement": item.model_dump(mode="json"),
                    "reason": f"这段范围与未点名的块共用行：{intruders}",
                }
            )
            continue
        text = item.markdown.strip()
        if not text:
            # A deletion also takes the blank line that separated the block, or the
            # document is left with a widening gap every time a passage is removed.
            while line_end < len(markdown) and markdown[line_end] == "\n":
                line_end += 1
                if line_end < len(markdown) and markdown[line_end] != "\n":
                    break
        spans.append((line_start, line_end, text, item))
    spans.sort(key=lambda entry: entry[0])
    for earlier, later in zip(spans, spans[1:], strict=False):
        if later[0] < earlier[1]:
            rejected.append(
                {"replacement": later[3].model_dump(mode="json"), "reason": "替换范围互相重叠"}
            )
    accepted = [
        entry
        for position, entry in enumerate(spans)
        if not (position and entry[0] < spans[position - 1][1])
    ]
    result = markdown
    new_regions: list[tuple[int, int]] = []
    # Applied last-first so an earlier span's offsets stay valid while later ones move.
    for line_start, line_end, replacement, _item in reversed(accepted):
        result = result[:line_start] + replacement + result[line_end:]
    # Offsets are recomputed forward once the text is final; a deletion shifts everything
    # after it, so the regions cannot be collected during the reverse pass.
    shift = 0
    for line_start, line_end, replacement, _item in accepted:
        start = line_start + shift
        new_regions.append((start, start + len(replacement)))
        shift += len(replacement) - (line_end - line_start)
    return AppliedPatch(
        markdown=result,
        new_regions=tuple(item for item in new_regions if item[1] > item[0]),
        rejected=tuple(rejected),
    )


def _clause_around(text: str, at: int) -> str:
    left = max(text.rfind(stop, 0, at) for stop in "。！？；;\n") + 1
    right = [text.find(stop, at) for stop in "。！？；;\n"]
    return text[left : min((value for value in right if value >= 0), default=len(text))]


def scan_markers(
    text: str,
    entity_names: Iterable[str] = (),
    *,
    block_kind: str | None = None,
) -> list[ClaimMarker]:
    """Classify surface markers only; this function never judges a claim's content.

    A marker in the retrieval family obliges the model to produce an evidence verdict for
    the span it sits in, so the family assignment decides how much of a report has to be
    defended fact by fact.  Three surface forms were demoted after they were measured on
    a real report: a heading is a label the section restates, a bare year is the period
    under discussion, and a Chinese quotation mark is usually emphasis.  Demoted markers
    still produce a candidate span -- the model may still bind evidence to them -- they
    simply no longer forbid the answer "this is analysis".
    """
    hits: list[ClaimMarker] = []

    def add(
        family: Literal["retrieval", "candidate", "advisory"],
        kind: str,
        match: re.Match[str],
    ) -> None:
        hits.append(
            ClaimMarker(
                family=family,
                kind=kind,
                text=match.group(),
                start_offset=match.start(),
                end_offset=match.end(),
            )
        )

    # A heading carries no statement of its own: the section below restates its content,
    # where it is checked in prose that can actually be sourced.
    anchors: Literal["retrieval", "candidate"] = (
        "candidate" if block_kind == "heading" else "retrieval"
    )
    date_spans: list[tuple[int, int]] = []
    for match in _DATE_RE.finditer(text):
        family = anchors if _FULL_DATE_RE.match(match.group()) else "candidate"
        add(family, "date", match)
        date_spans.append((match.start(), match.end()))
    for match in _NUMBER_RE.finditer(text):
        # A year already captured as a date is not a separate quantity.
        if any(start < match.end() and match.start() < end for start, end in date_spans):
            continue
        # A leading "1." is Markdown's list numbering, not a quantity in the prose.
        family = anchors
        if match.start() <= 2 and _LIST_ORDINAL_RE.match(text[: match.end() + 2]):
            family = "candidate"
        add(family, "number", match)
    for match in _QUOTE_RE.finditer(text):
        cued = _ATTRIBUTION_CUE_RE.search(_clause_around(text, match.start()))
        add(anchors if cued else "candidate", "quote", match)
    for word in SCOPE_WORDS:
        for match in re.finditer(re.escape(word), text):
            add(anchors, "scope", match)
    for word in ADVISORY_WORDS:
        for match in re.finditer(re.escape(word), text):
            add("advisory", "advisory", match)
    for name in sorted(
        {name.strip() for name in entity_names if len(name.strip()) >= 2}, key=len, reverse=True
    ):
        for match in re.finditer(re.escape(name), text, flags=re.IGNORECASE):
            # A named subject may introduce a retrievable fact, but its presence alone
            # must not drag the surrounding interpretation into evidence matching.
            add("candidate", "named_entity", match)
    dedup = {(hit.family, hit.kind, hit.start_offset, hit.end_offset): hit for hit in hits}
    return sorted(dedup.values(), key=lambda item: (item.start_offset, item.end_offset, item.kind))


def retrieval_candidate_spans(
    block: MarkdownBlock, markers: list[ClaimMarker]
) -> list[tuple[int, int]]:
    """Return clause-local candidates around deterministic retrieval marker positions."""
    retrieval = [marker for marker in markers if marker.family in {"retrieval", "candidate"}]
    spans: list[tuple[int, int]] = []
    for marker in retrieval:
        left = max(block.text.rfind(stop, 0, marker.start_offset) for stop in "。！？；;\n") + 1
        right_candidates = [block.text.find(stop, marker.end_offset) for stop in "。！？；;\n"]
        right = min((value for value in right_candidates if value >= 0), default=len(block.text))
        span = (left, right)
        if span not in spans:
            spans.append(span)
    return spans


@dataclass(frozen=True, slots=True)
class AttributionBatchSpec:
    """A consecutive-block slice whose serialized size fits the attribution budget."""

    batch_index: int
    block_ids: tuple[str, ...]
    candidate_refs: tuple[str, ...]


def batch_payload(
    blocks: Sequence[MarkdownBlock], candidates: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """The block/candidate fragment whose serialized size determines batch boundaries."""

    return {
        "blocks": [{"block_id": block.block_id, "text": block.text} for block in blocks],
        "candidates": [
            {
                "candidate_ref": item["candidate_ref"],
                "block_id": item["block_id"],
                "text": item["text"],
                "start_offset": item["start_offset"],
                "end_offset": item["end_offset"],
                "requires_evidence": item["requires_evidence"],
                "markers": item["markers"],
            }
            for item in candidates
        ],
    }


def serialized_batch_size(
    blocks: Sequence[MarkdownBlock], candidates: Sequence[dict[str, Any]]
) -> int:
    return len(json.dumps(batch_payload(blocks, candidates), ensure_ascii=False))


def partition_attribution_batches(
    blocks: Sequence[MarkdownBlock],
    candidates: Sequence[dict[str, Any]],
    *,
    char_budget: int = ATTRIBUTION_BATCH_CHAR_BUDGET,
) -> list[AttributionBatchSpec]:
    """Split consecutive Markdown blocks so each batch stays within the character budget.

    A single block that already exceeds the budget still becomes its own batch; the
    model never chooses where a batch starts or ends.
    """
    if char_budget < 1:
        raise ValueError("attribution batch character budget must be positive")
    by_block: dict[str, list[dict[str, Any]]] = {block.block_id: [] for block in blocks}
    for item in candidates:
        by_block[item["block_id"]].append(item)
    batches: list[AttributionBatchSpec] = []
    current_blocks: list[MarkdownBlock] = []
    current_candidates: list[dict[str, Any]] = []
    for block in blocks:
        next_blocks = [*current_blocks, block]
        next_candidates = [*current_candidates, *by_block[block.block_id]]
        if current_blocks and serialized_batch_size(next_blocks, next_candidates) > char_budget:
            batches.append(
                AttributionBatchSpec(
                    batch_index=len(batches),
                    block_ids=tuple(item.block_id for item in current_blocks),
                    candidate_refs=tuple(item["candidate_ref"] for item in current_candidates),
                )
            )
            current_blocks = [block]
            current_candidates = list(by_block[block.block_id])
        else:
            current_blocks = next_blocks
            current_candidates = next_candidates
    if current_blocks:
        batches.append(
            AttributionBatchSpec(
                batch_index=len(batches),
                block_ids=tuple(item.block_id for item in current_blocks),
                candidate_refs=tuple(item["candidate_ref"] for item in current_candidates),
            )
        )
    return batches


def validate_claim_span(claim: ClaimSpan, blocks: dict[str, MarkdownBlock]) -> None:
    block = blocks.get(claim.block_id)
    if block is None:
        raise MarkdownContractError(f"unknown block_id {claim.block_id}")
    if claim.end_offset > len(block.text):
        raise MarkdownContractError("claim span exceeds visible block text")
    actual = block.text[claim.start_offset : claim.end_offset]
    if actual != claim.text or text_hash(actual) != claim.text_hash:
        raise MarkdownContractError("claim text or hash does not match frozen revision")
    retrieval = [marker for marker in claim.markers if marker.family == "retrieval"]
    if retrieval and not any(
        claim.start_offset <= marker.start_offset < claim.end_offset for marker in retrieval
    ):
        raise MarkdownContractError("retrieval claim span must cover a scanned retrieval marker")
