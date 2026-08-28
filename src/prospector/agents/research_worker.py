"""Bounded Research Worker tool loop with clean assertion-projection handoff."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID, uuid4

from openai import AsyncOpenAI
from openai.types.shared_params import ResponseFormatJSONObject
from opentelemetry import trace
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from prospector.agents.llm import get_async_openai_client, mid_model, no_thinking_extra_body
from prospector.agents.prompts.research_worker import (
    worker_constraints_message,
    worker_coverage_message,
    worker_coverage_prompt,
    worker_runtime_message,
    worker_summary_prompt,
    worker_summary_slot,
    worker_system_prompt,
    worker_task_message,
)
from prospector.agents.usage import record_response_usage
from prospector.deterministic.gates import InformationGainCounter
from prospector.flow.cancellation import JobCancelledError
from prospector.obs.logging import get_logger
from prospector.schemas.evidence import Assertion, FindingInput
from prospector.schemas.plan import ResearchTask
from prospector.store.repositories import ResearchRepository
from prospector.tools.base import ToolContext, WorkerTool
from prospector.tools.save_findings import SaveFindingsArguments

WorkerDeclaredStopReason = Literal[
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
# Rounds cap depth; this caps per-round breadth so a single round cannot fan out
# unboundedly (each result still lengthens the thread and bills the Exa API).
MAX_PARALLEL_TOOL_CALLS_PER_ROUND = 8
# Search auto-fetch is a concrete runtime capability, not a semantic research stage.
AUTO_FETCH_TOP_N = 2
# Fetched page bodies stay in the thread only while the Worker is still working on them.
# Once they are older than this, the Evidence Store is the record; leaving stale copies in
# context both costs tokens and competes with the gap the Worker is currently closing.
KEEP_FULL_FETCH_ROUNDS = 2
LLM_EMPTY_RESPONSE_RETRIES = 2
LLM_RETRY_DELAYS = (1.0, 2.0)
log = get_logger("prospector.research_worker")
tracer = trace.get_tracer("prospector.research_worker")


class EmptyLLMResponseError(RuntimeError):
    """Raised when an LLM response contains no choices."""


def _require_first_choice(response: Any, *, label: str) -> Any:
    """Validate that the LLM response has at least one choice; raise if empty."""
    choices = getattr(response, "choices", None)
    if not choices:
        finish_reason = getattr(response, "choices", None)
        raise EmptyLLMResponseError(
            f"{label}: LLM returned empty choices (finish_reason={finish_reason!r})"
        )
    return choices[0]


class WorkerFinish(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stop_reason: WorkerDeclaredStopReason
    reason: str = Field(..., min_length=1, description="为何无法继续取得必需证据")

    @field_validator("reason")
    @classmethod
    def _strip_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason is required when the worker stops")
        return value.strip()


class WorkerSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        description="围绕单个证据缺口的一句完整自然语言问题或请求",
    )


EvidenceSourceRef = Annotated[
    str,
    StringConstraints(pattern=r"^s[1-9][0-9]*:h[1-9][0-9]*$"),
]


class WorkerFindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_refs: list[EvidenceSourceRef] = Field(..., min_length=1)
    statement: str = Field(..., min_length=1)

    @field_validator("source_refs")
    @classmethod
    def _deduplicate_source_refs(cls, values: list[EvidenceSourceRef]) -> list[EvidenceSourceRef]:
        return list(dict.fromkeys(values))

    @field_validator("statement")
    @classmethod
    def _strip_statement(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("statement must not be blank")
        return text


class WorkerSaveBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[WorkerFindingInput] = Field(..., min_length=1)


class WorkerSearchAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["search"]
    searches: list[WorkerSearch] = Field(
        ...,
        min_length=1,
        max_length=MAX_PARALLEL_TOOL_CALLS_PER_ROUND,
    )


class WorkerSaveAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["save"]
    save_batches: list[WorkerSaveBatch] = Field(
        ...,
        min_length=1,
        max_length=MAX_PARALLEL_TOOL_CALLS_PER_ROUND,
    )


class WorkerFinishAction(WorkerFinish):
    action: Literal["finish"]


WorkerActionPayload = Annotated[
    WorkerSearchAction | WorkerSaveAction | WorkerFinishAction,
    Field(discriminator="action"),
]


class WorkerAction(RootModel[WorkerActionPayload]):
    """One strict, flat Worker action for a single decision round."""

    @property
    def action(self) -> Literal["search", "save", "finish"]:
        return self.root.action

    @property
    def searches(self) -> list[WorkerSearch]:
        return self.root.searches if isinstance(self.root, WorkerSearchAction) else []

    @property
    def save_batches(self) -> list[WorkerSaveBatch]:
        return self.root.save_batches if isinstance(self.root, WorkerSaveAction) else []

    @property
    def finish(self) -> WorkerFinish | None:
        if not isinstance(self.root, WorkerFinishAction):
            return None
        return WorkerFinish(stop_reason=self.root.stop_reason, reason=self.root.reason)


# DeepSeek 等供应商只支持 response_format=text/json_object，不支持 json_schema；
# 动作结构改由系统提示词内嵌 WorkerAction JSON Schema 约束。
WORKER_ACTION_RESPONSE_FORMAT: ResponseFormatJSONObject = {"type": "json_object"}

WORKER_ACTION_SCHEMA = json.dumps(WorkerAction.model_json_schema(), ensure_ascii=False)


class SummaryItem(BaseModel):
    assertion_id: UUID
    text: str = Field(..., min_length=1, max_length=1000)


class WorkerSummary(BaseModel):
    items: list[SummaryItem]
    finish_reason: str = Field(..., min_length=1)


class WorkerCoverageAssessment(BaseModel):
    goal_met: bool
    reason: str = Field(..., min_length=1)

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


@dataclass(frozen=True, slots=True)
class ResolvedEvidenceSource:
    doc_id: UUID
    view_id: UUID
    source_id: str


class EvidenceSourceRegistry:
    def __init__(self) -> None:
        self._next_source_number = 1
        self._sources: dict[str, ResolvedEvidenceSource] = {}

    def expose_fetch_result(self, result: dict[str, Any]) -> dict[str, Any]:
        source_number = self._next_source_number
        self._next_source_number += 1
        exposed_items: list[dict[str, str]] = []
        for item in result.get("items", []):
            for source_id in item.get("source_ids", []):
                source_ref = f"s{source_number}:{source_id}"
                self._sources[source_ref] = ResolvedEvidenceSource(
                    doc_id=UUID(str(result["doc_id"])),
                    view_id=UUID(str(result["view_id"])),
                    source_id=str(source_id),
                )
                exposed_items.append(
                    {"source_ref": source_ref, "text": str(item.get("text") or "")}
                )
        return {
            "media_type": result.get("media_type"),
            "view_kind": result.get("view_kind"),
            "items": exposed_items,
        }

    def resolve_save_batch(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        batch = WorkerSaveBatch.model_validate(arguments)
        grouped: dict[tuple[UUID, UUID], list[FindingInput]] = {}
        for finding in batch.findings:
            sources_by_view: dict[tuple[UUID, UUID], list[str]] = {}
            for source_ref in finding.source_refs:
                source = self._sources.get(source_ref)
                if source is None:
                    raise ValueError(
                        f"source_ref is not available in the current worker: {source_ref}"
                    )
                key = (source.doc_id, source.view_id)
                sources_by_view.setdefault(key, []).append(source.source_id)
            for key, source_ids in sources_by_view.items():
                grouped.setdefault(key, []).append(
                    FindingInput(
                        source_ids=source_ids,
                        statement=finding.statement,
                        topic_tags=[],
                    )
                )
        return [
            SaveFindingsArguments(
                doc_id=doc_id,
                view_id=view_id,
                findings=findings,
            ).model_dump(mode="json")
            for (doc_id, view_id), findings in grouped.items()
        ]


def _extract_top_urls(
    search_executions: list[ToolExecution],
    *,
    top_n: int,
) -> list[str]:
    """Extract deduplicated top-N URLs from successful web_search executions."""
    seen: set[str] = set()
    urls: list[str] = []
    for execution in search_executions:
        results = (execution.result or {}).get("results", [])
        for item in results[:top_n]:
            url = str(item.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _pruned_results_summary(round_number: int, executions: list[ToolExecution]) -> str:
    """One-line replacement for a round whose fetched bodies have left the thread.

    Keeps what the Worker needs to avoid repeating itself — which queries ran, which
    sources came back — and drops the source refs along with the text, since an
    assertion must never be saved from material the Worker can no longer read.
    """
    lines = [f"第 {round_number} 轮结果（原文已移出上下文；已保存的内容见证据清单）："]
    for execution in executions:
        if execution.call.tool_name == "web_search":
            query = str(execution.call.arguments.get("query") or "").strip()
            if query:
                lines.append(f"- 搜索：{query}")
        elif execution.call.tool_name == "web_fetch":
            url = str(execution.call.arguments.get("url") or "").strip()
            if execution.error is not None:
                lines.append(f"- 抓取失败：{url}")
            elif url:
                lines.append(f"- 已抓取：{url}")
    lines.append("如需再次引用这些原文，必须重新检索抓取，不得凭印象落证。")
    return "\n".join(lines)


def _runtime_results_message(
    executions: list[ToolExecution],
    source_registry: EvidenceSourceRegistry,
) -> str:
    fetch_executions = [
        execution for execution in executions if execution.call.tool_name == "web_fetch"
    ]
    all_fetches_failed = bool(fetch_executions) and all(
        execution.error is not None for execution in fetch_executions
    )
    results = []
    for execution in executions:
        item: dict[str, Any] = {
            "tool": execution.call.tool_name,
            "tool_call_id": execution.call.tool_call_id,
        }
        if (
            execution.call.tool_name == "web_search"
            and execution.error is None
            and all_fetches_failed
        ):
            item["query"] = str(execution.call.arguments.get("query") or "")
            item["result"] = {
                "result_count": len((execution.result or {}).get("results", [])),
            }
            results.append(item)
            continue
        if execution.error is not None:
            item["error"] = str(execution.error)
        elif execution.call.tool_name == "web_fetch":
            item["result"] = source_registry.expose_fetch_result(execution.result or {})
        else:
            item["result"] = execution.result or {}
        results.append(item)
    return "上一轮运行结果：\n" + json.dumps(
        results,
        ensure_ascii=False,
        default=str,
    )


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

    async def _create_with_retry(self, *, label: str, **kwargs: Any) -> Any:
        """Call the LLM with automatic retry on empty choices."""
        last_exc: EmptyLLMResponseError | None = None
        for attempt in range(LLM_EMPTY_RESPONSE_RETRIES + 1):
            response = await self.client.chat.completions.create(**kwargs)
            record_response_usage(response, self.model)
            try:
                return _require_first_choice(response, label=label)
            except EmptyLLMResponseError as exc:
                last_exc = exc
                if attempt < LLM_EMPTY_RESPONSE_RETRIES:
                    delay = LLM_RETRY_DELAYS[min(attempt, len(LLM_RETRY_DELAYS) - 1)]
                    log.warning(
                        "llm.empty_choices",
                        label=label,
                        attempt=attempt + 1,
                        retry_in=f"{delay}s",
                    )
                    await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        action, content = await self._request_valid_action(messages)
        assistant_message = {"role": "assistant", "content": content}
        if action.action == "finish":
            return WorkerModelAction(
                assistant_message=assistant_message,
                finish=action.finish,
            )
        if action.action == "search":
            calls = [
                WorkerToolCall(
                    tool_name="web_search",
                    tool_call_id=f"worker-search-{uuid4()}",
                    arguments=search.model_dump(mode="json"),
                )
                for search in action.searches
            ]
        else:
            calls = [
                WorkerToolCall(
                    tool_name="save_findings",
                    tool_call_id=f"worker-save-{uuid4()}",
                    arguments=batch.model_dump(mode="json"),
                )
                for batch in action.save_batches
            ]
        return WorkerModelAction(
            assistant_message=assistant_message,
            tool_calls=calls,
        )

    async def _request_valid_action(
        self, messages: list[dict[str, Any]]
    ) -> tuple[WorkerAction, str]:
        # json_object 模式不由 API 强制 schema（DeepSeek 不支持 json_schema），
        # 校验失败时把错误反馈给模型修复一次，与 Planner 的修复惯例一致。
        repair_messages = messages
        for attempt in range(2):
            choice = await self._create_with_retry(
                label="worker.next_action",
                model=self.model,
                temperature=0.0,
                messages=repair_messages,  # type: ignore[arg-type]
                response_format=WORKER_ACTION_RESPONSE_FORMAT,
                extra_body=no_thinking_extra_body(self.model),
            )
            content = choice.message.content
            if not content:
                raise ValueError("Worker returned an empty action")
            try:
                return WorkerAction.model_validate_json(content), content
            except ValidationError as exc:
                if attempt == 1:
                    raise
                log.warning("worker.action_validation_error", error=str(exc).splitlines()[:4])
                repair_messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": (
                            "上一轮动作单未通过 JSON Schema 校验，错误如下：\n"
                            f"{exc}\n"
                            "请只输出修正后、符合系统提示词中 JSON Schema 的单个 JSON 动作单，"
                            "不要任何其他文本。"
                        ),
                    },
                ]
        raise AssertionError("unreachable")

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        choice = await self._create_with_retry(
            label="worker.assess_coverage",
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
            extra_body=no_thinking_extra_body(self.model),
        )
        content = choice.message.content
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
        choice = await self._create_with_retry(
            label="worker.summarize",
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
            extra_body=no_thinking_extra_body(self.model),
        )
        calls = choice.message.tool_calls or []
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
        cancel_requested: Callable[[UUID], bool] | None = None,
    ) -> None:
        self.repository = repository
        self.tools = {tool.name: tool for tool in tools}
        if set(self.tools) != {"web_search", "web_fetch", "save_findings"}:
            raise ValueError("Research Worker must expose exactly the three research tools")
        self.model = model or OpenAIWorkerModel()
        self.cancel_requested = cancel_requested or (lambda _job_id: False)

    async def run(self, job_id: UUID, task: ResearchTask, *, worker_id: str) -> WorkerFeedback:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": worker_system_prompt(action_schema=WORKER_ACTION_SCHEMA)},
            {"role": "user", "content": worker_task_message(task)},
        ]
        constraints_message = worker_constraints_message(
            await asyncio.to_thread(self.repository.get_job_user_constraints, job_id)
        )
        if constraints_message is not None:
            messages.append({"role": "user", "content": constraints_message})
        information_gain = InformationGainCounter()
        source_registry = EvidenceSourceRegistry()
        # (message index, replacement text) for each round's results message, oldest first.
        prunable_results: list[tuple[int, str]] = []
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
            try:
                resolved_calls = (
                    source_registry.resolve_save_batch(call.arguments)
                    if call.tool_name == "save_findings"
                    else [call.arguments]
                )
                results: list[dict[str, Any]] = []
                for index, arguments in enumerate(resolved_calls, start=1):
                    tool_call_id = (
                        f"{call.tool_call_id}:{index}"
                        if len(resolved_calls) > 1
                        else call.tool_call_id
                    )
                    call_context = ToolContext(
                        job_id=job_id,
                        task_id=task.task_id,
                        worker_id=worker_id,
                        task_question=task.question,
                        tool_call_id=tool_call_id,
                    )
                    with tracer.start_as_current_span(
                        f"tool.{call.tool_name}", attributes=span_attributes
                    ):
                        results.append(await self.tools[call.tool_name](arguments, call_context))
                result = (
                    {
                        "inserted": sum(int(value.get("inserted", 0)) for value in results),
                        "assertion_ids": list(
                            dict.fromkeys(
                                assertion_id
                                for value in results
                                for assertion_id in value.get("assertion_ids", [])
                            )
                        ),
                    }
                    if call.tool_name == "save_findings"
                    else results[0]
                )
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

        async def raise_if_cancelled() -> None:
            if await asyncio.to_thread(self.cancel_requested, job_id):
                raise JobCancelledError(f"Job {job_id} was cancelled")

        while True:
            await raise_if_cancelled()
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
                        auto_fetch_top_n=AUTO_FETCH_TOP_N,
                    ),
                }
            )
            with tracer.start_as_current_span("llm.call", attributes=span_attributes):
                action = await self.model.next_action(messages)
            worker_rounds_used += 1
            await asyncio.to_thread(
                self.repository.record_worker_round,
                job_id,
                task.task_id,
                rounds_used=worker_rounds_used,
                rounds_limit=task.budget.max_worker_rounds,
            )
            await raise_if_cancelled()
            messages.append(action.assistant_message)
            if action.finish is not None:
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
            model_callable_tools = {"web_search", "save_findings"}
            unavailable = sorted(
                {call.tool_name for call in calls if call.tool_name not in model_callable_tools}
            )
            if unavailable:
                raise ValueError(f"Worker selected unavailable tools: {unavailable}")

            executions = await asyncio.gather(*(execute_tool(call) for call in calls))
            await raise_if_cancelled()
            tool_calls_used += sum(1 for execution in executions if execution.error is None)
            round_executions = list(executions)

            # --- Auto-fetch: fan out web_fetch for top-N search results ---
            search_executions = [
                e for e in executions if e.call.tool_name == "web_search" and e.error is None
            ]
            if search_executions:
                urls_to_fetch = _extract_top_urls(
                    search_executions,
                    top_n=AUTO_FETCH_TOP_N,
                )
                if urls_to_fetch:
                    fetch_calls = [
                        WorkerToolCall(
                            tool_name="web_fetch",
                            tool_call_id=f"auto-fetch-{uuid4()}",
                            arguments={"url": url},
                        )
                        for url in urls_to_fetch
                    ]
                    fetch_executions = await asyncio.gather(
                        *(execute_tool(call) for call in fetch_calls),
                    )
                    round_executions.extend(fetch_executions)
                    tool_calls_used += sum(1 for e in fetch_executions if e.error is None)

            messages.append(
                {
                    "role": "user",
                    "content": _runtime_results_message(round_executions, source_registry),
                }
            )
            prunable_results.append(
                (
                    len(messages) - 1,
                    _pruned_results_summary(worker_rounds_used, round_executions),
                )
            )
            if len(prunable_results) > KEEP_FULL_FETCH_ROUNDS:
                index, summary = prunable_results[-KEEP_FULL_FETCH_ROUNDS - 1]
                messages[index]["content"] = summary

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
                        "content": worker_coverage_message(coverage.reason, current_assertions),
                    }
                )
            if saved_in_batch and information_gain.record_save(save_inserted):
                stop_reason = "low_information_gain"
                finish_reason = "连续两批 save_findings 未产生新的断言行。"
                break

        assertions = await asyncio.to_thread(self.repository.list_assertions, task.task_id)
        await raise_if_cancelled()
        with tracer.start_as_current_span("llm.call", attributes=span_attributes):
            summary_texts = await self.model.summarize(assertions)
        await raise_if_cancelled()
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
