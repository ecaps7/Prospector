"""Generate and print a report preview from one persisted verified Job."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from prospector.agents.llm import LlmNotConfiguredError, require_llm_settings
from prospector.agents.report_writer import OpenAIReportWriter, ReportWriterOutputError
from prospector.reporting.render import render_report_draft
from prospector.store.repositories import ResearchRepository

pytestmark = [pytest.mark.integration, pytest.mark.live]

# Generate from frozen research without rerunning Planner, Workers, or Verifier.
JOB_ID = UUID("2af66cd3-86bf-445a-b7f4-2a0b30ae632e")
VERIFIER_RUN_ID = UUID("ba962a23-49e1-4046-82be-6f3ee12c7c5f")


def test_print_report_writer_preview() -> None:
    """Call the real Writer and print only the final rendered Markdown."""
    try:
        require_llm_settings()
    except LlmNotConfiguredError as exc:
        pytest.skip(str(exc))

    repository = ResearchRepository()
    snapshot = repository.build_writer_snapshot(JOB_ID, VERIFIER_RUN_ID)
    try:
        result = OpenAIReportWriter().write(snapshot)
    except ReportWriterOutputError as exc:
        dump_path = Path(f"report_writer_failure_{str(JOB_ID)[:8]}.json")
        dump_path.write_text(
            json.dumps(
                {"error": str(exc), "raw_output": exc.raw_output},
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        pytest.fail(f"Writer output rejected; raw output dumped to {dump_path}: {exc}")
    rendered = render_report_draft(snapshot, result.draft)
    print(rendered.markdown)
