"""Stable paragraph segmentation retaining exact character spans."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Paragraph:
    para_id: int
    start: int
    end: int
    text: str


_BLOCK_RE = re.compile(r"\S(?:.*?\S)?(?=\n\s*\n|\Z)", re.DOTALL)


def segment_text(text: str) -> list[Paragraph]:
    """Split on blank lines without rewriting source characters."""
    return [
        Paragraph(para_id=index, start=match.start(), end=match.end(), text=match.group(0))
        for index, match in enumerate(_BLOCK_RE.finditer(text), start=1)
    ]


def select_paragraphs(text: str, para_ids: list[int]) -> tuple[str, dict[str, object]]:
    paragraphs = segment_text(text)
    selected_ids = sorted(set(para_ids))
    if not selected_ids:
        raise ValueError("at least one paragraph id is required")
    if selected_ids != list(range(selected_ids[0], selected_ids[-1] + 1)):
        raise ValueError("select_paragraphs requires a contiguous range; use select_excerpts")
    by_id = {paragraph.para_id: paragraph for paragraph in paragraphs}
    missing = [para_id for para_id in selected_ids if para_id not in by_id]
    if missing:
        raise ValueError(f"paragraph ids out of range: {missing}")
    selected = [by_id[para_id] for para_id in selected_ids]
    excerpt = "\n\n".join(paragraph.text for paragraph in selected)
    locator: dict[str, object] = {
        "para_ids": selected_ids,
        "segment_range": [selected_ids[0], selected_ids[-1]],
        "char_spans": [[paragraph.start, paragraph.end] for paragraph in selected],
        "char_span": [selected[0].start, selected[-1].end],
    }
    return excerpt, locator


def select_excerpts(text: str, para_ids: list[int]) -> list[tuple[str, dict[str, object]]]:
    """Return exact source slices, splitting non-contiguous paragraph selections."""
    paragraphs = segment_text(text)
    selected_ids = sorted(set(para_ids))
    if not selected_ids:
        raise ValueError("at least one paragraph id is required")
    by_id = {paragraph.para_id: paragraph for paragraph in paragraphs}
    missing = [para_id for para_id in selected_ids if para_id not in by_id]
    if missing:
        raise ValueError(f"paragraph ids out of range: {missing}")

    groups: list[list[int]] = []
    for para_id in selected_ids:
        if not groups or para_id != groups[-1][-1] + 1:
            groups.append([para_id])
        else:
            groups[-1].append(para_id)

    excerpts: list[tuple[str, dict[str, object]]] = []
    for group in groups:
        first = by_id[group[0]]
        last = by_id[group[-1]]
        excerpt = text[first.start : last.end]
        excerpts.append(
            (
                excerpt,
                {
                    "para_ids": group,
                    "segment_range": [group[0], group[-1]],
                    "char_span": [first.start, last.end],
                },
            )
        )
    return excerpts
