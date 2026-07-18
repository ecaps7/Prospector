"""Pure contracts for Planner decisions, budgets, thread admission, and evidence views."""

from __future__ import annotations

import json
from typing import Any
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
from prospector.schemas.brief import ResearchBrief
from prospector.schemas.decisions import PlannerDecision
from prospector.schemas.plan import ResearchTaskDraft, SourcePolicy
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
        subjects=["目标公司"],
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
    assert task.budget.max_worker_rounds == 18
    assert task.allowed_tools == ["web_search", "web_fetch", "save_findings"]


def test_task_budget_is_stage_differentiated() -> None:
    scout = ResearchTaskDraft(
        question="确认候选城市中最后一公里接驳的官方指标口径是否公开可得。",
        subjects=["东京", "新加坡", "深圳"],
        research_stage="scout",
        research_mode="factual",
        expected_evidence="每个候选城市的指标口径存在性与来源确认",
    )
    scout_task = inject_task_budget(scout, "standard")
    deep_task = inject_task_budget(
        ResearchTaskDraft(**{**_draft().model_dump(), "research_stage": "deep_dive"}),
        "standard",
    )

    assert scout_task.budget.max_worker_rounds == 24
    assert deep_task.budget.max_worker_rounds == 64
    assert scout_task.budget.max_worker_rounds < deep_task.budget.max_worker_rounds


def test_worker_rounds_are_the_only_task_budget() -> None:
    schema = ResearchTaskDraft.model_json_schema()
    assert "budget" not in schema["properties"]

    task = inject_task_budget(_draft(), "quick")
    assert set(task.budget.model_dump()) == {"max_worker_rounds"}


def test_subjects_granularity_is_schema_enforced() -> None:
    base = _draft().model_dump()

    with pytest.raises(ValidationError, match="exactly one subject"):
        ResearchTaskDraft.model_validate({**base, "subjects": ["东京", "新加坡"]})

    with pytest.raises(ValidationError):
        ResearchTaskDraft.model_validate({**base, "subjects": []})

    scout = ResearchTaskDraft.model_validate(
        {
            **base,
            "research_stage": "scout",
            "subjects": ["东京", "新加坡", "  东京  "],
        }
    )
    assert scout.subjects == ["东京", "新加坡"]

    with pytest.raises(ValidationError):
        ResearchTaskDraft.model_validate(
            {**base, "research_stage": "scout", "subjects": [f"城市{i}" for i in range(7)]}
        )


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
    assert "completion_criteria" not in system_prompt


def test_effort_maps_round_limit_and_stage_budgets() -> None:
    assert limits_for_effort("quick").decision_round_limit == 8
    assert limits_for_effort("standard").decision_round_limit == 12
    assert limits_for_effort("deep").decision_round_limit == 24

    for effort in ("quick", "standard", "deep"):
        stages = limits_for_effort(effort).stages  # type: ignore[arg-type]
        assert set(stages) == {"scout", "deep_dive", "verify"}
        scout, deep_dive = stages["scout"], stages["deep_dive"]
        # scout is cheap-and-wide, deep_dive is expensive-and-narrow.
        assert scout.max_concurrency > deep_dive.max_concurrency
        assert scout.max_worker_rounds < deep_dive.max_worker_rounds

    standard = limits_for_effort("standard").stages
    assert (
        standard["scout"].max_concurrency,
        standard["scout"].max_worker_rounds,
    ) == (6, 24)
    assert (
        standard["deep_dive"].max_concurrency,
        standard["deep_dive"].max_worker_rounds,
    ) == (3, 64)
    assert (
        standard["verify"].max_concurrency,
        standard["verify"].max_worker_rounds,
    ) == (3, 18)


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
