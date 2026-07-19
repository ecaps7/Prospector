from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from uuid import uuid4

from typer.testing import CliRunner

from prospector.api.schemas import JobCreateResponse, JobDetail
from prospector.cli.app import app
from prospector.cli.client import CliApiError
from prospector.cli.plain import AttachResult
from prospector.cli.view import JobView
from prospector.schemas.brief import ResearchBrief, ScopeOutcome

runner = CliRunner()


class FakeClient:
    def __init__(self) -> None:
        self.created = 0
        self.scoped = 0

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

    def scope(self, *_args, **_kwargs) -> ScopeOutcome:
        self.scoped += 1
        return ScopeOutcome(
            kind="brief_pending",
            brief=ResearchBrief(
                question="研究问题",
                brief_text="展开后的研究问题",
                effort="standard",
                language="zh",
            ),
        )

    def revise_scope(self, *_args, **_kwargs) -> ResearchBrief:
        raise AssertionError("revision not expected")

    def create_job(self, _brief: ResearchBrief) -> JobCreateResponse:
        self.created += 1
        return JobCreateResponse(
            job_id=uuid4(),
            brief_id=uuid4(),
            status="running",
            queue_position=None,
        )


def test_root_console_creates_job_and_returns_to_question_prompt(monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr("prospector.cli.app.require_tty", lambda: None)
    monkeypatch.setattr("prospector.cli.app.ProspectorClient", lambda: client)
    monkeypatch.setattr("prospector.cli.app._attach", lambda *_args, **_kwargs: 0)
    result = runner.invoke(app, ["--plain"], input="研究问题\nc\n")
    assert result.exit_code == 0, result.output
    assert "JOB_CREATED:" in result.output
    assert "JOB_RUNNING" in result.output
    assert "研究问题" in result.output
    assert "已退出 Prospector" in result.output
    assert client.created == 1


def test_root_console_q_returns_home_without_creating_job(monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr("prospector.cli.app.require_tty", lambda: None)
    monkeypatch.setattr("prospector.cli.app.ProspectorClient", lambda: client)
    result = runner.invoke(app, ["--plain"], input="研究问题\nq\n")
    assert result.exit_code == 0, result.output
    assert "用户放弃" in result.output
    assert "JOB_CREATED" not in result.output
    assert client.created == 0


def test_root_console_accepts_multiple_questions(monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr("prospector.cli.app.require_tty", lambda: None)
    monkeypatch.setattr("prospector.cli.app.ProspectorClient", lambda: client)
    result = runner.invoke(
        app,
        ["--plain"],
        input="第一个问题\nq\n第二个问题\nq\n",
    )
    assert result.exit_code == 0, result.output
    assert client.scoped == 2
    assert result.output.count("用户放弃") == 2


def test_attach_ctrl_c_leaves_remote_job_running(monkeypatch) -> None:
    client = FakeClient()
    job_id = uuid4()
    monkeypatch.setattr("prospector.cli.app.ProspectorClient", lambda: client)
    monkeypatch.setattr(
        "prospector.cli.app.attach_plain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    result = runner.invoke(app, ["job", "attach", str(job_id), "--plain"])
    assert result.exit_code == 0, result.output
    assert "任务继续运行" in result.output
    assert str(job_id) in result.output


def test_root_console_ctrl_c_after_job_creation_returns_home(monkeypatch) -> None:
    client = FakeClient()
    monkeypatch.setattr("prospector.cli.app.require_tty", lambda: None)
    monkeypatch.setattr("prospector.cli.app.ProspectorClient", lambda: client)
    monkeypatch.setattr(
        "prospector.cli.app._print_created",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    result = runner.invoke(app, ["--plain"], input="研究问题\nc\n")
    assert result.exit_code == 0, result.output
    assert client.created == 1
    assert "任务继续运行" in result.output


def test_terminal_failure_exit_codes(monkeypatch) -> None:
    client = FakeClient()
    job_id = uuid4()
    monkeypatch.setattr("prospector.cli.app.ProspectorClient", lambda: client)
    now = datetime.now(UTC)
    view = JobView.from_snapshot(
        JobDetail(
            job_id=job_id,
            question="研究问题",
            effort="standard",
            status="failed",
            phase="failed",
            outcome="failed",
            error_code="verifier_major_gap",
            created_at=now,
            updated_at=now,
            brief_id=uuid4(),
            language="zh",
            plan_version=1,
            tasks=[],
            usage=[],
            report=None,
        )
    )

    for error_code, expected in (("verifier_major_gap", 3), ("job_execution_error", 1)):
        monkeypatch.setattr(
            "prospector.cli.app.attach_plain",
            lambda *_args, error_code=error_code, **_kwargs: AttachResult(
                status="failed",
                phase="failed",
                outcome="failed",
                error_code=error_code,
                report_path=None,
                view=view,
            ),
        )
        result = runner.invoke(app, ["job", "attach", str(job_id), "--plain"])
        assert result.exit_code == expected, result.output


def test_unknown_job_is_usage_error(monkeypatch) -> None:
    class MissingJobClient(FakeClient):
        def health(self) -> None:
            pass

    client = MissingJobClient()
    job_id = uuid4()
    monkeypatch.setattr("prospector.cli.app.ProspectorClient", lambda: client)
    monkeypatch.setattr(
        "prospector.cli.app.attach_plain",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            CliApiError(404, "job_not_found", "Job not found")
        ),
    )
    result = runner.invoke(app, ["job", "attach", str(job_id), "--plain"])
    assert result.exit_code == 2, result.output
    assert "job_not_found" in result.output
