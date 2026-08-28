"""Live replay of the Report Verifier → Writer revise → Report Verifier loop."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from prospector.agents.llm import LlmNotConfiguredError, require_llm_settings
from prospector.agents.report_verifier import (
    OpenAIReportVerifier,
    ReportVerifierOutputError,
)
from prospector.agents.report_writer import OpenAIReportWriter, ReportWriterOutputError
from prospector.deterministic.dirty_propagation import (
    MAX_REPORT_REVISION_ROUNDS,
    can_revise_again,
    changed_statement_ids,
    dirty_statement_ids,
    skip_stage_one_after_requirement_rewrite,
)
from prospector.reporting.render import render_findings, render_report_draft
from prospector.schemas.report import ReportDraft
from prospector.store.repositories import ResearchRepository

pytestmark = [pytest.mark.integration, pytest.mark.live]

# Re-use the same frozen Job that already has a persisted draft.
JOB_ID = UUID("f512c9d2-157c-4c23-9130-4a6a9b294a45")


def _find_passed_verifier_run_id(repository: ResearchRepository, job_id: UUID) -> UUID | None:
    """Find the latest completed-and-passed Research Verifier run for *job_id*."""
    with repository.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT id FROM app.verifier_runs
                    WHERE job_id=:job_id AND status='completed'
                      AND release_decision='pass'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                ),
                {"job_id": job_id},
            )
            .mappings()
            .first()
        )
    return UUID(str(row["id"])) if row else None


def test_report_verifier_writer_loop() -> None:
    """Run the Verifier → Writer revise → Verifier loop on a persisted draft.

    1. Load a frozen report draft and verify every statement.
    2. If failures exist and revision budget allows, call Writer.revise().
    3. Re-verify the revised draft.
    4. Render and print the final report plus findings at each stage.
    """
    try:
        require_llm_settings()
    except LlmNotConfiguredError as exc:
        pytest.skip(str(exc))

    repository = ResearchRepository()

    # ── 1. Load persisted draft ────────────────────────────────────────
    stored = repository.get_report_revision(JOB_ID)
    if stored is None or stored["draft"] is None:
        pytest.skip(f"persisted replay report is unavailable: {JOB_ID}")

    report_id = UUID(str(stored["report_id"]))
    revision = int(stored["revision"])
    draft: ReportDraft = stored["draft"]

    verifier_run_id = _find_passed_verifier_run_id(repository, JOB_ID)
    if verifier_run_id is None:
        pytest.skip(f"no passed Research Verifier run found for job {JOB_ID}")

    writer_snapshot = repository.build_writer_snapshot(JOB_ID, verifier_run_id)

    # ── 2. First verification pass ────────────────────────────────────
    rv_snapshot = repository.build_report_verifier_snapshot(
        JOB_ID,
        report_id,
        revision=revision,
        round_number=1,
        dirty_statement_ids=dirty_statement_ids(draft),
        draft=draft,
    )

    print(
        f"\n{'=' * 60}\n"
        f" Round 1 verification · revision={revision}"
        f" · {len(rv_snapshot.statements)} statements\n"
        f"{'=' * 60}"
    )

    try:
        verifier_result = OpenAIReportVerifier().verify(rv_snapshot)
    except ReportVerifierOutputError as exc:
        pytest.fail(f"Report Verifier failed: {exc}")

    findings = verifier_result.findings
    print(render_findings(rv_snapshot, findings))

    n_passed = len(findings.passed_statement_ids)
    n_failed = len(findings.failures)
    print(f"\nRound 1 summary: {n_passed} passed, {n_failed} failed")

    # ── 3. Writer revision (if needed) ────────────────────────────────
    if findings.all_passed:
        print("\nAll statements passed — no revision needed.")
        rendered = render_report_draft(writer_snapshot, draft, verified=True)
        print(rendered.markdown)
        return

    if not can_revise_again(revision):
        print(
            f"\nRevision budget exhausted"
            f" (revision={revision}, max={MAX_REPORT_REVISION_ROUNDS})."
            f" Rendering final report as-is."
        )
        rendered = render_report_draft(writer_snapshot, draft, verified=True)
        print(rendered.markdown)
        return

    print(f"\n{'─' * 60}\n Writer revising {n_failed} failed statement(s) …\n{'─' * 60}")

    try:
        writer_result = OpenAIReportWriter().revise(writer_snapshot, draft, findings)
    except ReportWriterOutputError as exc:
        dump_path = Path(f"rv_writer_loop_failure_{str(JOB_ID)[:8]}.json")
        dump_path.write_text(
            json.dumps(
                {"error": str(exc), "raw_output": exc.raw_output},
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        pytest.fail(f"Writer revise failed; raw output dumped to {dump_path}: {exc}")

    revised_draft = writer_result.draft
    revised_revision = revision + 1

    # ── 4. Second verification pass on the revised draft ──────────────
    skip_stage_one = skip_stage_one_after_requirement_rewrite(findings)
    if skip_stage_one:
        current_ids = {item.statement_id for item in revised_draft.statements()}
        reused = [item for item in verifier_result.decisions if item.statement_id in current_ids]
        dirty = set()
    else:
        changed = changed_statement_ids(draft, revised_draft)
        dirty = dirty_statement_ids(
            revised_draft,
            changed_ids=changed,
            previous_clean_ids=set(findings.passed_statement_ids),
        )
        reused = []

    rv_snapshot_2 = repository.build_report_verifier_snapshot(
        JOB_ID,
        report_id,
        revision=revised_revision,
        round_number=1,
        dirty_statement_ids=dirty,
        draft=revised_draft,
        skip_statement_verification=skip_stage_one,
        reused_statement_decisions=reused,
    )

    print(
        f"\n{'=' * 60}\n"
        f" Round 2 verification · revision={revised_revision}"
        f" · skip_stage_one={skip_stage_one}"
        f" · {len(dirty)} dirty / {len(rv_snapshot_2.statements)} statements"
        f" · {len(reused)} reused\n"
        f"{'=' * 60}"
    )

    try:
        verifier_result_2 = OpenAIReportVerifier().verify(rv_snapshot_2)
    except ReportVerifierOutputError as exc:
        pytest.fail(f"Report Verifier (round 2) failed: {exc}")

    findings_2 = verifier_result_2.findings
    print(render_findings(rv_snapshot_2, findings_2))

    n_passed_2 = len(findings_2.passed_statement_ids)
    n_failed_2 = len(findings_2.failures)
    print(
        f"\nRound 2 summary: {n_passed_2} passed, {n_failed_2} failed"
        f"  (dirty subset: {len(dirty)} statements)"
    )

    # ── 5. Render final report ────────────────────────────────────────
    print(f"\n{'=' * 60}\n Final report (revision={revised_revision})\n{'=' * 60}")
    rendered = render_report_draft(writer_snapshot, revised_draft, verified=True)
    print(rendered.markdown)

    # ── 6. Structural assertions ──────────────────────────────────────
    assert len(revised_draft.statements()) == len(draft.statements()), (
        "revision must not add or remove statements"
    )
    assert findings_2.passed_statement_ids or findings_2.failures, (
        "second-round findings must be populated"
    )
