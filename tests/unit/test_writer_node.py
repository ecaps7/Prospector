"""Writer graph-node resume: an opened rewrite must not fall through to first write."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from prospector.agents.prompts.report_writer import (
    report_writer_messages,
    report_writer_revision_messages,
)
from prospector.agents.report_writer import ReportWriterResult
from prospector.flow.research_graph import ResearchGraphServices, _writer_node
from prospector.flow.state import initial_research_state
from prospector.schemas.claims import ReportRequirementFailure, ReportVerifierFindings
from prospector.schemas.report import ReportDraft, WriterSnapshot

EXCERPT_ID = UUID("10000000-0000-0000-0000-000000000001")
TASK_ID = UUID("50000000-0000-0000-0000-000000000001")
REPORT_ID = UUID("aaaaaaaa-0000-0000-0000-000000000001")
VERIFIER_RUN_ID = UUID("bbbbbbbb-0000-0000-0000-000000000001")


def _snapshot() -> WriterSnapshot:
    return WriterSnapshot.model_validate(
        {
            "job_id": "20000000-0000-0000-0000-000000000001",
            "brief": {
                "question": "测试深度研究报告",
                "brief_text": "比较竞争解释、反例和适用边界。",
                "output_format": "report_with_citations",
                "language": "zh",
                "effort": "quick",
            },
            "final_plan_summary": [
                {
                    "version": 1,
                    "tasks": [
                        {
                            "id": str(TASK_ID),
                            "question": "这条线索要回答的研究问题。",
                            "expected_evidence": "一条带口径的直接证据。",
                            "stop_reason": "expected_evidence_satisfied",
                        }
                    ],
                }
            ],
            "evidence_cards": [
                {
                    "assertion_id": "30000000-0000-0000-0000-000000000001",
                    "task_id": str(TASK_ID),
                    "assertion_statement": "公开材料记录了一个带时间口径的事实。",
                    "excerpts": [
                        {
                            "excerpt_id": str(EXCERPT_ID),
                            "text": "该口径下的年度数值为 42。",
                            "source": {
                                "title": "公开报告",
                                "source_uri": "https://example.test/report",
                                "document_version": 1,
                            },
                        }
                    ],
                }
            ],
        }
    )


def _draft() -> ReportDraft:
    return ReportDraft.model_validate(
        {
            "title": "深度研究报告",
            "introduction": [
                {
                    "paragraph_id": "p_intro",
                    "statements": [
                        {
                            "statement_id": "s_intro",
                            "text": "核心答案",
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
                                    "text": "公开材料记录了一个事实。",
                                    "kind": "evidence",
                                    "candidate_excerpt_ids": [str(EXCERPT_ID)],
                                    "premise_statement_ids": [],
                                }
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
                            "text": "总结",
                            "kind": "derived",
                            "candidate_excerpt_ids": [],
                            "premise_statement_ids": ["s_fact"],
                        },
                        {
                            "statement_id": "s_conclusion_2",
                            "text": "收束",
                            "kind": "derived",
                            "candidate_excerpt_ids": [],
                            "premise_statement_ids": ["s_fact"],
                        },
                    ],
                }
            ],
        }
    )


def _findings() -> ReportVerifierFindings:
    return ReportVerifierFindings(
        round=1,
        revision=1,
        requirement_failures=[
            ReportRequirementFailure(kind="core_answer", reason="未直接回答核心问题")
        ],
    )


class _FakeWriter:
    def __init__(self) -> None:
        self.write_calls = 0
        self.revise_calls = 0
        self.revised_draft: ReportDraft | None = None

    def write(self, snapshot: WriterSnapshot) -> ReportWriterResult:
        self.write_calls += 1
        draft = _draft()
        return ReportWriterResult(
            full_prompt=report_writer_messages(snapshot),
            raw_output=draft.model_dump(mode="json"),
            draft=draft,
        )

    def revise(
        self,
        snapshot: WriterSnapshot,
        draft: ReportDraft,
        findings: ReportVerifierFindings,
    ) -> ReportWriterResult:
        self.revise_calls += 1
        self.revised_draft = draft
        return ReportWriterResult(
            full_prompt=report_writer_revision_messages(snapshot, draft, findings),
            raw_output=draft.model_dump(mode="json"),
            draft=draft,
        )


class _FakeRepository:
    def __init__(self, rows: dict[int, dict[str, Any]], *, current: int) -> None:
        self.rows = rows
        self.current = current
        self.snapshot = _snapshot()
        self.findings = _findings()
        self.begin_calls: list[dict[str, Any]] = []
        self.completed: tuple[UUID, int] | None = None

    def build_writer_snapshot(self, job_id: UUID, verifier_run_id: UUID) -> WriterSnapshot:
        del job_id, verifier_run_id
        return self.snapshot

    def get_report_revision(
        self, job_id: UUID, *, revision: int | None = None
    ) -> dict[str, Any] | None:
        del job_id
        target = self.current if revision is None else revision
        row = self.rows.get(target)
        if row is None:
            return None
        return {**row, "revision": target}

    def get_latest_report_verifier_run(self, report_id: UUID) -> dict[str, Any]:
        del report_id
        return {"status": "completed", "findings": self.findings, "revision": 1}

    def begin_report_revision(
        self,
        job_id: UUID,
        verifier_run_id: UUID,
        full_prompt: list[dict[str, str]],
        *,
        bump: bool = False,
    ) -> tuple[UUID, int]:
        del job_id, verifier_run_id
        self.begin_calls.append({"bump": bump, "prompt": full_prompt})
        if bump:
            self.current += 1
        return REPORT_ID, self.current

    def complete_report_revision(
        self,
        report_id: UUID,
        draft: ReportDraft,
        raw_output: object,
        *,
        revision: int | None = None,
    ) -> None:
        del draft, raw_output
        self.completed = (report_id, revision if revision is not None else self.current)

    def fail_report_revision(self, report_id: UUID, revision: int, **kwargs: object) -> None:
        del report_id, revision, kwargs

    def set_research_outcome(self, job_id: UUID, **kwargs: object) -> None:
        del job_id, kwargs


def _row(
    *,
    status: str,
    revision_status: str,
    draft: ReportDraft | None,
) -> dict[str, Any]:
    return {
        "report_id": REPORT_ID,
        "verifier_run_id": VERIFIER_RUN_ID,
        "report_status": status,
        "revision_status": revision_status,
        "draft": draft,
        "markdown_ref": None,
        "json_ref": None,
    }


def _state() -> dict[str, Any]:
    state = cast(
        dict[str, Any],
        initial_research_state(job_id=str(uuid4()), brief_id=str(uuid4())),
    )
    state["last_verifier_run_id"] = str(VERIFIER_RUN_ID)
    return state


def _services(repository: _FakeRepository, writer: _FakeWriter) -> ResearchGraphServices:
    return ResearchGraphServices(
        repository=cast(Any, repository),
        planner=cast(Any, object()),
        worker=cast(Any, object()),
        verifier=cast(Any, object()),
        writer=cast(Any, writer),
    )


def test_opened_rewrite_replays_against_the_previous_draft() -> None:
    """SIGINT after bumping revision 2 leaves writing+prompted; resume must not first-write."""

    previous_draft = _draft()
    repository = _FakeRepository(
        {
            1: _row(status="writing", revision_status="generated", draft=previous_draft),
            2: _row(status="writing", revision_status="prompted", draft=None),
        },
        current=2,
    )
    writer = _FakeWriter()

    result = _writer_node(_services(repository, writer))(cast(Any, _state()))

    assert result["route"] == "report_verifier"
    assert writer.write_calls == 0
    assert writer.revise_calls == 1
    assert writer.revised_draft is previous_draft
    assert len(repository.begin_calls) == 1
    assert repository.begin_calls[0]["bump"] is False
    assert "请依据审稿结果重写完整报告" in repository.begin_calls[0]["prompt"][1]["content"]
    assert repository.completed == (REPORT_ID, 2)


def test_revising_a_generated_draft_still_bumps() -> None:
    draft = _draft()
    repository = _FakeRepository(
        {1: _row(status="revising", revision_status="generated", draft=draft)},
        current=1,
    )
    writer = _FakeWriter()

    result = _writer_node(_services(repository, writer))(cast(Any, _state()))

    assert result["route"] == "report_verifier"
    assert writer.write_calls == 0
    assert writer.revise_calls == 1
    assert repository.begin_calls[0]["bump"] is True
    assert repository.completed == (REPORT_ID, 2)


def test_interrupted_first_write_still_calls_write() -> None:
    repository = _FakeRepository(
        {1: _row(status="writing", revision_status="prompted", draft=None)},
        current=1,
    )
    writer = _FakeWriter()

    result = _writer_node(_services(repository, writer))(cast(Any, _state()))

    assert result["route"] == "report_verifier"
    assert writer.write_calls == 1
    assert writer.revise_calls == 0
    assert repository.begin_calls[0]["bump"] is False


def test_opened_rewrite_without_previous_draft_is_refused() -> None:
    repository = _FakeRepository(
        {2: _row(status="writing", revision_status="prompted", draft=None)},
        current=2,
    )
    writer = _FakeWriter()

    with pytest.raises(RuntimeError, match="previous generated draft"):
        _writer_node(_services(repository, writer))(cast(Any, _state()))

    assert writer.write_calls == 0
    assert writer.revise_calls == 0
    assert repository.begin_calls == []
