"""Deterministic dirty-set propagation along derived premise edges."""

from __future__ import annotations

from prospector.schemas.claims import MAX_REPORT_REVISION_ROUNDS
from prospector.schemas.report import ReportDraft, ReportStatement

__all__ = [
    "MAX_REPORT_REVISION_ROUNDS",
    "changed_statement_ids",
    "dirty_statement_ids",
    "can_revise_again",
]


def changed_statement_ids(before: ReportDraft, after: ReportDraft) -> set[str]:
    """Statements whose text, kind, or reference sets differ between two drafts."""
    before_map = {statement.statement_id: statement for statement in before.statements()}
    after_map = {statement.statement_id: statement for statement in after.statements()}
    changed: set[str] = set()
    for statement_id in set(before_map) | set(after_map):
        left = before_map.get(statement_id)
        right = after_map.get(statement_id)
        if left is None or right is None or _statement_signature(left) != _statement_signature(
            right
        ):
            changed.add(statement_id)
    return changed


def dirty_statement_ids(
    draft: ReportDraft,
    *,
    changed_ids: set[str] | None = None,
    previous_clean_ids: set[str] | None = None,
) -> set[str]:
    """Return statement ids that must be re-verified.

    A statement is dirty when:
    - it appears in ``changed_ids``, or
    - it is new / absent from ``previous_clean_ids``, or
    - any of its declared premises is dirty (propagated along premise edges).

    When ``changed_ids`` is None and ``previous_clean_ids`` is None, every
    statement is dirty (first verification of a revision).
    """
    statements = draft.statements()
    all_ids = {statement.statement_id for statement in statements}
    if changed_ids is None and previous_clean_ids is None:
        return set(all_ids)

    dirty = set(changed_ids or ())
    clean = set(previous_clean_ids or ())
    for statement_id in all_ids:
        if statement_id not in clean:
            dirty.add(statement_id)

    premises = {
        statement.statement_id: list(statement.premise_statement_ids)
        for statement in statements
    }
    changed = True
    while changed:
        changed = False
        for statement_id, premise_ids in premises.items():
            if statement_id in dirty:
                continue
            if any(premise_id in dirty for premise_id in premise_ids):
                dirty.add(statement_id)
                changed = True
    return dirty & all_ids


def can_revise_again(current_revision: int) -> bool:
    """True when Writer may still produce another revision after this verify."""
    # revision 1 = initial draft; after verify, Writer may revise while
    # current_revision <= MAX_REPORT_REVISION_ROUNDS (two revises → rev 3).
    return current_revision <= MAX_REPORT_REVISION_ROUNDS


def _statement_signature(statement: ReportStatement) -> tuple[object, ...]:
    return (
        statement.kind,
        statement.text,
        tuple(str(value) for value in statement.candidate_excerpt_ids),
        tuple(statement.premise_statement_ids),
    )
