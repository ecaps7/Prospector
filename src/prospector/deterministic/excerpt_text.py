"""Deterministic clipping of Excerpt bodies for prompt budgets.

Excerpt text is the auditable record, so it is never rewritten or summarized: the only
transformation allowed here is dropping a marked middle span. Head and tail are kept
because Exa highlights put the load-bearing sentence at one end or the other, and a
clipped excerpt still has to be recognizable as the same passage a reader can look up.
"""

from __future__ import annotations

CLIP_MARKER = "\n……（原文中段略去）……\n"

# Per-Excerpt budgets, kept together because they trade against each other in the same
# Job. The Writer sees every Excerpt once and pays the total across a long multi-turn
# conversation, so its budget is the tighter one per passage; the Verifier sees only the
# handful behind one statement's premises and can afford less clipping per passage.
WRITER_EXCERPT_CHAR_LIMIT = 1500
WRITER_EXCERPT_MIN_CHARS = 400
WRITER_EXCERPT_TOTAL_CHAR_BUDGET = 160_000
PREMISE_EXCERPT_CHAR_LIMIT = 900


def writer_excerpt_limit(excerpt_count: int) -> int:
    """Per-Excerpt budget for a Job holding *excerpt_count* distinct passages.

    Evidence volume varies by an order of magnitude across effort levels, so a fixed
    per-passage cap either starves a small Job or lets a large one outgrow the context
    window. Splitting a total budget keeps the material block roughly flat instead.

    The floor wins over the total for very large Jobs: a share too small to carry a
    usable passage would defeat the point of sending Excerpt text at all, so past
    roughly ``WRITER_EXCERPT_TOTAL_CHAR_BUDGET / WRITER_EXCERPT_MIN_CHARS`` passages the
    block does grow again.
    """
    if excerpt_count <= 0:
        return WRITER_EXCERPT_CHAR_LIMIT
    share = WRITER_EXCERPT_TOTAL_CHAR_BUDGET // excerpt_count
    return max(WRITER_EXCERPT_MIN_CHARS, min(WRITER_EXCERPT_CHAR_LIMIT, share))


def clip_excerpt_text(text: str, limit: int) -> str:
    """Return *text* shortened to at most *limit* characters, marking what was dropped.

    The marker counts against the budget, so a caller can size a prompt from *limit*
    alone. A limit too small to hold the marker degrades to a plain head cut.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return text
    keep = limit - len(CLIP_MARKER)
    if keep <= 0:
        return text[:limit]
    head = keep - keep // 3
    tail = keep - head
    return text[:head] + CLIP_MARKER + (text[-tail:] if tail else "")
