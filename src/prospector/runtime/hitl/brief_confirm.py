"""Brief confirmation HITL for interactive CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from prospector.schemas.brief import ResearchBrief, UserConstraints

EchoFn = Callable[[str], None]
PromptFn = Callable[[str], str]
EditFn = Callable[[ResearchBrief], ResearchBrief]
ReviseOnceFn = Callable[[ResearchBrief, str], ResearchBrief]

DEEP_EFFORT_HINT = "深度档最坏可能运行数小时；单次模型/抓取调用仍有超时上限。"

_YAML_KEYS = ("question", "brief_text", "effort", "language", "output_format")
_CONSTRAINT_KEY = "user_constraints"
_CONSTRAINT_SCALARS = ("time_range",)
_CONSTRAINT_LISTS = (
    "regions",
    "comparison_targets",
    "source_rules",
    "exclusions",
    "deliverable_rules",
)
_CONSTRAINT_LABELS = {
    "time_range": "时间范围",
    "regions": "地域",
    "comparison_targets": "比较对象",
    "source_rules": "来源要求",
    "exclusions": "排除",
    "deliverable_rules": "输出要求",
}
_LABEL_WIDTH = 14  # display width of left-hand field labels


class BriefConfirmAborted(Exception):
    """User abandoned Brief confirmation, or the terminal is non-interactive."""

    def __init__(
        self,
        message: str,
        *,
        reason: Literal["user_aborted", "tty_required", "editor_failed"] = "user_aborted",
    ) -> None:
        super().__init__(message)
        self.reason = reason


def require_tty() -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise BriefConfirmAborted(
            "Brief 确认需要交互终端（TTY）",
            reason="tty_required",
        )


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in {"F", "W"}:
            width += 2
        elif unicodedata.category(ch) in {"Mn", "Me", "Cf"}:
            continue
        else:
            width += 1
    return width


def _pad_to_width(text: str, width: int) -> str:
    pad = width - _display_width(text)
    if pad < 0:
        return text
    return text + (" " * pad)


def _wrap_display(text: str, width: int) -> list[str]:
    """Wrap ``text`` to at most ``width`` terminal columns (CJK-aware)."""
    if width < 1:
        return [text] if text else [""]
    if not text:
        return [""]
    lines: list[str] = []
    current = ""
    current_w = 0
    for ch in text:
        ch_w = _display_width(ch)
        if current and current_w + ch_w > width:
            lines.append(current)
            current = ch
            current_w = ch_w
        else:
            current += ch
            current_w += ch_w
    if current or not lines:
        lines.append(current)
    return lines


def _card_width(*, width: int | None = None) -> int:
    if width is not None:
        return max(width, 40)
    # Leave one column free: a line whose display width equals the terminal
    # width often wraps, leaving a stray fragment on the next row.
    cols = max(shutil.get_terminal_size((88, 24)).columns - 1, 40)
    return max(min(cols, 100), 56)


CONSTRAINTS_HEADING = "你明确要求的限制（不可协商）："


def constraint_lines(constraints: UserConstraints) -> list[str]:
    """Label each stated limit for display; empty when the user stated none.

    Shared by every surface that shows a Brief to the user, so a new surface cannot
    quietly drop the binding half of it.
    """
    if constraints.is_empty():
        return []
    lines = [CONSTRAINTS_HEADING]
    for key in (*_CONSTRAINT_SCALARS, *_CONSTRAINT_LISTS):
        value = getattr(constraints, key)
        rendered = value if isinstance(value, str) else "、".join(value)
        if rendered:
            lines.append(f"  {_CONSTRAINT_LABELS[key]}：{rendered}")
    return lines


def format_brief_card(
    brief: ResearchBrief,
    *,
    width: int | None = None,
    revision_used: bool = False,
) -> str:
    """Render a closed box card; wraps long lines so left/right borders stay intact.

    Title sits *inside* the box (not embedded in the top border). Mixing CJK
    with ``─`` in the top rule under-counts width on many terminals and wraps
    a ghost fragment past the right corner.
    """
    outer = _card_width(width=width)
    inner = max(outer - 4, 20)  # content between "│ " and " │"
    top = "┌" + ("─" * (outer - 2)) + "┐"
    divider = "├" + ("─" * (outer - 2)) + "┤"
    bottom = "└" + ("─" * (outer - 2)) + "┘"

    def row(content: str = "") -> str:
        return "│ " + _pad_to_width(content, inner) + " │"

    lines = [top]
    lines.append(row("Research Brief"))
    lines.append(row("确认后作为本次任务输入快照"))
    lines.append(divider)

    fields = [
        ("question", brief.question),
        ("effort", brief.effort),
        ("language", brief.language),
        ("output_format", brief.output_format),
    ]
    for label, value in fields:
        prefix = f"{label:<{_LABEL_WIDTH}} "
        hang = " " * _display_width(prefix)
        value_width = max(inner - _display_width(prefix), 8)
        wrapped = _wrap_display(value, value_width)
        lines.append(row(prefix + wrapped[0]))
        for cont in wrapped[1:]:
            lines.append(row(hang + cont))

    # Shown before brief_text and labelled as binding: the user needs to see what the
    # run will treat as non-negotiable, separately from directions Scope merely proposed.
    constraint_rows = constraint_lines(brief.user_constraints)
    if constraint_rows:
        lines.append(row())
        for entry in constraint_rows:
            hang = " " * (len(entry) - len(entry.lstrip()))
            for index, piece in enumerate(_wrap_display(entry, inner)):
                lines.append(row(piece if index == 0 else hang + piece))

    lines.append(row())
    lines.append(row("brief_text:"))
    for paragraph in brief.brief_text.splitlines() or [brief.brief_text]:
        if not paragraph.strip():
            lines.append(row())
            continue
        for piece in _wrap_display(paragraph, inner):
            lines.append(row(piece))

    lines.append(bottom)
    if brief.effort == "deep":
        lines.append(DEEP_EFFORT_HINT)
    instruct = "[i] 指令修订（已用完）" if revision_used else "[i] 指令修订（限一轮）"
    lines.append(f"  [c] 确认  [e] 直接编辑  {instruct}  [q] 放弃")
    return "\n".join(lines)


def _escape_yaml_scalar(value: str) -> str:
    if value == "" or any(ch in value for ch in ":#{}[]&*!|>'\"%@`") or value.strip() != value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _brief_to_yaml(brief: ResearchBrief) -> str:
    """Serialize ResearchBrief to a small, editable YAML subset (no PyYAML dep)."""
    lines: list[str] = []
    for key in _YAML_KEYS:
        value = getattr(brief, key)
        assert isinstance(value, str)
        if key == "brief_text" and ("\n" in value or len(value) > 80):
            lines.append("brief_text: |")
            for row in value.splitlines() or [""]:
                lines.append(f"  {row}")
        else:
            lines.append(f"{key}: {_escape_yaml_scalar(value)}")

    constraints = brief.user_constraints
    lines.append(f"{_CONSTRAINT_KEY}:")
    lines.append("  # 只写你自己提出的限制；留空表示没有该项限制")
    for key in _CONSTRAINT_SCALARS:
        lines.append(f"  {key}: {_escape_yaml_scalar(getattr(constraints, key))}")
    for key in _CONSTRAINT_LISTS:
        entries: list[str] = getattr(constraints, key)
        if not entries:
            lines.append(f"  {key}: []")
            continue
        lines.append(f"  {key}:")
        lines.extend(f"    - {_escape_yaml_scalar(entry)}" for entry in entries)
    return "\n".join(lines) + "\n"


def _parse_constraint_block(lines: list[str], start: int) -> tuple[dict[str, object], int]:
    """Read the two-space-indented user_constraints mapping starting after ``start``."""
    data: dict[str, object] = {}
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if not line.startswith("  ") or line.startswith("    "):
            break
        key, _, rest = line.strip().partition(":")
        key = key.strip()
        rest = rest.strip()
        if key not in (*_CONSTRAINT_SCALARS, *_CONSTRAINT_LISTS):
            raise ValueError(f"unknown user_constraints field: {key}")
        i += 1
        if key in _CONSTRAINT_SCALARS:
            data[key] = _unescape_yaml_scalar(rest)
            continue
        if rest and rest != "[]":
            raise ValueError(f"{key} must be a YAML list or []")
        entries: list[str] = []
        while i < len(lines):
            item = lines[i]
            if not item.strip() or item.lstrip().startswith("#"):
                i += 1
                continue
            if not item.startswith("    - "):
                break
            entries.append(_unescape_yaml_scalar(item.strip()[2:]))
            i += 1
        data[key] = entries
    return data, i


def _unescape_yaml_scalar(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        inner = text[1:-1]
        if text[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return text


def _brief_from_yaml(text: str) -> ResearchBrief:
    """Parse the Brief YAML subset produced by `_brief_to_yaml`."""
    data: dict[str, object] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            raise ValueError(f"invalid YAML line: {line!r}")
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if key == _CONSTRAINT_KEY:
            if rest:
                raise ValueError("user_constraints must be a nested block")
            data[key], i = _parse_constraint_block(lines, i + 1)
            continue
        if key not in _YAML_KEYS:
            raise ValueError(f"unknown Brief field: {key}")
        if rest in {"|", ">"}:
            block: list[str] = []
            i += 1
            while i < len(lines):
                row = lines[i]
                if row.startswith("  "):
                    block.append(row[2:])
                    i += 1
                    continue
                if row.strip() == "":
                    block.append("")
                    i += 1
                    continue
                break
            data[key] = "\n".join(block).rstrip("\n")
            continue
        data[key] = _unescape_yaml_scalar(rest)
        i += 1
    return ResearchBrief.model_validate(data)


def _default_open_editor(path: Path) -> None:
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    result = subprocess.run([editor, str(path)], check=False)
    if result.returncode != 0:
        raise BriefConfirmAborted(
            f"editor exited with code {result.returncode}",
            reason="editor_failed",
        )


def edit_brief(
    brief: ResearchBrief,
    *,
    open_editor: Callable[[Path], None] | None = None,
    work_dir: Path | None = None,
    echo: EchoFn | None = None,
) -> ResearchBrief:
    """Open Brief as YAML in an editor; retry until schema validation passes."""
    opener = open_editor or _default_open_editor
    say = echo or (lambda _msg: None)
    while True:
        if work_dir is not None:
            path = work_dir / "brief_edit.yaml"
            path.write_text(_brief_to_yaml(brief), encoding="utf-8")
            opener(path)
            raw = path.read_text(encoding="utf-8")
        else:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".yaml",
                prefix="prospector-brief-",
                delete=False,
                encoding="utf-8",
            ) as handle:
                handle.write(_brief_to_yaml(brief))
                path = Path(handle.name)
            try:
                opener(path)
                raw = path.read_text(encoding="utf-8")
            finally:
                path.unlink(missing_ok=True)
        try:
            return _brief_from_yaml(raw)
        except (ValidationError, ValueError) as exc:
            say(f"Brief 校验失败，请重新编辑：{exc}")


def confirm_brief(
    brief: ResearchBrief,
    *,
    prompt: PromptFn,
    revise_once_fn: ReviseOnceFn,
    edit_fn: EditFn | None = None,
    echo: EchoFn | None = None,
) -> ResearchBrief:
    """Run c/e/i/q confirmation.

    The model may revise once, but the revised Brief comes back for review rather than
    going straight to a run: seconds of rereading against a job that can take hours is
    the cheapest confirmation in the pipeline. Hand editing stays unlimited — that is
    the user's own typing, and it costs no model call.
    """
    say = echo or (lambda _msg: None)
    do_edit = edit_fn or (lambda b: edit_brief(b, echo=say))
    current = brief
    revision_used = False

    while True:
        say(format_brief_card(current, revision_used=revision_used))
        choice = prompt("选择").strip().lower()
        if choice == "c":
            return current
        if choice == "q":
            raise BriefConfirmAborted("用户放弃 Brief 确认", reason="user_aborted")
        if choice == "e":
            current = do_edit(current)
            continue
        if choice == "i":
            if revision_used:
                say("本次只允许一轮模型修订，已经用过了。可以 [e] 手工编辑，或 [c] 确认。")
                continue
            note = prompt("修订指令").strip()
            if not note:
                say("修订指令不能为空")
                continue
            current = revise_once_fn(current, note)
            revision_used = True
            say("已按指令修订，请复看后再确认。")
            continue
        say("请输入 c / e / i / q")
