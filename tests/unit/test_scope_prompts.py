"""Scope prompt contracts: what Scope asks for, and what it hands to the next step."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prospector.agents.prompts.scope import clarify_prompt, write_brief_prompt
from prospector.schemas.brief import ClarifyDecision


def test_clarify_prompt_states_the_json_contract() -> None:
    prompt = clarify_prompt("钠离子电池发展到了什么程度？")

    assert "need_clarification" in prompt
    assert "assessment" in prompt
    assert "空字符串" in prompt


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


def test_brief_prompt_injects_the_effort_the_caller_chose() -> None:
    for effort in ("quick", "standard", "deep"):
        prompt = write_brief_prompt("研究问题", effort=effort)
        assert f'effort 为 "{effort}"' in prompt
        assert "user_constraints" in prompt
        assert "brief_text" in prompt
