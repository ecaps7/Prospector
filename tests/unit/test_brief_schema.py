"""Unit tests for Research Brief schemas (no LLM)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from prospector.schemas.brief import ClarifyDecision, ResearchBrief, ScopeOutcome


def test_research_brief_rejects_blank_brief_text() -> None:
    with pytest.raises(ValidationError):
        ResearchBrief(question="q", brief_text="   ")


def test_research_brief_rejects_invalid_effort() -> None:
    with pytest.raises(ValidationError):
        ResearchBrief(question="q", brief_text="enough text", effort="turbo")  # type: ignore[arg-type]


def test_research_brief_strips_fields() -> None:
    brief = ResearchBrief(question="  title  ", brief_text="  expanded question  ", language=" zh ")
    assert brief.question == "title"
    assert brief.brief_text == "expanded question"
    assert brief.language == "zh"
    assert brief.effort == "standard"


def test_clarify_decision_requires_question_when_needed() -> None:
    with pytest.raises(ValidationError):
        ClarifyDecision(need_clarification=True, question="  ")


def test_clarify_decision_rejects_question_when_not_needed() -> None:
    with pytest.raises(ValidationError):
        ClarifyDecision(need_clarification=False, question="Which company?")


def test_clarify_decision_has_no_unused_verification_field() -> None:
    assert "verification" not in ClarifyDecision.model_fields


def test_scope_outcome_clarify_branch() -> None:
    out = ScopeOutcome(kind="clarify", clarification_question="Which company?")
    assert out.brief is None


def test_scope_outcome_brief_pending_branch() -> None:
    brief = ResearchBrief(question="q", brief_text="expanded research question")
    out = ScopeOutcome(kind="brief_pending", brief=brief)
    assert out.clarification_question is None


def test_scope_outcome_rejects_mismatched_payload() -> None:
    with pytest.raises(ValidationError):
        ScopeOutcome(kind="clarify", brief=ResearchBrief(question="q", brief_text="x"))
    with pytest.raises(ValidationError):
        ScopeOutcome(kind="brief_pending", clarification_question="hi")
