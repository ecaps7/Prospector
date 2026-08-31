"""Paid model contract checks, explicitly enabled with --live.

These are constructed cases, not a claim of historical real-job replay or quality coverage.
"""

import pytest

from prospector.agents.llm import get_async_openai_client
from prospector.agents.planner import (
    OpenAIPlannerModel,
    append_runtime_feedback,
    initial_planner_messages,
)
from prospector.agents.prompts.research_worker import worker_system_prompt
from prospector.agents.research_worker import WORKER_ACTION_SCHEMA, OpenAIWorkerModel
from prospector.agents.scope import run_scope
from prospector.schemas.brief import ResearchBrief

pytestmark = pytest.mark.live


def test_planner_dispatches_within_budget_when_no_evidence_exists():
    brief = ResearchBrief(question="城市如何改善轨道站点接驳？", brief_text="比较城市实践。")
    messages = append_runtime_feedback(
        initial_planner_messages(brief),
        feedback_type="research_state",
        payload={
            "available_decisions": ["dispatch"],
            "finish_allowed": False,
            "decision_rounds_remaining": 8,
            "max_tasks_per_dispatch": 6,
            "max_worker_rounds": 12,
        },
    )
    result = OpenAIPlannerModel().decide(messages).decision
    assert result.decision == "dispatch"
    assert result.tasks is not None and 1 <= len(result.tasks) <= 6
    assert all(task.question.strip() and task.expected_evidence.strip() for task in result.tasks)


async def test_worker_preserves_the_evidence_references_in_a_save_action():
    async with get_async_openai_client() as client:
        result = await OpenAIWorkerModel(client).next_action(
            [
                {
                    "role": "system",
                    "content": worker_system_prompt(action_schema=WORKER_ACTION_SCHEMA),
                },
                {
                    "role": "user",
                    "content": (
                        "任务：保存以下两项年度数据。已抓取原文 s1:h1：甲公司收入为 12 亿元；"
                        "s1:h2：乙公司收入为 8 亿元。现在用 save 动作分别保存两条断言，不合并。"
                    ),
                },
            ]
        )
    assert result.finish is None
    assert result.tool_calls and all(
        call.tool_name == "save_findings" for call in result.tool_calls
    )
    findings = [finding for call in result.tool_calls for finding in call.arguments["findings"]]
    assert len(findings) == 2
    assert {tuple(finding["source_refs"]) for finding in findings} == {("s1:h1",), ("s1:h2",)}


def test_scope_does_not_ask_again_after_a_clarification_answer():
    result = run_scope(
        "研究生物技术",
        clarification_question="关注哪个领域？",
        clarification_answer="工业发酵，不涉及医疗。",
        effort="quick",
        language="zh",
    )
    assert result.kind == "brief_pending" and result.brief is not None
    assert result.brief.effort == "quick" and result.brief.language == "zh"
