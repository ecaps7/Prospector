"""Unit tests for Brief HITL confirmation (no LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prospector.runtime.hitl.brief_confirm import (
    BriefConfirmAborted,
    _display_width,
    confirm_brief,
    edit_brief,
    format_brief_card,
    require_tty,
)
from prospector.schemas.brief import ResearchBrief


def _sample_brief(**overrides: object) -> ResearchBrief:
    data: dict[str, object] = {
        "question": "评估竞品亚太经营态势",
        "brief_text": "具体研究竞品在亚太区的经营态势，检验收缩与扩张等竞争解释。",
        "effort": "standard",
        "language": "zh",
        "output_format": "report_with_citations",
    }
    data.update(overrides)
    return ResearchBrief.model_validate(data)


def test_format_brief_card_includes_core_fields() -> None:
    card = format_brief_card(_sample_brief(), width=72)
    assert "评估竞品亚太经营态势" in card
    assert "standard" in card
    assert "zh" in card
    assert "report_with_citations" in card
    assert "检验收缩与扩张" in card
    assert "[c]" in card and "[e]" in card and "[i]" in card and "[q]" in card
    assert card.startswith("┌")
    assert card.splitlines()[0].endswith("┐")
    assert "Research Brief" in card
    assert "确认后作为本次任务输入快照" in card
    # Title must not be embedded in the top border rule.
    assert "Research Brief" not in card.splitlines()[0]
    assert any(line.startswith("└") and line.endswith("┘") for line in card.splitlines())
    assert any(line.startswith("├") and line.endswith("┤") for line in card.splitlines())


def test_format_brief_card_wraps_long_lines_with_borders() -> None:
    long_text = "生物制药" * 40
    card = format_brief_card(
        _sample_brief(question=long_text, brief_text=long_text),
        width=60,
    )
    box_rows = [
        line
        for line in card.splitlines()
        if line.startswith(("┌", "│", "└"))
    ]
    assert box_rows
    widths = {_display_width(line) for line in box_rows}
    assert widths == {60}, f"inconsistent display widths: {widths}"
    content_rows = [line for line in box_rows if line.startswith("│")]
    assert len(content_rows) > 6


def test_format_brief_card_warns_for_deep_effort() -> None:
    card = format_brief_card(_sample_brief(effort="deep"), width=72)
    assert "数小时" in card


def test_require_tty_rejects_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    with pytest.raises(BriefConfirmAborted, match="TTY|交互"):
        require_tty()


def test_confirm_accepts_current_brief() -> None:
    brief = _sample_brief()
    prompts = iter(["c"])
    result = confirm_brief(
        brief,
        prompt=lambda _msg: next(prompts),
        revise_once_fn=lambda _b, _n: (_ for _ in ()).throw(AssertionError("no revise")),
        edit_fn=lambda _b: (_ for _ in ()).throw(AssertionError("no edit")),
        echo=lambda _msg: None,
    )
    assert result == brief


def test_confirm_abort_raises() -> None:
    with pytest.raises(BriefConfirmAborted):
        confirm_brief(
            _sample_brief(),
            prompt=lambda _msg: "q",
            revise_once_fn=lambda _b, _n: _b,
            edit_fn=lambda _b: _b,
            echo=lambda _msg: None,
        )


def test_confirm_rejects_blank_then_accepts() -> None:
    brief = _sample_brief()
    prompts = iter(["x", "", "c"])
    result = confirm_brief(
        brief,
        prompt=lambda _msg: next(prompts),
        revise_once_fn=lambda _b, _n: _b,
        edit_fn=lambda _b: _b,
        echo=lambda _msg: None,
    )
    assert result == brief


def test_confirm_edit_then_accept() -> None:
    original = _sample_brief()
    edited = _sample_brief(brief_text="用户手改后的研究问题展开。")
    prompts = iter(["e", "c"])
    result = confirm_brief(
        original,
        prompt=lambda _msg: next(prompts),
        revise_once_fn=lambda _b, _n: (_ for _ in ()).throw(AssertionError("no revise")),
        edit_fn=lambda _b: edited,
        echo=lambda _msg: None,
    )
    assert result == edited


def test_confirm_instruct_revises_once_and_finalizes() -> None:
    original = _sample_brief()
    revised = _sample_brief(brief_text="按指令改写后的 Brief 正文。")
    calls: list[tuple[ResearchBrief, str]] = []

    def revise_once(brief: ResearchBrief, note: str) -> ResearchBrief:
        calls.append((brief, note))
        return revised

    prompts = iter(["i", "请补充更多反例路径"])
    result = confirm_brief(
        original,
        prompt=lambda _msg: next(prompts),
        revise_once_fn=revise_once,
        edit_fn=lambda _b: (_ for _ in ()).throw(AssertionError("no edit")),
        echo=lambda _msg: None,
    )
    assert result == revised
    assert len(calls) == 1
    assert calls[0][0] == original
    assert calls[0][1] == "请补充更多反例路径"


def test_edit_brief_roundtrip_yaml(tmp_path: Path) -> None:
    brief = _sample_brief()

    def open_editor(path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace(
                "具体研究竞品在亚太区的经营态势，检验收缩与扩张等竞争解释。",
                "编辑器保存后的 brief_text。",
            ),
            encoding="utf-8",
        )

    updated = edit_brief(brief, open_editor=open_editor, work_dir=tmp_path)
    assert updated.brief_text == "编辑器保存后的 brief_text。"
    assert updated.question == brief.question


def test_edit_brief_rejects_invalid_yaml_then_retries(tmp_path: Path) -> None:
    brief = _sample_brief()
    attempts = {"n": 0}

    def open_editor(path: Path) -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            path.write_text("question: \nbrief_text: \n", encoding="utf-8")
        else:
            path.write_text(
                "question: ok title\n"
                "brief_text: 合法的研究问题展开文本。\n"
                "effort: quick\n"
                "language: zh\n"
                "output_format: report_with_citations\n",
                encoding="utf-8",
            )

    updated = edit_brief(brief, open_editor=open_editor, work_dir=tmp_path)
    assert attempts["n"] == 2
    assert updated.question == "ok title"
    assert updated.effort == "quick"
