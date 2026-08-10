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
    worker_coverage_message,
    worker_system_prompt,
)
from prospector.agents.research_worker import (
    AUTO_FETCH_TOP_N,
    AUTO_FETCH_TOP_N_BY_STAGE,
    KEEP_FULL_FETCH_ROUNDS,
    WORKER_ACTION_RESPONSE_FORMAT,
    WORKER_ACTION_SCHEMA,
    EvidenceSourceRegistry,
    OpenAIWorkerModel,
    ResearchWorker,
    SummaryItem,
    WorkerCoverageAssessment,
    WorkerFinish,
    WorkerModelAction,
    WorkerToolCall,
)
from prospector.flow.cancellation import JobCancelledError
from prospector.schemas.brief import UserConstraints
from prospector.schemas.evidence import Assertion
from prospector.schemas.plan import ResearchTask, TaskBudget
from prospector.store.repositories import ResearchRepository
from prospector.tools.base import ToolContext


class FakeRepository:
    def __init__(self) -> None:
        self.tool_events: list[dict[str, Any]] = []
        self.round_events: list[tuple[int, int]] = []
        self.assertions: list[Assertion] = []
        self.user_constraints = UserConstraints()

    def get_job_user_constraints(self, job_id: UUID) -> UserConstraints:
        del job_id
        return self.user_constraints

    def record_worker_round(
        self,
        job_id: UUID,
        task_id: UUID,
        *,
        rounds_used: int,
        rounds_limit: int,
    ) -> None:
        del job_id, task_id
        self.round_events.append((rounds_used, rounds_limit))

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
    def __init__(self, *, content: str | None = None) -> None:
        self.content = content


class FakeActionCompletions:
    def __init__(self, message: FakeActionMessage) -> None:
        self.message = message
        self.request: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.request = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])


class FakeActionClient:
    def __init__(self, message: FakeActionMessage) -> None:
        self.completions = FakeActionCompletions(message)
        self.chat = SimpleNamespace(completions=self.completions)


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
        assert any(
            "上一轮运行结果" in str(item.get("content"))
            and "search-a" in str(item.get("content"))
            and "search-b" in str(item.get("content"))
            for item in messages
        )
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


class SearchForPartialFetchTool:
    name = "web_search"

    async def __call__(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        return {"results": [{"url": "https://example.test/source"}]}


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
                        tool_name="web_search",
                        tool_call_id="search-partial",
                        arguments={"query": "寻找可抓取的直接证据"},
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
            assert any("temporarily unavailable" in str(item.get("content")) for item in messages)
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
                    "temporarily unavailable" in str(item.get("content")) for item in messages
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
        if self.action_calls == 1:
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="web_search",
                        tool_call_id="search-for-save",
                        arguments={"query": "查找目标事实的直接证据"},
                    )
                ],
            )
        save_number = self.action_calls - 1
        if save_number > self.complete_after:
            raise AssertionError("落证满足 expected_evidence 后不应再请求下一步动作")
        if save_number > 1:
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
                        "findings": [
                            {
                                "source_refs": ["s1:h1"],
                                "statement": "目标事实已有带原文定位的直接证据。",
                                "topic_tags": ["目标事实"],
                            }
                        ],
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


async def test_openai_worker_action_uses_json_object_format() -> None:
    content = json.dumps(
        {
            "action": "finish",
            "searches": [],
            "save_batches": [],
            "finish": {
                "goal_met": True,
                "stop_reason": "expected_evidence_satisfied",
                "reason": "已覆盖任务要求的直接证据。",
            },
        },
        ensure_ascii=False,
    )
    message = FakeActionMessage(content=content)
    client = FakeActionClient(message)
    model = OpenAIWorkerModel(client=cast(Any, client), model="test-model")

    action = await model.next_action([])

    assert action.finish is not None
    assert action.finish.reason == "已覆盖任务要求的直接证据。"
    assert WORKER_ACTION_RESPONSE_FORMAT == {"type": "json_object"}
    assert json.loads(WORKER_ACTION_SCHEMA)["additionalProperties"] is False
    system_prompt = worker_system_prompt(action_schema=WORKER_ACTION_SCHEMA)
    assert "JSON Schema" in system_prompt
    assert client.completions.request is not None
    assert client.completions.request["response_format"] == WORKER_ACTION_RESPONSE_FORMAT
    assert "tools" not in client.completions.request

    prose_message = FakeActionMessage(content='我认为可以结束。\n{"goal_met": true}')
    prose_model = OpenAIWorkerModel(
        client=cast(Any, FakeActionClient(prose_message)),
        model="test-model",
    )
    with pytest.raises(ValidationError):
        await prose_model.next_action([])


async def test_openai_worker_save_action_keeps_findings_as_an_array() -> None:
    content = json.dumps(
        {
            "action": "save",
            "searches": [],
            "save_batches": [
                {
                    "findings": [
                        {
                            "source_refs": ["s1:h1"],
                            "statement": "带原文定位的事实。",
                            "topic_tags": [],
                        }
                    ],
                }
            ],
            "finish": None,
        },
        ensure_ascii=False,
    )
    model = OpenAIWorkerModel(
        client=cast(Any, FakeActionClient(FakeActionMessage(content=content))),
        model="test-model",
    )

    action = await model.next_action([])

    assert action.finish is None
    assert action.tool_calls is not None
    assert len(action.tool_calls) == 1
    call = action.tool_calls[0]
    assert call.tool_name == "save_findings"
    assert call.tool_call_id.startswith("worker-save-")
    assert isinstance(call.arguments["findings"], list)
    assert "doc_id" not in call.arguments
    assert "view_id" not in call.arguments


def test_source_registry_resolves_runtime_refs_without_exposing_storage_ids() -> None:
    registry = EvidenceSourceRegistry()
    doc_id = str(uuid4())
    view_id = str(uuid4())

    exposed = registry.expose_fetch_result(
        {
            "doc_id": doc_id,
            "view_id": view_id,
            "media_type": "html",
            "view_kind": "exa_highlights",
            "items": [{"text": "原文证据。", "source_ids": ["h1"]}],
        }
    )
    resolved = registry.resolve_save_batch(
        {
            "findings": [
                {
                    "source_refs": ["s1:h1"],
                    "statement": "原文支持目标事实。",
                    "topic_tags": [],
                }
            ]
        }
    )

    assert exposed["items"] == [{"source_ref": "s1:h1", "text": "原文证据。"}]
    assert doc_id not in str(exposed)
    assert view_id not in str(exposed)
    assert resolved[0]["doc_id"] == doc_id
    assert resolved[0]["view_id"] == view_id
    assert resolved[0]["findings"][0]["source_ids"] == ["h1"]

    with pytest.raises(ValueError, match="not available in the current worker"):
        registry.resolve_save_batch(
            {
                "findings": [
                    {
                        "source_refs": ["s99:h1"],
                        "statement": "不得接受编造的引用。",
                        "topic_tags": [],
                    }
                ]
            }
        )


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
    assert repository.round_events == [(1, 5), (2, 5)]
    assert feedback.stop_reason == "no_public_evidence"
    assert "已使用决策轮：0" in model.runtime_messages[0]
    assert "已使用决策轮：1" in model.runtime_messages[1]
    assert "单轮并行工具调用上限" in model.runtime_messages[0]


async def test_worker_stops_after_persisting_current_round_when_cancelled() -> None:
    class CancellingRepository(FakeRepository):
        cancelled = False

        def record_worker_round(self, *args: Any, **kwargs: Any) -> None:
            super().record_worker_round(*args, **kwargs)
            self.cancelled = True

    repository = CancellingRepository()
    tracker = {"active": 0, "max_active": 0}
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            ConcurrentTool("web_search", tracker),
            ConcurrentTool("web_fetch", tracker),
            ConcurrentTool("save_findings", tracker),
        ],
        ParallelWorkerModel(),
        cancel_requested=lambda _job_id: repository.cancelled,
    )

    with pytest.raises(JobCancelledError):
        await worker.run(uuid4(), _task(), worker_id="rw_cancel")
    assert repository.round_events == [(1, 5)]
    assert tracker["max_active"] == 0


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
            SearchForPartialFetchTool(),
            PartialFetchTool(repository),
            ConcurrentTool("save_findings", tracker),
        ],
        PartialFailureModel(),
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_partial")

    matching = [event for event in repository.tool_events if event.get("tool") == "web_fetch"]
    assert len(matching) == 2
    assert "doc_id" in matching[0]
    assert matching[1]["error"] == "Exa highlights 为空"
    assert feedback.tool_calls_used == 1


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
    model = SaveThenSatisfiedModel()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            SearchWithResultsTool([{"url": "https://example.test/evidence"}]),
            RecordingFetchTool(),
            SavingTool(repository),
        ],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_save")

    assert model.coverage_checks == 1
    assert model.action_calls == 2
    assert feedback.goal_met is True
    assert feedback.stop_reason == "expected_evidence_satisfied"
    assert feedback.finish_reason == "已取得满足任务要求的直接证据。"
    assert feedback.tool_calls_used == 3


async def test_worker_checks_coverage_after_every_save_until_goal_is_met() -> None:
    repository = FakeRepository()
    model = SaveThenSatisfiedModel(complete_after=2)
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            SearchWithResultsTool([{"url": "https://example.test/evidence"}]),
            RecordingFetchTool(),
            SavingTool(repository),
        ],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_save")

    assert model.coverage_checks == 2
    assert model.action_calls == 3
    assert feedback.goal_met is True
    assert feedback.stop_reason == "expected_evidence_satisfied"
    assert feedback.tool_calls_used == 4


def test_coverage_message_hands_the_worker_the_same_ledger_the_judge_read() -> None:
    """The judge decides from stored assertions in a clean context.

    Echoing that ledger back keeps the Worker deciding against the same record, instead
    of against a thread the judge never sees.
    """
    assertions = [
        Assertion(
            assertion_id=uuid4(),
            statement="东京都 2023 年调查的样本量为 3400 人。",
            excerpt_ids=[uuid4()],
            topic_tags=[],
            produced_by={"task_id": str(uuid4()), "worker": "rw_test"},
        )
    ]
    message = worker_coverage_message("缺少大阪的同口径数据", assertions)

    assert "东京都 2023 年调查的样本量为 3400 人。" in message
    assert "缺少大阪的同口径数据" in message
    assert "共 1 条" in message

    assert "尚无已落库断言" in worker_coverage_message("缺少直接证据", [])


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


# ---------------------------------------------------------------------------
# Auto-fetch after web_search
# ---------------------------------------------------------------------------


class SearchWithResultsTool:
    """web_search tool returning a fixed list of URL results."""

    name = "web_search"

    def __init__(self, results: list[dict[str, str]]) -> None:
        self.results = results

    async def __call__(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        del arguments, context
        return {"results": self.results}


class RecordingFetchTool:
    """web_fetch tool that records which URLs were fetched."""

    name = "web_fetch"

    def __init__(self, *, fail_urls: set[str] | None = None) -> None:
        self.fetched_urls: list[str] = []
        self.fail_urls = fail_urls or set()

    async def __call__(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        url = str(arguments.get("url", ""))
        self.fetched_urls.append(url)
        if url in self.fail_urls:
            raise RuntimeError(f"Exa returned no content for {url}")
        return {
            "doc_id": str(uuid4()),
            "view_id": str(uuid4()),
            "media_type": "html",
            "view_kind": "exa_highlights",
            "items": [{"text": "目标事实的原文证据。", "source_ids": ["h1"]}],
        }


class SearchThenFinishModel:
    """Issues one web_search round then finishes."""

    def __init__(self, query: str = "test query") -> None:
        self.calls = 0
        self.query = query
        self.seen_messages: list[dict[str, Any]] = []

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        self.seen_messages = list(messages)
        self.calls += 1
        if self.calls == 1:
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="web_search",
                        tool_call_id="search-1",
                        arguments={"query": self.query},
                    )
                ],
            )
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": "finish"},
            finish=WorkerFinish(
                goal_met=False,
                stop_reason="no_public_evidence",
                reason="搜索结果均无可保存证据。",
            ),
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
        raise AssertionError("没有落证时不应检查覆盖度")


class ParallelSearchThenFinishModel:
    """Issues two parallel web_search calls then finishes."""

    def __init__(self) -> None:
        self.calls = 0

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        self.calls += 1
        if self.calls == 1:
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="web_search",
                        tool_call_id="search-a",
                        arguments={"query": "query a"},
                    ),
                    WorkerToolCall(
                        tool_name="web_search",
                        tool_call_id="search-b",
                        arguments={"query": "query b"},
                    ),
                ],
            )
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": "finish"},
            finish=WorkerFinish(
                goal_met=False,
                stop_reason="no_public_evidence",
                reason="两条搜索路径均未发现可保存证据。",
            ),
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
        raise AssertionError("没有落证时不应检查覆盖度")


async def test_auto_fetch_after_web_search_fans_out_top_n_urls() -> None:
    urls = [f"https://example.test/{i}" for i in range(AUTO_FETCH_TOP_N)]
    search_tool = SearchWithResultsTool(
        [{"url": url, "title": f"Result {i}"} for i, url in enumerate(urls)]
    )
    fetch_tool = RecordingFetchTool()
    model = SearchThenFinishModel()
    repository = FakeRepository()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [search_tool, fetch_tool, ConcurrentTool("save_findings", {"active": 0, "max_active": 0})],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_auto_fetch")

    assert fetch_tool.fetched_urls == urls
    assert feedback.worker_rounds_used == 2
    # 1 search + AUTO_FETCH_TOP_N successful fetches
    assert feedback.tool_calls_used == 1 + AUTO_FETCH_TOP_N
    assert feedback.stop_reason == "no_public_evidence"


async def test_auto_fetch_deduplicates_urls_across_parallel_searches() -> None:
    shared_urls = [f"https://example.test/{i}" for i in range(AUTO_FETCH_TOP_N)]
    extra_url = "https://example.test/unique"

    # Search A returns shared_urls; Search B returns shared_urls[1:] + extra_url
    class DualSearchTool:
        name = "web_search"

        def __init__(self) -> None:
            self.call_count = 0

        async def __call__(
            self,
            arguments: dict[str, Any],
            context: ToolContext,
        ) -> dict[str, Any]:
            self.call_count += 1
            if self.call_count == 1:
                return {"results": [{"url": u} for u in shared_urls]}
            return {"results": [{"url": u} for u in shared_urls[1:]] + [{"url": extra_url}]}

    fetch_tool = RecordingFetchTool()
    model = ParallelSearchThenFinishModel()
    repository = FakeRepository()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            DualSearchTool(),
            fetch_tool,
            ConcurrentTool("save_findings", {"active": 0, "max_active": 0}),
        ],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_dedup")

    # All shared_urls + extra_url should be fetched exactly once
    assert len(fetch_tool.fetched_urls) == len(set(fetch_tool.fetched_urls))
    assert set(fetch_tool.fetched_urls) == set(shared_urls) | {extra_url}
    assert feedback.worker_rounds_used == 2


async def test_auto_fetch_handles_errors_without_breaking_loop() -> None:
    good_url = "https://example.test/good"
    bad_url = "https://example.test/bad"
    search_tool = SearchWithResultsTool([{"url": good_url}, {"url": bad_url}])
    fetch_tool = RecordingFetchTool(fail_urls={bad_url})
    model = SearchThenFinishModel()
    repository = FakeRepository()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [search_tool, fetch_tool, ConcurrentTool("save_findings", {"active": 0, "max_active": 0})],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_fetch_err")

    # Both URLs were attempted
    assert good_url in fetch_tool.fetched_urls
    assert bad_url in fetch_tool.fetched_urls
    # Only the successful fetch counts
    assert feedback.tool_calls_used == 1 + 1  # 1 search + 1 good fetch
    assert feedback.worker_rounds_used == 2
    assert feedback.stop_reason == "no_public_evidence"


async def test_all_auto_fetches_failed_keeps_query_but_hides_search_results() -> None:
    query = "哪些官方材料能够直接证明目标公司的扩张计划？"
    fetched_urls = ["https://example.test/failed-a", "https://example.test/failed-b"]
    hidden_url = "https://example.test/search-only"
    search_tool = SearchWithResultsTool(
        [
            {
                "url": fetched_urls[0],
                "title": "raw failed title a",
                "summary": "raw failed summary a",
            },
            {
                "url": fetched_urls[1],
                "title": "raw failed title b",
                "summary": "raw failed summary b",
            },
            {
                "url": hidden_url,
                "title": "raw hidden title",
                "summary": "raw hidden summary",
            },
        ]
    )
    fetch_tool = RecordingFetchTool(fail_urls=set(fetched_urls))
    model = SearchThenFinishModel(query)
    repository = FakeRepository()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            search_tool,
            fetch_tool,
            ConcurrentTool("save_findings", {"active": 0, "max_active": 0}),
        ],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_all_fetch_failed")

    result_message = next(
        str(message["content"])
        for message in model.seen_messages
        if "上一轮运行结果" in str(message.get("content"))
    )
    assert fetch_tool.fetched_urls == fetched_urls
    assert query in result_message
    assert '"result_count": 3' in result_message
    assert "raw failed title" not in result_message
    assert "raw failed summary" not in result_message
    assert "raw hidden title" not in result_message
    assert "raw hidden summary" not in result_message
    assert hidden_url not in result_message
    assert all(f"Exa returned no content for {url}" in result_message for url in fetched_urls)
    assert feedback.tool_calls_used == 1


async def test_partial_auto_fetch_success_keeps_complete_search_results() -> None:
    good_url = "https://example.test/good-context"
    bad_url = "https://example.test/bad-context"
    search_tool = SearchWithResultsTool(
        [
            {"url": good_url, "title": "visible good title"},
            {"url": bad_url, "title": "visible bad title"},
        ]
    )
    fetch_tool = RecordingFetchTool(fail_urls={bad_url})
    model = SearchThenFinishModel()
    repository = FakeRepository()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [
            search_tool,
            fetch_tool,
            ConcurrentTool("save_findings", {"active": 0, "max_active": 0}),
        ],
        model,
    )

    await worker.run(uuid4(), _task(), worker_id="rw_partial_fetch_context")

    result_message = next(
        str(message["content"])
        for message in model.seen_messages
        if "上一轮运行结果" in str(message.get("content"))
    )
    assert "visible good title" in result_message
    assert "visible bad title" in result_message
    assert good_url in result_message
    assert bad_url in result_message


async def test_auto_fetch_respects_top_n_limit() -> None:
    num_results = AUTO_FETCH_TOP_N + 3  # more than top_n
    urls = [f"https://example.test/{i}" for i in range(num_results)]
    search_tool = SearchWithResultsTool([{"url": u} for u in urls])
    fetch_tool = RecordingFetchTool()
    model = SearchThenFinishModel()
    repository = FakeRepository()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [search_tool, fetch_tool, ConcurrentTool("save_findings", {"active": 0, "max_active": 0})],
        model,
    )

    feedback = await worker.run(uuid4(), _task(), worker_id="rw_top_n")

    # Only top-N URLs should be fetched
    assert len(fetch_tool.fetched_urls) == AUTO_FETCH_TOP_N
    assert fetch_tool.fetched_urls == urls[:AUTO_FETCH_TOP_N]
    assert feedback.tool_calls_used == 1 + AUTO_FETCH_TOP_N


class RepeatingSearchModel:
    """Searches every round until the round budget runs out."""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[dict[str, Any]] = []

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        self.seen_messages = [dict(message) for message in messages]
        self.calls += 1
        return WorkerModelAction(
            assistant_message={"role": "assistant", "content": None},
            tool_calls=[
                WorkerToolCall(
                    tool_name="web_search",
                    tool_call_id=f"search-{self.calls}",
                    arguments={"query": f"第 {self.calls} 个证据缺口的检索问题？"},
                )
            ],
        )

    async def assess_coverage(self, *args: Any, **kwargs: Any) -> WorkerCoverageAssessment:
        del args, kwargs
        return WorkerCoverageAssessment(goal_met=False, reason="仍缺少直接证据")

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        return [item.statement for item in assertions]


async def test_fetched_bodies_leave_the_thread_once_they_are_no_longer_current() -> None:
    """Old page bodies are dropped: the Evidence Store is the record, not the thread.

    Stale full text both inflates every later round and competes with the gap the
    Worker is currently being told to close.
    """
    rounds = KEEP_FULL_FETCH_ROUNDS + 3
    search_tool = SearchWithResultsTool([{"url": "https://example.test/a"}])
    fetch_tool = RecordingFetchTool()
    model = RepeatingSearchModel()
    repository = FakeRepository()
    worker = ResearchWorker(
        cast(ResearchRepository, repository),
        [search_tool, fetch_tool, ConcurrentTool("save_findings", {"active": 0, "max_active": 0})],
        model,
    )

    await worker.run(uuid4(), _task(max_worker_rounds=rounds), worker_id="rw_prune")

    results = [
        str(message["content"])
        for message in model.seen_messages
        if message["role"] == "user" and "运行结果" in str(message["content"])
    ]
    pruned = [
        str(message["content"])
        for message in model.seen_messages
        if message["role"] == "user" and "原文已移出上下文" in str(message["content"])
    ]
    assert len(results) == KEEP_FULL_FETCH_ROUNDS
    assert pruned, "older rounds must be replaced by a summary"

    # The summary keeps what stops the Worker repeating itself, and drops the source
    # refs along with the text so nothing can be saved from material it cannot read.
    assert "https://example.test/a" in pruned[0]
    assert "第 1 个证据缺口的检索问题？" in pruned[0]
    assert "目标事实的原文证据。" not in pruned[0]


def test_auto_fetch_depth_follows_the_research_stage() -> None:
    """scout screens breadth-first, so it should not pull the most full text."""
    assert AUTO_FETCH_TOP_N_BY_STAGE["scout"] < AUTO_FETCH_TOP_N_BY_STAGE["verify"]
    assert AUTO_FETCH_TOP_N_BY_STAGE["verify"] < AUTO_FETCH_TOP_N_BY_STAGE["deep_dive"]


async def test_scout_fetches_fewer_bodies_than_deep_dive() -> None:
    urls = [f"https://example.test/{i}" for i in range(5)]

    async def fetched_for(stage: str) -> list[str]:
        fetch_tool = RecordingFetchTool()
        worker = ResearchWorker(
            cast(ResearchRepository, FakeRepository()),
            [
                SearchWithResultsTool([{"url": url} for url in urls]),
                fetch_tool,
                ConcurrentTool("save_findings", {"active": 0, "max_active": 0}),
            ],
            SearchThenFinishModel(),
        )
        task = _task().model_copy(update={"research_stage": stage, "subjects": ["目标公司"]})
        await worker.run(uuid4(), task, worker_id=f"rw_{stage}")
        return fetch_tool.fetched_urls

    assert len(await fetched_for("scout")) == AUTO_FETCH_TOP_N_BY_STAGE["scout"]
    assert len(await fetched_for("deep_dive")) == AUTO_FETCH_TOP_N_BY_STAGE["deep_dive"]
