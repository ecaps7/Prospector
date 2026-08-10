"""Unit tests for Report Verifier adapter and patch assembly."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from prospector.agents.report_verifier import (
    OpenAIReportVerifier,
    ReportVerifierOutputError,
)
from prospector.agents.report_writer import OpenAIReportWriter
from prospector.deterministic.statement_patches import apply_statement_patches
from prospector.schemas.claims import (
    ReportVerifierFindings,
    ReportVerifierSnapshot,
    ReportVerifierStatementInput,
    StatementFailure,
)
from prospector.schemas.report import ReportDraft, ReportStatement, WriterSnapshot
from prospector.schemas.report_patch import ReportPatchAssembler

EXCERPT_ID = UUID("10000000-0000-0000-0000-000000000001")


def _snapshot() -> WriterSnapshot:
    return WriterSnapshot.model_validate(
        {
            "job_id": "20000000-0000-0000-0000-000000000001",
            "brief": {
                "question": "q",
                "brief_text": "b",
                "output_format": "report_with_citations",
                "language": "zh",
                "effort": "quick",
            },
            "final_plan_summary": [],
            "evidence_cards": [
                {
                    "assertion_id": "30000000-0000-0000-0000-000000000001",
                    "assertion_statement": "事实",
                    "excerpts": [
                        {
                            "excerpt_id": str(EXCERPT_ID),
                            "source": {
                                "title": "t",
                                "author": None,
                                "published_at": None,
                                "source_uri": "https://example.test/a",
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
            "title": "t",
            "introduction": [
                {
                    "paragraph_id": "p_intro",
                    "statements": [
                        {
                            "statement_id": "s_intro",
                            "text": "引言给出核心答案。",
                            "kind": "elaboration",
                            "candidate_excerpt_ids": [],
                            "premise_statement_ids": [],
                        }
                    ],
                }
            ],
            "sections": [
                {
                    "section_id": "sec_1",
                    "title": "一",
                    "paragraphs": [
                        {
                            "paragraph_id": "p_1",
                            "statements": [
                                {
                                    "statement_id": "s_fact",
                                    "text": "原文支持的事实。",
                                    "kind": "evidence",
                                    "candidate_excerpt_ids": [str(EXCERPT_ID)],
                                    "premise_statement_ids": [],
                                },
                                {
                                    "statement_id": "s_analysis",
                                    "text": "因此可得出分析。",
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
                    "paragraph_id": "p_c",
                    "statements": [
                        {
                            "statement_id": "s_c",
                            "text": "综上。",
                            "kind": "derived",
                            "candidate_excerpt_ids": [],
                            "premise_statement_ids": ["s_analysis"],
                        }
                    ],
                }
            ],
        }
    )


def _text_of(draft: ReportDraft, statement_id: str) -> str:
    return next(
        statement.text for statement in draft.statements() if statement.statement_id == statement_id
    )


class _FakeCompletions:
    def __init__(self, payloads: dict[str, dict[str, object]]) -> None:
        self.payloads = payloads
        self.calls = 0

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        messages = kwargs["messages"]
        user = messages[0]["content"] if len(messages) == 1 else messages[1]["content"]
        statement = json.loads(user.split("请审阅下面句子：\n", 1)[1])
        payload = self.payloads[statement["statement_id"]]
        return _completion(json.dumps(payload))


def _completion(content: str, finish_reason: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
        ]
    )


class _TruncatingCompletions:
    """Cuts the first *truncate_times* answers off mid-string, like a real length stop."""

    def __init__(self, payload: dict[str, object], truncate_times: int) -> None:
        self.payload = payload
        self.truncate_times = truncate_times
        self.calls = 0
        self.message_counts: list[int] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        self.message_counts.append(len(kwargs["messages"]))
        if self.calls <= self.truncate_times:
            full = json.dumps(self.payload, ensure_ascii=False)
            return _completion(full[: len(full) // 2], finish_reason="length")
        return _completion(json.dumps(self.payload, ensure_ascii=False))


class _MalformedCompletions:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        return _completion(
            '{"statement_id":"s_intro","kind":"elaboration",'
            '"contains_factual_claim":false,"reason":"',
            finish_reason="stop",
        )


def _bridge_snapshot(statement_id: str = "s_intro") -> ReportVerifierSnapshot:
    return ReportVerifierSnapshot(
        job_id=UUID("20000000-0000-0000-0000-000000000001"),
        report_id=UUID("40000000-0000-0000-0000-000000000001"),
        revision=1,
        round=1,
        brief_question="q",
        statements=[
            ReportVerifierStatementInput(
                statement_id=statement_id,
                text="接下来讨论这一变化的影响。",
                kind="elaboration",
            )
        ],
        allowed_excerpt_ids=[EXCERPT_ID],
    )


_BRIDGE_PASS: dict[str, object] = {
    "statement_id": "s_intro",
    "kind": "elaboration",
    "contains_factual_claim": False,
    "reason": "仅为衔接",
}


def test_a_truncated_answer_is_retried_rather_than_sent_to_the_syntax_repairer() -> None:
    """Repair cannot recover a length stop: the syntax is fine, the content is missing."""
    completions = _TruncatingCompletions(_BRIDGE_PASS, truncate_times=1)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client)  # type: ignore[arg-type]

    result = verifier.verify(_bridge_snapshot())

    assert result.findings.all_passed
    assert completions.calls == 2
    assert completions.message_counts == [2, 2]


def test_two_unusable_answers_fail_the_verifier_without_inventing_a_finding() -> None:
    """No valid decision means the verifier failed; it does not mean the prose is wrong."""
    completions = _TruncatingCompletions(_BRIDGE_PASS, truncate_times=99)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client)  # type: ignore[arg-type]

    with pytest.raises(ReportVerifierOutputError, match="cut off twice") as raised:
        verifier.verify(_bridge_snapshot())

    assert completions.calls == 2
    raw_output = cast(dict[str, dict[str, list[dict[str, str]]]], raised.value.raw_output)
    attempts = raw_output["s_intro"]["attempts"]
    assert attempts[0]["finish_reason"] == "length"
    assert attempts[1]["finish_reason"] == "length"


def test_unterminated_json_with_stop_reason_is_retried_then_fails_clearly() -> None:
    completions = _MalformedCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client)  # type: ignore[arg-type]

    with pytest.raises(ReportVerifierOutputError, match="Unterminated string") as raised:
        verifier.verify(_bridge_snapshot())

    raw_output = cast(dict[str, dict[str, list[dict[str, str]]]], raised.value.raw_output)
    attempts = raw_output["s_intro"]["attempts"]
    assert completions.calls == 2
    assert attempts[0]["finish_reason"] == "stop"
    assert "Unterminated string" in attempts[0]["parse_error"]


def test_complete_long_reason_is_accepted_without_retry() -> None:
    reason = (
        "原文明确记载宁德时代2027年小批量生产、2030年规模化量产，"
        "比亚迪2027年Q1小批量装车、2030年大规模商业化及液固同价，"
        "丰田推迟至2028年，与句子完全一致。"
    )
    completions = _FakeCompletions(
        {
            "s_fact": {
                "statement_id": "s_fact",
                "kind": "evidence",
                "claim_type": "fact",
                "pairs": [{"excerpt_id": "E1", "relation": "support"}],
                "status": "pass",
                "reason": reason,
            }
        }
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client)  # type: ignore[arg-type]

    result = verifier.verify(
        ReportVerifierSnapshot(
            job_id=UUID("20000000-0000-0000-0000-000000000001"),
            report_id=UUID("40000000-0000-0000-0000-000000000001"),
            revision=1,
            round=1,
            brief_question="q",
            statements=[
                ReportVerifierStatementInput(
                    statement_id="s_fact",
                    text="企业量产时间表与原文一致。",
                    kind="evidence",
                    candidate_excerpts=[{"excerpt_id": str(EXCERPT_ID), "text": "候选证据原文。"}],
                )
            ],
            allowed_excerpt_ids=[EXCERPT_ID],
        )
    )

    assert result.findings.all_passed
    assert result.decisions[0].reason == reason
    assert completions.calls == 1


def test_report_verifier_passes_evidence_and_derived() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(
                {
                    "s_fact": {
                        "statement_id": "s_fact",
                        "kind": "evidence",
                        "claim_type": "fact",
                        "pairs": [{"excerpt_id": "E1", "relation": "support"}],
                        "status": "pass",
                        "reason": "原文支持",
                    },
                    "s_analysis": {
                        "statement_id": "s_analysis",
                        "kind": "derived",
                        "claim_type": "causal",
                        "inference_note": "由事实归纳",
                        "status": "pass",
                        "reason": "推理成立",
                    },
                }
            )
        )
    )
    verifier = OpenAIReportVerifier(client=client)  # type: ignore[arg-type]
    result = verifier.verify(
        ReportVerifierSnapshot(
            job_id=UUID("20000000-0000-0000-0000-000000000001"),
            report_id=UUID("40000000-0000-0000-0000-000000000001"),
            revision=1,
            round=1,
            brief_question="q",
            statements=[
                ReportVerifierStatementInput(
                    statement_id="s_fact",
                    text="原文支持的事实。",
                    kind="evidence",
                    candidate_excerpts=[
                        {"excerpt_id": str(EXCERPT_ID), "text": "原文支持的事实。"}
                    ],
                    premises=[],
                    premises_all_passed=True,
                    premise_depth=0,
                ),
                ReportVerifierStatementInput(
                    statement_id="s_analysis",
                    text="因此可得出分析。",
                    kind="derived",
                    candidate_excerpts=[],
                    premises=[
                        {
                            "statement_id": "s_fact",
                            "text": "原文支持的事实。",
                            "kind": "evidence",
                            "passed": False,
                        }
                    ],
                    premises_all_passed=True,
                    premise_depth=1,
                ),
            ],
            allowed_excerpt_ids=[EXCERPT_ID],
        )
    )
    assert result.findings.all_passed
    assert client.chat.completions.calls == 2


def test_derived_with_failed_premises_calls_llm_and_lets_it_decide() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(
                {
                    "s_analysis": {
                        "statement_id": "s_analysis",
                        "kind": "derived",
                        "claim_type": "causal",
                        "inference_note": "由事实归纳",
                        "status": "pass",
                        "reason": "推理本身成立，但前提存在风险",
                    }
                }
            )
        )
    )
    verifier = OpenAIReportVerifier(client=client)  # type: ignore[arg-type]
    result = verifier.verify(
        ReportVerifierSnapshot(
            job_id=UUID("20000000-0000-0000-0000-000000000001"),
            report_id=UUID("40000000-0000-0000-0000-000000000001"),
            revision=1,
            round=1,
            brief_question="q",
            statements=[
                ReportVerifierStatementInput(
                    statement_id="s_analysis",
                    text="因此可得出分析。",
                    kind="derived",
                    candidate_excerpts=[],
                    premises=[
                        {
                            "statement_id": "s_fact",
                            "text": "原文支持的事实。",
                            "kind": "evidence",
                            "passed": False,
                        }
                    ],
                    premises_all_passed=False,
                    premise_depth=1,
                )
            ],
            allowed_excerpt_ids=[],
        )
    )
    assert result.findings.all_passed
    assert client.chat.completions.calls == 1


def test_apply_statement_patches_only_replaces_named_sentences() -> None:
    draft = _draft()
    patched = apply_statement_patches(
        draft,
        [
            ReportStatement(
                statement_id="s_fact",
                text="修订后的事实。",
                kind="evidence",
                candidate_excerpt_ids=[EXCERPT_ID],
            )
        ],
        allowed_statement_ids={"s_fact"},
    )
    assert _text_of(patched, "s_fact") == "修订后的事实。"
    assert _text_of(patched, "s_analysis") == _text_of(draft, "s_analysis")
    assert _text_of(patched, "s_intro") == _text_of(draft, "s_intro")


def test_patch_assembler_rejects_unlisted_statement() -> None:
    assembler = ReportPatchAssembler(snapshot=_snapshot(), allowed_statement_ids={"s_fact"})
    outcome = assembler.consume(
        json.dumps(
            {
                "record": "patch_statement",
                "statement_id": "s_analysis",
                "text": "x",
                "kind": "derived",
                "candidate_excerpt_ids": [],
                "premise_statement_ids": ["s_fact"],
            },
            ensure_ascii=False,
        )
    )
    assert outcome.error is not None
    assert "不在审稿失败列表" in outcome.error


def test_writer_revise_applies_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = OpenAIReportWriter(client=SimpleNamespace())  # type: ignore[arg-type]
    lines = [
        json.dumps(
            {
                "record": "patch_statement",
                "statement_id": "s_fact",
                "text": "补丁事实",
                "kind": "evidence",
                "candidate_excerpt_ids": ["e_01"],
                "premise_statement_ids": [],
            },
            ensure_ascii=False,
        ),
        '{"record":"end"}',
    ]
    monkeypatch.setattr(writer, "_stream_content", lambda _messages: "\n".join(lines))
    findings = ReportVerifierFindings(
        round=1,
        revision=1,
        failures=[
            StatementFailure(
                statement_id="s_fact",
                kind="evidence",
                status="unsupported",
                reason="数字对不上",
                allowed_excerpt_ids=[EXCERPT_ID],
            )
        ],
    )
    result = writer.revise(_snapshot(), _draft(), findings)
    assert _text_of(result.draft, "s_fact") == "补丁事实"
