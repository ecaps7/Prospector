"""Read-only live replay of Report Verifier against one persisted report."""

from __future__ import annotations

from collections import Counter
from uuid import UUID

import pytest

from prospector.agents.llm import LlmNotConfiguredError, require_llm_settings
from prospector.agents.report_verifier import OpenAIReportVerifier
from prospector.deterministic.dirty_propagation import dirty_statement_ids
from prospector.reporting.render import render_findings
from prospector.store.repositories import ResearchRepository

pytestmark = [pytest.mark.integration, pytest.mark.live]

JOB_ID = UUID("f512c9d2-157c-4c23-9130-4a6a9b294a45")


def test_replay_persisted_report_through_report_verifier_without_writes() -> None:
    """Verify the frozen report draft with the real model without persisting results."""
    try:
        require_llm_settings()
    except LlmNotConfiguredError as exc:
        pytest.skip(str(exc))

    repository = ResearchRepository()
    stored = repository.get_report_revision(JOB_ID)
    if stored is None or stored["draft"] is None:
        pytest.skip(f"persisted replay report is unavailable: {JOB_ID}")

    report_id = UUID(str(stored["report_id"]))
    revision = int(stored["revision"])
    draft = stored["draft"]
    snapshot = repository.build_report_verifier_snapshot(
        JOB_ID,
        report_id,
        revision=revision,
        round_number=1,
        dirty_statement_ids=dirty_statement_ids(draft),
        draft=draft,
    )

    assert len(snapshot.statements) == 51
    assert Counter(item.kind for item in snapshot.statements) == {
        "evidence": 43,
        "derived": 8,
    }
    assert all(item.candidate_excerpts for item in snapshot.statements if item.kind == "evidence")

    result = OpenAIReportVerifier().verify(snapshot)

    print(render_findings(snapshot, result.findings))
    assert len(result.decisions) == len(snapshot.statements)
    assert {decision.statement_id for decision in result.decisions} == {
        statement.statement_id for statement in snapshot.statements
    }
