"""Human-readable rendering and live following for persisted research events."""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID

from prospector.deterministic.budget import ResearchLimits
from prospector.schemas.plan import ResearchTask

POLL_INTERVAL_SECONDS = 0.2
TERMINAL_PHASES = {"verifier_pending", "failed"}

EmitLine = Callable[[str], None]

_RESET = "\033[0m"
_BOLD = "\033[1m"
# 256-color dark hues — readable on light terminal backgrounds.
_DARK_CYAN = "\033[38;5;30m"
_DARK_MAGENTA = "\033[38;5;90m"
_DARK_ORANGE = "\033[38;5;130m"
_DARK_BLUE = "\033[38;5;25m"
_DARK_GREEN = "\033[38;5;28m"
_DARK_RED = "\033[38;5;124m"
_NEAR_BLACK = "\033[38;5;232m"

_TASK_COLORS = (_DARK_CYAN, _DARK_MAGENTA, _DARK_ORANGE, _DARK_BLUE, _DARK_GREEN)
_TASK_PREFIX_RE = re.compile(r"^\[T(\d+)\]")
_BRANCH_TASK_RE = re.compile(r"^  [├└]─ T(\d+)\b")
_GLOBAL_PREFIX_RE = re.compile(r"^\[(?:研究|轮\s+\d+)\]")


def _timeline_colors_enabled() -> bool:
    if os.environ.get("NO_COLOR", ""):
        return False
    if os.environ.get("FORCE_COLOR", ""):
        return True
    return sys.stdout.isatty()


def _task_color(index: int) -> str:
    return _TASK_COLORS[(index - 1) % len(_TASK_COLORS)]


def colorize_timeline_line(line: str, *, colors: bool | None = None) -> str:
    """Apply terminal colors to a rendered timeline line.

    Task lines keep a stable dark hue by ``[Tn]`` index. Failures override to
    dark red; evidence drops use dark green; finish lines stay on the task hue
    but bold. Job / planner lines use near-black so they stay readable on light
    backgrounds without competing with worker noise.
    """
    enabled = _timeline_colors_enabled() if colors is None else colors
    if not enabled or not line:
        return line

    task_match = _TASK_PREFIX_RE.match(line)
    if task_match is not None:
        task_color = _task_color(int(task_match.group(1)))
        if "失败" in line:
            return f"{_DARK_RED}{line}{_RESET}"
        if "落证" in line:
            return f"{_DARK_GREEN}{line}{_RESET}"
        if "收工" in line:
            return f"{_BOLD}{task_color}{line}{_RESET}"
        return f"{task_color}{line}{_RESET}"

    branch_match = _BRANCH_TASK_RE.match(line)
    if branch_match is not None:
        return f"{_task_color(int(branch_match.group(1)))}{line}{_RESET}"

    if _GLOBAL_PREFIX_RE.match(line):
        if "失败" in line:
            return f"{_BOLD}{_DARK_RED}{line}{_RESET}"
        return f"{_BOLD}{_NEAR_BLACK}{line}{_RESET}"

    return line


def emit_timeline_line(line: str) -> None:
    """Print one timeline line to stdout with optional ANSI coloring."""
    print(colorize_timeline_line(line), flush=True)


class TimelineRepository(Protocol):
    def get_task(self, task_id: UUID) -> ResearchTask: ...

    def list_events_after(self, job_id: UUID, after_id: int) -> list[dict[str, Any]]: ...


_STAGE_LABELS = {
    "scout": "探索",
    "deep_dive": "深挖",
    "verify": "核验",
}

_MODE_LABELS = {
    "factual": "事实核验",
    "comparison": "对比",
    "counterargument": "反证",
    "risk_scan": "风险扫描",
    "timeline": "时间线",
}

_STOP_REASON_LABELS = {
    "expected_evidence_satisfied": "证据目标满足",
    "budget_exhausted": "工具预算耗尽",  # legacy rows only; new runs use rounds
    "worker_rounds_exhausted": "Worker 决策轮耗尽",
    "no_public_evidence": "未发现公开证据",
    "low_information_gain": "连续两批未产生新证据",
    "blocked_by_scope": "受任务范围限制",
    "tool_error": "运行时错误",
}

_REJECTION_LABELS = {
    "over_concurrency": "派发任务超过并发上限",
    "over_scope": "单任务申报对象超出工具预算可覆盖范围",
    "schema_error": "输出格式不合法",
    "empty_finish": "尚无证据，不能结束研究",
}


def _first_line(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.splitlines()[0].strip()


def _short_id(value: object) -> str:
    return str(value or "")[:8]


class ResearchTimelineRenderer:
    """Render the append-only event ledger into stable per-task progress lines."""

    def __init__(self, repository: TimelineRepository, limits: ResearchLimits) -> None:
        self.repository = repository
        self.limits = limits
        self._task_labels: dict[str, str] = {}

    def _task_label(self, task_id: object) -> str:
        key = str(task_id)
        label = self._task_labels.get(key)
        if label is None:
            label = f"T{len(self._task_labels) + 1}"
            self._task_labels[key] = label
        return label

    def _remaining_rounds(self, decision_round: int) -> int:
        return max(0, self.limits.decision_round_limit - decision_round)

    def render(self, event: dict[str, Any]) -> list[str]:
        event_type = str(event["event_type"])
        payload = dict(event.get("payload") or {})

        if event_type == "brief.confirmed":
            return [f"[研究] Brief 已确认（{payload.get('effort', 'unknown')}）"]

        if event_type == "job.phase_changed":
            return self._render_phase(payload)

        if event_type == "planner.decided":
            return self._render_planner_decision(payload)

        if event_type == "planner.rejected":
            decision_round = int(payload["decision_round"])
            reason_code = str(payload.get("reason_code") or "")
            reason = _REJECTION_LABELS.get(reason_code, reason_code)
            remaining = self._remaining_rounds(decision_round)
            return [f"[轮 {decision_round}] Planner 决策被拒绝：{reason}（余 {remaining} 轮）"]

        task_id = payload.get("task_id") or event.get("task_id")
        if task_id is None:
            return []
        task_label = self._task_label(task_id)

        if event_type == "task.started":
            stage = _STAGE_LABELS.get(str(payload.get("research_stage")), "未知")
            mode = _MODE_LABELS.get(
                str(payload.get("research_mode")), str(payload.get("research_mode") or "未知")
            )
            budget = dict(payload.get("budget") or {})
            round_limit = int(budget.get("max_worker_rounds", 0))
            return [
                f"[{task_label}] 开始：{stage}阶段 / {mode}（Worker 决策轮预算 {round_limit} 轮）"
            ]

        if event_type == "task.tool_used":
            return self._render_tool_event(task_label, payload)

        if event_type == "task.evidence_saved":
            assertion_count = len(payload.get("assertion_ids") or [])
            excerpt_count = int(payload.get("excerpt_count", 0))
            return [f"[{task_label}] 落证 {assertion_count} 条断言（{excerpt_count} 段原文）"]

        if event_type == "task.finished":
            reason_code = str(payload.get("stop_reason") or "")
            reason = _STOP_REASON_LABELS.get(reason_code, reason_code)
            rounds_used = int(payload.get("rounds_used", payload.get("used", 0)))
            rounds_limit = int(payload.get("rounds_limit", payload.get("limit", 0)))
            tool_calls_used = int(payload.get("tool_calls_used", 0))
            assertion_count = int(payload.get("assertion_count", 0))
            finish_reason = _first_line(payload.get("finish_reason"))
            reason_suffix = f"：{finish_reason}" if finish_reason else ""
            return [
                f"[{task_label}] 收工：{reason}（轮 {rounds_used}/{rounds_limit}，"
                f"工具 {tool_calls_used} 次，累计断言 {assertion_count} 条）{reason_suffix}"
            ]

        return []

    def _render_phase(self, payload: dict[str, Any]) -> list[str]:
        phase = str(payload.get("phase") or "")
        if phase == "research":
            return ["[研究] 开始"]
        if phase == "verifier_pending":
            return ["[研究] 研究阶段结束：等待 Verifier"]
        if phase == "failed":
            error_code = str(payload.get("error_code") or "unknown_error")
            if error_code == "research_budget_exhausted_without_evidence":
                return ["[研究] 失败：预算耗尽且没有保存任何证据"]
            return [f"[研究] 失败：{error_code}"]
        return []

    def _render_planner_decision(self, payload: dict[str, Any]) -> list[str]:
        decision_round = int(payload["decision_round"])
        decision = str(payload.get("decision") or "")
        if decision == "dispatch":
            task_ids = [str(value) for value in payload.get("task_ids") or []]
            remaining = self._remaining_rounds(decision_round)
            reason = _first_line(payload.get("reason"))
            reason_suffix = f"：{reason}" if reason else ""
            lines = [
                f"[轮 {decision_round}] Planner 派发 {len(task_ids)} 个任务"
                f"（Plan v{payload['plan_version']}，余 {remaining} 轮）{reason_suffix}"
            ]
            for index, task_id in enumerate(task_ids):
                task = self.repository.get_task(UUID(task_id))
                label = self._task_label(task_id)
                branch = "└─" if index == len(task_ids) - 1 else "├─"
                lines.append(f"  {branch} {label} {_first_line(task.question)}")
            return lines
        if decision == "reflect":
            return [f"[轮 {decision_round}] Planner reflect：{_first_line(payload.get('note'))}"]
        if decision == "finish":
            return [f"[轮 {decision_round}] Planner finish：{_first_line(payload.get('reason'))}"]
        return []

    @staticmethod
    def _render_tool_event(task_label: str, payload: dict[str, Any]) -> list[str]:
        tool = str(payload.get("tool") or "unknown_tool")
        if payload.get("error"):
            return [f"[{task_label}] {tool} 失败：{_first_line(payload['error'])}"]
        if tool == "web_search":
            query = str(payload.get("query") or "")
            count = int(payload.get("result_count", 0))
            return [f'[{task_label}] 搜索 "{query}" → {count} 条结果']
        if tool == "web_fetch":
            url = str(payload.get("url") or "")
            doc_id = _short_id(payload.get("doc_id"))
            return [f"[{task_label}] 网页快照已保存 {url} → {doc_id}"]
        if tool == "save_findings" and int(payload.get("result_count", 0)) == 0:
            return [f"[{task_label}] 落证：未产生新证据"]
        return []


def drain_timeline(
    repository: TimelineRepository,
    renderer: ResearchTimelineRenderer,
    job_id: UUID,
    after_id: int,
    emit: EmitLine,
) -> tuple[int, bool]:
    """Emit all currently committed events after the cursor exactly once."""
    last_id = after_id
    terminal = False
    for event in repository.list_events_after(job_id, after_id):
        for line in renderer.render(event):
            emit(line)
        last_id = int(event["id"])
        payload = dict(event.get("payload") or {})
        terminal = terminal or (
            event["event_type"] == "job.phase_changed" and payload.get("phase") in TERMINAL_PHASES
        )
    return last_id, terminal


class ResearchTimelineFollower:
    """Poll committed events on a background thread while the research graph runs."""

    def __init__(
        self,
        repository: TimelineRepository,
        renderer: ResearchTimelineRenderer,
        job_id: UUID,
        *,
        after_id: int,
        emit: EmitLine,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self.repository = repository
        self.renderer = renderer
        self.job_id = job_id
        self.last_id = after_id
        self.emit = emit
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="research-timeline", daemon=True)
        self._error: BaseException | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
        if self._error is not None:
            raise RuntimeError("research timeline follower failed") from self._error

    def _poll(self) -> None:
        self.last_id, _ = drain_timeline(
            self.repository,
            self.renderer,
            self.job_id,
            self.last_id,
            self.emit,
        )

    def _run(self) -> None:
        try:
            self._poll()
            while not self._stop.wait(self.poll_interval):
                self._poll()
            self._poll()
        except BaseException as exc:
            self._error = exc


def follow_timeline(
    repository: TimelineRepository,
    renderer: ResearchTimelineRenderer,
    job_id: UUID,
    *,
    emit: EmitLine,
    follow: bool,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Replay a job timeline and optionally poll until research becomes terminal."""
    last_id = 0
    while True:
        last_id, terminal = drain_timeline(repository, renderer, job_id, last_id, emit)
        if not follow or terminal:
            return
        time.sleep(poll_interval)
