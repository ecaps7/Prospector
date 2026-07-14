"""Planner-Worker graph with mock models and Exa against real PG/MinIO persistence."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from prospector.agents.planner import PlannerModelResult, PlannerOutputError
from prospector.agents.research_worker import (
    ResearchWorker,
    SummaryItem,
    WorkerFinish,
    WorkerModelAction,
    WorkerSummary,
    WorkerToolCall,
)
from prospector.deterministic.budget import inject_task_budget
from prospector.flow.research_graph import (
    ResearchGraphServices,
    _initialize_node,
    _planner_node,
    _run_one_worker,
    build_research_graph,
    thread_config,
)
from prospector.flow.state import ResearchState, initial_research_state
from prospector.schemas.brief import ResearchBrief
from prospector.schemas.decisions import PlannerDecision
from prospector.schemas.evidence import Assertion
from prospector.store.checkpoint import checkpointer_session, close_pool, setup_checkpointer
from prospector.store.jobs import create_job
from prospector.store.object_store import ObjectStore
from prospector.store.repositories import ResearchRepository
from prospector.tools.save_findings import SaveFindingsTool
from prospector.tools.web_fetch import CompressedPoint, CompressedView, WebFetchTool
from prospector.tools.web_search import WebSearchTool

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _environment() -> Iterator[None]:
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    config = Config(str(REPO_ROOT / "alembic.ini"))
    command.upgrade(config, "head")
    setup_checkpointer()
    ObjectStore().ensure_bucket()
    yield
    close_pool()


def _task(label: str) -> dict[str, Any]:
    return {
        "question": f"调查 {label} 的公开事实、时间口径和可能推翻当前解释的相反信号。",
        "research_stage": "verify",
        "research_mode": "counterargument",
        "source_policy": {"preferred_tiers": ["official", "industry"]},
        "expected_evidence": "至少保存一条带原文段号、时间和限定条件的直接证据",
    }


class ScriptedPlanner:
    def __init__(self, decisions: list[PlannerDecision | Exception]) -> None:
        self.decisions = decisions
        self.calls = 0

    def decide(self, messages: list[dict[str, Any]]) -> PlannerModelResult:
        item = self.decisions[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return PlannerModelResult(raw_output=item.model_dump(mode="json"), decision=item)


class MockExa:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def _enter(self) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.03)

    async def search(self, query: str, num_results: int) -> dict[str, Any]:
        await self._enter()
        self.active -= 1
        return {
            "results": [
                {
                    "title": f"来源 {query}",
                    "url": f"https://example.test/{query}",
                    "publishedDate": "2026-07-01",
                    "author": "Example Publisher",
                }
            ]
        }

    async def contents(self, url: str) -> dict[str, Any]:
        await self._enter()
        self.active -= 1
        return {
            "results": [
                {
                    "title": "Mock source",
                    "url": url,
                    "publishedDate": "2026-07-01",
                    "author": "Example Publisher",
                    "text": f"{url} 在 2026 年公开了可核对事实，统计口径为年度。\n\n"
                    "另一段记录了可能的相反信号与适用边界。",
                }
            ]
        }


class MockCompressor:
    async def compress(self, task_question: str, paragraphs: list[Any]) -> CompressedView:
        assert task_question
        return CompressedView(points=[CompressedPoint(text="公开事实与年度口径", para_ids=[1])])


class LedgerWorkerModel:
    def __init__(self) -> None:
        self.research_runs: dict[str, int] = {}

    @staticmethod
    def _task(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return json.loads(str(messages[1]["content"]).partition("\n")[2])

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        task = self._task(messages)
        task_id = task["task_id"]
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            self.research_runs[task_id] = self.research_runs.get(task_id, 0) + 1
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="web_search",
                        tool_call_id=f"search-{task_id}",
                        arguments={"query": task_id, "num_results": 5},
                    )
                ],
            )
        last = json.loads(tool_messages[-1]["content"])
        if len(tool_messages) == 1:
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="web_fetch",
                        tool_call_id=f"fetch-{task_id}",
                        arguments={"url": last["results"][0]["url"]},
                    )
                ],
            )
        if len(tool_messages) == 2:
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="save_findings",
                        tool_call_id=f"save-{task_id}",
                        arguments={
                            "doc_id": last["doc_id"],
                            "findings": [
                                {
                                    "para_ids": [1],
                                    "statement": f"任务 {task_id} 找到带年度口径的公开事实。",
                                    "topic_tags": ["公开事实"],
                                }
                            ],
                        },
                    )
                ],
            )
        return WorkerModelAction(
            assistant_message={
                "role": "assistant",
                "content": ('{"goal_met":true,"stop_reason":"expected_evidence_satisfied"}'),
            },
            finish=WorkerFinish(
                goal_met=True,
                stop_reason="expected_evidence_satisfied",
                gap_note="仍需由 Verifier 判断整体覆盖",
            ),
        )

    async def summarize(
        self,
        assertions: list[Assertion],
        *,
        goal_met: bool,
        stop_reason: str,
        gap_note: str,
    ) -> WorkerSummary:
        assert goal_met is True
        assert stop_reason == "expected_evidence_satisfied"
        return WorkerSummary(
            items=[
                SummaryItem(assertion_id=item.assertion_id, text=item.statement)
                for item in assertions
            ],
            gap_note=gap_note,
        )


def _services(planner: ScriptedPlanner) -> tuple[ResearchGraphServices, MockExa, LedgerWorkerModel]:
    repository = ResearchRepository()
    object_store = ObjectStore()
    exa = MockExa()
    model = LedgerWorkerModel()
    worker = ResearchWorker(
        repository,
        [
            WebSearchTool(repository, exa),  # type: ignore[arg-type]
            WebFetchTool(repository, object_store, exa, MockCompressor()),  # type: ignore[arg-type]
            SaveFindingsTool(repository, object_store),
        ],
        model,
    )
    return ResearchGraphServices(repository, planner, worker), exa, model


def _create_research_job(effort: str = "standard") -> tuple[UUID, UUID, ResearchRepository]:
    repository = ResearchRepository()
    job_id = create_job()
    brief_id = repository.freeze_brief(
        job_id,
        ResearchBrief(
            question="并行研究测试",
            brief_text="比较两条独立证据路径，主动寻找相反信号，不预设结论。",
            effort=effort,  # type: ignore[arg-type]
        ),
    )
    return job_id, brief_id, repository


def test_schema_and_concurrency_rejection_then_parallel_evidence_and_finish() -> None:
    over_limit = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "dispatch": {
                "tasks": [_task(str(index)) for index in range(4)],
                "reason": "先一次派四条路径",
            },
        }
    )
    valid_dispatch = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "dispatch": {
                "tasks": [_task("直接证据"), _task("相反信号")],
                "reason": "缩小为两条相互独立的路径",
            },
        }
    )
    finish = PlannerDecision.model_validate(
        {"decision": "finish", "finish": {"reason": "已有两条落库证据，交质量门"}}
    )
    planner = ScriptedPlanner(
        [
            PlannerOutputError("未调用强制决策工具", {"content": "我认为应该开始研究"}),
            over_limit,
            valid_dispatch,
            finish,
        ]
    )
    services, exa, _ = _services(planner)
    job_id, brief_id, repository = _create_research_job()

    try:
        with checkpointer_session() as checkpointer:
            graph = build_research_graph(checkpointer, services)
            result = graph.invoke(
                initial_research_state(job_id=str(job_id), brief_id=str(brief_id)),
                thread_config(str(job_id)),
            )

        assert result["decision_round"] == 4
        assert result["plan_version"] == 1
        assert result["outcome"] == "ready_for_verifier"
        assert exa.max_active >= 2

        events = repository.list_events(job_id)
        event_types = [row["event_type"] for row in events]
        tool_events = [row for row in events if row["event_type"] == "task.tool_used"]
        assert "planner.rejected" in event_types
        assert event_types.count("task.started") == 2
        assert event_types.count("task.evidence_saved") == 2
        assert event_types.count("task.finished") == 2
        assert event_types[-1] == "job.phase_changed"
        assert all(event["payload"].get("tool_call_id") for event in tool_events)

        with repository.engine.connect() as conn:
            prompts = (
                conn.execute(
                    text(
                        """
                    SELECT decision_round, full_prompt FROM app.decision_log
                    WHERE job_id=:job_id ORDER BY decision_round
                    """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .all()
            )
            assertions = (
                conn.execute(
                    text("SELECT task_id, produced_by FROM app.assertions WHERE job_id=:job_id"),
                    {"job_id": job_id},
                )
                .mappings()
                .all()
            )
        assert len(prompts) == 4
        assert len(assertions) == 2
        assert all(str(row["task_id"]) == row["produced_by"]["task_id"] for row in assertions)
        assert "compressed_view" not in json.dumps(prompts, ensure_ascii=False, default=str)
        assert "你的输出不合法" in json.dumps(
            prompts[1]["full_prompt"], ensure_ascii=False, default=str
        )
    finally:
        with repository.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_empty_finish_burns_every_round_then_fails_without_verifier() -> None:
    empty_finish = PlannerDecision.model_validate(
        {"decision": "finish", "finish": {"reason": "没有证据也尝试结束"}}
    )
    planner = ScriptedPlanner([empty_finish, empty_finish, empty_finish])
    services, _, _ = _services(planner)
    job_id, brief_id, repository = _create_research_job("quick")
    try:
        with checkpointer_session() as checkpointer:
            graph = build_research_graph(checkpointer, services)
            result = graph.invoke(
                initial_research_state(job_id=str(job_id), brief_id=str(brief_id)),
                thread_config(str(job_id)),
            )
        assert result["decision_round"] == 3
        assert result["outcome"] == "failed"
        assert result["error_code"] == "research_budget_exhausted_without_evidence"
        rejected = [
            event
            for event in repository.list_events(job_id)
            if event["event_type"] == "planner.rejected"
        ]
        assert [event["payload"]["reason_code"] for event in rejected] == [
            "empty_finish",
            "empty_finish",
            "empty_finish",
        ]
    finally:
        with repository.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_completed_task_is_not_reexecuted_when_worker_node_replays() -> None:
    planner = ScriptedPlanner([])
    services, _, model = _services(planner)
    job_id, _, repository = _create_research_job("quick")
    task = services.repository.create_plan(
        job_id,
        1,
        [
            inject_task_budget(
                PlannerDecision.model_validate(
                    {
                        "decision": "dispatch",
                        "dispatch": {"tasks": [_task("恢复")], "reason": "恢复测试"},
                    }
                ).dispatch.tasks[0],  # type: ignore[union-attr]
                "quick",
            )
        ],
        reason="恢复测试",
    )
    task_id = task.task_ids[0]
    try:
        first = asyncio.run(_run_one_worker(services, job_id, task_id, 1))
        second = asyncio.run(_run_one_worker(services, job_id, task_id, 1))
        assert first["status"] == "done", first
        assert first["research_stage"] == "verify"
        assert first["question"]
        assert second["status"] == "done"
        assert second["research_stage"] == "verify"
        assert model.research_runs[str(task_id)] == 1
    finally:
        with repository.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_planner_replay_reuses_logged_decision_and_versioned_plan() -> None:
    dispatch = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "dispatch": {"tasks": [_task("幂等重放")], "reason": "测试提交后被杀"},
        }
    )
    planner = ScriptedPlanner([dispatch])
    services, _, _ = _services(planner)
    job_id, brief_id, repository = _create_research_job("quick")
    try:
        base = initial_research_state(job_id=str(job_id), brief_id=str(brief_id))
        initialized = cast(ResearchState, {**base, **_initialize_node(services)(base)})

        first = _planner_node(services)(initialized)
        second = _planner_node(services)(initialized)

        assert planner.calls == 1
        assert first["active_task_ids"] == second["active_task_ids"]
        assert first["plan_version"] == second["plan_version"] == 1
        with repository.engine.connect() as conn:
            plan_count = conn.execute(
                text("SELECT COUNT(*) FROM app.plans WHERE job_id=:job_id"),
                {"job_id": job_id},
            ).scalar_one()
            decision_events = conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM app.events
                    WHERE job_id=:job_id AND event_type='planner.decided'
                    """
                ),
                {"job_id": job_id},
            ).scalar_one()
        assert plan_count == 1
        assert decision_events == 1
    finally:
        with repository.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})
