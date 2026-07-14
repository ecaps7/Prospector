"""Worker prompt, stopping, budget, and parallel tool-call contracts."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from prospector.agents.prompts.research_worker import (
    worker_runtime_message,
    worker_system_prompt,
)
from prospector.agents.research_worker import (
    ResearchWorker,
    StopReason,
    WorkerFinish,
    WorkerModelAction,
    WorkerSummary,
    WorkerToolCall,
)
from prospector.schemas.evidence import Assertion
from prospector.schemas.plan import ResearchTask, TaskBudget
from prospector.store.repositories import ResearchRepository
from prospector.tools.base import ToolContext


class FakeRepository:
    def __init__(self) -> None:
        self.tool_call_ids: set[str] = set()

    def has_task_tool_event(self, task_id: UUID, tool_call_id: str) -> bool:
        del task_id
        return tool_call_id in self.tool_call_ids

    def record_tool_used(
        self,
        job_id: UUID,
        task_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        del job_id, task_id
        self.tool_call_ids.add(str(payload["tool_call_id"]))

    def list_assertions(self, task_id: UUID) -> list[Assertion]:
        del task_id
        return []


class ConcurrentTool:
    def __init__(self, name: str, tracker: dict[str, int]) -> None:
        self.name = name
        self.tracker = tracker

    async def __call__(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        self.tracker["active"] += 1
        self.tracker["max_active"] = max(self.tracker["max_active"], self.tracker["active"])
        await asyncio.sleep(0.02)
        self.tracker["active"] -= 1
        return {"results": []}


class ParallelWorkerModel:
    def __init__(self) -> None:
        self.calls = 0
        self.runtime_messages: list[str] = []

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        self.runtime_messages.append(str(messages[-1]["content"]))
        self.calls += 1
        if self.calls == 1:
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="web_search",
                        tool_call_id="search-a",
                        arguments={"query": "a"},
                    ),
                    WorkerToolCall(
                        tool_name="web_search",
                        tool_call_id="search-b",
                        arguments={"query": "b"},
                    ),
                ],
            )
        assert len([item for item in messages if item.get("role") == "tool"]) == 2
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": "finish"},
            finish=WorkerFinish(
                goal_met=False,
                stop_reason="no_public_evidence",
                gap_note="两条独立路径均未发现可保存证据。",
            ),
        )

    async def summarize(
        self,
        assertions: list[Assertion],
        *,
        goal_met: bool,
        stop_reason: StopReason,
        gap_note: str,
    ) -> WorkerSummary:
        assert assertions == []
        assert goal_met is False
        assert stop_reason == "no_public_evidence"
        return WorkerSummary(items=[], gap_note=gap_note)


def _task(max_tool_calls: int = 2) -> ResearchTask:
    return ResearchTask(
        task_id=uuid4(),
        question="核验两个彼此独立的公开资料路径，并记录当前能够获得的直接证据。",
        research_stage="verify",
        research_mode="factual",
        expected_evidence="至少一条能够定位到原文段落的直接证据",
        budget=TaskBudget(max_tool_calls=max_tool_calls),
    )


def test_worker_prompt_describes_parallel_calls_and_actual_fetch_contract() -> None:
    system = worker_system_prompt(today="2026-07-14")
    runtime = worker_runtime_message(
        max_tool_calls=8,
        used_tool_calls=3,
        remaining_tool_calls=5,
    )

    assert "同一轮可以调用多个彼此独立的工具" in system
    assert "压缩要点只用于定位，不是证据" in system
    assert "synthesize" not in system
    assert "completion_criteria" not in system
    assert "一次只调用一个工具" not in system
    assert "剩余：5" in runtime


def test_worker_finish_requires_goal_and_reason_to_agree() -> None:
    with pytest.raises(ValidationError):
        WorkerFinish(
            goal_met=True,
            stop_reason="no_public_evidence",
            gap_note="",
        )


async def test_worker_executes_independent_calls_concurrently_and_finishes_at_zero_budget() -> None:
    repository = FakeRepository()
    tracker = {"active": 0, "max_active": 0}
    model = ParallelWorkerModel()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            ConcurrentTool("web_search", tracker),
            ConcurrentTool("web_fetch", tracker),
            ConcurrentTool("save_findings", tracker),
        ],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_parallel")

    assert tracker["max_active"] == 2
    assert feedback.tool_calls_used == 2
    assert feedback.stop_reason == "no_public_evidence"
    assert "已使用：0" in model.runtime_messages[0]
    assert "剩余：0" in model.runtime_messages[1]
