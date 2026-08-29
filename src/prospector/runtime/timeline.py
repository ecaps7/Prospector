"""Human-readable rendering and live following for persisted research events."""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol
from uuid import UUID

from prospector.deterministic.budget import ResearchLimits

POLL_INTERVAL_SECONDS = 0.2
TERMINAL_PHASES = {"draft_rendered", "report_rendered", "failed"}

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
_BRANCH_GAP_RE = re.compile(r"^  [├└]─ ")
_BRANCH_CONT_RE = re.compile(r"^  │")
_GLOBAL_PREFIX_RE = re.compile(r"^\[(?:研究|核验|重规划|轮\s+\d+)\]")


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

    if _BRANCH_GAP_RE.match(line) or _BRANCH_CONT_RE.match(line):
        if "重大" in line:
            return f"{_DARK_RED}{line}{_RESET}"
        return f"{_DARK_ORANGE}{line}{_RESET}"

    if _GLOBAL_PREFIX_RE.match(line):
        if "失败" in line or "不通过" in line:
            return f"{_BOLD}{_DARK_RED}{line}{_RESET}"
        if "通过" in line or "收工" in line:
            return f"{_BOLD}{_DARK_GREEN}{line}{_RESET}"
        return f"{_BOLD}{_NEAR_BLACK}{line}{_RESET}"

    return line


def emit_timeline_line(line: str) -> None:
    """Print one timeline line to stdout with optional ANSI coloring."""
    print(colorize_timeline_line(line), flush=True)


class TimelineTask(Protocol):
    @property
    def question(self) -> str: ...


class TimelineRenderRepository(Protocol):
    def get_task(self, task_id: UUID) -> TimelineTask: ...


class TimelineRepository(TimelineRenderRepository, Protocol):
    def list_events_after(self, job_id: UUID, after_id: int) -> list[dict[str, Any]]: ...


_STOP_REASON_LABELS = {
    "expected_evidence_satisfied": "证据目标满足",
    "budget_exhausted": "工具预算耗尽",  # legacy rows only; new runs use rounds
    "worker_rounds_exhausted": "Worker 决策轮耗尽",
    "no_public_evidence": "未发现公开证据",
    "low_information_gain": "连续两批未产生新证据",
    "repeating_without_progress": "连续重复同一组检索且未落库",
    "blocked_by_scope": "受任务范围限制",
    "tool_error": "运行时错误",
}

_REJECTION_LABELS = {
    "over_concurrency": "派发任务超过并发上限",
    "schema_error": "输出格式不合法",
    "empty_finish": "尚无证据，不能结束研究",
}

_GAP_KIND_LABELS = {
    "plan_coverage": "覆盖",
    "brief_alignment": "Brief对齐",
    "conflict": "冲突",
    "source_credibility": "来源可信度",
}

_GAP_SEVERITY_LABELS = {
    "major": "重大",
    "minor": "次要",
}

_CONFLICT_DECISION_LABELS = {
    "present_both": "并陈",
    "adjudicated": "裁决",
}

_VERIFIER_TRIGGER_LABELS = {
    "planner_finish": "Planner finish",
    "budget_exhausted": "决策轮耗尽",
    "synthesis_gap": "研究综合请求补研究",
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

    def __init__(self, repository: TimelineRenderRepository, limits: ResearchLimits) -> None:
        self.repository = repository
        self.limits = limits
        self._task_labels: dict[str, str] = {}
        # decision_round in the event payload is the storage key, which also advances on
        # malformed output, so it no longer equals the research budget spent. The runtime
        # writes the authoritative count into the payload; this mirrors the latest one it
        # has seen so events that predate the field (or omit it) still render a number.
        self._research_decisions_used = 0

    def _task_label(self, task_id: object) -> str:
        key = str(task_id)
        label = self._task_labels.get(key)
        if label is None:
            label = f"T{len(self._task_labels) + 1}"
            self._task_labels[key] = label
        return label

    def register_tasks(self, task_ids: Iterable[object]) -> None:
        """Bind task labels in the same stable order used by the task snapshot."""
        for task_id in task_ids:
            self._task_label(task_id)

    def _track_research_decisions(self, payload: Mapping[str, Any]) -> None:
        reported = payload.get("research_decisions_used")
        if reported is None:
            self._research_decisions_used += 1
        else:
            self._research_decisions_used = int(reported)

    def _remaining_rounds(self) -> int:
        return max(0, self.limits.decision_round_limit - self._research_decisions_used)

    def render(self, event: dict[str, Any]) -> list[str]:
        event_type = str(event["event_type"])
        payload = dict(event.get("payload") or {})

        if event_type == "brief.confirmed":
            return [f"[研究] Brief 已确认（{payload.get('effort', 'unknown')}）"]

        if event_type == "job.phase_changed":
            return self._render_phase(payload)

        if event_type == "planner.started":
            return [f"[轮 {int(payload['decision_round'])}] Planner 开始制定计划"]

        if event_type == "planner.decided":
            return self._render_planner_decision(payload)

        if event_type == "planner.rejected":
            decision_round = int(payload["decision_round"])
            reason_code = str(payload.get("reason_code") or "")
            reason = _REJECTION_LABELS.get(reason_code, reason_code)
            if reason_code == "schema_error":
                return [f"[轮 {decision_round}] Planner 输出格式不合法，重试（不计研究轮）"]
            self._track_research_decisions(payload)
            remaining = self._remaining_rounds()
            return [f"[轮 {decision_round}] Planner 决策被拒绝：{reason}（余 {remaining} 轮）"]

        if event_type == "verifier.completed":
            return self._render_verifier_completed(event, payload)

        if event_type == "replan.triggered":
            return [
                f"[重规划] Verifier {_short_id(payload.get('verifier_run_id'))} "
                f"触发 Plan v{int(payload['plan_version'])}"
            ]

        if event_type == "report.draft_rendered":
            lines = ["[成文] 报告已渲染：" + str(payload.get("markdown_ref") or "")]
            structure = payload.get("structure") or {}
            if structure:
                lines.append(
                    "[成文] 正文结构：事实句 {evidence} / 推理句 {derived}"
                    "，最长连续事实句 {run}，无判断段落 {bare}/{paragraphs}".format(
                        evidence=structure.get("evidence_count", 0),
                        derived=structure.get("derived_count", 0),
                        run=structure.get("longest_evidence_run", 0),
                        bare=structure.get("paragraphs_without_derived", 0),
                        paragraphs=structure.get("paragraph_count", 0),
                    )
                )
            return lines

        task_id = payload.get("task_id") or event.get("task_id")
        if task_id is None:
            return []
        task_label = self._task_label(task_id)

        if event_type == "task.started":
            budget = dict(payload.get("budget") or {})
            round_limit = int(budget.get("max_worker_rounds", 0))
            return [f"[{task_label}] 开始调查（Worker 决策轮预算 {round_limit} 轮）"]

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

    def _render_verifier_completed(
        self, event: dict[str, Any], payload: dict[str, Any]
    ) -> list[str]:
        reported = payload.get("research_decisions_used")
        if reported is not None:
            self._research_decisions_used = int(reported)
        plan_version = int(payload["plan_version"])
        major_count = int(payload.get("major_gap_count", 0))
        minor_count = int(payload.get("minor_gap_count", 0))
        conflict_count = int(payload.get("conflict_resolution_count", 0))
        unusable_count = int(payload.get("unusable_assertion_count", 0))
        reason = _first_line(payload.get("decision_reason"))
        gap_summaries = list(payload.get("gap_summaries") or [])
        conflict_summaries = list(payload.get("conflict_summaries") or [])
        unusable_summaries = list(payload.get("unusable_summaries") or [])

        if payload.get("release_decision") == "pass":
            lines = [
                f"[核验] Plan v{plan_version} 通过"
                f"（重大缺口 {major_count}，冲突裁决 {conflict_count}"
                f"，废证 {unusable_count}）"
            ]
            if reason:
                lines.append(f"[核验] 收工：{reason}")
            if unusable_summaries:
                lines.extend(
                    self._render_unusable_tree(unusable_summaries, total_count=unusable_count)
                )
            return lines

        remaining = self._remaining_rounds()
        header = (
            f"[核验] Plan v{plan_version} 不通过：{major_count} 个重大缺口"
            f"（次要 {minor_count}，冲突 {conflict_count}，废证 {unusable_count}）"
        )
        lines = [header]
        lines.extend(self._render_gap_tree(gap_summaries))
        if unusable_summaries:
            lines.extend(self._render_unusable_tree(unusable_summaries, total_count=unusable_count))
        if conflict_summaries:
            points = "；".join(
                f"{_CONFLICT_DECISION_LABELS.get(str(item.get('decision')), item.get('decision'))}"
                f"「{_first_line(item.get('disputed_point'))}」"
                for item in conflict_summaries
            )
            lines.append(f"[核验] 冲突处理：{points}")
        if remaining == 0:
            lines.append(
                "[核验] 失败：重大缺口且 Planner 决策轮已耗尽" + (f"：{reason}" if reason else "")
            )
        else:
            lines.append(
                f"[核验] 返回 Planner 补查（余 {remaining} 轮）" + (f"：{reason}" if reason else "")
            )
        return lines

    @staticmethod
    def _render_gap_tree(gap_summaries: list[Any]) -> list[str]:
        if not gap_summaries:
            return []
        lines: list[str] = []
        for index, gap in enumerate(gap_summaries):
            item = dict(gap or {})
            branch = "└─" if index == len(gap_summaries) - 1 else "├─"
            severity = _GAP_SEVERITY_LABELS.get(
                str(item.get("severity") or ""), str(item.get("severity") or "")
            )
            kind = _GAP_KIND_LABELS.get(str(item.get("kind") or ""), str(item.get("kind") or ""))
            description = _first_line(item.get("description"))
            lines.append(f"  {branch} {severity}·{kind}：{description}")
            evidence_needed = _first_line(item.get("evidence_needed"))
            if evidence_needed:
                if index == len(gap_summaries) - 1:
                    lines.append(f"        待补证据：{evidence_needed}")
                else:
                    lines.append(f"  │     待补证据：{evidence_needed}")
        return lines

    @staticmethod
    def _render_unusable_tree(unusable_summaries: list[Any], *, total_count: int) -> list[str]:
        if not unusable_summaries:
            return []
        displayed_count = len(unusable_summaries)
        if total_count > displayed_count:
            header = f"[核验] 废证 {total_count} 条（以下展示 {displayed_count} 条）："
        else:
            header = f"[核验] 废证 {total_count} 条："
        lines = [header]
        for index, item in enumerate(unusable_summaries):
            row = dict(item or {})
            branch = "└─" if index == len(unusable_summaries) - 1 else "├─"
            assertion_id = _short_id(row.get("assertion_id"))
            reason = _first_line(row.get("reason"))
            lines.append(f"  {branch} {assertion_id}：{reason}")
        return lines

    def _render_phase(self, payload: dict[str, Any]) -> list[str]:
        phase = str(payload.get("phase") or "")
        if phase == "research":
            return ["[研究] 开始"]
        if phase == "verifier":
            plan_version = payload.get("plan_version")
            trigger = str(payload.get("trigger") or "")
            trigger_label = _VERIFIER_TRIGGER_LABELS.get(trigger, trigger or "未知")
            plan_part = f"Plan v{plan_version}，" if plan_version is not None else ""
            return [f"[研究] 研究阶段结束，等待核验（{plan_part}触发：{trigger_label}）"]
        if phase == "composition_pending":
            return ["[综合] Research Verifier 已放行，等待 Research Synthesis"]
        if phase == "writing":
            return ["[成文] Writer 正在组织深度研究报告"]
        if phase == "verifying":
            return ["[核验] Report Verifier 正在逐句验证"]
        if phase == "revising":
            return ["[成文] 存在未通过语句，Writer 正在修订"]
        if phase == "verified":
            return ["[成文] 逐句验证全部通过"]
        if phase == "revisions_exhausted":
            return ["[成文] 修订轮次已用尽，仍有未通过语句；报告将标记为部分通过"]
        if phase == "rendering":
            return ["[成文] 正在渲染最终报告"]
        if phase == "composition":
            return ["[成文] 研究综合完成，开始写作"]
        if phase == "attributing":
            return ["[成文] 正在为正文寻找出处"]
        if phase == "reviewing":
            return ["[成文] 正在通读全文审阅"]
        if phase == "partial":
            return ["[成文] 部分内容未获事实支持，报告仍会交付"]
        if phase == "report_failed":
            return ["[成文] 核验未通过，报告仍会随核验结论交付"]
        if phase in {"draft_rendered", "report_rendered"}:
            return ["[成文] 报告渲染完成"]
        if phase == "failed":
            error_code = str(payload.get("error_code") or "unknown_error")
            if error_code == "research_budget_exhausted_without_evidence":
                return ["[研究] 失败：预算耗尽且没有保存任何证据"]
            if error_code == "verifier_major_gap":
                return ["[核验] 失败：存在重大缺口且 Planner 决策轮已耗尽"]
            if error_code == "planner_schema_error_limit":
                return ["[研究] 失败：Planner 连续多次输出非法格式"]
            return [f"[研究] 失败：{error_code}"]
        return []

    def _render_planner_decision(self, payload: dict[str, Any]) -> list[str]:
        decision_round = int(payload["decision_round"])
        decision = str(payload.get("decision") or "")
        self._track_research_decisions(payload)
        if decision == "dispatch":
            task_ids = [str(value) for value in payload.get("task_ids") or []]
            remaining = self._remaining_rounds()
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
        if decision == "finish":
            return [f"[轮 {decision_round}] Planner finish：{_first_line(payload.get('reason'))}"]
        return []

    @staticmethod
    def _render_tool_event(task_label: str, payload: dict[str, Any]) -> list[str]:
        tool = str(payload.get("tool") or "unknown_tool")
        if payload.get("error"):
            raw = str(payload["error"]).strip()
            head, *rest = raw.splitlines()
            lines = [f"[{task_label}] {tool} 失败：{head.strip()}"]
            for extra in rest:
                stripped = extra.strip()
                if stripped:
                    lines.append(f"[{task_label}]   {stripped}")
            return lines
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
