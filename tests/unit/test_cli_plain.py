from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from prospector.api.schemas import JobDetail, JobTaskView
from prospector.cli.plain import attach_plain
from prospector.cli.sse import ServerEvent


def _detail(job_id: UUID, tasks: list[JobTaskView]) -> JobDetail:
    now = datetime.now(UTC)
    return JobDetail(
        job_id=job_id,
        question="研究问题",
        effort="standard",
        status="running",
        phase="research",
        outcome=None,
        error_code=None,
        created_at=now,
        updated_at=now,
        brief_id=uuid4(),
        language="zh",
        plan_version=1,
        latest_event_id=0,
        tasks=tasks,
        usage=[],
        report=None,
    )


def test_attach_plain_refreshes_tasks_and_downloads_report(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    job_id = uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    task = JobTaskView(
        task_id=task_id,
        question="核验竞品亚太收入",
        subjects=["竞品"],
        research_stage="scout",
        research_mode="factual",
        status="pending",
        stop_reason=None,
        budget={"max_worker_rounds": 24},
        tool_calls_used=0,
        created_at=now,
        started_at=None,
        finished_at=None,
    )

    class FakeClient:
        def __init__(self) -> None:
            self.snapshots = 0

        def get_job(self, requested: UUID) -> JobDetail:
            assert requested == job_id
            self.snapshots += 1
            return _detail(job_id, [] if self.snapshots == 1 else [task])

        def download_report(self, requested: UUID, report_format: str = "md") -> bytes:
            assert requested == job_id
            assert report_format == "md"
            return b"# completed report"

    events = [
        ServerEvent(
            id=1,
            event_type="planner.decided",
            payload={
                "decision_round": 1,
                "decision": "dispatch",
                "plan_version": 1,
                "task_ids": [str(task_id)],
                "reason": "开始调查",
            },
            task_id=None,
            decision_round=1,
            created_at=None,
        ),
        ServerEvent(
            id=2,
            event_type="job.stopped",
            payload={
                "status": "completed",
                "phase": "draft_rendered",
                "outcome": "draft_rendered",
                "error_code": None,
            },
            task_id=None,
            decision_round=None,
            created_at=None,
        ),
    ]
    monkeypatch.setattr(
        "prospector.cli.attach.follow_events",
        lambda *_args, **_kwargs: iter(events),
    )
    client = FakeClient()
    result = attach_plain(
        client,  # type: ignore[arg-type]
        job_id,
        report_root=tmp_path,
    )
    assert client.snapshots == 3
    assert result.status == "completed"
    report_path = result.report_path
    assert report_path == tmp_path / str(job_id) / "report.md"
    assert report_path is not None
    assert report_path.read_bytes() == b"# completed report"
    assert "核验竞品亚太收入" in capsys.readouterr().out
