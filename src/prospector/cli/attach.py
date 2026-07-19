"""Shared attach lifecycle for Rich and plain renderers."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from prospector.cli.client import CliLocalError, CliProtocolError, ProspectorClient
from prospector.cli.sse import ServerEvent, follow_events
from prospector.cli.view import JobView

ViewCallback = Callable[[JobView, list[str]], None]


@dataclass(frozen=True, slots=True)
class AttachResult:
    status: str
    phase: str
    outcome: str | None
    error_code: str | None
    report_path: Path | None
    view: JobView


def write_report(
    client: ProspectorClient,
    job_id: UUID,
    report_root: Path,
) -> Path:
    destination = report_root / str(job_id) / "report.md"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(client.download_report(job_id, "md"))
    except OSError as exc:
        raise CliLocalError(f"无法写入报告：{destination}") from exc
    return destination


def run_attach(
    client: ProspectorClient,
    job_id: UUID,
    *,
    on_update: ViewCallback,
    on_reconnect: Callable[[JobView, float], None],
    on_connected: Callable[[JobView], None],
    report_root: Path | None = None,
) -> AttachResult:
    view = JobView.from_snapshot(client.get_job(job_id))
    on_update(view, [])
    refreshed_at = time.monotonic()

    def connected() -> None:
        view.connected()
        on_connected(view)

    def reconnecting(delay: float) -> None:
        view.reconnecting(delay)
        on_reconnect(view, delay)

    stopped: ServerEvent | None = None
    for event in follow_events(
        client,
        job_id,
        on_reconnect=reconnecting,
        on_connected=connected,
    ):
        force_refresh = event.event_type in {
            "job.phase_changed",
            "task.finished",
            "job.stopped",
        } or (event.event_type == "planner.decided" and event.payload.get("decision") == "dispatch")
        now = time.monotonic()
        if force_refresh or now - refreshed_at >= 1.0:
            view.merge_snapshot(client.get_job(job_id))
            refreshed_at = now
        lines = view.fold(event)
        on_update(view, lines)
        if event.event_type == "job.stopped":
            stopped = event

    if stopped is None:
        raise CliProtocolError("SSE stream ended without job.stopped")
    payload = stopped.payload
    status = payload.get("status")
    phase = payload.get("phase")
    if status not in {"completed", "failed", "cancelled"}:
        raise CliProtocolError("job.stopped has an invalid status")
    if not isinstance(phase, str) or not phase:
        raise CliProtocolError("job.stopped is missing phase")
    report_path = None
    if status == "completed":
        report_path = write_report(
            client,
            job_id,
            report_root or (Path.home() / ".prospector" / "reports"),
        )
    return AttachResult(
        status=status,
        phase=phase,
        outcome=None if payload.get("outcome") is None else str(payload["outcome"]),
        error_code=(None if payload.get("error_code") is None else str(payload["error_code"])),
        report_path=report_path,
        view=view,
    )
