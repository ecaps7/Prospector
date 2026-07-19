from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from typer.testing import CliRunner

from prospector.api.sse import encode_event
from prospector.cli.app import app
from prospector.cli.client import ProspectorClient
from prospector.schemas.brief import ResearchBrief, ScopeOutcome


def test_root_console_runs_http_sse_report_flow(monkeypatch, tmp_path: Path) -> None:
    job_id = uuid4()
    brief_id = uuid4()
    now = datetime.now(UTC)
    brief = ResearchBrief(
        question="研究问题",
        brief_text="展开后的研究问题",
        effort="standard",
        language="zh",
    )
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/scope":
            return httpx.Response(
                200,
                json=ScopeOutcome(kind="brief_pending", brief=brief).model_dump(mode="json"),
            )
        if request.url.path == "/jobs" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "job_id": str(job_id),
                    "brief_id": str(brief_id),
                    "status": "running",
                    "queue_position": None,
                },
            )
        if request.url.path == f"/jobs/{job_id}/events":
            content = "".join(
                [
                    encode_event(
                        {
                            "id": 1,
                            "event_type": "brief.confirmed",
                            "payload": {"effort": "standard"},
                            "task_id": None,
                            "decision_round": None,
                            "created_at": now,
                        }
                    ),
                    encode_event(
                        {
                            "id": 2,
                            "event_type": "job.stopped",
                            "payload": {
                                "status": "completed",
                                "phase": "draft_rendered",
                                "outcome": "draft_rendered",
                                "error_code": None,
                                "report_markdown_ref": "s3://reports/report.md",
                                "report_json_ref": None,
                            },
                            "task_id": None,
                            "decision_round": None,
                            "created_at": now,
                        }
                    ),
                ]
            )
            return httpx.Response(200, text=content, headers={"content-type": "text/event-stream"})
        if request.url.path == f"/jobs/{job_id}/report":
            return httpx.Response(200, content=b"# report\n")
        if request.url.path == f"/jobs/{job_id}":
            return httpx.Response(
                200,
                json={
                    "job_id": str(job_id),
                    "question": brief.question,
                    "effort": brief.effort,
                    "status": "running",
                    "phase": "research",
                    "outcome": None,
                    "error_code": None,
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                    "brief_id": str(brief_id),
                    "language": "zh",
                    "plan_version": 0,
                    "tasks": [],
                    "usage": [],
                    "report": None,
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("prospector.cli.app.require_tty", lambda: None)
    monkeypatch.setattr(
        "prospector.cli.app.ProspectorClient",
        lambda: ProspectorClient("http://prospector.test", transport=transport),
    )
    monkeypatch.setattr("prospector.cli.attach.Path.home", lambda: tmp_path)

    result = CliRunner().invoke(app, ["--plain"], input="研究问题\nc\n")
    assert result.exit_code == 0, result.output
    assert f"JOB_CREATED: {job_id}" in result.output
    assert "Brief 已确认" in result.output
    assert "RESEARCH_STOPPED: status=completed" in result.output
    report_path = tmp_path / ".prospector" / "reports" / str(job_id) / "report.md"
    assert report_path.read_bytes() == b"# report\n"
    assert requests == [
        ("GET", "/healthz"),
        ("POST", "/scope"),
        ("POST", "/jobs"),
        ("GET", f"/jobs/{job_id}"),
        ("GET", f"/jobs/{job_id}/events"),
        ("GET", f"/jobs/{job_id}"),
        ("GET", f"/jobs/{job_id}/report"),
    ]
