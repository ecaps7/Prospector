"""Unit tests for Brief HITL confirmation (no LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest

from prospector.runtime.entrypoints.local import format_confirmed_brief, format_scope_outcome
from prospector.runtime.hitl.brief_confirm import (
    BriefConfirmAborted,
    _display_width,
    confirm_brief,
    edit_brief,
    format_brief_card,
    require_tty,
)
from prospector.schemas.brief import ResearchBrief, ScopeOutcome


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
    box_rows = [line for line in card.splitlines() if line.startswith(("┌", "│", "└"))]
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


def test_confirm_instruct_revises_then_returns_for_review() -> None:
    """A model revision must be seen before it starts a run that can last hours."""
    original = _sample_brief()
    revised = _sample_brief(brief_text="按指令改写后的 Brief 正文。")
    calls: list[tuple[ResearchBrief, str]] = []
    shown: list[str] = []

    def revise_once(brief: ResearchBrief, note: str) -> ResearchBrief:
        calls.append((brief, note))
        return revised

    prompts = iter(["i", "请补充更多反例路径", "c"])
    result = confirm_brief(
        original,
        prompt=lambda _msg: next(prompts),
        revise_once_fn=revise_once,
        edit_fn=lambda _b: (_ for _ in ()).throw(AssertionError("no edit")),
        echo=shown.append,
    )
    assert result == revised
    assert len(calls) == 1
    assert calls[0][0] == original
    assert calls[0][1] == "请补充更多反例路径"
    # The revised text was rendered before the user was asked to confirm it.
    assert any("按指令改写后的 Brief 正文。" in message for message in shown)


def test_confirm_allows_only_one_model_revision() -> None:
    original = _sample_brief()
    revised = _sample_brief(brief_text="第一轮改写。")
    calls: list[str] = []

    def revise_once(brief: ResearchBrief, note: str) -> ResearchBrief:
        del brief
        calls.append(note)
        return revised

    shown: list[str] = []
    prompts = iter(["i", "第一次修订", "i", "c"])
    result = confirm_brief(
        original,
        prompt=lambda _msg: next(prompts),
        revise_once_fn=revise_once,
        edit_fn=lambda _b: (_ for _ in ()).throw(AssertionError("no edit")),
        echo=shown.append,
    )
    assert result == revised
    assert calls == ["第一次修订"], "the second instruction must be refused, not sent"
    assert any("已经用过了" in message for message in shown)
    assert any("已用完" in message for message in shown)


def test_confirm_hand_editing_stays_unlimited_after_a_model_revision() -> None:
    original = _sample_brief()
    revised = _sample_brief(brief_text="模型改写。")
    edits = iter([_sample_brief(brief_text="手改一次。"), _sample_brief(brief_text="手改两次。")])
    prompts = iter(["i", "改一下", "e", "e", "c"])
    result = confirm_brief(
        original,
        prompt=lambda _msg: next(prompts),
        revise_once_fn=lambda _b, _n: revised,
        edit_fn=lambda _b: next(edits),
        echo=lambda _msg: None,
    )
    assert result.brief_text == "手改两次。"


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


def test_user_constraints_survive_the_editor_roundtrip(tmp_path: Path) -> None:
    """What the user stated must come back byte-identical after a no-op edit.

    These fields are the binding half of the Brief, so a serializer that silently
    dropped or reordered them would quietly widen the run's scope.
    """
    brief = _sample_brief(
        user_constraints={
            "time_range": "近三年",
            "regions": ["日本", "韩国"],
            "source_rules": ["只要一手数据", "不要媒体转述"],
            "exclusions": ["不涉及监管政策"],
        }
    )

    def open_editor(path: Path) -> None:
        del path  # save without changes

    updated = edit_brief(brief, open_editor=open_editor, work_dir=tmp_path)
    assert updated.user_constraints == brief.user_constraints


def test_editor_can_clear_and_add_constraints(tmp_path: Path) -> None:
    brief = _sample_brief(user_constraints={"regions": ["日本"], "exclusions": ["不涉及监管政策"]})

    def open_editor(path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        text = text.replace("  regions:\n    - 日本\n", "  regions: []\n")
        text = text.replace("  time_range: \n", "  time_range: 近五年\n")
        path.write_text(text, encoding="utf-8")

    updated = edit_brief(brief, open_editor=open_editor, work_dir=tmp_path)
    assert updated.user_constraints.regions == []
    assert updated.user_constraints.exclusions == ["不涉及监管政策"]


def test_every_surface_that_shows_a_brief_shows_its_binding_limits() -> None:
    """The card and both log formatters must not diverge.

    Each one is somewhere a person reads the Brief and decides whether to let the run
    proceed; a surface that silently omits the binding half is worse than no surface.
    """
    brief = _sample_brief(
        user_constraints={"source_rules": ["只要一手数据"], "exclusions": ["不涉及监管政策"]}
    )
    pending = ScopeOutcome(kind="brief_pending", brief=brief)

    surfaces = [
        format_brief_card(brief, width=72),
        format_scope_outcome(pending),
        format_confirmed_brief(brief),
    ]
    for rendered in surfaces:
        assert "不可协商" in rendered
        assert "只要一手数据" in rendered
        assert "不涉及监管政策" in rendered

    # With nothing stated the heading disappears rather than printing an empty section.
    bare = _sample_brief()
    assert "不可协商" not in format_confirmed_brief(bare)
    assert bare.brief_text in format_confirmed_brief(bare)


def test_brief_card_marks_user_constraints_as_binding() -> None:
    card = format_brief_card(
        _sample_brief(user_constraints={"source_rules": ["只要一手数据"]}), width=72
    )
    assert "不可协商" in card
    assert "只要一手数据" in card

    plain = format_brief_card(_sample_brief(), width=72)
    assert "不可协商" not in plain
