from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self
from uuid import UUID, uuid4

from typer.testing import CliRunner

from prospector.api.schemas import (
    JobCancelResponse,
    JobDetail,
    JobListItem,
    JobTaskView,
    ReportView,
    UsageView,
)
from prospector.cli.app import app


class FakeClient:
    job_id = uuid4()
    now = datetime.now(UTC)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        pass

    def health(self) -> None:
        pass

    def list_jobs(self) -> list[JobListItem]:
        return [
            JobListItem(
                job_id=self.job_id,
                question="亚太竞品研究",
                effort="standard",
                status="completed",
                phase="draft_rendered",
                outcome="draft_rendered",
                error_code=None,
                created_at=self.now,
                updated_at=self.now,
            )
        ]

    def get_job(self, job_id: UUID) -> JobDetail:
        assert job_id == self.job_id
        return JobDetail(
            **self.list_jobs()[0].model_dump(),
            brief_id=uuid4(),
            language="zh",
            plan_version=2,
            tasks=[
                JobTaskView(
                    task_id=uuid4(),
                    question="核验收入",
                    subjects=["竞品"],
                    research_stage="scout",
                    research_mode="factual",
                    status="done",
                    stop_reason="expected_evidence_satisfied",
                    budget={"max_worker_rounds": 24},
                    tool_calls_used=3,
                    created_at=self.now,
                    started_at=self.now,
                    finished_at=self.now,
                )
            ],
            usage=[
                UsageView(
                    component="planner",
                    input_tokens=100,
                    output_tokens=25,
                    tool_calls=0,
                )
            ],
            report=ReportView(
                report_id=uuid4(),
                status="draft_rendered",
                verification_status="verified",
                markdown_ref="s3://bucket/report.md",
                json_ref="s3://bucket/report.json",
            ),
        )

    def download_report(self, job_id: UUID, report_format: str = "md") -> bytes:
        assert job_id == self.job_id
        return b"# Report title\n" if report_format == "md" else b'{"title":"Report"}'

    def cancel_job(self, job_id: UUID) -> JobCancelResponse:
        assert job_id == self.job_id
        return JobCancelResponse(job_id=job_id, status="cancelling")


def test_job_list_and_status_render_authoritative_snapshots(monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr("prospector.cli.app.ProspectorClient", lambda: client)
    runner = CliRunner()

    listed = runner.invoke(app, ["job", "list"])
    assert listed.exit_code == 0, listed.output
    assert str(client.job_id) in listed.output
    assert "亚太竞品研究" in listed.output

    status = runner.invoke(app, ["job", "status", str(client.job_id)])
    assert status.exit_code == 0, status.output
    assert "Plan" in status.output
    assert "v2" in status.output
    assert "planner" in status.output
    assert "TOTAL" in status.output


def test_report_show_and_export_original_bytes(monkeypatch, tmp_path: Path) -> None:
    client = FakeClient()
    monkeypatch.setattr("prospector.cli.app.ProspectorClient", lambda: client)
    runner = CliRunner()

    shown = runner.invoke(app, ["report", "show", str(client.job_id)])
    assert shown.exit_code == 0, shown.output
    assert "Report title" in shown.output

    destination = tmp_path / "result.json"
    exported = runner.invoke(
        app,
        [
            "report",
            "export",
            str(client.job_id),
            "--format",
            "json",
            "-o",
            str(destination),
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert destination.read_bytes() == b'{"title":"Report"}'

    refused = runner.invoke(
        app,
        ["report", "export", str(client.job_id), "-o", str(destination)],
    )
    assert refused.exit_code == 2
    assert "目标文件已存在" in refused.output


def test_job_cancel_reports_cooperative_stop(monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr("prospector.cli.app.ProspectorClient", lambda: client)
    result = CliRunner().invoke(app, ["job", "cancel", str(client.job_id)])
    assert result.exit_code == 0, result.output
    assert f"CANCEL_REQUESTED: {client.job_id}" in result.output
    assert "安全边界" in result.output
