"""Pure client-side projection for persisted Job snapshots and SSE events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from prospector.api.schemas import JobDetail, JobTaskView, UsageView
from prospector.cli.client import CliProtocolError
from prospector.cli.sse import ServerEvent
from prospector.deterministic.budget import limits_for_effort
from prospector.runtime.timeline import ResearchTimelineRenderer


@dataclass(slots=True)
class ViewTask:
    task_id: UUID
    question: str
    status: str
    stop_reason: str | None
    rounds_used: int = 0
    rounds_limit: int = 0
    tool_calls_used: int = 0
    seen_tool_call_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_snapshot(cls, task: JobTaskView) -> ViewTask:
        return cls(
            task_id=task.task_id,
            question=task.question,
            status=task.status,
            stop_reason=task.stop_reason,
            rounds_limit=int(task.budget.get("max_worker_rounds", 0)),
            tool_calls_used=task.tool_calls_used,
        )


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    created_at: str
    line: str
    event_type: str


@dataclass(slots=True)
class JobView:
    job_id: UUID
    question: str
    effort: str
    language: str
    status: str
    phase: str
    outcome: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    plan_version: int
    tasks: dict[UUID, ViewTask]
    usage: list[UsageView]
    connection_state: str = "connected"
    reconnect_delay: float | None = None
    plan_reason: str | None = None
    timeline: list[TimelineEntry] = field(default_factory=list)
    stopped: bool = False
    last_event_id: int = 0
    phase_index: int = 0
    _renderer: ResearchTimelineRenderer = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._renderer = ResearchTimelineRenderer(
            self,
            limits_for_effort(self.effort),  # type: ignore[arg-type]
        )
        self._renderer.register_tasks(self.tasks)
        self.phase_index = self._phase_index(self.phase)

    @classmethod
    def from_snapshot(cls, detail: JobDetail) -> JobView:
        if detail.effort is None:
            raise CliProtocolError("Job snapshot is missing effort")
        return cls(
            job_id=detail.job_id,
            question=detail.question or "（未命名研究）",
            effort=detail.effort,
            language=detail.language or "unknown",
            status=detail.status,
            phase=detail.phase,
            outcome=detail.outcome,
            error_code=detail.error_code,
            created_at=detail.created_at,
            updated_at=detail.updated_at,
            plan_version=detail.plan_version,
            tasks={task.task_id: ViewTask.from_snapshot(task) for task in detail.tasks},
            usage=list(detail.usage),
        )

    def get_task(self, task_id: UUID) -> ViewTask:
        try:
            return self.tasks[task_id]
        except KeyError as exc:
            raise CliProtocolError(f"Task {task_id} is missing from job snapshot") from exc

    def merge_snapshot(self, detail: JobDetail) -> None:
        self.question = detail.question or self.question
        self.status = detail.status
        self.phase = detail.phase
        self.outcome = detail.outcome
        self.error_code = detail.error_code
        self.updated_at = detail.updated_at
        self.plan_version = detail.plan_version
        self.usage = list(detail.usage)
        self.phase_index = max(self.phase_index, self._phase_index(detail.phase))
        for snapshot_task in detail.tasks:
            current = self.tasks.get(snapshot_task.task_id)
            if current is None:
                self.tasks[snapshot_task.task_id] = ViewTask.from_snapshot(snapshot_task)
                continue
            current.question = snapshot_task.question
            current.stop_reason = snapshot_task.stop_reason
            current.rounds_limit = int(
                snapshot_task.budget.get("max_worker_rounds", current.rounds_limit)
            )
            current.tool_calls_used = max(
                current.tool_calls_used,
                snapshot_task.tool_calls_used,
            )
            if (
                snapshot_task.status in {"done", "failed", "cancelled"}
                or current.status == "pending"
            ):
                current.status = snapshot_task.status
        self._renderer.register_tasks(self.tasks)

    def connected(self) -> None:
        self.connection_state = "connected"
        self.reconnect_delay = None

    def reconnecting(self, delay: float) -> None:
        self.connection_state = "reconnecting"
        self.reconnect_delay = delay

    def fold(self, event: ServerEvent) -> list[str]:
        if event.id <= self.last_event_id:
            raise CliProtocolError("SSE event ids are not strictly increasing")
        self.last_event_id = event.id
        payload = event.payload
        event_type = event.event_type

        if event_type == "job.phase_changed":
            phase = str(payload.get("phase") or self.phase)
            self.phase = phase
            self.phase_index = max(self.phase_index, self._phase_index(phase))
            self.outcome = _optional_text(payload.get("outcome"))
            self.error_code = _optional_text(payload.get("error_code"))
        elif event_type == "planner.decided":
            self.plan_version = int(payload.get("plan_version") or self.plan_version)
            self.plan_reason = _optional_text(payload.get("reason") or payload.get("note"))
            self.phase_index = max(self.phase_index, 1)
        elif event_type == "replan.triggered":
            self.plan_version = int(payload.get("plan_version") or self.plan_version)
        elif event_type == "task.started":
            task = self._event_task(event)
            task.status = "running"
            task.rounds_limit = int(
                dict(payload.get("budget") or {}).get("max_worker_rounds", task.rounds_limit)
            )
            self.phase_index = max(self.phase_index, 2)
        elif event_type == "task.tool_used":
            task = self._event_task(event)
            call_id = _optional_text(payload.get("tool_call_id"))
            if call_id is not None and call_id not in task.seen_tool_call_ids:
                task.seen_tool_call_ids.add(call_id)
                task.tool_calls_used += 1
        elif event_type == "task.round_advanced":
            task = self._event_task(event)
            task.rounds_used = int(payload.get("rounds_used", task.rounds_used))
            task.rounds_limit = int(payload.get("rounds_limit", task.rounds_limit))
        elif event_type == "task.finished":
            task = self._event_task(event)
            task.status = (
                "failed"
                if payload.get("error") or payload.get("stop_reason") == "tool_error"
                else "done"
            )
            task.stop_reason = _optional_text(payload.get("stop_reason"))
            task.rounds_used = int(payload.get("rounds_used", 0))
            task.rounds_limit = int(payload.get("rounds_limit", task.rounds_limit))
            task.tool_calls_used = int(payload.get("tool_calls_used", task.tool_calls_used))
        elif event_type == "verifier.completed":
            self.phase_index = max(self.phase_index, 3)
        elif event_type == "report.draft_rendered":
            self.phase_index = max(self.phase_index, 6)
        elif event_type == "job.stopped":
            self.status = str(payload.get("status") or self.status)
            self.phase = str(payload.get("phase") or self.phase)
            self.outcome = _optional_text(payload.get("outcome"))
            self.error_code = _optional_text(payload.get("error_code"))
            self.stopped = True

        lines = self._renderer.render(event.as_timeline_event())
        timestamp = _clock(event.created_at)
        self.timeline.extend(
            TimelineEntry(created_at=timestamp, line=line, event_type=event_type) for line in lines
        )
        self.timeline = self.timeline[-8:]
        return lines

    def total_tokens(self) -> int | None:
        if not self.usage:
            return None
        return sum(item.input_tokens + item.output_tokens for item in self.usage)

    def total_tool_calls(self) -> int:
        usage_calls = sum(item.tool_calls for item in self.usage)
        return max(usage_calls, sum(task.tool_calls_used for task in self.tasks.values()))

    def running_tasks(self) -> int:
        return sum(task.status == "running" for task in self.tasks.values())

    def elapsed_seconds(self) -> int:
        end = self.updated_at if self.stopped else datetime.now(UTC)
        return max(0, int((end - self.created_at).total_seconds()))

    def _event_task(self, event: ServerEvent) -> ViewTask:
        raw_id = event.payload.get("task_id") or event.task_id
        if raw_id is None:
            raise CliProtocolError(f"{event.event_type} is missing task_id")
        return self.get_task(UUID(str(raw_id)))

    @staticmethod
    def _phase_index(phase: str) -> int:
        return {
            "initialize": 0,
            "queued": 0,
            "running": 0,
            "research": 2,
            "verifier": 3,
            "composition_pending": 4,
            "synthesizing": 4,
            "composition": 4,
            "writing": 4,
            "verifying": 5,
            "attributing": 5,
            "reviewing": 5,
            "revising": 5,
            "verified": 5,
            "partial": 5,
            "revisions_exhausted": 5,
            "draft_rendered": 6,
            "report_rendered": 6,
            "cancelling": 0,
            "cancelled": 0,
        }.get(phase, 0)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _clock(value: str | None) -> str:
    if value is None:
        return "--:--:--"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%H:%M:%S")
    except ValueError:
        return "--:--:--"
