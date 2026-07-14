"""Bounded Research Worker tool loop with clean assertion-projection handoff."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from openai import AsyncOpenAI
from opentelemetry import trace
from pydantic import BaseModel, Field, model_validator

from prospector.agents.llm import get_async_openai_client, mid_model
from prospector.agents.prompts.research_worker import (
    worker_runtime_message,
    worker_summary_prompt,
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
    "budget_exhausted",
    "no_public_evidence",
    "low_information_gain",
    "blocked_by_scope",
    "tool_error",
]
MAX_CONSECUTIVE_FAILED_BATCHES = 2
tracer = trace.get_tracer("prospector.research_worker")


class WorkerFinish(BaseModel):
    goal_met: bool
    stop_reason: WorkerDeclaredStopReason
    gap_note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def _validate_goal_and_reason(self) -> WorkerFinish:
        satisfied = self.stop_reason == "expected_evidence_satisfied"
        if self.goal_met != satisfied:
            raise ValueError("goal_met must be true exactly when expected_evidence is satisfied")
        return self


class SummaryItem(BaseModel):
    assertion_id: UUID
    text: str = Field(..., min_length=1, max_length=1000)


class WorkerSummary(BaseModel):
    items: list[SummaryItem]
    gap_note: str = Field(default="", max_length=2000)


class WorkerFeedback(BaseModel):
    task_id: UUID
    goal_met: bool
    stop_reason: StopReason
    gap_note: str
    summary: WorkerSummary
    tool_calls_used: int
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

    async def summarize(
        self,
        assertions: list[Assertion],
        *,
        goal_met: bool,
        stop_reason: StopReason,
        gap_note: str,
    ) -> WorkerSummary: ...


class OpenAIWorkerModel:
    def __init__(self, client: AsyncOpenAI | None = None, model: str | None = None) -> None:
        self.client = client or get_async_openai_client()
        self.model = model or mid_model()
        self.tool_schemas = [WEB_SEARCH_SCHEMA, WEB_FETCH_SCHEMA, SAVE_FINDINGS_SCHEMA]

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
        if not message.content:
            raise ValueError("Worker returned neither a tool call nor finish JSON")
        finish = WorkerFinish.model_validate_json(message.content)
        return WorkerModelAction(assistant_message=serialized, finish=finish)

    async def summarize(
        self,
        assertions: list[Assertion],
        *,
        goal_met: bool,
        stop_reason: StopReason,
        gap_note: str,
    ) -> WorkerSummary:
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[
                {
                    "role": "user",
                    "content": worker_summary_prompt(
                        assertions,
                        goal_met=goal_met,
                        stop_reason=stop_reason,
                        gap_note=gap_note,
                    ),
                }
            ],
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("Worker summary returned empty content")
        return WorkerSummary.model_validate_json(content)


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
        consecutive_failed_batches = 0
        tool_calls_used = 0
        goal_met = False
        gap_note = ""
        stop_reason: StopReason = "budget_exhausted"
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
                    self.repository.has_task_tool_event,
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
            remaining_tool_calls = task.budget.max_tool_calls - tool_calls_used
            messages.append(
                {
                    "role": "user",
                    "content": worker_runtime_message(
                        max_tool_calls=task.budget.max_tool_calls,
                        used_tool_calls=tool_calls_used,
                        remaining_tool_calls=remaining_tool_calls,
                    ),
                }
            )
            with tracer.start_as_current_span("llm.call", attributes=span_attributes):
                action = await self.model.next_action(messages)
            messages.append(action.assistant_message)
            if action.finish is not None:
                goal_met = action.finish.goal_met
                gap_note = action.finish.gap_note.strip()
                stop_reason = action.finish.stop_reason
                break
            calls = action.tool_calls or []
            if not calls:
                raise ValueError("Worker returned neither tool calls nor a finish declaration")
            if len(calls) > remaining_tool_calls:
                if remaining_tool_calls == 0:
                    stop_reason = "budget_exhausted"
                    gap_note = "工具调用预算已用尽。"
                    break
                raise ValueError(
                    "Worker requested more tool calls than the remaining runtime budget"
                )
            unavailable = sorted(
                {call.tool_name for call in calls if call.tool_name not in self.tools}
            )
            if unavailable:
                raise ValueError(f"Worker selected unavailable tools: {unavailable}")

            tool_calls_used += len(calls)
            executions = await asyncio.gather(*(execute_tool(call) for call in calls))
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

            if all(execution.error is not None for execution in executions):
                consecutive_failed_batches += 1
            else:
                consecutive_failed_batches = 0
            if consecutive_failed_batches >= MAX_CONSECUTIVE_FAILED_BATCHES:
                stop_reason = "tool_error"
                last_error = next(
                    execution.error
                    for execution in reversed(executions)
                    if execution.error is not None
                )
                gap_note = f"连续工具批次失败，无法继续：{last_error}"
                break

            save_inserted = sum(
                int((execution.result or {}).get("inserted", 0))
                for execution in executions
                if execution.call.tool_name == "save_findings" and execution.error is None
            )
            saved_in_batch = any(
                execution.call.tool_name == "save_findings" and execution.error is None
                for execution in executions
            )
            if saved_in_batch and information_gain.record_save(save_inserted):
                stop_reason = "low_information_gain"
                gap_note = "连续两批 save_findings 未产生新的断言行。"
                break

        assertions = await asyncio.to_thread(self.repository.list_assertions, task.task_id)
        with tracer.start_as_current_span("llm.call", attributes=span_attributes):
            summary = await self.model.summarize(
                assertions,
                goal_met=goal_met,
                stop_reason=stop_reason,
                gap_note=gap_note,
            )
        allowed_ids = {assertion.assertion_id for assertion in assertions}
        cited_ids = {item.assertion_id for item in summary.items}
        unknown = cited_ids - allowed_ids
        if unknown:
            raise ValueError(
                f"Worker summary cited assertions outside its ledger: {sorted(unknown)}"
            )
        if cited_ids != allowed_ids or len(summary.items) != len(assertions):
            raise ValueError("Worker summary must project every task assertion exactly once")
        gap_note = summary.gap_note.strip()
        return WorkerFeedback(
            task_id=task.task_id,
            goal_met=goal_met,
            stop_reason=stop_reason,
            gap_note=gap_note,
            summary=summary,
            tool_calls_used=tool_calls_used,
            assertion_count=len(assertions),
        )
