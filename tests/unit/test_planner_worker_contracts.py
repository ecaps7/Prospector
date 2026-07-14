"""Pure contracts for Planner decisions, budgets, thread admission, and evidence slicing."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from prospector.agents.planner import append_runtime_feedback, initial_planner_messages
from prospector.deterministic.budget import inject_task_budget, limits_for_effort
from prospector.deterministic.gates import (
    InformationGainCounter,
    PlannerRejection,
    dispatch_rejection,
    finish_rejection,
)
from prospector.deterministic.segment import segment_text, select_excerpts, select_paragraphs
from prospector.schemas.brief import ResearchBrief
from prospector.schemas.decisions import PlannerDecision
from prospector.schemas.plan import ResearchTaskDraft, SourcePolicy


def _draft() -> ResearchTaskDraft:
    return ResearchTaskDraft(
        question="检验目标公司在不同年份的公开扩张信号，并寻找相反证据与口径差异。",
        research_stage="verify",
        research_mode="counterargument",
        source_policy=SourcePolicy(preferred_tiers=["official", "industry"]),
        expected_evidence="有时间与口径的直接证据及至少一条相反信号",
    )


def test_planner_decision_is_exactly_one_of_three() -> None:
    valid = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "dispatch": {"tasks": [_draft().model_dump()], "reason": "先查直接信号"},
        }
    )
    assert valid.dispatch is not None

    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "decision": "finish",
                "finish": {"reason": "完成"},
                "reflect": {"note": "同时反思"},
            }
        )


def test_planner_cannot_author_runtime_budget() -> None:
    schema = ResearchTaskDraft.model_json_schema()
    assert "budget" not in schema["properties"]
    assert "task_id" not in schema["properties"]

    task = inject_task_budget(_draft(), "standard")
    assert task.budget.max_tool_calls == 15
    assert task.allowed_tools == ["web_search", "web_fetch", "save_findings"]


def test_research_stage_is_required_and_excludes_synthesis() -> None:
    schema = ResearchTaskDraft.model_json_schema()
    assert "completion_criteria" not in schema["properties"]
    assert "research_stage" in schema["required"]
    assert schema["properties"]["research_stage"]["enum"] == [
        "scout",
        "deep_dive",
        "verify",
    ]

    with pytest.raises(ValidationError):
        ResearchTaskDraft.model_validate({**_draft().model_dump(), "research_stage": "synthesize"})


def test_planner_prompt_requires_bounded_stages_without_rigid_evidence_counts() -> None:
    brief = ResearchBrief(question="研究问题", brief_text="研究一个具体问题并比较竞争解释。")
    messages = initial_planner_messages(brief, limits_for_effort("standard"))
    system_prompt = str(messages[0]["content"])

    assert "scout" in system_prompt
    assert "deep_dive" in system_prompt
    assert "verify" in system_prompt
    assert "不要把公开世界未必存在的材料写成僵硬数量指标" in system_prompt
    assert "completion_criteria" not in system_prompt


def test_effort_maps_only_the_three_hard_limits() -> None:
    assert (
        limits_for_effort("quick").decision_round_limit,
        limits_for_effort("quick").max_concurrency,
        limits_for_effort("quick").max_tool_calls,
    ) == (3, 1, 8)
    assert (
        limits_for_effort("standard").decision_round_limit,
        limits_for_effort("standard").max_concurrency,
        limits_for_effort("standard").max_tool_calls,
    ) == (6, 3, 15)
    assert (
        limits_for_effort("deep").decision_round_limit,
        limits_for_effort("deep").max_concurrency,
        limits_for_effort("deep").max_tool_calls,
    ) == (12, 4, 25)


def test_hard_gates_are_deterministic() -> None:
    assert dispatch_rejection(4, 3) == PlannerRejection.OVER_CONCURRENCY
    assert dispatch_rejection(3, 3) is None
    assert finish_rejection(0) == PlannerRejection.EMPTY_FINISH
    assert finish_rejection(1) is None

    counter = InformationGainCounter()
    assert counter.record_save(0) is False
    assert counter.record_save(1) is False
    assert counter.record_save(0) is False
    assert counter.record_save(0) is True


def test_segmentation_preserves_exact_source_text_and_spans() -> None:
    source = "第一段原文。\n\n第二段有数字 42。\n仍属第二段。\n\n第三段。"
    paragraphs = segment_text(source)
    assert [paragraph.text for paragraph in paragraphs] == [
        "第一段原文。",
        "第二段有数字 42。\n仍属第二段。",
        "第三段。",
    ]
    excerpt, locator = select_paragraphs(source, [2, 3])
    assert excerpt == "第二段有数字 42。\n仍属第二段。\n\n第三段。"
    span = locator["char_span"]
    assert isinstance(span, list)
    assert source[int(span[0]) : int(span[1])] == excerpt
    non_contiguous = select_excerpts(source, [1, 3])
    assert [item[0] for item in non_contiguous] == ["第一段原文。", "第三段。"]
    for exact_text, exact_locator in non_contiguous:
        exact_span = exact_locator["char_span"]
        assert isinstance(exact_span, list)
        assert source[int(exact_span[0]) : int(exact_span[1])] == exact_text


def test_planner_thread_admission_rejects_worker_trace_and_document_content() -> None:
    brief = ResearchBrief(question="研究问题", brief_text="研究一个具体问题并比较竞争解释。")
    messages = initial_planner_messages(brief, limits_for_effort("standard"))
    accepted = append_runtime_feedback(
        messages,
        feedback_type="worker_projection",
        payload={
            "tasks": [
                {
                    "task_id": str(uuid4()),
                    "assertions": [{"assertion_id": str(uuid4()), "text": "已落库断言"}],
                    "stop_reason": "expected_evidence_satisfied",
                    "gap_note": "",
                }
            ]
        },
    )
    assert len(accepted) == len(messages) + 1

    with pytest.raises(ValueError, match="compressed_view"):
        append_runtime_feedback(
            messages,
            feedback_type="worker_projection",
            payload={"compressed_view": "网页压缩内容"},
        )
    with pytest.raises(ValueError, match="worker_messages"):
        append_runtime_feedback(
            messages,
            feedback_type="worker_projection",
            payload={"worker_messages": []},
        )
