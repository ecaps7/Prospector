"""Bounded Research Worker tool loop with clean assertion-projection handoff."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from openai import AsyncOpenAI
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, model_validator

from prospector.agents.llm import get_async_openai_client, mid_model
from prospector.agents.prompts.research_worker import (
    worker_coverage_message,
    worker_coverage_prompt,
    worker_runtime_message,
    worker_summary_prompt,
    worker_summary_slot,
    worker_system_prompt,
    worker_task_message,
)
from prospector.deterministic.gates import InformationGainCounter
from prospector.schemas.evidence import Assertion
from prospector.schemas.plan import ResearchTask
from prospector.store.repositories import ResearchRepository
from prospector.tools.base import ToolContext, WorkerTool
from prospector.tools.save_findings import SAVE_FINDINGS_SCHEMA
from prospector.tools.web_fetch import WEB_FETCH_SCHEMA
from prospector.tools.web_search import WEB_SEARCH_SCHEMA

WorkerDeclaredStopReason = Literal[
    "expected_evidence_satisfied",
    "no_public_evidence",
    "low_information_gain",
    "blocked_by_scope",
]
StopReason = Literal[
    "expected_evidence_satisfied",
    "worker_rounds_exhausted",
    "no_public_evidence",
    "low_information_gain",
    "blocked_by_scope",
]
SUMMARY_TOOL_NAME = "submit_worker_summary"
FINISH_TOOL_NAME = "submit_worker_finish"
# Rounds cap depth; this caps per-round breadth so a single round cannot fan out
# unboundedly (each result still lengthens the thread and bills the Exa API).
MAX_PARALLEL_TOOL_CALLS_PER_ROUND = 8
tracer = trace.get_tracer("prospector.research_worker")


class WorkerFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_met: bool
    stop_reason: WorkerDeclaredStopReason
    reason: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description="一句极短中文：为何现在结束",
    )

    @model_validator(mode="after")
    def _validate_goal_and_reason(self) -> WorkerFinish:
        satisfied = self.stop_reason == "expected_evidence_satisfied"
        if self.goal_met != satisfied:
            raise ValueError("goal_met must be true exactly when expected_evidence is satisfied")
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("reason is required when the worker stops")
        return self


WORKER_FINISH_SCHEMA = {
    "type": "function",
    "function": {
        "name": FINISH_TOOL_NAME,
        "description": "结束当前研究任务并提交简短原因。",
        "parameters": WorkerFinish.model_json_schema(),
        "strict": True,
    },
}


class SummaryItem(BaseModel):
    assertion_id: UUID
    text: str = Field(..., min_length=1, max_length=1000)


class WorkerSummary(BaseModel):
    items: list[SummaryItem]
    finish_reason: str = Field(..., min_length=1, max_length=300)


class WorkerCoverageAssessment(BaseModel):
    goal_met: bool
    reason: str = Field(..., min_length=1, max_length=300)

    @model_validator(mode="after")
    def _validate_reason(self) -> WorkerCoverageAssessment:
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("reason is required for a coverage decision")
        return self


class WorkerFeedback(BaseModel):
    task_id: UUID
    goal_met: bool
    stop_reason: StopReason
    finish_reason: str
    summary: WorkerSummary
    tool_calls_used: int
    worker_rounds_used: int
    assertion_count: int


@dataclass(slots=True)
class WorkerToolCall:
    tool_name: str
    tool_call_id: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class WorkerModelAction:
    assistant_message: dict[str, Any]
    tool_calls: list[WorkerToolCall] | None = None
    finish: WorkerFinish | None = None


@dataclass(slots=True)
class ToolExecution:
    call: WorkerToolCall
    result: dict[str, Any] | None = None
    error: Exception | None = None


class WorkerModel(Protocol):
    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction: ...

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment: ...

    async def summarize(self, assertions: list[Assertion]) -> list[str]: ...


class OpenAIWorkerModel:
    def __init__(self, client: AsyncOpenAI | None = None, model: str | None = None) -> None:
        self.client = client or get_async_openai_client()
        self.model = model or mid_model()
        self.tool_schemas = [
            WEB_SEARCH_SCHEMA,
            WEB_FETCH_SCHEMA,
            SAVE_FINDINGS_SCHEMA,
            WORKER_FINISH_SCHEMA,
        ]

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=messages,  # type: ignore[arg-type]
            tools=self.tool_schemas,  # type: ignore[arg-type]
            tool_choice="auto",
            parallel_tool_calls=True,
            extra_body={"enable_thinking": False},
        )
        message = response.choices[0].message
        serialized = message.model_dump(mode="json")
        calls = message.tool_calls or []
        if calls:
            finish_calls = [
                call
                for call in calls
                if getattr(getattr(call, "function", None), "name", None) == FINISH_TOOL_NAME
            ]
            if finish_calls:
                if len(calls) != 1:
                    raise ValueError("Worker finish must be the only action in its round")
                function = getattr(finish_calls[0], "function", None)
                if function is None:
                    raise ValueError("Worker returned a non-function finish call")
                arguments = json.loads(function.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("Worker finish arguments must be a JSON object")
                return WorkerModelAction(
                    assistant_message=serialized,
                    finish=WorkerFinish.model_validate(arguments),
                )
            tool_calls: list[WorkerToolCall] = []
            for call in calls:
                function = getattr(call, "function", None)
                if function is None:
                    raise ValueError("Worker returned a non-function tool call")
                arguments = json.loads(function.arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("Worker tool arguments must be a JSON object")
                tool_calls.append(
                    WorkerToolCall(
                        tool_name=function.name,
                        tool_call_id=call.id,
                        arguments=arguments,
                    )
                )
            return WorkerModelAction(
                assistant_message=serialized,
                tool_calls=tool_calls,
            )
        raise ValueError("Worker returned neither research tool calls nor submit_worker_finish")

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[
                {
                    "role": "user",
                    "content": worker_coverage_prompt(
                        assertions,
                        task_question=task_question,
                        expected_evidence=expected_evidence,
                    ),
                }
            ],
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("Worker coverage assessment returned empty content")
        return WorkerCoverageAssessment.model_validate_json(content)

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        if not assertions:
            return []
        slots = [worker_summary_slot(index) for index in range(len(assertions))]
        parameters = {
            "type": "object",
            "properties": {
                slot: {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 1000,
                    "description": "该固定槽位对应断言的压缩文本",
                }
                for slot in slots
            },
            "required": slots,
            "additionalProperties": False,
        }
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[
                {
                    "role": "user",
                    "content": worker_summary_prompt(assertions),
                }
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": SUMMARY_TOOL_NAME,
                        "description": "提交每个固定槽位对应的单条断言摘要。",
                        "parameters": parameters,
                        "strict": True,
                    },
                }
            ],  # type: ignore[arg-type]
            tool_choice={
                "type": "function",
                "function": {"name": SUMMARY_TOOL_NAME},
            },
            parallel_tool_calls=False,
            extra_body={"enable_thinking": False},
        )
        calls = response.choices[0].message.tool_calls or []
        call = calls[0] if len(calls) == 1 else None
        function = getattr(call, "function", None)
        if function is None or function.name != SUMMARY_TOOL_NAME:
            raise ValueError("Worker summary must make exactly one forced summary tool call")
        arguments = json.loads(function.arguments)
        if not isinstance(arguments, dict) or set(arguments) != set(slots):
            raise ValueError("Worker summary must return every fixed summary slot exactly once")
        texts: list[str] = []
        for slot in slots:
            value = arguments[slot]
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Worker summary slot {slot} must be non-empty text")
            texts.append(value.strip())
        return texts


class ResearchWorker:
    def __init__(
        self,
        repository: ResearchRepository,
        tools: list[WorkerTool],
        model: WorkerModel | None = None,
    ) -> None:
        self.repository = repository
        self.tools = {tool.name: tool for tool in tools}
        if set(self.tools) != {"web_search", "web_fetch", "save_findings"}:
            raise ValueError("Research Worker must expose exactly the three research tools")
        self.model = model or OpenAIWorkerModel()

    async def run(self, job_id: UUID, task: ResearchTask, *, worker_id: str) -> WorkerFeedback:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": worker_system_prompt()},
            {"role": "user", "content": worker_task_message(task)},
        ]
        information_gain = InformationGainCounter()
        tool_calls_used = 0
        worker_rounds_used = 0
        goal_met = False
        finish_reason = ""
        stop_reason: StopReason = "worker_rounds_exhausted"
        span_attributes = {
            "prospector.job_id": str(job_id),
            "prospector.task_id": str(task.task_id),
            "prospector.worker_id": worker_id,
        }

        async def execute_tool(call: WorkerToolCall) -> ToolExecution:
            call_context = ToolContext(
                job_id=job_id,
                task_id=task.task_id,
                worker_id=worker_id,
                task_question=task.question,
                tool_call_id=call.tool_call_id,
            )
            try:
                with tracer.start_as_current_span(
                    f"tool.{call.tool_name}", attributes=span_attributes
                ):
                    result = await self.tools[call.tool_name](call.arguments, call_context)
                return ToolExecution(call=call, result=result)
            except Exception as exc:
                recorded = await asyncio.to_thread(
                    self.repository.has_task_tool_error_event,
                    task.task_id,
                    call.tool_call_id,
                )
                if not recorded:
                    await asyncio.to_thread(
                        self.repository.record_tool_used,
                        job_id,
                        task.task_id,
                        {
                            "tool": call.tool_name,
                            "tool_call_id": call.tool_call_id,
                            "error": str(exc),
                            "result_count": 0,
                        },
                    )
                return ToolExecution(call=call, error=exc)

        while True:
            remaining_worker_rounds = task.budget.max_worker_rounds - worker_rounds_used
            if remaining_worker_rounds == 0:
                stop_reason = "worker_rounds_exhausted"
                finish_reason = (
                    f"Worker 决策轮已用尽（{worker_rounds_used}/{task.budget.max_worker_rounds}）。"
                )
                break
            messages.append(
                {
                    "role": "user",
                    "content": worker_runtime_message(
                        max_worker_rounds=task.budget.max_worker_rounds,
                        used_worker_rounds=worker_rounds_used,
                        remaining_worker_rounds=remaining_worker_rounds,
                        max_parallel_tool_calls=MAX_PARALLEL_TOOL_CALLS_PER_ROUND,
                    ),
                }
            )
            with tracer.start_as_current_span("llm.call", attributes=span_attributes):
                action = await self.model.next_action(messages)
            worker_rounds_used += 1
            messages.append(action.assistant_message)
            if action.finish is not None:
                goal_met = action.finish.goal_met
                finish_reason = action.finish.reason
                stop_reason = action.finish.stop_reason
                break
            calls = action.tool_calls or []
            if not calls:
                raise ValueError("Worker returned neither tool calls nor a finish declaration")
            if len(calls) > MAX_PARALLEL_TOOL_CALLS_PER_ROUND:
                raise ValueError(
                    "Worker requested more parallel tool calls than the per-round limit"
                )
            unavailable = sorted(
                {call.tool_name for call in calls if call.tool_name not in self.tools}
            )
            if unavailable:
                raise ValueError(f"Worker selected unavailable tools: {unavailable}")

            executions = await asyncio.gather(*(execute_tool(call) for call in calls))
            tool_calls_used += sum(1 for execution in executions if execution.error is None)
            for execution in executions:
                content: dict[str, Any]
                if execution.error is not None:
                    content = {"error": str(execution.error)}
                else:
                    content = execution.result or {}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": execution.call.tool_call_id,
                        "content": json.dumps(content, ensure_ascii=False, default=str),
                    }
                )

            save_inserted = sum(
                int((execution.result or {}).get("inserted", 0))
                for execution in executions
                if execution.call.tool_name == "save_findings" and execution.error is None
            )
            saved_in_batch = any(
                execution.call.tool_name == "save_findings" and execution.error is None
                for execution in executions
            )
            if saved_in_batch:
                current_assertions = await asyncio.to_thread(
                    self.repository.list_assertions,
                    task.task_id,
                )
                with tracer.start_as_current_span("llm.call", attributes=span_attributes):
                    coverage = await self.model.assess_coverage(
                        current_assertions,
                        task_question=task.question,
                        expected_evidence=task.expected_evidence,
                    )
                if coverage.goal_met:
                    goal_met = True
                    stop_reason = "expected_evidence_satisfied"
                    finish_reason = coverage.reason
                    break
                messages.append(
                    {
                        "role": "user",
                        "content": worker_coverage_message(coverage.reason),
                    }
                )
            if saved_in_batch and information_gain.record_save(save_inserted):
                stop_reason = "low_information_gain"
                finish_reason = "连续两批 save_findings 未产生新的断言行。"
                break

        assertions = await asyncio.to_thread(self.repository.list_assertions, task.task_id)
        with tracer.start_as_current_span("llm.call", attributes=span_attributes):
            summary_texts = await self.model.summarize(assertions)
        if len(summary_texts) != len(assertions):
            raise ValueError("Worker summary must return one text per task assertion")
        summary = WorkerSummary(
            items=[
                SummaryItem(assertion_id=assertion.assertion_id, text=text)
                for assertion, text in zip(assertions, summary_texts, strict=True)
            ],
            finish_reason=finish_reason,
        )
        return WorkerFeedback(
            task_id=task.task_id,
            goal_met=goal_met,
            stop_reason=stop_reason,
            finish_reason=finish_reason,
            summary=summary,
            tool_calls_used=tool_calls_used,
            worker_rounds_used=worker_rounds_used,
            assertion_count=len(assertions),
        )
