"""Worker prompt, stopping, budget, and parallel tool-call contracts."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from prospector.agents.prompts.research_worker import (
    worker_system_prompt,
)
from prospector.agents.research_worker import (
    FINISH_TOOL_NAME,
    WORKER_FINISH_SCHEMA,
    OpenAIWorkerModel,
    ResearchWorker,
    SummaryItem,
    WorkerCoverageAssessment,
    WorkerFinish,
    WorkerModelAction,
    WorkerToolCall,
)
from prospector.schemas.evidence import Assertion
from prospector.schemas.plan import ResearchTask, TaskBudget
from prospector.store.repositories import ResearchRepository
from prospector.tools.base import ToolContext


class FakeRepository:
    def __init__(self) -> None:
        self.tool_events: list[dict[str, Any]] = []
        self.assertions: list[Assertion] = []

    def has_task_tool_error_event(self, task_id: UUID, tool_call_id: str) -> bool:
        del task_id
        return any(
            event.get("tool_call_id") == tool_call_id and "error" in event
            for event in self.tool_events
        )

    def record_tool_used(
        self,
        job_id: UUID,
        task_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        del job_id, task_id
        self.tool_events.append(payload)

    def list_assertions(self, task_id: UUID) -> list[Assertion]:
        del task_id
        return self.assertions


class FakeSummaryCompletions:
    def __init__(self, arguments: dict[str, str]) -> None:
        self.arguments = arguments
        self.request: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.request = kwargs
        function = SimpleNamespace(
            name="submit_worker_summary",
            arguments=json.dumps(self.arguments, ensure_ascii=False),
        )
        message = SimpleNamespace(tool_calls=[SimpleNamespace(function=function)])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeSummaryClient:
    def __init__(self, arguments: dict[str, str]) -> None:
        self.completions = FakeSummaryCompletions(arguments)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeActionMessage:
    def __init__(self, *, tool_calls: list[Any], content: str | None = None) -> None:
        self.tool_calls = tool_calls
        self.content = content

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return {"role": "assistant", "content": self.content, "tool_calls": []}


class FakeActionCompletions:
    def __init__(self, message: FakeActionMessage) -> None:
        self.message = message

    async def create(self, **kwargs: Any) -> Any:
        del kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])


class FakeActionClient:
    def __init__(self, message: FakeActionMessage) -> None:
        self.chat = SimpleNamespace(completions=FakeActionCompletions(message))


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
                reason="两条独立路径均未发现可保存证据。",
            ),
        )

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        assert assertions == []
        return []

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        del assertions, task_question, expected_evidence
        raise AssertionError("没有落证时不应检查覆盖度")


class PartialFetchTool:
    name = "web_fetch"

    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository

    async def __call__(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments
        self.repository.record_tool_used(
            context.job_id,
            context.task_id,
            {
                "tool": self.name,
                "tool_call_id": context.tool_call_id,
                "url": "https://example.test/source",
                "doc_id": str(uuid4()),
            },
        )
        raise RuntimeError("Exa highlights 为空")


class PartialFailureModel:
    def __init__(self) -> None:
        self.calls = 0

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        del messages
        self.calls += 1
        if self.calls == 1:
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="web_fetch",
                        tool_call_id="fetch-partial",
                        arguments={"url": "https://example.test/source"},
                    )
                ],
            )
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": "finish"},
            finish=WorkerFinish(
                goal_met=False,
                stop_reason="no_public_evidence",
                reason="网页快照已保存，但压缩失败，未获得可提交证据。",
            ),
        )

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        assert assertions == []
        return []

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        del assertions, task_question, expected_evidence
        raise AssertionError("工具失败且没有落证时不应检查覆盖度")


class FailingThenSucceedingTool:
    def __init__(self, name: str, *, fail_times: int) -> None:
        self.name = name
        self.fail_times = fail_times
        self.calls = 0

    async def __call__(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"{self.name} temporarily unavailable")
        return {"results": []}


class FailThenSucceedModel:
    def __init__(self) -> None:
        self.action_calls = 0
        self.runtime_messages: list[str] = []

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        self.runtime_messages.append(str(messages[-1]["content"]))
        self.action_calls += 1
        if self.action_calls == 1:
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="web_search",
                        tool_call_id="search-fail",
                        arguments={"query": "first"},
                    )
                ],
            )
        if self.action_calls == 2:
            assert any(
                item.get("role") == "tool" and "temporarily unavailable" in str(item.get("content"))
                for item in messages
            )
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="web_search",
                        tool_call_id="search-ok",
                        arguments={"query": "retry"},
                    )
                ],
            )
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": "finish"},
            finish=WorkerFinish(
                goal_met=False,
                stop_reason="no_public_evidence",
                reason="重试后仍无可用公开证据。",
            ),
        )

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        assert assertions == []
        return []

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        del assertions, task_question, expected_evidence
        raise AssertionError("没有落证时不应检查覆盖度")


class FailTwiceThenSucceedModel:
    def __init__(self) -> None:
        self.action_calls = 0

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        self.action_calls += 1
        if self.action_calls <= 3:
            if self.action_calls > 1:
                assert any(
                    item.get("role") == "tool"
                    and "temporarily unavailable" in str(item.get("content"))
                    for item in messages
                )
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="web_search",
                        tool_call_id=f"search-{self.action_calls}",
                        arguments={"query": f"path-{self.action_calls}"},
                    )
                ],
            )
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": "finish"},
            finish=WorkerFinish(
                goal_met=False,
                stop_reason="no_public_evidence",
                reason="更换检索路径后仍无可用公开证据。",
            ),
        )

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        assert assertions == []
        return []

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        del assertions, task_question, expected_evidence
        raise AssertionError("没有落证时不应检查覆盖度")


class AlwaysFailingModel:
    def __init__(self) -> None:
        self.action_calls = 0

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        del messages
        self.action_calls += 1
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": None},
            tool_calls=[
                WorkerToolCall(
                    tool_name="web_search",
                    tool_call_id=f"search-fail-{self.action_calls}",
                    arguments={"query": f"path-{self.action_calls}"},
                )
            ],
        )

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        assert assertions == []
        return []

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        del assertions, task_question, expected_evidence
        raise AssertionError("没有落证时不应检查覆盖度")


class SavingTool:
    name = "save_findings"

    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository

    async def __call__(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments
        assertion = Assertion(
            assertion_id=uuid4(),
            statement="目标事实已有带原文定位的直接证据。",
            excerpt_ids=[uuid4()],
            topic_tags=["目标事实"],
            produced_by={"task_id": str(context.task_id), "worker": context.worker_id},
        )
        self.repository.assertions.append(assertion)
        return {"inserted": 1, "assertion_ids": [str(assertion.assertion_id)]}


class SaveThenSatisfiedModel:
    def __init__(self, *, complete_after: int = 1) -> None:
        self.complete_after = complete_after
        self.action_calls = 0
        self.coverage_checks = 0

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        self.action_calls += 1
        if self.action_calls > self.complete_after:
            raise AssertionError("落证满足 expected_evidence 后不应再请求下一步动作")
        if self.action_calls > 1:
            assert any(
                "还缺少第二条独立来源的直接证据" in str(message.get("content", ""))
                for message in messages
            )
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": None},
            tool_calls=[
                WorkerToolCall(
                    tool_name="save_findings",
                    tool_call_id="save-complete",
                    arguments={
                        "doc_id": str(uuid4()),
                        "view_id": str(uuid4()),
                        "findings": [{"source_ids": ["h1"]}],
                    },
                )
            ],
        )

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        self.coverage_checks += 1
        assert len(assertions) == self.coverage_checks
        assert task_question.startswith("核验两个彼此独立的公开资料路径")
        assert expected_evidence == "至少一条能够定位到原文段落的直接证据"
        if self.coverage_checks < self.complete_after:
            return WorkerCoverageAssessment(
                goal_met=False,
                reason="还缺少第二条独立来源的直接证据。",
            )
        return WorkerCoverageAssessment(
            goal_met=True,
            reason="已取得满足任务要求的直接证据。",
        )

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        return [assertion.statement for assertion in assertions]


class SlotSummaryModel:
    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        del messages
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": "finish"},
            finish=WorkerFinish(
                goal_met=False,
                stop_reason="no_public_evidence",
                reason="仍缺少独立来源。",
            ),
        )

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        assert len(assertions) == 1
        return ["压缩后的断言文本。"]

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        del assertions, task_question, expected_evidence
        raise AssertionError("没有新增落证时不应检查覆盖度")


def _task(max_worker_rounds: int = 5) -> ResearchTask:
    return ResearchTask(
        task_id=uuid4(),
        question="核验两个彼此独立的公开资料路径，并记录当前能够获得的直接证据。",
        subjects=["目标公司"],
        research_stage="verify",
        research_mode="factual",
        expected_evidence="至少一条能够定位到原文段落的直接证据",
        budget=TaskBudget(max_worker_rounds=max_worker_rounds),
    )


def test_worker_prompt_excludes_invalid_concepts() -> None:
    system = worker_system_prompt(today="2026-07-14")

    assert "synthesize" not in system
    assert "completion_criteria" not in system
    assert "一次只调用一个工具" not in system


async def test_openai_summary_uses_required_fixed_slots_and_returns_ledger_order() -> None:
    assertions = [
        Assertion(
            assertion_id=uuid4(),
            statement="第一条断言。",
            excerpt_ids=[uuid4()],
            topic_tags=[],
            produced_by={"task_id": str(uuid4()), "worker": "rw_test"},
        ),
        Assertion(
            assertion_id=uuid4(),
            statement="第二条断言。",
            excerpt_ids=[uuid4()],
            topic_tags=[],
            produced_by={"task_id": str(uuid4()), "worker": "rw_test"},
        ),
    ]
    client = FakeSummaryClient({"summary_1": "第二条摘要。", "summary_0": "第一条摘要。"})
    model = OpenAIWorkerModel(client=cast(Any, client), model="test-model")

    texts = await model.summarize(assertions)

    assert texts == ["第一条摘要。", "第二条摘要。"]
    assert client.completions.request is not None
    function = client.completions.request["tools"][0]["function"]
    assert function["strict"] is True
    assert function["parameters"]["required"] == ["summary_0", "summary_1"]
    assert function["parameters"]["additionalProperties"] is False


async def test_openai_summary_rejects_missing_fixed_slot() -> None:
    assertion = Assertion(
        assertion_id=uuid4(),
        statement="唯一断言。",
        excerpt_ids=[uuid4()],
        topic_tags=[],
        produced_by={"task_id": str(uuid4()), "worker": "rw_test"},
    )
    client = FakeSummaryClient({})
    model = OpenAIWorkerModel(client=cast(Any, client), model="test-model")

    with pytest.raises(ValueError, match="every fixed summary slot exactly once"):
        await model.summarize([assertion])


def test_worker_finish_requires_goal_and_reason_to_agree() -> None:
    with pytest.raises(ValidationError):
        WorkerFinish(
            goal_met=True,
            stop_reason="no_public_evidence",
            reason="目标与原因不一致",
        )


def test_worker_finish_requires_reason() -> None:
    with pytest.raises(ValidationError):
        WorkerFinish(
            goal_met=False,
            stop_reason="no_public_evidence",
            reason="",
        )


async def test_openai_worker_finish_is_a_strict_tool_action() -> None:
    function = SimpleNamespace(
        name=FINISH_TOOL_NAME,
        arguments=json.dumps(
            {
                "goal_met": True,
                "stop_reason": "expected_evidence_satisfied",
                "reason": "已覆盖任务要求的直接证据。",
            },
            ensure_ascii=False,
        ),
    )
    message = FakeActionMessage(tool_calls=[SimpleNamespace(function=function)])
    model = OpenAIWorkerModel(client=cast(Any, FakeActionClient(message)), model="test-model")

    action = await model.next_action([])

    assert action.finish is not None
    assert action.finish.reason == "已覆盖任务要求的直接证据。"
    assert WORKER_FINISH_SCHEMA["function"]["strict"] is True
    assert WORKER_FINISH_SCHEMA["function"]["parameters"]["additionalProperties"] is False

    prose_message = FakeActionMessage(
        tool_calls=[],
        content='我认为可以结束。\n{"goal_met": true}',
    )
    prose_model = OpenAIWorkerModel(
        client=cast(Any, FakeActionClient(prose_message)),
        model="test-model",
    )
    with pytest.raises(ValueError, match="submit_worker_finish"):
        await prose_model.next_action([])


async def test_worker_executes_independent_calls_concurrently_within_round_budget() -> None:
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
    assert feedback.worker_rounds_used == 2
    assert feedback.stop_reason == "no_public_evidence"
    assert "已使用决策轮：0" in model.runtime_messages[0]
    assert "已使用决策轮：1" in model.runtime_messages[1]
    assert "单轮并行工具调用上限" in model.runtime_messages[0]


class OverParallelModel:
    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        del messages
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": None},
            tool_calls=[
                WorkerToolCall(
                    tool_name="web_search",
                    tool_call_id=f"search-{index}",
                    arguments={"query": f"path-{index}"},
                )
                for index in range(9)
            ],
        )

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        return []

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        raise AssertionError("不应触发覆盖判断")


async def test_worker_rejects_rounds_that_exceed_the_parallel_call_limit() -> None:
    repository = FakeRepository()
    tracker = {"active": 0, "max_active": 0}
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            ConcurrentTool("web_search", tracker),
            ConcurrentTool("web_fetch", tracker),
            ConcurrentTool("save_findings", tracker),
        ],
        OverParallelModel(),
    )

    with pytest.raises(ValueError, match="per-round limit"):
        await worker.run(uuid4(), _task(), worker_id="rw_over_parallel")


async def test_partial_fetch_event_does_not_hide_the_tool_error() -> None:
    repository = FakeRepository()
    tracker = {"active": 0, "max_active": 0}
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            ConcurrentTool("web_search", tracker),
            PartialFetchTool(repository),
            ConcurrentTool("save_findings", tracker),
        ],
        PartialFailureModel(),
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_partial")

    matching = [
        event for event in repository.tool_events if event.get("tool_call_id") == "fetch-partial"
    ]
    assert len(matching) == 2
    assert "doc_id" in matching[0]
    assert matching[1]["error"] == "Exa highlights 为空"
    assert feedback.tool_calls_used == 0


async def test_failed_tool_calls_still_consume_rounds_but_not_call_count() -> None:
    repository = FakeRepository()
    tracker = {"active": 0, "max_active": 0}
    model = FailThenSucceedModel()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            FailingThenSucceedingTool("web_search", fail_times=1),
            ConcurrentTool("web_fetch", tracker),
            ConcurrentTool("save_findings", tracker),
        ],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_fail_budget")

    assert model.action_calls == 3
    assert feedback.tool_calls_used == 1
    assert feedback.worker_rounds_used == 3
    assert feedback.stop_reason == "no_public_evidence"
    assert "已使用决策轮：0" in model.runtime_messages[0]
    assert "已使用决策轮：1" in model.runtime_messages[1]
    assert "已使用决策轮：2" in model.runtime_messages[2]
    assert "剩余决策轮：3" in model.runtime_messages[2]


async def test_two_failed_batches_do_not_force_worker_to_stop() -> None:
    repository = FakeRepository()
    tracker = {"active": 0, "max_active": 0}
    model = FailTwiceThenSucceedModel()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            FailingThenSucceedingTool("web_search", fail_times=2),
            ConcurrentTool("web_fetch", tracker),
            ConcurrentTool("save_findings", tracker),
        ],
        model,
    )

    feedback = await worker.run(
        uuid4(),
        _task(max_worker_rounds=4),
        worker_id="rw_retry_paths",
    )

    assert model.action_calls == 4
    assert feedback.tool_calls_used == 1
    assert feedback.stop_reason == "no_public_evidence"


async def test_persistent_failures_stop_at_worker_round_limit_without_using_tool_budget() -> None:
    repository = FakeRepository()
    tracker = {"active": 0, "max_active": 0}
    model = AlwaysFailingModel()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            FailingThenSucceedingTool("web_search", fail_times=100),
            ConcurrentTool("web_fetch", tracker),
            ConcurrentTool("save_findings", tracker),
        ],
        model,
    )

    feedback = await worker.run(
        uuid4(),
        _task(max_worker_rounds=3),
        worker_id="rw_round_limit",
    )

    assert model.action_calls == 3
    assert feedback.tool_calls_used == 0
    assert feedback.stop_reason == "worker_rounds_exhausted"
    assert feedback.finish_reason == "Worker 决策轮已用尽（3/3）。"


async def test_worker_checks_coverage_after_save_and_stops_with_budget_remaining() -> None:
    repository = FakeRepository()
    tracker = {"active": 0, "max_active": 0}
    model = SaveThenSatisfiedModel()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            ConcurrentTool("web_search", tracker),
            ConcurrentTool("web_fetch", tracker),
            SavingTool(repository),
        ],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_save")

    assert model.coverage_checks == 1
    assert model.action_calls == 1
    assert feedback.goal_met is True
    assert feedback.stop_reason == "expected_evidence_satisfied"
    assert feedback.finish_reason == "已取得满足任务要求的直接证据。"
    assert feedback.tool_calls_used == 1


async def test_worker_checks_coverage_after_every_save_until_goal_is_met() -> None:
    repository = FakeRepository()
    tracker = {"active": 0, "max_active": 0}
    model = SaveThenSatisfiedModel(complete_after=2)
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            ConcurrentTool("web_search", tracker),
            ConcurrentTool("web_fetch", tracker),
            SavingTool(repository),
        ],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_save")

    assert model.coverage_checks == 2
    assert model.action_calls == 2
    assert feedback.goal_met is True
    assert feedback.stop_reason == "expected_evidence_satisfied"
    assert feedback.tool_calls_used == 2


async def test_worker_binds_summary_text_to_ledger_id_without_model_copying_uuid() -> None:
    repository = FakeRepository()
    assertion_id = UUID("258596fe-0b97-4998-b94b-2dfc4b88c1be")
    repository.assertions = [
        Assertion(
            assertion_id=assertion_id,
            statement="体验舒适度与空间管理相关。",
            excerpt_ids=[uuid4()],
            topic_tags=["体验舒适度"],
            produced_by={"task_id": str(uuid4()), "worker": "rw_test"},
        )
    ]
    tracker = {"active": 0, "max_active": 0}
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            ConcurrentTool("web_search", tracker),
            ConcurrentTool("web_fetch", tracker),
            ConcurrentTool("save_findings", tracker),
        ],
        SlotSummaryModel(),
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_test")

    assert feedback.summary.items == [
        SummaryItem(assertion_id=assertion_id, text="压缩后的断言文本。")
    ]
    assert feedback.finish_reason == "仍缺少独立来源。"
