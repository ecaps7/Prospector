"""Deterministic report-structure metrics."""

from __future__ import annotations

from prospector.deterministic.report_structure import measure_report_structure
from prospector.schemas.report import ReportDraft

# Sections, each a list of paragraphs, each a list of statement kinds.
Sections = list[list[list[str]]]


def _statement(statement_id: str, kind: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "statement_id": statement_id,
        "text": "句子",
        "kind": kind,
        "candidate_excerpt_ids": [],
        "premise_statement_ids": [],
    }
    if kind == "evidence":
        payload["candidate_excerpt_ids"] = ["10000000-0000-0000-0000-000000000001"]
    if kind == "derived":
        payload["premise_statement_ids"] = ["s_i1"]
    return payload


def _draft(sections: Sections) -> ReportDraft:
    counter = iter(range(1000))
    return ReportDraft.model_validate(
        {
            "title": "报告",
            "introduction": [
                {"paragraph_id": "p_i", "statements": [_statement("s_i1", "evidence")]}
            ],
            "sections": [
                {
                    "section_id": f"sec_{index}",
                    "title": f"第{index}节",
                    "paragraphs": [
                        {
                            "paragraph_id": f"p_{index}_{position}",
                            "statements": [
                                _statement(f"s_{next(counter)}", kind) for kind in kinds
                            ],
                        }
                        for position, kinds in enumerate(paragraphs)
                    ],
                }
                for index, paragraphs in enumerate(sections)
            ],
            "conclusion": [{"paragraph_id": "p_c", "statements": [_statement("s_c1", "derived")]}],
        }
    )


def test_a_chronicle_and_an_argument_are_told_apart() -> None:
    """The signature of a list is a long undigested run, not just a lopsided ratio."""
    chronicle = measure_report_structure(_draft([[["evidence"] * 9], [["evidence"] * 6]]))

    assert chronicle.longest_evidence_run == 9
    assert chronicle.derived_count == 1  # the conclusion alone
    assert (chronicle.paragraphs_without_derived, chronicle.paragraph_count) == (3, 4)

    argued = measure_report_structure(
        _draft(
            [
                [["evidence", "evidence", "derived"]],
                [["evidence", "derived", "evidence"]],
            ]
        )
    )

    assert argued.longest_evidence_run == 2
    assert argued.derived_count == 3
    assert argued.paragraphs_without_derived == 1  # only the introduction


def test_a_run_breaks_on_any_non_evidence_statement() -> None:
    """A bridge or a limitation interrupts a pile of facts just as a judgement does."""
    measured = measure_report_structure(
        _draft([[["evidence", "evidence", "limitation", "evidence"]]])
    )

    assert measured.longest_evidence_run == 2


def test_section_granularity_alone_would_miss_a_chronicle() -> None:
    """One analytic sentence anywhere redeems a whole section, so sections read clean.

    The report that motivated these metrics had 3 of 20 paragraphs reaching no judgement
    while all 9 of its top-level scopes held at least one derived statement — its list-like
    section closed with an analytic paragraph after two bare ones. A gate set on sections
    would never have fired, which is why the paragraph count is the one to watch.
    """
    measured = measure_report_structure(
        _draft([[["evidence"] * 9, ["evidence"] * 8, ["evidence", "derived"]]])
    )

    assert measured.scopes_without_derived == 1  # the introduction only
    assert measured.paragraphs_without_derived == 3  # intro plus the two bare ones
    assert measured.longest_evidence_run == 9


def test_metrics_serialize_onto_the_render_event() -> None:
    payload = measure_report_structure(_draft([[["evidence", "derived"]]])).as_payload()

    assert payload["statement_count"] == 4
    assert set(payload) == {
        "statement_count",
        "evidence_count",
        "derived_count",
        "paragraph_count",
        "longest_evidence_run",
        "paragraphs_without_derived",
        "scopes_without_derived",
        "scope_count",
    }
