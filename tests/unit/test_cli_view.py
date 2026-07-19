from __future__ import annotations

import os
import pty
import termios
import threading
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from rich.console import Console

from prospector.api.schemas import JobCancelResponse, JobDetail, JobTaskView, UsageView
from prospector.cli.client import CliProtocolError
from prospector.cli.sse import ServerEvent
from prospector.cli.tui import _CancelShortcut, _TerminalKeyListener, render_job_view
from prospector.cli.view import JobView


def _detail(job_id: UUID, task_id: UUID) -> JobDetail:
    now = datetime.now(UTC)
    return JobDetail(
        job_id=job_id,
        question="评估亚太竞品经营态势",
        effort="deep",
        status="running",
        phase="research",
        outcome=None,
        error_code=None,
        created_at=now,
        updated_at=now,
        brief_id=uuid4(),
        language="zh",
        plan_version=1,
        tasks=[
            JobTaskView(
                task_id=task_id,
                question="核验区域收入与客户证据",
                subjects=["竞品"],
                research_stage="scout",
                research_mode="factual",
                status="pending",
                stop_reason=None,
                budget={"max_worker_rounds": 32},
                tool_calls_used=0,
                created_at=now,
                started_at=None,
                finished_at=None,
            )
        ],
        usage=[UsageView(component="planner", input_tokens=100, output_tokens=20, tool_calls=0)],
        report=None,
    )


def _event(event_id: int, event_type: str, payload: dict[str, object]) -> ServerEvent:
    return ServerEvent(
        id=event_id,
        event_type=event_type,
        payload=payload,
        task_id=None if payload.get("task_id") is None else str(payload["task_id"]),
        decision_round=None,
        created_at=datetime.now(UTC).isoformat(),
    )


def test_job_view_folds_task_progress_usage_and_terminal_state() -> None:
    job_id = uuid4()
    task_id = uuid4()
    view = JobView.from_snapshot(_detail(job_id, task_id))

    view.fold(
        _event(
            1,
            "task.started",
            {"task_id": str(task_id), "budget": {"max_worker_rounds": 32}},
        )
    )
    view.fold(
        _event(
            2,
            "task.round_advanced",
            {"task_id": str(task_id), "rounds_used": 1, "rounds_limit": 32},
        )
    )
    assert view.tasks[task_id].rounds_used == 1
    view.fold(
        _event(
            3,
            "task.tool_used",
            {"task_id": str(task_id), "tool": "web_search", "tool_call_id": "call-1"},
        )
    )
    view.fold(
        _event(
            4,
            "task.finished",
            {
                "task_id": str(task_id),
                "stop_reason": "expected_evidence_satisfied",
                "rounds_used": 4,
                "rounds_limit": 32,
                "tool_calls_used": 1,
            },
        )
    )
    view.fold(
        _event(
            5,
            "job.stopped",
            {
                "status": "completed",
                "phase": "draft_rendered",
                "outcome": "draft_rendered",
                "error_code": None,
            },
        )
    )

    task = view.tasks[task_id]
    assert task.status == "done"
    assert task.rounds_used == 4
    assert task.tool_calls_used == 1
    assert view.total_tokens() == 120
    assert view.status == "completed"
    assert view.stopped is True

    with pytest.raises(CliProtocolError):
        view.fold(_event(5, "brief.confirmed", {"effort": "deep"}))


def test_timeline_task_labels_follow_snapshot_order_not_event_arrival_order() -> None:
    job_id = uuid4()
    first_task_id = uuid4()
    second_task_id = uuid4()
    detail = _detail(job_id, first_task_id)
    second = detail.tasks[0].model_copy(
        update={
            "task_id": second_task_id,
            "question": "第二个任务",
        }
    )
    detail.tasks.append(second)
    view = JobView.from_snapshot(detail)

    lines = view.fold(
        _event(
            1,
            "task.started",
            {
                "task_id": str(second_task_id),
                "budget": {"max_worker_rounds": 32},
            },
        )
    )

    assert lines[0].startswith("[T2]")
    assert view.tasks[first_task_id].status == "pending"
    assert view.tasks[second_task_id].status == "running"


@pytest.mark.parametrize("width", [80, 140])
def test_rich_job_view_renders_narrow_and_wide_layout(width: int) -> None:
    view = JobView.from_snapshot(_detail(uuid4(), uuid4()))
    console = Console(width=width, record=True, force_terminal=False)
    console.print(render_job_view(view, width))
    rendered = console.export_text()
    assert "Prospector" in rendered
    assert "Plan v1" in rendered
    assert "限额与用量" in rendered
    assert "评估亚太竞品经营态势" in rendered
    assert "Ctrl-C 离开（任务继续）" in rendered
    assert "x 终止 Job" in rendered


def test_tui_cancel_shortcut_requests_cancel_once_and_reports_state() -> None:
    job_id = uuid4()
    calls: list[UUID] = []
    notices: list[str] = []

    class Client:
        def cancel_job(self, requested_job_id: UUID) -> JobCancelResponse:
            calls.append(requested_job_id)
            return JobCancelResponse(job_id=requested_job_id, status="cancelling")

    shortcut = _CancelShortcut(Client(), job_id, notices.append)  # type: ignore[arg-type]
    shortcut.handle_key("a")
    shortcut.handle_key("x")
    shortcut.handle_key("X")

    assert calls == [job_id]
    assert notices == [
        "正在发送取消请求…",
        "取消请求已发送，等待当前调用到达安全边界…",
    ]


def test_terminal_key_listener_reads_one_key_and_restores_terminal() -> None:
    master_fd, slave_fd = pty.openpty()
    received: list[str] = []
    key_read = threading.Event()

    def record(key: str) -> None:
        received.append(key)
        key_read.set()

    try:
        before = termios.tcgetattr(slave_fd)
        with os.fdopen(slave_fd, "r") as stream:
            with _TerminalKeyListener(record, stream):
                os.write(master_fd, b"x")
                assert key_read.wait(timeout=1)
            assert termios.tcgetattr(stream.fileno()) == before
    finally:
        os.close(master_fd)

    assert received == ["x"]
