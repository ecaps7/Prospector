"""Checkpointed Planner decision loop with parallel, ledger-backed Research Workers."""

from __future__ import annotations

import asyncio
import hashlib
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
from prospector.agents.prompts.research_verifier import research_verifier_messages
from prospector.agents.report_writer import (
    OpenAIReportWriter,
    ReportWriterModel,
    ReportWriterOutputError,
)
from prospector.agents.research_verifier import OpenAIResearchVerifier, VerifierModel
from prospector.agents.research_worker import ResearchWorker
from prospector.deterministic.budget import inject_task_budget, limits_for_effort
from prospector.deterministic.gates import dispatch_rejection, finish_rejection
from prospector.flow.state import ResearchState
from prospector.obs.logging import get_logger
from prospector.reporting.render import render_report_draft
from prospector.schemas.decisions import PlannerDecision
from prospector.schemas.verifier import VerifierDecision
from prospector.store.object_store import ObjectStore, workspace_key
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
    verifier: VerifierModel
    writer: ReportWriterModel | None = None
    object_store: ObjectStore | None = None


class VerifierMajorGapError(RuntimeError):
    """Raised after persisting a terminal Research Verifier rejection."""


def default_research_services() -> ResearchGraphServices:
    repository = ResearchRepository()
    object_store = ObjectStore(repository.settings)
    exa = ExaClient(repository.settings)
    tools = [
        WebSearchTool(repository, exa),
        WebFetchTool(repository, object_store, exa),
        SaveFindingsTool(repository),
    ]
    return ResearchGraphServices(
        repository=repository,
        planner=OpenAIPlannerModel(),
        worker=ResearchWorker(repository, tools),
        verifier=OpenAIResearchVerifier(),
        writer=OpenAIReportWriter(),
        object_store=object_store,
    )


def _stage_budget_payload(limits: Any) -> dict[str, dict[str, int]]:
    return {
        stage: {
            "max_concurrency": budget.max_concurrency,
            "max_worker_rounds": budget.max_worker_rounds,
        }
        for stage, budget in limits.stages.items()
    }


def _stage_concurrency(state: Mapping[str, Any]) -> dict[str, int]:
    return {
        stage: int(budget["max_concurrency"])
        for stage, budget in dict(state["stage_budgets"]).items()
    }


def _research_state_message(state: Mapping[str, Any]) -> dict[str, Any]:
    used = int(state.get("decision_round", 0))
    limit = int(state["decision_round_limit"])
    return {
        "current_research_stage": str(state["current_research_stage"]),
        "decision_rounds_remaining": max(0, limit - used),
        "stage_concurrency": _stage_concurrency(state),
    }


def _initialize_node(services: ResearchGraphServices):
    def initialize(state: ResearchState) -> dict[str, Any]:
        if state.get("planner_messages"):
            return {}
        brief = services.repository.get_brief(UUID(state["brief_id"]))
        services.repository.record_phase_changed(UUID(state["job_id"]), "research")
        limits = limits_for_effort(brief.effort)
        stage_budgets = _stage_budget_payload(limits)
        messages = initial_planner_messages(brief, limits)
        messages = append_runtime_feedback(
            messages,
            feedback_type="research_state",
            payload={
                "current_research_stage": "scout",
                "decision_rounds_remaining": limits.decision_round_limit,
                "stage_concurrency": {
                    stage: budget["max_concurrency"] for stage, budget in stage_budgets.items()
                },
            },
        )
        return {
            "phase": "research",
            "current_research_stage": "scout",
            "plan_version": 0,
            "decision_round": 0,
            "decision_round_limit": limits.decision_round_limit,
            "stage_budgets": stage_budgets,
            "active_task_ids": [],
            "outcome": None,
            "error_code": None,
            "planner_messages": messages,
            "verifier_trigger": None,
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
    return {
        "phase": "verifier",
        "outcome": "ready_for_verifier",
        "error_code": None,
        "verifier_trigger": "budget_exhausted",
        "route": "verifier",
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
                feedback_type="research_state",
                payload=_research_state_message({**state, **next_state}),
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
            batch_stage = decision.dispatch.tasks[0].research_stage
            stage_concurrency = int(state["stage_budgets"][batch_stage]["max_concurrency"])
            rejection = dispatch_rejection(len(decision.dispatch.tasks), stage_concurrency)
            if rejection is not None:
                feedback = (
                    f"整批派发被拒绝：本轮提交 {len(decision.dispatch.tasks)} 个任务，"
                    f"{batch_stage} 阶段的并发上限为 {stage_concurrency}。请缩小本轮任务批次。"
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
                    "current_research_stage": decision.dispatch.tasks[0].research_stage,
                    "plan_version": plan.version,
                    "active_task_ids": [str(task_id) for task_id in plan.task_ids],
                    "last_verifier_run_id": None,
                    "report_id": None,
                    "report_markdown_ref": None,
                    "report_json_ref": None,
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
                return {
                    "decision_round": decision_round,
                    "planner_messages": messages,
                    "phase": "verifier",
                    "outcome": "ready_for_verifier",
                    "error_code": None,
                    "verifier_trigger": "planner_finish",
                    "route": "verifier",
                }

        updated_state = {**state, "decision_round": decision_round, **result}
        messages = append_runtime_feedback(
            messages,
            feedback_type="research_state",
            payload=_research_state_message(updated_state),
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
            "finish_reason": stored.get("finish_reason") or "",
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
            finish_reason=feedback.finish_reason,
            summary=feedback.summary.model_dump(mode="json"),
            tool_calls_used=feedback.tool_calls_used,
            worker_rounds_used=feedback.worker_rounds_used,
            worker_rounds_limit=task.budget.max_worker_rounds,
        )
        return {
            "task_id": str(task_id),
            "question": task.question,
            "research_stage": task.research_stage,
            "status": "done",
            "assertions": [item.model_dump(mode="json") for item in feedback.summary.items],
            "stop_reason": feedback.stop_reason,
            "goal_met": feedback.goal_met,
            "finish_reason": feedback.finish_reason,
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
            finish_reason=str(exc),
            summary={"items": []},
            tool_calls_used=tool_calls_used,
            worker_rounds_used=0,
            worker_rounds_limit=task.budget.max_worker_rounds,
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
            "finish_reason": str(exc),
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
            feedback_type="research_state",
            payload=_research_state_message(state),
        )
        return {
            "active_task_ids": [],
            "planner_messages": messages,
            "route": "planner",
        }

    return workers


def _major_gap_summary(decision: VerifierDecision) -> str:
    return "；".join(gap.description for gap in decision.gaps if gap.severity == "major")


def _verifier_node(services: ResearchGraphServices):
    def verifier(state: ResearchState) -> dict[str, Any]:
        job_id = UUID(state["job_id"])
        plan_version = int(state["plan_version"])
        decision_round = int(state["decision_round"])
        trigger = state.get("verifier_trigger")
        if trigger not in {"planner_finish", "budget_exhausted"}:
            raise RuntimeError("Verifier entered without a valid trigger")

        services.repository.record_phase_changed(
            job_id,
            "verifier",
            plan_version=plan_version,
            trigger=trigger,
        )

        stored = services.repository.get_completed_verifier_run(job_id, plan_version)
        if stored is None:
            snapshot = services.repository.build_verifier_snapshot(
                job_id,
                trigger=trigger,
                decision_round=decision_round,
                decision_round_limit=int(state["decision_round_limit"]),
            )
            full_prompt = research_verifier_messages(snapshot)
            run_id = services.repository.begin_verifier_run(
                job_id,
                evaluated_plan_version=plan_version,
                decision_round=decision_round,
                trigger=trigger,
                full_prompt=full_prompt,
            )
            with tracer.start_as_current_span(
                "llm.call",
                attributes={
                    "prospector.job_id": str(job_id),
                    "prospector.plan_version": plan_version,
                    "prospector.agent": "research_verifier",
                },
            ):
                model_result = services.verifier.verify(snapshot)
            if model_result.full_prompt != full_prompt:
                raise RuntimeError("Verifier model used a different prompt than the persisted run")
            decision = model_result.decision
            services.repository.complete_verifier_run(
                job_id,
                run_id,
                decision=decision,
                raw_output=model_result.raw_output,
                evaluated_plan_version=plan_version,
                decision_round=decision_round,
            )
            major_gap_count = sum(gap.severity == "major" for gap in decision.gaps)
            log.debug(
                "verifier.completed",
                message=decision.decision_reason,
                outcome=decision.release_decision,
                reason_code=decision.release_decision,
                job_id=str(job_id),
                plan_version=plan_version,
                verifier_run_id=str(run_id),
                major_gap_count=major_gap_count,
            )
        else:
            run_id = UUID(str(stored["run_id"]))
            decision = stored["decision"]

        if decision.release_decision == "pass":
            services.repository.set_research_outcome(
                job_id,
                outcome="ready_for_writer",
                error_code=None,
                phase="composition_pending",
            )
            return {
                "phase": "composition_pending",
                "outcome": "ready_for_writer",
                "error_code": None,
                "last_verifier_run_id": str(run_id),
                "verifier_trigger": None,
                "route": "writer",
            }

        rounds_remaining = int(state["decision_round_limit"]) - decision_round
        if rounds_remaining <= 0:
            services.repository.set_research_outcome(
                job_id,
                outcome="failed",
                error_code="verifier_major_gap",
                phase="failed",
            )
            raise VerifierMajorGapError(
                "Verifier 发现重大缺口，且 Planner 决策轮已耗尽：" + _major_gap_summary(decision)
            )

        major_gaps = [
            gap.model_dump(mode="json") for gap in decision.gaps if gap.severity == "major"
        ]
        unusable_assertions = services.repository.list_unusable_assertion_details(
            job_id, decision.assertion_dispositions
        )
        messages = append_runtime_feedback(
            state["planner_messages"],
            feedback_type="verifier_gap",
            payload={
                "verifier_run_id": str(run_id),
                "major_gaps": major_gaps,
                "unusable_assertions": unusable_assertions,
            },
        )
        messages = append_runtime_feedback(
            messages,
            feedback_type="research_state",
            payload=_research_state_message(state),
        )
        return {
            "phase": "research",
            "outcome": None,
            "error_code": None,
            "last_verifier_run_id": str(run_id),
            "planner_messages": messages,
            "verifier_trigger": None,
            "route": "planner",
        }

    return verifier


def _writer_node(services: ResearchGraphServices):
    def writer(state: ResearchState) -> dict[str, Any]:
        job_id = UUID(state["job_id"])
        raw_run_id = state.get("last_verifier_run_id")
        if raw_run_id is None:
            raise RuntimeError("Report Writer entered without a completed Verifier run")
        verifier_run_id = UUID(raw_run_id)
        if services.writer is None or services.object_store is None:
            raise RuntimeError("Report Writer services are not configured")
        services.repository.record_phase_changed(job_id, "writing")

        try:
            snapshot = services.repository.build_writer_snapshot(job_id, verifier_run_id)
            stored = services.repository.get_report_revision(job_id)
            if stored is not None and stored["report_status"] == "draft_rendered":
                return {
                    "phase": "draft_rendered",
                    "outcome": "draft_rendered",
                    "error_code": None,
                    "report_id": str(stored["report_id"]),
                    "report_markdown_ref": stored["markdown_ref"],
                    "report_json_ref": stored["json_ref"],
                    "route": "end",
                }

            if stored is not None and stored["revision_status"] in {"generated", "rendered"}:
                report_id = UUID(str(stored["report_id"]))
                draft = stored["draft"]
            else:
                from prospector.agents.prompts.report_writer import report_writer_messages

                full_prompt = report_writer_messages(snapshot)
                report_id = services.repository.begin_report_revision(
                    job_id,
                    verifier_run_id,
                    full_prompt,
                )
                with tracer.start_as_current_span(
                    "llm.call",
                    attributes={
                        "prospector.job_id": str(job_id),
                        "prospector.agent": "report_writer",
                    },
                ):
                    result = services.writer.write(snapshot)
                if result.full_prompt[: len(full_prompt)] != full_prompt:
                    raise RuntimeError("Report Writer used a different prompt than persisted")
                draft = result.draft
                services.repository.complete_report_revision(
                    report_id,
                    draft,
                    result.raw_output,
                )
        except (ReportWriterOutputError, ValueError) as exc:
            services.repository.set_research_outcome(
                job_id,
                outcome="failed",
                error_code="writer_contract_error",
                phase="failed",
            )
            log.error("writer.contract_error", job_id=str(job_id), message=str(exc))
            return {
                "phase": "failed",
                "outcome": "failed",
                "error_code": "writer_contract_error",
                "route": "end",
            }

        try:
            rendered = render_report_draft(snapshot, draft)
            base_key = workspace_key(
                services.repository.settings.workspace_id,
                "reports",
                str(job_id),
                "1",
            )
            markdown_bytes = rendered.markdown.encode("utf-8")
            json_bytes = rendered.json_text.encode("utf-8")
            markdown_ref = services.object_store.put_bytes(
                f"{base_key}/report.md",
                markdown_bytes,
                content_type="text/markdown; charset=utf-8",
            )
            json_ref = services.object_store.put_bytes(
                f"{base_key}/report.json",
                json_bytes,
                content_type="application/json; charset=utf-8",
            )
            services.repository.complete_report_render(
                job_id,
                report_id,
                markdown_ref=markdown_ref.as_uri(),
                markdown_hash="sha256:" + hashlib.sha256(markdown_bytes).hexdigest(),
                json_ref=json_ref.as_uri(),
                json_hash="sha256:" + hashlib.sha256(json_bytes).hexdigest(),
            )
        except Exception as exc:
            services.repository.set_research_outcome(
                job_id,
                outcome="failed",
                error_code="draft_render_error",
                phase="failed",
            )
            log.error("writer.render_error", job_id=str(job_id), message=str(exc))
            return {
                "phase": "failed",
                "outcome": "failed",
                "error_code": "draft_render_error",
                "report_id": str(report_id),
                "route": "end",
            }
        return {
            "phase": "draft_rendered",
            "outcome": "draft_rendered",
            "error_code": None,
            "report_id": str(report_id),
            "report_markdown_ref": markdown_ref.as_uri(),
            "report_json_ref": json_ref.as_uri(),
            "route": "end",
        }

    return writer


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
    graph.add_node("verifier", _verifier_node(runtime))
    graph.add_node("writer", _writer_node(runtime))
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "planner")
    graph.add_conditional_edges(
        "planner",
        _route,
        {
            "planner": "planner",
            "workers": "workers",
            "verifier": "verifier",
            "writer": "writer",
            "end": END,
        },
    )
    graph.add_edge("workers", "planner")
    graph.add_conditional_edges(
        "verifier",
        _route,
        {"planner": "planner", "writer": "writer", "end": END},
    )
    graph.add_edge("writer", END)
    return graph.compile(checkpointer=checkpointer)


def thread_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
