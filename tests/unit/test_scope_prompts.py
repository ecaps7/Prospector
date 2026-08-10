"""Scope prompt contracts: what Scope asks for, and what it hands to the next step."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prospector.agents.prompts.planner import planner_system_prompt
from prospector.agents.prompts.research_worker import worker_system_prompt
from prospector.agents.prompts.scope import clarify_prompt, write_brief_prompt
from prospector.schemas.brief import ClarifyDecision


def test_clarify_step_must_report_its_reasoning_either_way() -> None:
    prompt = clarify_prompt("钠离子电池发展到了什么程度？")

    assert "assessment" in prompt
    assert "无论是否需要澄清" in prompt


def test_clarify_decision_carries_an_assessment_without_requiring_one() -> None:
    decision = ClarifyDecision.model_validate(
        {
            "need_clarification": False,
            "question": "",
            "assessment": "  研究对象明确，缺的是决策角度，应由 Brief 展开。  ",
        }
    )
    assert decision.assessment == "研究对象明确，缺的是决策角度，应由 Brief 展开。"

    # A model that omits it loses a hint; it must not fail Scope, which has no retry.
    assert ClarifyDecision.model_validate({"need_clarification": False}).assessment == ""

    with pytest.raises(ValidationError):
        ClarifyDecision.model_validate({"need_clarification": True, "question": ""})


def test_brief_writer_receives_the_clarify_verdict_instead_of_rederiving_it() -> None:
    verdict = "研究对象明确，用户没说决策角度，这个缺口应由 Brief 展开而非追问。"
    prompt = write_brief_prompt("钠离子电池发展到了什么程度？", assessment=verdict)

    assert verdict in prompt
    assert "澄清环节的判断" in prompt

    # No verdict means no empty block.
    assert "澄清环节的判断" not in write_brief_prompt("钠离子电池发展到了什么程度？")
    assert "澄清环节的判断" not in write_brief_prompt("问题", assessment="   ")


def test_brief_width_is_tied_to_the_effort_the_user_chose() -> None:
    """Brief width is research cost, so the two must not be decided independently."""
    for effort in ("quick", "standard", "deep"):
        prompt = write_brief_prompt("研究问题", effort=effort)
        assert f"本次档位：{effort}" in prompt
        assert "Brief 的宽度就是后续的研究成本" in prompt

    quick = write_brief_prompt("研究问题", effort="quick")
    assert "只展开 1 到 2 个最关键的方向" in quick


def test_query_examples_span_unrelated_domains() -> None:
    """Every run reads these examples, whatever the user actually asked about.

    A single-domain example set does not just look odd — it nudges framing and
    attribution habits toward that domain on unrelated questions.
    """
    worker = worker_system_prompt(today="2026-08-09")
    planner = planner_system_prompt(today="2026-08-09")

    transit = ("车站", "接驳", "轨道交通")
    assert sum(term in worker for term in transit) > 0, "the contrast pair is still useful"
    # ...but it must no longer be the only domain the model ever sees.
    assert "降压药" in worker or "老年患者" in worker
    assert "宋代" in worker

    assert not any(term in planner for term in transit), (
        "the Planner example should not reuse the Worker's domain"
    )
