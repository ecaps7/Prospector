"""Pure contracts for Planner decisions, budgets, thread admission, and evidence views."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from prospector.agents.planner import append_runtime_feedback, initial_planner_messages
from prospector.agents.prompts.research_worker import worker_constraints_message
from prospector.deterministic.budget import inject_task_budget, limits_for_effort
from prospector.deterministic.gates import (
    InformationGainCounter,
    PlannerRejection,
    dispatch_rejection,
    finish_rejection,
)
from prospector.flow.research_graph import _research_state_message
from prospector.schemas.brief import ResearchBrief, UserConstraints
from prospector.schemas.decisions import PlannerDecision
from prospector.schemas.plan import ResearchTaskDraft
from prospector.tools.save_findings import SaveFindingsArguments
from prospector.tools.web_search import ExaClient


class RecordingExaClient(ExaClient):
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert path == "contents"
        self.payload = payload
        return {"results": []}


def _draft() -> ResearchTaskDraft:
    return ResearchTaskDraft(
        question="检验目标公司在不同年份的公开扩张信号，并寻找相反证据与口径差异。",
        expected_evidence="有时间与口径的直接证据及至少一条相反信号",
    )


def test_planner_decision_is_exactly_one_of_two() -> None:
    valid = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "tasks": [_draft().model_dump()],
            "reason": "先查直接信号",
        }
    )
    assert valid.tasks is not None

    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "decision": "finish",
                "reason": "完成",
                "tasks": [_draft().model_dump()],
            }
        )


def test_dispatch_tasks_contain_only_the_worker_contract() -> None:
    decision = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "tasks": [_draft().model_dump(), _draft().model_dump()],
            "reason": "并行确认两个证据问题",
        }
    )
    assert decision.tasks is not None
    assert all(
        set(task.model_dump()) == {"question", "expected_evidence"} for task in decision.tasks
    )


def test_planner_cannot_author_runtime_budget() -> None:
    schema = ResearchTaskDraft.model_json_schema()
    assert "budget" not in schema["properties"]
    assert "task_id" not in schema["properties"]

    task = inject_task_budget(_draft(), "standard")
    assert task.budget.max_worker_rounds == 20
    assert task.allowed_tools == ["web_search", "web_fetch", "save_findings"]


def test_task_budget_depends_only_on_effort() -> None:
    another = ResearchTaskDraft(
        question="确认候选城市中最后一公里接驳的官方指标口径是否公开可得。",
        expected_evidence="每个候选城市的指标口径存在性与来源确认",
    )
    first = inject_task_budget(another, "standard")
    second = inject_task_budget(_draft(), "standard")

    assert first.budget.max_worker_rounds == 20
    assert second.budget.max_worker_rounds == 20


def test_worker_rounds_are_the_only_task_budget() -> None:
    schema = ResearchTaskDraft.model_json_schema()
    assert "budget" not in schema["properties"]

    task = inject_task_budget(_draft(), "quick")
    assert set(task.budget.model_dump()) == {"max_worker_rounds"}


def test_planner_task_contains_only_worker_question_and_evidence_goal() -> None:
    schema = ResearchTaskDraft.model_json_schema()
    assert set(schema["properties"]) == {"question", "expected_evidence"}
    assert set(schema["required"]) == {"question", "expected_evidence"}

    with pytest.raises(ValidationError):
        ResearchTaskDraft.model_validate({**_draft().model_dump(), "research_mode": "factual"})


def test_dispatch_requires_at_least_one_task_and_finish_omits_dispatch_fields() -> None:
    with pytest.raises(ValidationError):
        PlannerDecision.model_validate(
            {
                "decision": "dispatch",
                "tasks": [],
                "reason": "开始研究",
            }
        )

    finish = PlannerDecision.model_validate({"decision": "finish", "reason": "证据已充分"})
    assert finish.model_dump(exclude_none=True) == {
        "decision": "finish",
        "reason": "证据已充分",
    }


def test_planner_sees_user_limits_and_scope_suggestions_as_separate_blocks() -> None:
    """Binding limits and proposed directions must not arrive as one paragraph.

    They carry different authority, so the Planner should be able to tell them apart by
    position rather than by re-reading the prose every round.
    """
    brief = ResearchBrief(
        question="研究问题",
        brief_text="研究一个具体问题并比较竞争解释。",
        user_constraints=UserConstraints(
            time_range="近三年",
            source_rules=["只要一手数据"],
            exclusions=["不涉及监管政策"],
        ),
    )
    message = str(initial_planner_messages(brief)[1]["content"])

    assert "最终结果必须遵守" in message
    assert "近三年" in message
    assert "只要一手数据" in message
    assert "不涉及监管政策" in message
    binding = message.index("最终结果必须遵守")
    suggestions = message.index("供你取舍")
    assert binding < suggestions < message.index("研究一个具体问题")

    bare = ResearchBrief(question="研究问题", brief_text="研究一个具体问题并比较竞争解释。")
    bare_message = str(initial_planner_messages(bare)[1]["content"])
    assert "最终结果必须遵守" not in bare_message
    assert "没有提出额外限制" in bare_message


def test_worker_receives_source_rules_and_exclusions_directly() -> None:
    assert worker_constraints_message(UserConstraints()) is None

    message = worker_constraints_message(
        UserConstraints(source_rules=["只要一手数据"], exclusions=["不涉及监管政策"])
    )
    assert message is not None
    assert "只要一手数据" in message
    assert "不涉及监管政策" in message
    assert "不可协商" in message

    # deliverable_rules govern the report, not the Worker's searching, so they stay out.
    assert worker_constraints_message(UserConstraints(deliverable_rules=["附带图表"])) is None


def test_effort_maps_to_flat_research_limits() -> None:
    quick = limits_for_effort("quick")
    standard = limits_for_effort("standard")
    deep = limits_for_effort("deep")

    assert (quick.decision_round_limit, quick.max_concurrency, quick.max_worker_rounds) == (
        8,
        6,
        12,
    )
    assert (
        standard.decision_round_limit,
        standard.max_concurrency,
        standard.max_worker_rounds,
    ) == (12, 5, 20)
    assert (deep.decision_round_limit, deep.max_concurrency, deep.max_worker_rounds) == (
        24,
        6,
        32,
    )


def test_planner_receives_concrete_runtime_capabilities() -> None:
    payload = _research_state_message(
        {"decision_round_limit": 12, "research_decisions_used": 1},
        excerpt_count=2,
        effort="standard",
    )

    assert payload == {
        "available_decisions": ["dispatch", "finish"],
        "decision_rounds_remaining": 11,
        "max_tasks_per_dispatch": 5,
        "max_worker_rounds": 20,
        "worker_actions": ["search", "save", "finish"],
        "worker_tools": ["web_search", "web_fetch", "save_findings"],
        "max_parallel_tool_calls": 8,
        "search_auto_fetch_top_n": 2,
        "finish_allowed": True,
    }


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


def test_save_findings_uses_strict_persisted_view_source_ids() -> None:
    parameters = SaveFindingsArguments.model_json_schema()
    finding = parameters["$defs"]["FindingInput"]
    assert parameters["required"] == ["doc_id", "view_id", "findings"]
    assert finding["required"] == ["source_ids", "statement", "topic_tags"]
    assert finding["properties"]["source_ids"]["items"]["pattern"] == "^h[1-9][0-9]*$"

    parsed = SaveFindingsArguments.model_validate(
        {
            "doc_id": str(uuid4()),
            "view_id": str(uuid4()),
            "findings": [
                {
                    "source_ids": ["h1", "h2"],
                    "statement": "来源视图支持该项事实。",
                    "topic_tags": [],
                }
            ],
        }
    )
    assert parsed.findings[0].source_ids == ["h1", "h2"]

    with pytest.raises(ValidationError, match="String should match pattern"):
        SaveFindingsArguments.model_validate(
            {
                "doc_id": str(uuid4()),
                "view_id": str(uuid4()),
                "findings": [
                    {
                        "source_ids": ["p1"],
                        "statement": "非法来源编号。",
                        "topic_tags": [],
                    }
                ],
            }
        )


def test_save_findings_rejects_stringified_findings() -> None:
    with pytest.raises(ValidationError):
        SaveFindingsArguments.model_validate(
            {
                "doc_id": str(uuid4()),
                "view_id": str(uuid4()),
                "findings": json.dumps(
                    [
                        {
                            "source_ids": ["h1"],
                            "statement": "字符串化数组不得进入工具层。",
                            "topic_tags": [],
                        }
                    ],
                    ensure_ascii=False,
                ),
            }
        )


@pytest.mark.asyncio
async def test_exa_contents_always_requests_task_highlights() -> None:
    exa = RecordingExaClient()

    await exa.contents("https://example.test/source", "核验目标事实")

    assert exa.payload == {
        "urls": ["https://example.test/source"],
        "text": True,
        "highlights": {"query": "核验目标事实"},
    }


def test_planner_thread_admission_rejects_worker_trace_and_document_content() -> None:
    brief = ResearchBrief(question="研究问题", brief_text="研究一个具体问题并比较竞争解释。")
    messages = initial_planner_messages(brief)
    accepted = append_runtime_feedback(
        messages,
        feedback_type="worker_projection",
        payload={
            "tasks": [
                {
                    "task_id": str(uuid4()),
                    "assertions": [{"assertion_id": str(uuid4()), "text": "已落库断言"}],
                    "stop_reason": "expected_evidence_satisfied",
                    "finish_reason": "已覆盖任务要求的证据。",
                }
            ]
        },
    )
    assert len(accepted) == len(messages) + 1

    with pytest.raises(ValueError, match="document_view"):
        append_runtime_feedback(
            messages,
            feedback_type="worker_projection",
            payload={"document_view": "Exa highlights 内容"},
        )
    with pytest.raises(ValueError, match="worker_messages"):
        append_runtime_feedback(
            messages,
            feedback_type="worker_projection",
            payload={"worker_messages": []},
        )
