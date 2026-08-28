"""Rich Live rendering for a remote Prospector Job."""

from __future__ import annotations

import os
import select
import sys
import termios
import threading
import tty
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO
from uuid import UUID

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from prospector.cli.attach import AttachResult, run_attach
from prospector.cli.client import (
    CliApiError,
    CliConnectionError,
    CliProtocolError,
    ProspectorClient,
)
from prospector.cli.view import JobView, TimelineEntry, ViewTask
from prospector.deterministic.budget import limits_for_effort

PHASE_LABELS = ("Brief", "规划", "搜集", "验证", "成文", "句级验证", "渲染")


class _TerminalKeyListener(AbstractContextManager["_TerminalKeyListener"]):
    """Read single TUI keys without changing Ctrl-C signal semantics."""

    def __init__(self, on_key: Callable[[str], None], stream: TextIO | None = None) -> None:
        self._on_key = on_key
        self._stream = stream or sys.stdin
        self._fd: int | None = None
        self._attributes: Any | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _TerminalKeyListener:
        if not self._stream.isatty():
            return self
        fd = self._stream.fileno()
        self._fd = fd
        self._attributes = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        self._thread = threading.Thread(target=self._read_keys, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if self._fd is not None and self._attributes is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._attributes)

    def _read_keys(self) -> None:
        assert self._fd is not None
        while not self._stop.is_set():
            readable, _, _ = select.select([self._fd], [], [], 0.1)
            if readable:
                self._on_key(os.read(self._fd, 1).decode(errors="ignore"))


class _CancelShortcut:
    """Dispatch the TUI cancel key exactly once after a successful request."""

    def __init__(
        self,
        client: ProspectorClient,
        job_id: UUID,
        on_notice: Callable[[str], None],
    ) -> None:
        self._client = client
        self._job_id = job_id
        self._on_notice = on_notice
        self._lock = threading.Lock()
        self._requested = False

    def handle_key(self, key: str) -> None:
        if key.lower() != "x":
            return
        with self._lock:
            if self._requested:
                return
            self._requested = True
        self._on_notice("正在发送取消请求…")
        try:
            result = self._client.cancel_job(self._job_id)
        except (CliApiError, CliConnectionError, CliProtocolError) as exc:
            with self._lock:
                self._requested = False
            self._on_notice(f"取消失败：{exc}")
            return
        if result.status == "cancelled":
            self._on_notice("Job 已取消，等待终止事件…")
        else:
            self._on_notice("取消请求已发送，等待当前调用到达安全边界…")


def attach_tui(
    client: ProspectorClient,
    job_id: UUID,
    *,
    console: Console | None = None,
    report_root: Path | None = None,
) -> AttachResult:
    output = console or Console()
    render_lock = threading.Lock()
    latest_view: JobView | None = None
    notice: str | None = None
    with Live(
        Text("正在连接 Prospector…", style="cyan"),
        console=output,
        refresh_per_second=4,
        transient=True,
    ) as live:

        def render(view: JobView) -> None:
            nonlocal latest_view
            with render_lock:
                latest_view = view
                live.update(render_job_view(view, output.width, notice=notice))

        def show_notice(message: str) -> None:
            nonlocal notice
            with render_lock:
                notice = message
                if latest_view is not None:
                    live.update(render_job_view(latest_view, output.width, notice=notice))

        shortcut = _CancelShortcut(client, job_id, show_notice)
        with _TerminalKeyListener(shortcut.handle_key):
            return run_attach(
                client,
                job_id,
                report_root=report_root,
                on_update=lambda view, _lines: render(view),
                on_reconnect=lambda view, _delay: render(view),
                on_connected=render,
            )


def render_job_view(view: JobView, width: int, *, notice: str | None = None) -> RenderableType:
    parts: list[RenderableType] = [
        _header(view),
        _phase_track(view),
    ]
    plan = _plan_panel(view)
    usage = _usage_panel(view)
    if width < 100:
        parts.extend((plan, usage))
    else:
        middle = Table.grid(expand=True)
        middle.add_column(ratio=3)
        middle.add_column(ratio=2)
        middle.add_row(plan, usage)
        parts.append(middle)
    parts.extend((_timeline_panel(view), _footer(notice)))
    return Group(*parts)


def _header(view: JobView) -> Panel:
    table = Table.grid(expand=True)
    table.add_column(no_wrap=True)
    table.add_column(ratio=1)
    table.add_column(justify="right", no_wrap=True)
    if view.connection_state == "reconnecting":
        state: RenderableType = Spinner("dots", text="重连中", style="yellow")
    elif view.status == "completed":
        state = Text("✔ 已完成", style="bold green")
    elif view.status == "failed":
        state = Text("✘ 失败", style="bold red")
    elif view.status == "cancelled":
        state = Text("⊘ 已取消", style="bold yellow")
    elif view.status == "cancelling":
        state = Spinner("dots", text="正在取消", style="yellow")
    elif view.status == "queued":
        state = Text("○ 排队中", style="yellow")
    else:
        state = Spinner("dots", text="研究中", style="cyan")
    table.add_row(
        state,
        Text(f"  {str(view.job_id)[:8]}   {view.question}", overflow="ellipsis"),
        Text(f"{view.effort} · {view.language}", style="dim"),
    )
    return Panel(table, title="Prospector", border_style="cyan")


def _phase_track(view: JobView) -> Text:
    track = Text("  ")
    for index, label in enumerate(PHASE_LABELS):
        if index:
            track.append(" ─── ", style="dim")
        if view.status == "failed" and index == view.phase_index:
            track.append(f"✘ {label}", style="bold red")
        elif index < view.phase_index or view.status == "completed":
            track.append(f"✔ {label}", style="green")
        elif index == view.phase_index:
            track.append(f"◉ {label}", style="bold cyan")
        else:
            track.append(f"○ {label}", style="dim")
    return track


def _plan_panel(view: JobView) -> Panel:
    rows = Table.grid(expand=True, padding=(0, 1))
    rows.add_column(no_wrap=True)
    rows.add_column(ratio=1)
    rows.add_column(no_wrap=True)
    if view.plan_reason:
        rows.add_row("", Text(view.plan_reason, style="magenta"), "")
    if not view.tasks:
        rows.add_row("", Text("等待 Planner 派发任务", style="dim"), "")
    for index, task in enumerate(view.tasks.values(), start=1):
        rows.add_row(
            f"T{index}",
            Text(task.question, overflow="ellipsis"),
            _task_status(task),
        )
        if task.status == "running":
            rows.add_row(
                "",
                Text(
                    f"└ Worker 轮数 {_bar(task.rounds_used, task.rounds_limit, 10)} "
                    f"{task.rounds_used}/{task.rounds_limit}",
                    style="cyan",
                ),
                "",
            )
    return Panel(rows, title=f"Plan v{view.plan_version}", border_style="magenta")


def _task_status(task: ViewTask) -> Text:
    if task.status == "running":
        return Text("◉", style="cyan")
    if task.status == "done":
        return Text("✔", style="green")
    if task.status == "failed":
        return Text("✘", style="red")
    return Text("○", style="dim")


def _usage_panel(view: JobView) -> Panel:
    rows = Table.grid(expand=True)
    rows.add_column()
    rows.add_column(justify="right")
    running = view.running_tasks()
    limit = _concurrency_limit(view)
    rows.add_row("并发", f"{_bar(running, limit, 10)}  {running}/{limit}")
    tokens = view.total_tokens()
    rows.add_row("tokens", "暂无记录" if tokens is None else _compact_number(tokens))
    rows.add_row("工具调用", str(view.total_tool_calls()))
    rows.add_row("已运行", _duration(view.elapsed_seconds()))
    return Panel(rows, title="限额与用量", border_style="cyan")


def _timeline_panel(view: JobView) -> Panel:
    rows = Table.grid(expand=True)
    rows.add_column(style="dim", no_wrap=True)
    rows.add_column(ratio=1)
    if not view.timeline:
        rows.add_row("--:--:--", Text("等待研究事件", style="dim"))
    for entry in view.timeline:
        rows.add_row(entry.created_at, _timeline_text(entry))
    return Panel(rows, title="时间线")


def _timeline_text(entry: TimelineEntry) -> Text:
    style = ""
    if entry.event_type == "task.tool_used":
        style = "dim"
    elif entry.event_type == "verifier.completed":
        style = "yellow"
    elif entry.event_type == "replan.triggered":
        style = "magenta"
    elif "失败" in entry.line:
        style = "red"
    return Text(entry.line, style=style)


def _footer(notice: str | None) -> Text:
    footer = Text("Ctrl-C 离开（任务继续）   x 终止 Job", style="dim")
    if notice is not None:
        footer.append(f"   {notice}", style="yellow")
    footer.append(" " * 6)
    footer.append("prospector · v0.1.0", style="dim")
    return footer


def _concurrency_limit(view: JobView) -> int:
    limits = limits_for_effort(view.effort)  # type: ignore[arg-type]
    return limits.max_concurrency


def _bar(value: int, limit: int, width: int) -> str:
    filled = 0 if limit <= 0 else min(width, round(width * value / limit))
    return "▰" * filled + "▱" * (width - filled)


def _duration(seconds: int) -> str:
    minutes, second = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}"


def _compact_number(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)
