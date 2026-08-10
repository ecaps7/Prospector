"""Planner-Worker graph with mock models and Exa against real PG/MinIO persistence."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from prospector.agents.planner import PlannerModelResult, PlannerOutputError
from prospector.agents.prompts.report_writer import report_writer_messages
from prospector.agents.prompts.research_verifier import research_verifier_messages
from prospector.agents.report_verifier import ReportVerifierOutputError
from prospector.agents.report_writer import ReportWriterResult
from prospector.agents.research_verifier import VerifierModelResult
from prospector.agents.research_worker import (
    ResearchWorker,
    WorkerCoverageAssessment,
    WorkerModelAction,
    WorkerToolCall,
)
from prospector.deterministic.budget import inject_task_budget, limits_for_effort
from prospector.flow.research_graph import (
    ResearchGraphServices,
    _initialize_node,
    _planner_node,
    _run_one_worker,
    _writer_node,
    build_research_graph,
    thread_config,
)
from prospector.flow.state import ResearchState, initial_research_state
from prospector.runtime.timeline import ResearchTimelineRenderer, drain_timeline
from prospector.schemas.brief import ResearchBrief
from prospector.schemas.decisions import PlannerDecision
from prospector.schemas.evidence import Assertion
from prospector.schemas.report import ReportDraft, WriterSnapshot
from prospector.schemas.verifier import AssertionDisposition, VerifierDecision, VerifierGap
from prospector.store.checkpoint import checkpointer_session, close_pool, setup_checkpointer
from prospector.store.jobs import create_job
from prospector.store.object_store import ObjectStore
from prospector.store.repositories import ResearchRepository
from prospector.tools.base import ToolContext
from prospector.tools.save_findings import SaveFindingsTool
from prospector.tools.web_fetch import MediaType, WebFetchTool
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
    # scout, because the runtime now refuses a first batch that skips screening; these
    # fixtures are the Planner's opening move.
    return {
        "question": f"调查 {label} 的公开事实、时间口径和可能推翻当前解释的相反信号。",
        "subjects": [label],
        "research_stage": "scout",
        "research_mode": "counterargument",
        "source_policy": {"preferred_tiers": ["official", "industry"]},
        "expected_evidence": "至少保存一条带原文 highlight、时间和限定条件的直接证据",
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


class PassingVerifier:
    def verify(self, snapshot: dict[str, Any]) -> VerifierModelResult:
        decision = VerifierDecision(
            release_decision="pass",
            decision_reason="测试证据足以履行 Plan。",
            brief_alignment="aligned",
            coverage_rationale="测试证据履行了 Plan。",
            brief_alignment_rationale="测试研究未偏离 Brief。",
            credibility_rationale="测试来源足以支撑当前研究出口。",
        )
        return VerifierModelResult(
            full_prompt=research_verifier_messages(snapshot),
            raw_output=decision.model_dump(mode="json"),
            decision=decision,
        )


class CredibilityGapVerifier:
    """Reject once with source_credibility + unusable disposition, then pass."""

    def __init__(self) -> None:
        self.calls = 0

    def verify(self, snapshot: dict[str, Any]) -> VerifierModelResult:
        self.calls += 1
        assertions = list(snapshot.get("assertions") or [])
        assertion_id = UUID(str(assertions[0]["assertion_id"]))
        if self.calls == 1:
            decision = VerifierDecision(
                release_decision="needs_research",
                decision_reason="核心断言依赖不可信来源。",
                brief_alignment="aligned",
                coverage_rationale="假断言不能履行 Plan。",
                brief_alignment_rationale="研究仍围绕 Brief。",
                credibility_rationale="来源不可信。",
                gaps=[
                    VerifierGap(
                        kind="source_credibility",
                        severity="major",
                        related_assertion_ids=[assertion_id],
                        description="核心断言来源不可信",
                        attempted_paths=["查阅现有落证"],
                        why_insufficient="缺乏独立真实来源",
                        recommended_research="在权威来源中补查替代证据",
                    )
                ],
                assertion_dispositions=[
                    AssertionDisposition(
                        assertion_id=assertion_id,
                        status="unusable",
                        reason="测试废证：伪学术来源。",
                    )
                ],
            )
        else:
            decision = VerifierDecision(
                release_decision="pass",
                decision_reason="补查后真实证据已足够。",
                brief_alignment="aligned",
                coverage_rationale="剩余可用证据履行 Plan。",
                brief_alignment_rationale="研究仍围绕 Brief。",
                credibility_rationale="可信来源已到位。",
                assertion_dispositions=[
                    AssertionDisposition(
                        assertion_id=assertion_id,
                        status="unusable",
                        reason="测试废证：伪学术来源。",
                    )
                ],
            )
        return VerifierModelResult(
            full_prompt=research_verifier_messages(snapshot),
            raw_output=decision.model_dump(mode="json"),
            decision=decision,
        )


class PassingReportVerifier:
    def __init__(self) -> None:
        self.calls = 0

    def verify(self, snapshot: Any) -> Any:
        from prospector.agents.report_verifier import (
            ReportVerifierModelResult,
            materialize_findings,
        )
        from prospector.schemas.claims import (
            BridgeStatementDecision,
            DerivedStatementDecision,
            EvidenceStatementDecision,
        )

        self.calls += 1
        decisions: list[Any] = []
        for item in snapshot.statements:
            if item.kind == "evidence":
                decisions.append(
                    EvidenceStatementDecision(
                        statement_id=item.statement_id,
                        claim_type="fact",
                        pairs=[
                            {
                                "excerpt_id": excerpt["excerpt_id"],
                                "relation": "support",
                            }
                            for excerpt in item.candidate_excerpts
                        ],
                        status="pass",
                        reason="测试放行：候选片段支持该事实句",
                    )
                )
            elif item.kind == "derived":
                decisions.append(
                    DerivedStatementDecision(
                        statement_id=item.statement_id,
                        inference_note="测试推理",
                        status="pass",
                        reason="测试放行：推理合理",
                    )
                )
            else:
                decisions.append(
                    BridgeStatementDecision(
                        statement_id=item.statement_id,
                        kind=item.kind,
                        contains_factual_claim=False,
                        reason="测试放行：仅承担衔接作用",
                    )
                )
        findings = materialize_findings(
            revision=snapshot.revision,
            round_number=snapshot.round,
            decisions=decisions,
            allowed_excerpt_ids=list(snapshot.allowed_excerpt_ids),
        )
        return ReportVerifierModelResult(
            findings=findings, decisions=decisions, raw_outputs={}
        )


class BrokenReportVerifier:
    def verify(self, snapshot: Any) -> Any:
        statement_id = snapshot.statements[0].statement_id
        raise ReportVerifierOutputError(
            f"invalid Report Verifier decision for {statement_id}",
            {
                "attempts": [
                    {
                        "content": '{"statement_id":"s_intro","reason":"',
                        "finish_reason": "stop",
                    }
                ]
            },
        )


class PassingWriter:
    def __init__(self) -> None:
        self.calls = 0

    def write(self, snapshot: WriterSnapshot) -> ReportWriterResult:
        self.calls += 1
        excerpt_id = snapshot.evidence_cards[0].excerpts[0].excerpt_id
        fact = "研究材料显示了一个可核对的年度事实。" * 120
        analysis = "综合现有材料，这一事实需要结合相反信号与适用边界理解。" * 100
        draft = ReportDraft.model_validate(
            {
                "title": "并行研究测试报告",
                "introduction": [
                    {
                        "paragraph_id": "p_intro",
                        "statements": [
                            {
                                "statement_id": "s_intro",
                                "text": "现有材料支持一个需要结合反例理解的核心判断。",
                                "kind": "elaboration",
                                "candidate_excerpt_ids": [],
                                "premise_statement_ids": [],
                            }
                        ],
                    }
                ],
                "sections": [
                    {
                        "section_id": "sec_answer",
                        "title": "核心判断",
                        "paragraphs": [
                            {
                                "paragraph_id": "p_answer",
                                "statements": [
                                    {
                                        "statement_id": "s_fact",
                                        "text": fact,
                                        "kind": "evidence",
                                        "candidate_excerpt_ids": [str(excerpt_id)],
                                        "premise_statement_ids": [],
                                    },
                                    {
                                        "statement_id": "s_analysis",
                                        "text": analysis,
                                        "kind": "derived",
                                        "candidate_excerpt_ids": [],
                                        "premise_statement_ids": ["s_fact"],
                                    },
                                ],
                            }
                        ],
                    }
                ],
                "conclusion": [
                    {
                        "paragraph_id": "p_conclusion",
                        "statements": [
                            {
                                "statement_id": "s_conclusion_1",
                                "text": "综合来看，现有材料支持上述有边界的判断。",
                                "kind": "derived",
                                "candidate_excerpt_ids": [],
                                "premise_statement_ids": ["s_fact", "s_analysis"],
                            },
                            {
                                "statement_id": "s_conclusion_2",
                                "text": "最终结论不应超出这些事实与分析的范围。",
                                "kind": "derived",
                                "candidate_excerpt_ids": [],
                                "premise_statement_ids": ["s_analysis"],
                            },
                        ],
                    }
                ],
            }
        )
        return ReportWriterResult(
            full_prompt=report_writer_messages(snapshot),
            raw_output=draft.model_dump(mode="json"),
            draft=draft,
        )

    def revise(
        self,
        snapshot: WriterSnapshot,
        draft: ReportDraft,
        findings: Any,
    ) -> ReportWriterResult:
        self.calls += 1
        from prospector.agents.prompts.report_writer import report_writer_revision_messages

        return ReportWriterResult(
            full_prompt=report_writer_revision_messages(snapshot, draft, findings),
            raw_output=draft.model_dump(mode="json"),
            draft=draft,
        )


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

    async def contents(
        self,
        url: str,
        task_question: str,
    ) -> dict[str, Any]:
        assert task_question
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
                    "highlights": ["2026 年公开了可核对事实，统计口径为年度。"],
                }
            ]
        }


class MockMediaProbe:
    async def detect(self, url: str) -> MediaType:
        assert url
        return "html"


class PdfMediaProbe:
    async def detect(self, url: str) -> MediaType:
        assert url
        return "pdf"


class PdfExa:
    async def contents(
        self,
        url: str,
        task_question: str,
    ) -> dict[str, Any]:
        assert task_question
        return {
            "results": [
                {
                    "title": "PDF source",
                    "url": url,
                    "text": "第一页连续文本\n第二页连续文本\n第三页连续文本",
                    "highlights": ["第一页连续文本", "第三页连续文本"],
                }
            ]
        }


class EmptyHighlightPdfExa(PdfExa):
    async def contents(
        self,
        url: str,
        task_question: str,
    ) -> dict[str, Any]:
        result = await super().contents(url, task_question)
        result["results"][0]["highlights"] = []
        return result


class LedgerWorkerModel:
    def __init__(self) -> None:
        self.research_runs: dict[str, int] = {}
        self.coverage_checks: dict[str, int] = {}

    @staticmethod
    def _task(messages: list[dict[str, Any]]) -> dict[str, Any]:
        return json.loads(str(messages[1]["content"]).partition("\n")[2])

    async def next_action(self, messages: list[dict[str, Any]]) -> WorkerModelAction:
        task = self._task(messages)
        task_id = task["task_id"]
        runtime_messages = [
            message
            for message in messages
            if str(message.get("content", "")).startswith("上一轮运行结果：")
        ]
        if not runtime_messages:
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
        latest_results = json.loads(str(runtime_messages[-1]["content"]).partition("\n")[2])
        fetched = [
            item["result"]
            for item in latest_results
            if item["tool"] == "web_fetch" and "result" in item
        ]
        if fetched:
            source = fetched[0]
            source_ref = source["items"][0]["source_ref"]
            return WorkerModelAction(
                assistant_message={"role": "assistant", "content": None},
                tool_calls=[
                    WorkerToolCall(
                        tool_name="save_findings",
                        tool_call_id=f"save-{task_id}",
                        arguments={
                            "findings": [
                                {
                                    "source_refs": [source_ref],
                                    "statement": f"任务 {task_id} 找到带年度口径的公开事实。",
                                    "topic_tags": ["公开事实"],
                                }
                            ],
                        },
                    )
                ],
            )
        raise AssertionError("运行结果中没有可保存的 web_fetch 视图")

    async def summarize(self, assertions: list[Assertion]) -> list[str]:
        return [item.statement for item in assertions]

    async def assess_coverage(
        self,
        assertions: list[Assertion],
        *,
        task_question: str,
        expected_evidence: str,
    ) -> WorkerCoverageAssessment:
        assert assertions
        assert task_question
        assert expected_evidence
        task_id = assertions[0].produced_by["task_id"]
        self.coverage_checks[task_id] = self.coverage_checks.get(task_id, 0) + 1
        return WorkerCoverageAssessment(
            goal_met=True,
            reason="已保存任务要求的直接证据。",
        )


def _services(
    planner: ScriptedPlanner,
    *,
    verifier: Any | None = None,
    report_verifier: Any | None = None,
) -> tuple[ResearchGraphServices, MockExa, LedgerWorkerModel]:
    repository = ResearchRepository()
    object_store = ObjectStore()
    exa = MockExa()
    model = LedgerWorkerModel()
    worker = ResearchWorker(
        repository,
        [
            WebSearchTool(repository, exa),  # type: ignore[arg-type]
            WebFetchTool(
                repository,
                object_store,
                exa,  # type: ignore[arg-type]
                media_probe=MockMediaProbe(),
            ),
            SaveFindingsTool(repository),
        ],
        model,
    )
    return (
        ResearchGraphServices(
            repository=repository,
            planner=planner,
            worker=worker,
            verifier=verifier or PassingVerifier(),
            writer=PassingWriter(),
            report_verifier=report_verifier or PassingReportVerifier(),
            object_store=object_store,
        ),
        exa,
        model,
    )


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


@pytest.mark.parametrize(
    (
        "label",
        "url",
        "media_probe",
        "exa",
        "expected_media_type",
        "expected_source_ids",
        "selected_source_id",
        "expected_excerpt",
    ),
    [
        (
            "HTML",
            "https://example.test/report.html",
            MockMediaProbe(),
            MockExa(),
            "html",
            [["h1"]],
            "h1",
            "2026 年公开了可核对事实，统计口径为年度。",
        ),
        (
            "PDF",
            "https://example.test/report.pdf",
            PdfMediaProbe(),
            PdfExa(),
            "pdf",
            [["h1"], ["h2"]],
            "h2",
            "第三页连续文本",
        ),
    ],
)
async def test_all_web_media_use_the_same_persisted_exa_highlights_contract(
    label: str,
    url: str,
    media_probe: MockMediaProbe | PdfMediaProbe,
    exa: MockExa | PdfExa,
    expected_media_type: MediaType,
    expected_source_ids: list[list[str]],
    selected_source_id: str,
    expected_excerpt: str,
) -> None:
    job_id, _, repository = _create_research_job("quick")
    draft = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "dispatch": {"tasks": [_task(f"{label} highlights")], "reason": "验证统一落证路径"},
        }
    ).dispatch
    assert draft is not None
    plan = repository.create_plan(
        job_id,
        1,
        [inject_task_budget(draft.tasks[0], "quick")],
        reason="验证统一落证路径",
    )
    task_id = plan.task_ids[0]
    object_store = ObjectStore()
    context = ToolContext(
        job_id=job_id,
        task_id=task_id,
        worker_id="source-worker",
        task_question=f"核验 {label} 中的公开事实。",
        tool_call_id="fetch-source",
    )
    try:
        fetched = await WebFetchTool(
            repository,
            object_store,
            exa,  # type: ignore[arg-type]
            media_probe=media_probe,
        )({"url": url}, context)

        assert fetched["media_type"] == expected_media_type
        assert fetched["view_kind"] == "exa_highlights"
        assert [item["source_ids"] for item in fetched["items"]] == expected_source_ids

        saved = await SaveFindingsTool(repository)(
            {
                "doc_id": fetched["doc_id"],
                "view_id": fetched["view_id"],
                "findings": [
                    {
                        "source_ids": [selected_source_id],
                        "statement": f"{label} highlight 记录了目标事实。",
                        "topic_tags": [label],
                    }
                ],
            },
            ToolContext(
                job_id=job_id,
                task_id=task_id,
                worker_id="source-worker",
                task_question=context.task_question,
                tool_call_id="save-source",
            ),
        )
        assert len(saved["assertion_ids"]) == 1
        with repository.engine.connect() as conn:
            excerpt = (
                conn.execute(
                    text("SELECT text, locator FROM app.excerpts WHERE job_id=:job_id"),
                    {"job_id": job_id},
                )
                .mappings()
                .one()
            )
        assert excerpt["text"] == expected_excerpt
        assert excerpt["locator"]["kind"] == "exa_highlight"
        assert excerpt["locator"]["highlight_id"] == selected_source_id

        with pytest.raises(ValueError, match="not present in document view"):
            await SaveFindingsTool(repository)(
                {
                    "doc_id": fetched["doc_id"],
                    "view_id": fetched["view_id"],
                    "findings": [
                        {
                            "source_ids": ["h99"],
                            "statement": "不存在的 highlight。",
                            "topic_tags": [],
                        }
                    ],
                },
                ToolContext(
                    job_id=job_id,
                    task_id=task_id,
                    worker_id="source-worker",
                    task_question=context.task_question,
                    tool_call_id="save-invalid-highlight",
                ),
            )

        with pytest.raises(ValueError, match="does not belong to the current task"):
            await SaveFindingsTool(repository)(
                {
                    "doc_id": fetched["doc_id"],
                    "view_id": fetched["view_id"],
                    "findings": [
                        {
                            "source_ids": ["h1"],
                            "statement": "跨任务复用 highlight。",
                            "topic_tags": [],
                        }
                    ],
                },
                ToolContext(
                    job_id=job_id,
                    task_id=uuid4(),
                    worker_id="other-worker",
                    task_question=context.task_question,
                    tool_call_id="save-cross-task",
                ),
            )

        with pytest.raises(RuntimeError, match="no highlights for source"):
            await WebFetchTool(
                repository,
                object_store,
                EmptyHighlightPdfExa(),  # type: ignore[arg-type]
                media_probe=PdfMediaProbe(),
            )(
                {"url": "https://example.test/empty-highlights.pdf"},
                ToolContext(
                    job_id=job_id,
                    task_id=task_id,
                    worker_id="source-worker",
                    task_question=context.task_question,
                    tool_call_id="fetch-empty-highlights",
                ),
            )
    finally:
        with repository.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_schema_and_concurrency_rejection_then_parallel_evidence_and_finish() -> None:
    over_limit = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "dispatch": {
                # scout allows 6 concurrent tasks at standard, so overshoot needs 7.
                "tasks": [_task(str(index)) for index in range(7)],
                "reason": "先一次派七条路径",
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
        assert result["outcome"] == "draft_rendered"
        assert exa.max_active >= 2
        writer = cast(PassingWriter, services.writer)
        assert writer.calls == 1
        replay = _writer_node(services)(cast(Any, result))
        assert replay["outcome"] == "draft_rendered"
        assert writer.calls == 1

        events = repository.list_events(job_id)
        event_types = [row["event_type"] for row in events]
        tool_events = [row for row in events if row["event_type"] == "task.tool_used"]
        assert "planner.rejected" in event_types
        assert event_types.count("task.started") == 2
        assert event_types.count("task.evidence_saved") == 2
        assert event_types.count("task.finished") == 2
        assert event_types[-1] == "job.phase_changed"
        assert all(event["payload"].get("tool_call_id") for event in tool_events)
        verifier_event = next(
            event for event in events if event["event_type"] == "verifier.completed"
        )
        assert verifier_event["payload"]["decision_reason"] == "测试证据足以履行 Plan。"

        timeline_lines: list[str] = []
        standard_limits = limits_for_effort("standard")
        last_id, terminal = drain_timeline(
            repository,
            ResearchTimelineRenderer(repository, standard_limits),
            job_id,
            0,
            timeline_lines.append,
        )
        assert terminal is True
        assert last_id == int(events[-1]["id"])
        # Round 1 was a schema error, which no longer spends research budget: only the
        # over-concurrency rejection and this dispatch do, so 10 of 12 remain.
        assert (
            "[轮 3] Planner 派发 2 个任务（Plan v1，余 10 轮）：缩小为两条相互独立的路径"
            in timeline_lines
        )
        assert any(line.startswith("[T1] 搜索 ") for line in timeline_lines)
        assert any(line.startswith("[T2] 搜索 ") for line in timeline_lines)
        assert sum("：已保存任务要求的直接证据。" in line for line in timeline_lines) == 2
        scout_budget = standard_limits.stages["scout"]
        assert any(
            f"Worker 决策轮预算 {scout_budget.max_worker_rounds} 轮" in line
            for line in timeline_lines
        )
        assert "[核验] Plan v1 通过（重大缺口 0，冲突裁决 0，废证 0）" in timeline_lines
        assert "[核验] 收工：测试证据足以履行 Plan。" in timeline_lines
        assert "[成文] Research Verifier 已放行，等待 Writer" in timeline_lines
        assert "[成文] Writer 正在组织深度研究报告" in timeline_lines
        assert any(line.startswith("[成文] 报告已渲染") for line in timeline_lines)
        assert timeline_lines[-1] == "[成文] 报告渲染完成"
        assert any(
            line.startswith("[研究] 研究阶段结束，等待核验（Plan v1，触发：")
            for line in timeline_lines
        )
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
            verifier_reason = conn.execute(
                text("SELECT decision_reason FROM app.verifier_runs WHERE job_id=:job_id"),
                {"job_id": job_id},
            ).scalar_one()
            report_row = (
                conn.execute(
                    text(
                        """
                        SELECT r.status, rr.full_prompt, rr.body_char_count,
                               r.markdown_ref, r.json_ref,
                               (SELECT COUNT(*) FROM app.report_statements rs
                                WHERE rs.report_id=r.id) AS statement_count
                        FROM app.reports r JOIN app.report_revisions rr ON rr.report_id=r.id
                        WHERE r.job_id=:job_id
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .one()
            )
        assert len(prompts) == 4
        assert len(assertions) == 2
        assert verifier_reason == "测试证据足以履行 Plan。"
        assert report_row["status"] == "draft_rendered"
        assert int(report_row["body_char_count"]) > 0
        assert int(report_row["statement_count"]) == 4
        assert str(report_row["markdown_ref"]).endswith("/report.md")
        assert str(report_row["json_ref"]).endswith("/report.json")
        assert "2026 年公开了可核对事实" not in json.dumps(
            report_row["full_prompt"], ensure_ascii=False
        )
        assert all(str(row["task_id"]) == row["produced_by"]["task_id"] for row in assertions)
        assert "document_view" not in json.dumps(prompts, ensure_ascii=False, default=str)
        assert "你的输出不合法" in json.dumps(
            prompts[1]["full_prompt"], ensure_ascii=False, default=str
        )
    finally:
        with repository.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_report_verifier_contract_failure_closes_every_persisted_state() -> None:
    dispatch = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "dispatch": {"tasks": [_task("逐句验证失败")], "reason": "先取得一条证据"},
        }
    )
    finish = PlannerDecision.model_validate(
        {"decision": "finish", "finish": {"reason": "已有证据，进入成文"}}
    )
    planner = ScriptedPlanner([dispatch, finish])
    services, _, _ = _services(planner, report_verifier=BrokenReportVerifier())
    job_id, brief_id, repository = _create_research_job("quick")
    try:
        with checkpointer_session() as checkpointer:
            graph = build_research_graph(checkpointer, services)
            result = graph.invoke(
                initial_research_state(job_id=str(job_id), brief_id=str(brief_id)),
                thread_config(str(job_id)),
            )

        assert result["outcome"] == "failed"
        assert result["error_code"] == "report_verifier_contract_error"
        with repository.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        SELECT j.status AS job_status, r.status AS report_status,
                               rv.status AS run_status, rv.error
                        FROM app.jobs j
                        JOIN app.reports r ON r.job_id=j.id
                        JOIN app.report_verifier_runs rv ON rv.report_id=r.id
                        WHERE j.id=:job_id
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .one()
            )
        assert row["job_status"] == "failed"
        assert row["report_status"] == "verification_failed"
        assert row["run_status"] == "failed"
        assert "s_intro" in row["error"]["message"]
        assert row["error"]["raw_output"]["attempts"][0]["finish_reason"] == "stop"
    finally:
        with repository.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_empty_finish_burns_every_round_then_fails_without_verifier() -> None:
    empty_finish = PlannerDecision.model_validate(
        {"decision": "finish", "finish": {"reason": "没有证据也尝试结束"}}
    )
    round_limit = limits_for_effort("quick").decision_round_limit
    planner = ScriptedPlanner([empty_finish] * round_limit)
    services, _, _ = _services(planner)
    job_id, brief_id, repository = _create_research_job("quick")
    try:
        with checkpointer_session() as checkpointer:
            graph = build_research_graph(checkpointer, services)
            result = graph.invoke(
                initial_research_state(job_id=str(job_id), brief_id=str(brief_id)),
                thread_config(str(job_id)),
            )
        assert result["decision_round"] == round_limit
        assert result["outcome"] == "failed"
        assert result["error_code"] == "research_budget_exhausted_without_evidence"
        rejected = [
            event
            for event in repository.list_events(job_id)
            if event["event_type"] == "planner.rejected"
        ]
        assert [event["payload"]["reason_code"] for event in rejected] == [
            "empty_finish"
        ] * round_limit
    finally:
        with repository.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_unusable_disposition_filters_assertions_and_replans() -> None:
    dispatch = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "dispatch": {
                "tasks": [_task("废证")],
                "reason": "先落一条证据再触发废证",
            },
        }
    )
    finish = PlannerDecision.model_validate(
        {"decision": "finish", "finish": {"reason": "证据已落库，交给核验"}}
    )
    replan_dispatch = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "dispatch": {
                "tasks": [_task("补查")],
                "reason": "按废证缺口补查真实来源",
            },
        }
    )
    finish_again = PlannerDecision.model_validate(
        {"decision": "finish", "finish": {"reason": "补查完成，再次交给核验"}}
    )
    verifier = CredibilityGapVerifier()
    planner = ScriptedPlanner([dispatch, finish, replan_dispatch, finish_again])
    services, _, _ = _services(planner, verifier=verifier)
    job_id, brief_id, repository = _create_research_job("quick")
    try:
        with checkpointer_session() as checkpointer:
            graph = build_research_graph(checkpointer, services)
            result = graph.invoke(
                initial_research_state(job_id=str(job_id), brief_id=str(brief_id)),
                thread_config(str(job_id)),
            )
        assert result["outcome"] == "draft_rendered"
        assert verifier.calls == 2
        events = repository.list_events(job_id)
        gap_feedback = next(
            event
            for event in events
            if event["event_type"] == "verifier.completed"
            and event["payload"].get("release_decision") == "needs_research"
        )
        assert gap_feedback["payload"]["unusable_assertion_count"] == 1
        with repository.engine.connect() as conn:
            first_task_id = UUID(
                str(
                    conn.execute(
                        text(
                            """
                            SELECT id FROM app.tasks
                            WHERE job_id=:job_id
                            ORDER BY created_at, id LIMIT 1
                            """
                        ),
                        {"job_id": job_id},
                    ).scalar_one()
                )
            )
        all_assertions = repository.list_assertions(first_task_id)
        usable = repository.list_assertions(first_task_id, usable_only=True)
        assert len(all_assertions) == 1
        assert usable == []
        assert all_assertions[0].assertion_id in repository.list_effective_unusable_assertion_ids(
            job_id
        )
        with repository.engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        """
                        SELECT full_prompt FROM app.decision_log
                        WHERE job_id=:job_id ORDER BY decision_round
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .all()
            )
            decision_prompts = [json.dumps(row["full_prompt"], ensure_ascii=False) for row in rows]
        assert any("unusable_assertions" in prompt for prompt in decision_prompts)
        assert any("测试废证：伪学术来源。" in prompt for prompt in decision_prompts)
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
        assert first["research_stage"] == "scout"
        assert first["question"]
        assert second["status"] == "done"
        assert second["research_stage"] == "scout"
        assert model.research_runs[str(task_id)] == 1
        assert model.coverage_checks[str(task_id)] == 1
        feedback = repository.get_task_feedback(task_id)
        assert feedback["status"] == "done"
        assert feedback["stop_reason"] == "expected_evidence_satisfied"
        assert feedback["tool_calls_used"] == 3
        assert feedback["error"] is None
        assertions = repository.list_assertions(task_id)
        assert len(assertions) == 1
        assert feedback["worker_summary"]["items"][0]["assertion_id"] == str(
            assertions[0].assertion_id
        )
        assert first["assertions"][0]["assertion_id"] == str(assertions[0].assertion_id)
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


def test_tool_call_count_is_distinct_when_partial_success_is_followed_by_error() -> None:
    job_id, _, repository = _create_research_job("quick")
    draft = PlannerDecision.model_validate(
        {
            "decision": "dispatch",
            "dispatch": {"tasks": [_task("部分成功")], "reason": "工具事件关联测试"},
        }
    ).dispatch
    assert draft is not None
    plan = repository.create_plan(
        job_id,
        1,
        [inject_task_budget(draft.tasks[0], "quick")],
        reason="工具事件关联测试",
    )
    task_id = plan.task_ids[0]
    try:
        repository.record_tool_used(
            job_id,
            task_id,
            {
                "tool": "web_fetch",
                "tool_call_id": "fetch-partial",
                "url": "https://example.test/source",
                "doc_id": str(UUID("00000000-0000-4000-8000-000000000123")),
            },
        )
        repository.record_tool_used(
            job_id,
            task_id,
            {
                "tool": "web_fetch",
                "tool_call_id": "fetch-partial",
                "error": "Exa highlights 为空",
                "result_count": 0,
            },
        )

        assert repository.count_task_tool_events(task_id) == 1
        assert repository.has_task_tool_error_event(task_id, "fetch-partial") is True
    finally:
        with repository.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})
