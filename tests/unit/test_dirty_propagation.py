"""Unit tests for statement dirty-set propagation."""

from __future__ import annotations

from uuid import UUID

from prospector.deterministic.dirty_propagation import (
    can_revise_again,
    changed_statement_ids,
    dirty_statement_ids,
)
from prospector.schemas.claims import MAX_REPORT_REVISION_ROUNDS
from prospector.schemas.report import ReportDraft

EXCERPT = UUID("10000000-0000-0000-0000-000000000001")


def _draft(*, fact_text: str = "事实句", analysis_text: str = "推理句") -> ReportDraft:
    return ReportDraft.model_validate(
        {
            "title": "t",
            "introduction": [
                {
                    "paragraph_id": "p_intro",
                    "statements": [
                        {
                            "statement_id": "s_intro",
                            "text": "引言",
                            "kind": "elaboration",
                            "candidate_excerpt_ids": [],
                            "premise_statement_ids": [],
                        }
                    ],
                }
            ],
            "sections": [
                {
                    "section_id": "sec_1",
                    "title": "一",
                    "paragraphs": [
                        {
                            "paragraph_id": "p_1",
                            "statements": [
                                {
                                    "statement_id": "s_fact",
                                    "text": fact_text,
                                    "kind": "evidence",
                                    "candidate_excerpt_ids": [str(EXCERPT)],
                                    "premise_statement_ids": [],
                                },
                                {
                                    "statement_id": "s_bridge",
                                    "text": "过渡",
                                    "kind": "elaboration",
                                    "candidate_excerpt_ids": [],
                                    "premise_statement_ids": [],
                                },
                                {
                                    "statement_id": "s_analysis",
                                    "text": analysis_text,
                                    "kind": "derived",
                                    "candidate_excerpt_ids": [],
                                    "premise_statement_ids": ["s_fact"],
                                },
                            ],
                        }
                    ],
                }
            ],
            "conclusion": [
                {
                    "paragraph_id": "p_c",
                    "statements": [
                        {
                            "statement_id": "s_conclusion",
                            "text": "结论",
                            "kind": "derived",
                            "candidate_excerpt_ids": [],
                            "premise_statement_ids": ["s_analysis"],
                        }
                    ],
                }
            ],
        }
    )


def test_first_verify_marks_all_dirty() -> None:
    draft = _draft()
    # s_intro is in the set: introduction sentences are verified like any other.
    assert dirty_statement_ids(draft) == {
        "s_intro",
        "s_fact",
        "s_bridge",
        "s_analysis",
        "s_conclusion",
    }


def test_changing_premise_dirties_dependents() -> None:
    before = _draft()
    after = _draft(fact_text="改写后的事实句")
    changed = changed_statement_ids(before, after)
    assert changed == {"s_fact"}
    dirty = dirty_statement_ids(
        after,
        changed_ids=changed,
        previous_clean_ids={"s_intro", "s_fact", "s_bridge", "s_analysis", "s_conclusion"},
    )
    assert dirty == {"s_fact", "s_analysis", "s_conclusion"}
    assert "s_bridge" not in dirty


def test_unchanged_clean_statements_stay_clean() -> None:
    draft = _draft()
    dirty = dirty_statement_ids(
        draft,
        changed_ids=set(),
        previous_clean_ids={"s_intro", "s_fact", "s_bridge", "s_analysis", "s_conclusion"},
    )
    assert dirty == set()


def test_can_revise_again_respects_cap() -> None:
    assert can_revise_again(1) is True
    assert can_revise_again(MAX_REPORT_REVISION_ROUNDS) is True
    assert can_revise_again(MAX_REPORT_REVISION_ROUNDS + 1) is False
