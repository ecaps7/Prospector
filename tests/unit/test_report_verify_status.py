"""What "verified" is allowed to mean after a verification pass."""

from __future__ import annotations

from prospector.deterministic.dirty_propagation import MAX_REPORT_REVISION_ROUNDS
from prospector.flow.research_graph import _post_verify_status
from prospector.schemas.claims import ReportVerifierFindings, StatementFailure

_FAILURE = StatementFailure(
    statement_id="s_fact",
    kind="evidence",
    status="unsupported",
    reason="原文没有给出这个数字",
)


def _findings(*, failed: bool) -> ReportVerifierFindings:
    return ReportVerifierFindings(
        round=1,
        revision=1,
        failures=[_FAILURE] if failed else [],
        passed_statement_ids=[] if failed else ["s_fact"],
    )


def test_a_clean_pass_is_verified() -> None:
    assert _post_verify_status(_findings(failed=False), 1) == "verified"


def test_failures_with_rounds_left_go_back_to_the_writer() -> None:
    assert _post_verify_status(_findings(failed=True), 1) == "revising"


def test_running_out_of_rounds_is_not_the_same_as_passing() -> None:
    """Both statuses render, but only one of them means every sentence checked out."""
    exhausted = MAX_REPORT_REVISION_ROUNDS + 1

    assert _post_verify_status(_findings(failed=True), exhausted) == "revisions_exhausted"
    assert _post_verify_status(_findings(failed=False), exhausted) == "verified"
