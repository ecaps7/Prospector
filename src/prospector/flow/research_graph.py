"""Checkpointed Planner decision loop with parallel, ledger-backed Research Workers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from prospector.agents.planner import (
    OpenAIPlannerModel,
    PlannerModel,
    PlannerModelResult,
    PlannerOutputError,
    append_decision,
    append_runtime_feedback,
    initial_planner_messages,
)
from prospector.agents.research_worker import ResearchWorker
from prospector.deterministic.budget import inject_task_budget, limits_for_effort
from prospector.deterministic.gates import dispatch_rejection, finish_rejection
from prospector.flow.state import ResearchState
from prospector.obs.logging import get_logger
from prospector.schemas.decisions import PlannerDecision
from prospector.store.object_store import ObjectStore
from prospector.store.repositories import ResearchRepository
from prospector.tools.save_findings import SaveFindingsTool
from prospector.tools.web_fetch import WebFetchTool
from prospector.tools.web_search import ExaClient, WebSearchTool

log = get_logger("prospector.research_graph")
tracer = trace.get_tracer("prospector.research_graph")


@dataclass(slots=True)
class ResearchGraphServices:
    repository: ResearchRepository
    planner: PlannerModel
    worker: ResearchWorker


def default_research_services() -> ResearchGraphServices:
    repository = ResearchRepository()
    object_store = ObjectStore(repository.settings)
    exa = ExaClient(repository.settings)
    tools = [
        WebSearchTool(repository, exa),
        WebFetchTool(repository, object_store, exa),
        SaveFindingsTool(repository, object_store),
    ]
    return ResearchGraphServices(
        repository=repository,
        planner=OpenAIPlannerModel(),
        worker=ResearchWorker(repository, tools),
    )


def _budget_message(state: Mapping[str, Any]) -> dict[str, int]:
    used = int(state.get("decision_round", 0))
    limit = int(state["decision_round_limit"])
    return {
        "decision_rounds_remaining": max(0, limit - used),
        "max_concurrency": int(state["max_concurrency"]),
    }


def _initialize_node(services: ResearchGraphServices):
    def initialize(state: ResearchState) -> dict[str, Any]:
        if state.get("planner_messages"):
            return {}
        brief = services.repository.get_brief(UUID(state["brief_id"]))
        services.repository.record_phase_changed(UUID(state["job_id"]), "research")
        limits = limits_for_effort(brief.effort)
        messages = initial_planner_messages(brief, limits)
        messages = append_runtime_feedback(
            messages,
            feedback_type="budget",
            payload={
                "decision_rounds_remaining": limits.decision_round_limit,
                "max_concurrency": limits.max_concurrency,
            },
        )
        return {
            "phase": "research",
            "plan_version": 0,
            "decision_round": 0,
            "decision_round_limit": limits.decision_round_limit,
            "max_concurrency": limits.max_concurrency,
            "max_tool_calls": limits.max_tool_calls,
            "active_task_ids": [],
            "outcome": None,
            "error_code": None,
            "planner_messages": messages,
            "route": "planner",
        }

    return initialize


def _end_for_budget(state: ResearchState, services: ResearchGraphServices) -> dict[str, Any]:
    job_id = UUID(state["job_id"])
    if services.repository.count_excerpts(job_id) == 0:
        services.repository.set_research_outcome(
            job_id,
            outcome="failed",
            error_code="research_budget_exhausted_without_evidence",
            phase="failed",
        )
        return {
            "phase": "failed",
            "outcome": "failed",
            "error_code": "research_budget_exhausted_without_evidence",
            "route": "end",
        }
    services.repository.set_research_outcome(
        job_id,
        outcome="ready_for_verifier",
        error_code=None,
        phase="verifier_pending",
    )
    return {
        "phase": "verifier_pending",
        "outcome": "ready_for_verifier",
        "error_code": None,
        "route": "end",
    }


def _planner_node(services: ResearchGraphServices):
    def planner(state: ResearchState) -> dict[str, Any]:
        if int(state["decision_round"]) >= int(state["decision_round_limit"]):
            return _end_for_budget(state, services)

        job_id = UUID(state["job_id"])
        decision_round = int(state["decision_round"]) + 1
        prompt = list(state["planner_messages"])
        stored = services.repository.get_completed_decision(job_id, decision_round, prompt)
        replayed = stored is not None
        schema_error: PlannerOutputError | None = None
        if stored is not None:
            if stored["decision_payload"] is None:
                schema_error = PlannerOutputError(
                    stored.get("feedback") or "invalid Planner decision",
                    stored["raw_output"],
                )
                model_result = None
            else:
                decision = PlannerDecision.model_validate(stored["decision_payload"])
                model_result = PlannerModelResult(
                    raw_output=stored["raw_output"],
                    decision=decision,
                )
        else:
            services.repository.begin_decision(job_id, decision_round, prompt)
            try:
                with tracer.start_as_current_span(
                    "llm.call",
                    attributes={
                        "prospector.job_id": str(job_id),
                        "prospector.decision_round": decision_round,
                        "prospector.agent": "planner",
                    },
                ):
                    model_result = services.planner.decide(prompt)
            except PlannerOutputError as exc:
                schema_error = exc
                model_result = None

        if schema_error is not None:
            exc = schema_error
            feedback = (
                str(stored["feedback"])
                if replayed and stored and stored.get("feedback")
                else f"你的输出不合法：{exc}"
            )
            if not replayed:
                services.repository.complete_decision(
                    job_id,
                    decision_round,
                    decision=None,
                    raw_output=exc.raw_output,
                    feedback=feedback,
                    status="schema_error",
                )
            services.repository.record_planner_rejection(job_id, decision_round, "schema_error")
            messages = append_decision(prompt, exc.raw_output)
            messages = append_runtime_feedback(
                messages,
                feedback_type="schema_error",
                payload={"reason": feedback},
            )
            next_state: dict[str, Any] = {
                "decision_round": decision_round,
                "planner_messages": messages,
                "route": "planner",
            }
            messages = append_runtime_feedback(
                messages,
                feedback_type="budget",
                payload=_budget_message({**state, **next_state}),
            )
            next_state["planner_messages"] = messages
            return next_state

        assert model_result is not None
        decision = model_result.decision
        if not replayed:
            services.repository.complete_decision(
                job_id,
                decision_round,
                decision=decision,
                raw_output=model_result.raw_output,
            )
        messages = append_decision(prompt, decision)

        if decision.decision == "dispatch":
            assert decision.dispatch is not None
            rejection = dispatch_rejection(
                len(decision.dispatch.tasks), int(state["max_concurrency"])
            )
            if rejection is not None:
                feedback = (
                    f"整批派发被拒绝：本轮提交 {len(decision.dispatch.tasks)} 个任务，"
                    f"并发上限为 {state['max_concurrency']}。请缩小本轮任务批次。"
                )
                services.repository.complete_decision(
                    job_id,
                    decision_round,
                    decision=decision,
                    raw_output=model_result.raw_output,
                    feedback=feedback,
                    status="rejected",
                )
                services.repository.record_planner_rejection(
                    job_id, decision_round, rejection.value
                )
                messages = append_runtime_feedback(
                    messages,
                    feedback_type="rejection",
                    payload={"reason_code": rejection.value, "reason": feedback},
                )
                route = "planner"
                result: dict[str, Any] = {"route": route}
            else:
                brief = services.repository.get_brief(UUID(state["brief_id"]))
                tasks = [
                    inject_task_budget(draft, brief.effort) for draft in decision.dispatch.tasks
                ]
                plan = services.repository.create_plan(
                    job_id,
                    decision_round,
                    tasks,
                    reason=decision.dispatch.reason,
                    trigger_verifier_run=(
                        UUID(state["last_verifier_run_id"])
                        if state.get("last_verifier_run_id")
                        else None
                    ),
                )
                result = {
                    "plan_version": plan.version,
                    "active_task_ids": [str(task_id) for task_id in plan.task_ids],
                    "route": "workers",
                }

        elif decision.decision == "reflect":
            assert decision.reflect is not None
            services.repository.record_planner_event(
                job_id,
                decision_round,
                decision,
                {"note": decision.reflect.note.splitlines()[0]},
            )
            messages = append_runtime_feedback(
                messages,
                feedback_type="reflection_recorded",
                payload={"note_recorded": True},
            )
            result = {"route": "planner"}

        else:
            assert decision.finish is not None
            rejection = finish_rejection(services.repository.count_excerpts(job_id))
            if rejection is not None:
                feedback = "finish 被拒绝：当前 Job 尚无任何 Excerpt，不能空手宣布完成。"
                services.repository.complete_decision(
                    job_id,
                    decision_round,
                    decision=decision,
                    raw_output=model_result.raw_output,
                    feedback=feedback,
                    status="rejected",
                )
                services.repository.record_planner_rejection(
                    job_id, decision_round, rejection.value
                )
                messages = append_runtime_feedback(
                    messages,
                    feedback_type="rejection",
                    payload={"reason_code": rejection.value, "reason": feedback},
                )
                result = {"route": "planner"}
            else:
                services.repository.record_planner_event(
                    job_id,
                    decision_round,
                    decision,
                    {"reason": decision.finish.reason.splitlines()[0]},
                )
                services.repository.set_research_outcome(
                    job_id,
                    outcome="ready_for_verifier",
                    error_code=None,
                    phase="verifier_pending",
                )
                return {
                    "decision_round": decision_round,
                    "planner_messages": messages,
                    "phase": "verifier_pending",
                    "outcome": "ready_for_verifier",
                    "error_code": None,
                    "route": "end",
                }

        updated_state = {**state, "decision_round": decision_round, **result}
        messages = append_runtime_feedback(
            messages,
            feedback_type="budget",
            payload=_budget_message(updated_state),
        )
        return {"decision_round": decision_round, "planner_messages": messages, **result}

    return planner


async def _run_one_worker_body(
    services: ResearchGraphServices,
    job_id: UUID,
    task_id: UUID,
    worker_index: int,
) -> dict[str, Any]:
    task = await asyncio.to_thread(services.repository.get_task, task_id)
    if task.status in {"done", "failed"}:
        stored = await asyncio.to_thread(services.repository.get_task_feedback, task_id)
        assertions = await asyncio.to_thread(services.repository.list_assertions, task_id)
        return {
            "task_id": str(task_id),
            "question": task.question,
            "research_stage": task.research_stage,
            "status": task.status,
            "assertions": [
                {"assertion_id": str(item.assertion_id), "text": item.statement}
                for item in assertions
            ],
            "stop_reason": stored.get("stop_reason"),
            "gap_note": stored.get("gap_note") or "",
            "error": stored.get("error"),
        }

    await asyncio.to_thread(services.repository.start_task, job_id, task)
    try:
        feedback = await services.worker.run(
            job_id,
            task,
            worker_id=f"rw_{worker_index:02d}",
        )
        await asyncio.to_thread(
            services.repository.finish_task,
            job_id,
            task_id,
            stop_reason=feedback.stop_reason,
            gap_note=feedback.gap_note,
            summary=feedback.summary.model_dump(mode="json"),
            tool_calls_used=feedback.tool_calls_used,
            tool_calls_limit=task.budget.max_tool_calls,
        )
        return {
            "task_id": str(task_id),
            "question": task.question,
            "research_stage": task.research_stage,
            "status": "done",
            "assertions": [item.model_dump(mode="json") for item in feedback.summary.items],
            "stop_reason": feedback.stop_reason,
            "goal_met": feedback.goal_met,
            "gap_note": feedback.gap_note,
        }
    except Exception as exc:
        assertions = await asyncio.to_thread(services.repository.list_assertions, task_id)
        tool_calls_used = await asyncio.to_thread(
            services.repository.count_task_tool_events, task_id
        )
        await asyncio.to_thread(
            services.repository.finish_task,
            job_id,
            task_id,
            stop_reason="tool_error",
            gap_note=str(exc),
            summary={"items": []},
            tool_calls_used=tool_calls_used,
            tool_calls_limit=task.budget.max_tool_calls,
            error=str(exc),
        )
        return {
            "task_id": str(task_id),
            "question": task.question,
            "research_stage": task.research_stage,
            "status": "failed",
            "assertions": [
                {"assertion_id": str(item.assertion_id), "text": item.statement}
                for item in assertions
            ],
            "stop_reason": "tool_error",
            "goal_met": False,
            "gap_note": str(exc),
            "error": str(exc),
        }


async def _run_one_worker(
    services: ResearchGraphServices,
    job_id: UUID,
    task_id: UUID,
    worker_index: int,
) -> dict[str, Any]:
    with tracer.start_as_current_span(
        "task.execute",
        attributes={
            "prospector.job_id": str(job_id),
            "prospector.task_id": str(task_id),
            "prospector.worker_id": f"rw_{worker_index:02d}",
        },
    ):
        return await _run_one_worker_body(
            services,
            job_id,
            task_id,
            worker_index,
        )


def _workers_node(services: ResearchGraphServices):
    def workers(state: ResearchState) -> dict[str, Any]:
        job_id = UUID(state["job_id"])
        task_ids = [UUID(value) for value in state["active_task_ids"]]

        async def run_all() -> list[dict[str, Any]]:
            return await asyncio.gather(
                *(
                    _run_one_worker(services, job_id, task_id, index)
                    for index, task_id in enumerate(task_ids, start=1)
                )
            )

        feedback = asyncio.run(run_all())
        messages = append_runtime_feedback(
            state["planner_messages"],
            feedback_type="worker_projection",
            payload={"tasks": feedback},
        )
        messages = append_runtime_feedback(
            messages,
            feedback_type="budget",
            payload=_budget_message(state),
        )
        return {
            "active_task_ids": [],
            "planner_messages": messages,
            "route": "planner",
        }

    return workers


def _route(state: ResearchState) -> str:
    return state["route"]


def build_research_graph(
    checkpointer: BaseCheckpointSaver,
    services: ResearchGraphServices | None = None,
) -> Any:
    runtime = services or default_research_services()
    graph = StateGraph(ResearchState)
    graph.add_node("initialize", _initialize_node(runtime))
    graph.add_node("planner", _planner_node(runtime))
    graph.add_node("workers", _workers_node(runtime))
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "planner")
    graph.add_conditional_edges(
        "planner",
        _route,
        {"planner": "planner", "workers": "workers", "end": END},
    )
    graph.add_edge("workers", "planner")
    return graph.compile(checkpointer=checkpointer)


def thread_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
