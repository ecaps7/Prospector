"""Checkpointed Planner decision loop with parallel, ledger-backed Research Workers."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
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
from prospector.agents.report_verifier import (
    OpenAIReportVerifier,
    ReportVerifierModel,
    ReportVerifierOutputError,
)
from prospector.agents.report_writer import (
    OpenAIReportWriter,
    ReportWriterModel,
    ReportWriterOutputError,
)
from prospector.agents.research_verifier import (
    OpenAIResearchVerifier,
    VerifierModel,
    VerifierOutputError,
)
from prospector.agents.research_worker import ResearchWorker
from prospector.agents.usage import collect_usage
from prospector.deterministic.budget import inject_task_budget, limits_for_effort
from prospector.deterministic.citation_render import render_verified_report
from prospector.deterministic.dirty_propagation import can_revise_again, dirty_statement_ids
from prospector.deterministic.gates import (
    dispatch_rejection,
    finish_rejection,
    mixed_stage_rejection,
    stage_order_rejection,
)
from prospector.flow.cancellation import JobCancelledError
from prospector.flow.state import ResearchState
from prospector.obs.logging import get_logger
from prospector.schemas.claims import ReportVerifierFindings
from prospector.schemas.decisions import PlannerDecision
from prospector.schemas.report import ReportDraft
from prospector.schemas.verifier import VerifierDecision
from prospector.store.object_store import ObjectStore, workspace_key
from prospector.store.repositories import ResearchRepository
from prospector.tools.save_findings import SaveFindingsTool
from prospector.tools.web_fetch import WebFetchTool
from prospector.tools.web_search import ExaClient, WebSearchTool

log = get_logger("prospector.research_graph")
tracer = trace.get_tracer("prospector.research_graph")

# Malformed Planner output is retried at the runtime layer rather than charged to the
# research budget; this cap is what keeps that retry loop terminating.
MAX_CONSECUTIVE_SCHEMA_ERRORS = 3


@dataclass(slots=True)
class ResearchGraphServices:
    repository: ResearchRepository
    planner: PlannerModel
    worker: ResearchWorker
    verifier: VerifierModel
    writer: ReportWriterModel | None = None
    report_verifier: ReportVerifierModel | None = None
    object_store: ObjectStore | None = None
    cancel_requested: Callable[[UUID], bool] = lambda _job_id: False


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
        worker=ResearchWorker(
            repository,
            tools,
            cancel_requested=repository.job_cancel_requested,
        ),
        verifier=OpenAIResearchVerifier(),
        writer=OpenAIReportWriter(),
        report_verifier=OpenAIReportVerifier(),
        object_store=object_store,
        cancel_requested=repository.job_cancel_requested,
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
    used = int(state.get("research_decisions_used", 0))
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
            "research_decisions_used": 0,
            "consecutive_schema_errors": 0,
            "decision_round_limit": limits.decision_round_limit,
            "scout_dispatched": False,
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
        if int(state["research_decisions_used"]) >= int(state["decision_round_limit"]):
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
                with (
                    tracer.start_as_current_span(
                        "llm.call",
                        attributes={
                            "prospector.job_id": str(job_id),
                            "prospector.decision_round": decision_round,
                            "prospector.agent": "planner",
                        },
                    ),
                    collect_usage(services.repository, job_id, "planner"),
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
            # A malformed decision is a formatting failure, not a research decision: it
            # advances the storage key but never the research budget. The consecutive cap
            # is what keeps the retry loop terminating.
            consecutive = int(state["consecutive_schema_errors"]) + 1
            if consecutive >= MAX_CONSECUTIVE_SCHEMA_ERRORS:
                services.repository.set_research_outcome(
                    job_id,
                    outcome="failed",
                    error_code="planner_schema_error_limit",
                    phase="failed",
                )
                log.error(
                    "planner.schema_error_limit",
                    job_id=str(job_id),
                    consecutive=consecutive,
                    decision_round=decision_round,
                )
                return {
                    "decision_round": decision_round,
                    "consecutive_schema_errors": consecutive,
                    "phase": "failed",
                    "outcome": "failed",
                    "error_code": "planner_schema_error_limit",
                    "route": "end",
                }
            messages = append_decision(prompt, exc.raw_output)
            messages = append_runtime_feedback(
                messages,
                feedback_type="schema_error",
                payload={"reason": feedback},
            )
            next_state: dict[str, Any] = {
                "decision_round": decision_round,
                "consecutive_schema_errors": consecutive,
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
        research_decisions_used = int(state["research_decisions_used"]) + 1
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
            batch_stages = [task.research_stage for task in decision.dispatch.tasks]
            batch_stage = batch_stages[0]
            stage_concurrency = int(state["stage_budgets"][batch_stage]["max_concurrency"])
            # Order matters: a mixed batch has no single stage, so its concurrency ceiling
            # is undefined until that is ruled out.
            rejection = mixed_stage_rejection(batch_stages)
            feedback = ""
            if rejection is not None:
                feedback = (
                    f"整批派发被拒绝：本轮提交的任务分属 {'、'.join(sorted(set(batch_stages)))} "
                    "多个阶段。每批只能使用一个 research_stage。"
                    "请先派发其中一个阶段的任务，其余留到后续轮次。"
                )
            if rejection is None:
                rejection = stage_order_rejection(
                    batch_stage, scout_dispatched=bool(state["scout_dispatched"])
                )
                if rejection is not None:
                    feedback = (
                        f"整批派发被拒绝：本任务尚未派发过任何 scout 任务，不能直接进入 "
                        f"{batch_stage}。请先用 scout 确认研究对象、可用指标和资料来源。"
                    )
            if rejection is None:
                rejection = dispatch_rejection(len(decision.dispatch.tasks), stage_concurrency)
                if rejection is not None:
                    feedback = (
                        f"整批派发被拒绝：本轮提交 {len(decision.dispatch.tasks)} 个任务，"
                        f"{batch_stage} 阶段的并发上限为 {stage_concurrency}。请缩小本轮任务批次。"
                    )
            if rejection is not None:
                services.repository.complete_decision(
                    job_id,
                    decision_round,
                    decision=decision,
                    raw_output=model_result.raw_output,
                    feedback=feedback,
                    status="rejected",
                )
                services.repository.record_planner_rejection(
                    job_id,
                    decision_round,
                    rejection.value,
                    research_decisions_used=research_decisions_used,
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
                    research_decisions_used=research_decisions_used,
                )
                result = {
                    "current_research_stage": batch_stage,
                    "scout_dispatched": bool(state["scout_dispatched"]) or batch_stage == "scout",
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
                research_decisions_used=research_decisions_used,
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
                    job_id,
                    decision_round,
                    rejection.value,
                    research_decisions_used=research_decisions_used,
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
                    research_decisions_used=research_decisions_used,
                )
                return {
                    "decision_round": decision_round,
                    "research_decisions_used": research_decisions_used,
                    "consecutive_schema_errors": 0,
                    "planner_messages": messages,
                    "phase": "verifier",
                    "outcome": "ready_for_verifier",
                    "error_code": None,
                    "verifier_trigger": "planner_finish",
                    "route": "verifier",
                }

        counters = {
            "decision_round": decision_round,
            "research_decisions_used": research_decisions_used,
            "consecutive_schema_errors": 0,
        }
        updated_state = {**state, **counters, **result}
        messages = append_runtime_feedback(
            messages,
            feedback_type="research_state",
            payload=_research_state_message(updated_state),
        )
        return {**counters, "planner_messages": messages, **result}

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
        with collect_usage(
            services.repository,
            job_id,
            "research_worker",
            task_id=task_id,
        ):
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
    except JobCancelledError:
        raise
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
            try:
                with (
                    tracer.start_as_current_span(
                        "llm.call",
                        attributes={
                            "prospector.job_id": str(job_id),
                            "prospector.plan_version": plan_version,
                            "prospector.agent": "research_verifier",
                        },
                    ),
                    collect_usage(services.repository, job_id, "research_verifier"),
                ):
                    model_result = services.verifier.verify(snapshot)
            except VerifierOutputError as exc:
                # Keep the raw answer and stop the Job cleanly. Both matter: without the
                # answer the rejection cannot be diagnosed, and without the outcome the
                # Job sits at 'running' forever after the process is already gone.
                services.repository.fail_verifier_run(
                    job_id, run_id, raw_output=exc.raw_output, error=str(exc)
                )
                services.repository.set_research_outcome(
                    job_id,
                    outcome="failed",
                    error_code="verifier_output_invalid",
                    phase="failed",
                )
                raise
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
                research_decisions_used=int(state["research_decisions_used"]),
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
        if services.writer is None:
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

            # Resume: draft already generated for current revision → verify next.
            if (
                stored is not None
                and stored["report_status"] in {"verifying", "writing"}
                and stored["revision_status"] == "generated"
            ):
                return {
                    "phase": "verifying",
                    "outcome": None,
                    "error_code": None,
                    "report_id": str(stored["report_id"]),
                    "route": "report_verifier",
                }

            # Sentence-level revision after Report Verifier findings.
            if stored is not None and stored["report_status"] == "revising":
                from prospector.agents.prompts.report_writer import (
                    report_writer_revision_messages,
                )

                report_id = UUID(str(stored["report_id"]))
                draft = stored["draft"]
                assert isinstance(draft, ReportDraft)
                latest = services.repository.get_latest_report_verifier_run(report_id)
                if latest is None or latest["status"] != "completed" or latest["findings"] is None:
                    raise RuntimeError("Revision requested without completed findings")
                findings = latest["findings"]
                full_prompt = report_writer_revision_messages(snapshot, draft, findings)
                report_id, revision = services.repository.begin_report_revision(
                    job_id,
                    verifier_run_id,
                    full_prompt,
                    bump=True,
                )
                with (
                    tracer.start_as_current_span(
                        "llm.call",
                        attributes={
                            "prospector.job_id": str(job_id),
                            "prospector.agent": "report_writer",
                            "prospector.mode": "revise",
                        },
                    ),
                    collect_usage(services.repository, job_id, "report_writer"),
                ):
                    result = services.writer.revise(snapshot, draft, findings)
                if result.full_prompt[: len(full_prompt)] != full_prompt:
                    raise RuntimeError(
                        "Report Writer revise used a different prompt than persisted"
                    )
                services.repository.complete_report_revision(
                    report_id,
                    result.draft,
                    result.raw_output,
                    revision=revision,
                )
                return {
                    "phase": "verifying",
                    "outcome": None,
                    "error_code": None,
                    "report_id": str(report_id),
                    "route": "report_verifier",
                }

            from prospector.agents.prompts.report_writer import report_writer_messages

            full_prompt = report_writer_messages(snapshot)
            report_id, revision = services.repository.begin_report_revision(
                job_id,
                verifier_run_id,
                full_prompt,
            )
            # Replay: revision already prompted/generated.
            stored_after = services.repository.get_report_revision(job_id)
            if (
                stored_after is not None
                and stored_after["revision_status"] == "generated"
                and stored_after["draft"] is not None
            ):
                return {
                    "phase": "verifying",
                    "outcome": None,
                    "error_code": None,
                    "report_id": str(report_id),
                    "route": "report_verifier",
                }
            with (
                tracer.start_as_current_span(
                    "llm.call",
                    attributes={
                        "prospector.job_id": str(job_id),
                        "prospector.agent": "report_writer",
                    },
                ),
                collect_usage(services.repository, job_id, "report_writer"),
            ):
                result = services.writer.write(snapshot)
            if result.full_prompt[: len(full_prompt)] != full_prompt:
                raise RuntimeError("Report Writer used a different prompt than persisted")
            services.repository.complete_report_revision(
                report_id,
                result.draft,
                result.raw_output,
                revision=revision,
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

        return {
            "phase": "verifying",
            "outcome": None,
            "error_code": None,
            "report_id": str(report_id),
            "route": "report_verifier",
        }

    return writer


_VERIFY_TERMINAL_STATUSES = frozenset({"verified", "revisions_exhausted"})


def _post_verify_status(findings: ReportVerifierFindings, revision: int) -> str:
    """Where the report goes after one verification pass.

    "verified" is reserved for a report where every statement passed. A report that
    still has failures but has used up its revision rounds also renders, but under
    its own status: collapsing the two would make "verified" mean two different
    confidence levels and quietly poison any eval set built from these records.
    """
    if findings.all_passed:
        return "verified"
    if not can_revise_again(revision):
        return "revisions_exhausted"
    return "revising"


def _report_verifier_node(services: ResearchGraphServices):
    def report_verifier(state: ResearchState) -> dict[str, Any]:
        job_id = UUID(state["job_id"])
        if services.report_verifier is None:
            raise RuntimeError("Report Verifier services are not configured")
        stored = services.repository.get_report_revision(job_id)
        if stored is None or stored["draft"] is None:
            raise RuntimeError("Report Verifier entered without a generated draft")
        if stored["report_status"] == "draft_rendered":
            return {
                "phase": "draft_rendered",
                "outcome": "draft_rendered",
                "error_code": None,
                "report_id": str(stored["report_id"]),
                "report_markdown_ref": stored["markdown_ref"],
                "report_json_ref": stored["json_ref"],
                "route": "end",
            }
        if stored["report_status"] in _VERIFY_TERMINAL_STATUSES:
            return {
                "phase": str(stored["report_status"]),
                "outcome": None,
                "error_code": None,
                "report_id": str(stored["report_id"]),
                "route": "render",
            }

        report_id = UUID(str(stored["report_id"]))
        revision = int(stored["revision"])
        draft = stored["draft"]
        assert isinstance(draft, ReportDraft)

        latest = services.repository.get_latest_report_verifier_run(report_id)
        if (
            latest is not None
            and int(latest["revision"]) == revision
            and latest["status"] == "completed"
            and latest["findings"] is not None
        ):
            findings = latest["findings"]
            resumed_status = _post_verify_status(findings, revision)
            if resumed_status in _VERIFY_TERMINAL_STATUSES:
                services.repository.set_report_status(report_id, resumed_status)
                return {
                    "phase": resumed_status,
                    "outcome": None,
                    "error_code": None,
                    "report_id": str(report_id),
                    "route": "render",
                }
            services.repository.set_report_status(report_id, "revising")
            return {
                "phase": "revising",
                "outcome": None,
                "error_code": None,
                "report_id": str(report_id),
                "route": "writer",
            }

        round_number = 1
        # Each revision is verified in full. Incremental dirty propagation is used
        # inside unit tests and can be wired for same-revision multi-round later.
        dirty = dirty_statement_ids(draft)

        run = services.repository.get_report_verifier_run(
            report_id, revision=revision, round_number=round_number
        )
        if run is not None and run["status"] == "completed" and run["findings"] is not None:
            findings = run["findings"]
        else:
            run_id = services.repository.begin_report_verifier_run(
                report_id,
                revision=revision,
                round_number=round_number,
                dirty_statement_ids=sorted(dirty),
            )
            run = services.repository.get_report_verifier_run(
                report_id, revision=revision, round_number=round_number
            )
            if run is not None and run["status"] == "completed" and run["findings"] is not None:
                findings = run["findings"]
            else:
                try:
                    rv_snapshot = services.repository.build_report_verifier_snapshot(
                        job_id,
                        report_id,
                        revision=revision,
                        round_number=round_number,
                        dirty_statement_ids=dirty,
                        draft=draft,
                    )
                    with (
                        tracer.start_as_current_span(
                            "llm.call",
                            attributes={
                                "prospector.job_id": str(job_id),
                                "prospector.agent": "report_verifier",
                            },
                        ),
                        collect_usage(
                            services.repository,
                            job_id,
                            "report_verifier",
                        ),
                    ):
                        result = services.report_verifier.verify(rv_snapshot)
                except (ReportVerifierOutputError, ValueError) as exc:
                    raw_output = (
                        exc.raw_output if isinstance(exc, ReportVerifierOutputError) else None
                    )
                    services.repository.fail_report_verifier_run(
                        job_id,
                        report_id,
                        run_id,
                        error={"message": str(exc), "raw_output": raw_output},
                    )
                    log.error(
                        "report_verifier.contract_error",
                        job_id=str(job_id),
                        message=str(exc),
                    )
                    return {
                        "phase": "failed",
                        "outcome": "failed",
                        "error_code": "report_verifier_contract_error",
                        "report_id": str(report_id),
                        "route": "end",
                    }
                findings = result.findings
                next_status = _post_verify_status(findings, revision)
                log.info(
                    "report_verifier.completed",
                    revision=revision,
                    round=round_number,
                    passed=len(findings.passed_statement_ids),
                    failed=len(findings.failures),
                    next=next_status,
                )
                services.repository.complete_report_verifier_run(
                    run_id,
                    findings=findings,
                    statement_checks=[
                        decision.model_dump(mode="json") for decision in result.decisions
                    ],
                    decisions=result.decisions,
                    draft=draft,
                    report_id=report_id,
                    revision=revision,
                    next_status=next_status,
                )

        final_status = _post_verify_status(findings, revision)
        if final_status in _VERIFY_TERMINAL_STATUSES:
            return {
                "phase": final_status,
                "outcome": None,
                "error_code": None,
                "report_id": str(report_id),
                "route": "render",
            }
        return {
            "phase": "revising",
            "outcome": None,
            "error_code": None,
            "report_id": str(report_id),
            "route": "writer",
        }

    return report_verifier


def _render_node(services: ResearchGraphServices):
    def render(state: ResearchState) -> dict[str, Any]:
        job_id = UUID(state["job_id"])
        raw_run_id = state.get("last_verifier_run_id")
        if raw_run_id is None:
            raise RuntimeError("Render entered without a completed Research Verifier run")
        if services.object_store is None:
            raise RuntimeError("Object store is not configured")
        stored = services.repository.get_report_revision(job_id)
        if stored is None or stored["draft"] is None:
            raise RuntimeError("Render entered without a generated draft")
        if stored["report_status"] == "draft_rendered":
            return {
                "phase": "draft_rendered",
                "outcome": "draft_rendered",
                "error_code": None,
                "report_id": str(stored["report_id"]),
                "report_markdown_ref": stored["markdown_ref"],
                "report_json_ref": stored["json_ref"],
                "route": "end",
            }

        report_id = UUID(str(stored["report_id"]))
        revision = int(stored["revision"])
        draft = stored["draft"]
        assert isinstance(draft, ReportDraft)
        snapshot = services.repository.build_writer_snapshot(job_id, UUID(raw_run_id))
        citation_map = services.repository.get_verified_citation_map(report_id, revision=revision)
        latest = services.repository.get_latest_report_verifier_run(report_id)
        failed_ids: list[str] = []
        verification_status: str = "verified"
        if latest is not None and latest["findings"] is not None:
            failed_ids = [item.statement_id for item in latest["findings"].failures]
            verification_status = "partial" if failed_ids else "verified"

        try:
            rendered = render_verified_report(
                snapshot,
                draft,
                citation_map=citation_map,
                verification_status=verification_status,  # type: ignore[arg-type]
                failed_statement_ids=failed_ids,
            )
            base_key = workspace_key(
                services.repository.settings.workspace_id,
                "reports",
                str(job_id),
                str(revision),
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
                verification_status=verification_status,
            )
        except Exception as exc:
            services.repository.set_research_outcome(
                job_id,
                outcome="failed",
                error_code="draft_render_error",
                phase="failed",
            )
            log.error("render.error", job_id=str(job_id), message=str(exc))
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

    return render


def _route(state: ResearchState) -> str:
    return state["route"]


class _GraphNode(Protocol):
    """A graph node. LangGraph's node protocol requires the parameter be named ``state``,
    which a bare ``Callable[[ResearchState], ...]`` does not preserve."""

    def __call__(self, state: ResearchState) -> dict[str, Any]: ...


def _guard_cancelled(
    node: _GraphNode,
    services: ResearchGraphServices,
) -> _GraphNode:
    def guarded(state: ResearchState) -> dict[str, Any]:
        job_id = UUID(state["job_id"])
        if services.cancel_requested(job_id):
            raise JobCancelledError(f"Job {job_id} was cancelled")
        result = node(state)
        if services.cancel_requested(job_id):
            raise JobCancelledError(f"Job {job_id} was cancelled")
        return result

    return guarded


def build_research_graph(
    checkpointer: BaseCheckpointSaver,
    services: ResearchGraphServices | None = None,
) -> Any:
    runtime = services or default_research_services()
    graph = StateGraph(ResearchState)
    graph.add_node("initialize", _guard_cancelled(_initialize_node(runtime), runtime))
    graph.add_node("planner", _guard_cancelled(_planner_node(runtime), runtime))
    graph.add_node("workers", _guard_cancelled(_workers_node(runtime), runtime))
    graph.add_node("verifier", _guard_cancelled(_verifier_node(runtime), runtime))
    graph.add_node("writer", _guard_cancelled(_writer_node(runtime), runtime))
    graph.add_node(
        "report_verifier",
        _guard_cancelled(_report_verifier_node(runtime), runtime),
    )
    graph.add_node("render", _guard_cancelled(_render_node(runtime), runtime))
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
    graph.add_conditional_edges(
        "writer",
        _route,
        {"report_verifier": "report_verifier", "end": END},
    )
    graph.add_conditional_edges(
        "report_verifier",
        _route,
        {"writer": "writer", "render": "render", "end": END},
    )
    graph.add_conditional_edges(
        "render",
        _route,
        {"end": END},
    )
    return graph.compile(checkpointer=checkpointer)


def thread_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
