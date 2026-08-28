"""Unit tests for Report Verifier adapter and patch assembly."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from prospector.agents.prompts.report_verifier import (
    report_quality_messages,
    report_verifier_messages,
)
from prospector.agents.report_verifier import (
    OpenAIReportVerifier,
    ReportVerifierOutputError,
    _encode_excerpt_ids,
)
from prospector.agents.report_writer import OpenAIReportWriter
from prospector.deterministic.statement_patches import (
    apply_statement_patches,
    preceding_statement_ids,
)
from prospector.schemas.claims import (
    BridgeStatementDecision,
    EvidenceStatementDecision,
    ReportRequirementFailure,
    ReportVerifierFindings,
    ReportVerifierSnapshot,
    ReportVerifierStatementInput,
    StatementFailure,
)
from prospector.schemas.report import (
    ReportDraft,
    ReportStatement,
    WriterSnapshot,
)
from prospector.schemas.report_patch import ReportPatchAssembler

EXCERPT_ID = UUID("10000000-0000-0000-0000-000000000001")
CONFLICT_EXCERPT_ID = UUID("10000000-0000-0000-0000-000000000099")


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
                    "task_id": "50000000-0000-0000-0000-000000000001",
                    "assertion_statement": "事实",
                    "excerpts": [
                        {
                            "excerpt_id": str(EXCERPT_ID),
                            "text": "原文片段。",
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


class _SequencedCompletions:
    """Answers a fixed sequence of payloads, recording the system prompt each was asked with."""

    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = payloads
        self.calls = 0
        self.systems: list[str] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.systems.append(kwargs["messages"][0]["content"])
        payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        return _completion(json.dumps(payload, ensure_ascii=False))


class _StatementAndQualityCompletions:
    def __init__(self) -> None:
        self.calls = 0
        self.quality_user = ""

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        user = kwargs["messages"][1]["content"]
        if "请检查下面完整报告的组织质量" in user:
            self.quality_user = user
            return _completion(
                json.dumps(
                    {
                        "requirement_failures": [
                            {
                                "kind": "core_answer",
                                "statement_ids": ["s_intro"],
                                "reason": "结论只重复前文，没有回答研究问题。",
                            }
                        ],
                        "reminders": [
                            {
                                "kind": "repetition",
                                "location": "综合结论",
                                "statement_ids": ["s_intro"],
                                "reason": "结论重复前文。",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        return _completion(json.dumps(_BRIDGE_PASS, ensure_ascii=False))


class _FailedStatementThenQualityCompletions:
    def __init__(self) -> None:
        self.calls = 0
        self.quality_seen = False

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls += 1
        user = kwargs["messages"][1]["content"]
        if "请检查下面完整报告的组织质量" in user:
            self.quality_seen = True
            return _completion(json.dumps({"requirement_failures": [], "reminders": []}))
        return _completion(
            json.dumps(
                {
                    **_BRIDGE_PASS,
                    "contains_factual_claim": True,
                    "reason": "衔接句夹带事实。",
                },
                ensure_ascii=False,
            )
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


def test_report_requirement_failure_requires_locations_matching_repair_scope() -> None:
    local = ReportRequirementFailure(
        kind="internal_consistency",
        repair_scope="paragraph",
        paragraph_ids=["p_c"],
        reason="结论段内部矛盾。",
    )

    assert local.paragraph_ids == ["p_c"]
    with pytest.raises(ValueError, match="paragraph repair requires paragraph_ids"):
        ReportRequirementFailure(
            kind="internal_consistency",
            repair_scope="paragraph",
            reason="缺少修订位置。",
        )
    with pytest.raises(ValueError, match="report repair must not name paragraph_ids"):
        ReportRequirementFailure(
            kind="core_answer",
            repair_scope="report",
            paragraph_ids=["p_c"],
            reason="全文问题不能伪装成局部问题。",
        )


def test_whole_report_prompt_distinguishes_blockers_from_writing_reminders() -> None:
    system = report_quality_messages({"scopes": [], "statement_checks": []})[0]["content"]

    for kind in (
        "core_answer",
        "user_constraint",
        "conclusion_support",
        "internal_consistency",
        "material_omission",
        "overall_calibration",
    ):
        assert kind in system
    assert "repair_scope=paragraph" in system
    assert "普通重复" not in system
    assert "只记录、不阻止通过" in system


def test_a_truncated_answer_is_retried_rather_than_sent_to_the_syntax_repairer() -> None:
    """Repair cannot recover a length stop: the syntax is fine, the content is missing."""
    completions = _TruncatingCompletions(_BRIDGE_PASS, truncate_times=1)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

    result = verifier.verify(_bridge_snapshot())

    assert result.findings.all_passed
    assert completions.calls == 2
    assert completions.message_counts == [2, 2]


def test_two_unusable_answers_fail_the_verifier_without_inventing_a_finding() -> None:
    """No valid decision means the verifier failed; it does not mean the prose is wrong."""
    completions = _TruncatingCompletions(_BRIDGE_PASS, truncate_times=99)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

    snapshot = _bridge_snapshot()
    snapshot.report_context = {
        "brief_question": "q",
        "title": "t",
        "scopes": [],
    }
    with pytest.raises(ReportVerifierOutputError, match="cut off twice") as raised:
        verifier.verify(snapshot)

    assert completions.calls == 2
    raw_output = cast(dict[str, dict[str, list[dict[str, str]]]], raised.value.raw_output)
    attempts = raw_output["s_intro"]["attempts"]
    assert attempts[0]["finish_reason"] == "length"
    assert attempts[1]["finish_reason"] == "length"


def test_unterminated_json_with_stop_reason_is_retried_then_fails_clearly() -> None:
    completions = _MalformedCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

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
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

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


def test_incomplete_evidence_pairs_are_retried_with_the_rule_that_was_broken() -> None:
    """A readable verdict that misses one candidate excerpt is repairable, not fatal.

    The contract checks used to sit after the retry loop, so a single slip on any one
    statement ended a Job carrying a hundred-odd verdicts that had already passed.
    """
    second_excerpt = UUID("10000000-0000-0000-0000-000000000002")
    covers_one: dict[str, object] = {
        "statement_id": "s_fact",
        "kind": "evidence",
        "claim_type": "fact",
        "pairs": [{"excerpt_id": "E1", "relation": "support"}],
        "status": "pass",
        "reason": "第一条候选支持该句。",
    }
    covers_both: dict[str, object] = {
        **covers_one,
        "pairs": [
            {"excerpt_id": "E1", "relation": "support"},
            {"excerpt_id": "E2", "relation": "support"},
        ],
    }
    completions = _SequencedCompletions([covers_one, covers_both])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

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
                    text="两条候选都支持该句。",
                    kind="evidence",
                    candidate_excerpts=[
                        {"excerpt_id": str(EXCERPT_ID), "text": "候选一。"},
                        {"excerpt_id": str(second_excerpt), "text": "候选二。"},
                    ],
                )
            ],
            allowed_excerpt_ids=[EXCERPT_ID, second_excerpt],
        )
    )

    assert result.findings.all_passed
    assert completions.calls == 2
    # The retry names the broken rule, in the short codes the model was actually shown.
    retry_system = completions.systems[1]
    assert "不满足契约" in retry_system
    assert "缺少 ['E2']" in retry_system


def test_known_conflicts_do_not_leak_non_candidate_excerpt_ids() -> None:
    """Conflict cards are context. Only candidate excerpts keep a pairs-writable code."""
    payload = ReportVerifierStatementInput(
        statement_id="s_fact",
        text="该指标已经得到证实。",
        kind="evidence",
        candidate_excerpts=[
            {"excerpt_id": str(EXCERPT_ID), "text": "来源甲称该指标为 42%。", "title": "甲"}
        ],
        known_conflicts=[
            {
                "conflict_key": "conflict:test",
                "disputed_point": "该指标是否为 42%",
                "decision": "adjudicated",
                "winning_excerpt_ids": [str(CONFLICT_EXCERPT_ID)],
                "excerpts": [
                    {
                        "excerpt_id": str(EXCERPT_ID),
                        "text": "来源甲称该指标为 42%。",
                        "title": "甲",
                    },
                    {
                        "excerpt_id": str(CONFLICT_EXCERPT_ID),
                        "text": "来源乙记录了不同口径。",
                        "title": "乙",
                    },
                ],
            }
        ],
    ).model_dump(mode="json")

    _encode_excerpt_ids(payload)
    user = report_verifier_messages(payload)[1]["content"]
    conflict = payload["known_conflicts"][0]
    by_title = {item["title"]: item for item in conflict["excerpts"]}

    assert str(EXCERPT_ID) not in user
    assert str(CONFLICT_EXCERPT_ID) not in user
    assert "winning_excerpt_ids" not in conflict
    assert by_title["甲"]["excerpt_id"] == "E1"
    assert by_title["甲"]["winning"] is False
    assert "excerpt_id" not in by_title["乙"]
    assert by_title["乙"]["winning"] is True


def test_non_candidate_pairs_are_dropped_without_retry() -> None:
    """Extra conflict-card ids are bookkeeping, not a verifier crash."""
    completions = _FakeCompletions(
        {
            "s_fact": {
                "statement_id": "s_fact",
                "kind": "evidence",
                "claim_type": "fact",
                "pairs": [
                    {"excerpt_id": "E1", "relation": "support"},
                    {"excerpt_id": str(CONFLICT_EXCERPT_ID), "relation": "irrelevant"},
                ],
                "status": "pass",
                "reason": "候选原文支持该句。",
            }
        }
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

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
                    text="候选原文支持的事实。",
                    kind="evidence",
                    candidate_excerpts=[{"excerpt_id": str(EXCERPT_ID), "text": "候选证据原文。"}],
                    known_conflicts=[
                        {
                            "conflict_key": "conflict:test",
                            "decision": "present_both",
                            "disputed_point": "口径是否一致",
                            "excerpts": [
                                {
                                    "excerpt_id": str(CONFLICT_EXCERPT_ID),
                                    "text": "另一口径。",
                                    "title": "乙",
                                }
                            ],
                        }
                    ],
                )
            ],
            allowed_excerpt_ids=[EXCERPT_ID],
        )
    )

    assert result.findings.all_passed
    assert completions.calls == 1
    decision = result.decisions[0]
    assert isinstance(decision, EvidenceStatementDecision)
    assert [pair.excerpt_id for pair in decision.pairs] == [EXCERPT_ID]


def test_pairs_that_only_cite_known_conflicts_still_retry_for_the_missing_candidate() -> None:
    completions = _SequencedCompletions(
        [
            {
                "statement_id": "s_fact",
                "kind": "evidence",
                "claim_type": "fact",
                "pairs": [{"excerpt_id": str(CONFLICT_EXCERPT_ID), "relation": "support"}],
                "status": "pass",
                "reason": "用了冲突卡里的另一条原文。",
            },
            {
                "statement_id": "s_fact",
                "kind": "evidence",
                "claim_type": "fact",
                "pairs": [{"excerpt_id": "E1", "relation": "support"}],
                "status": "pass",
                "reason": "改回本句候选原文。",
            },
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

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
                    text="需要逐句核验的事实。",
                    kind="evidence",
                    candidate_excerpts=[{"excerpt_id": str(EXCERPT_ID), "text": "候选证据原文。"}],
                )
            ],
            allowed_excerpt_ids=[EXCERPT_ID],
        )
    )

    assert result.findings.all_passed
    assert completions.calls == 2
    assert "缺少 ['E1']" in completions.systems[1]
    assert str(CONFLICT_EXCERPT_ID) not in completions.systems[1]


def test_schema_valid_but_inconsistent_verdict_gets_targeted_retry() -> None:
    invalid: dict[str, object] = {
        "statement_id": "s_fact",
        "kind": "evidence",
        "claim_type": "fact",
        "pairs": [{"excerpt_id": "E1", "relation": "support"}],
        "conflict_keys": ["conflict:test"],
        "status": "pass",
        "reason": "正文已经呈现分歧，因此可以通过。",
    }
    corrected: dict[str, object] = {
        "statement_id": "s_fact",
        "kind": "evidence",
        "claim_type": "fact",
        "pairs": [{"excerpt_id": "E1", "relation": "support"}],
        "conflict_keys": [],
        "status": "pass",
        "reason": "正文已经呈现分歧，pass 时不记录 conflict_keys。",
    }
    completions = _SequencedCompletions([invalid, corrected])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

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
                    text="需要逐句核验的事实。",
                    kind="evidence",
                    candidate_excerpts=[
                        {"excerpt_id": str(EXCERPT_ID), "text": "只覆盖部分背景。"}
                    ],
                )
            ],
            allowed_excerpt_ids=[EXCERPT_ID],
        )
    )

    assert completions.calls == 2
    assert "conflict_keys are only valid for conflicted decisions" in completions.systems[1]
    assert result.decisions[0].status == corrected["status"]


def test_unsupported_compound_statement_retains_supporting_pairs() -> None:
    second_excerpt = UUID("10000000-0000-0000-0000-000000000002")
    completions = _SequencedCompletions(
        [
            {
                "statement_id": "s_fact",
                "kind": "evidence",
                "claim_type": "fact",
                "pairs": [
                    {"excerpt_id": "E1", "relation": "support"},
                    {"excerpt_id": "E2", "relation": "partial"},
                ],
                "conflict_keys": [],
                "status": "unsupported",
                "reason": "E1 支持部分数据，但关键日期没有原文支持，整句不能通过。",
            }
        ]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

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
                    text="部分数据有依据，但关键日期没有依据。",
                    kind="evidence",
                    candidate_excerpts=[
                        {"excerpt_id": str(EXCERPT_ID), "text": "支持其中一项数据。"},
                        {"excerpt_id": str(second_excerpt), "text": "只覆盖部分背景。"},
                    ],
                )
            ],
            allowed_excerpt_ids=[EXCERPT_ID, second_excerpt],
        )
    )

    assert completions.calls == 1
    assert not result.findings.all_passed
    assert result.findings.failures[0].status == "unsupported"
    decision = result.decisions[0]
    assert [pair.relation for pair in decision.pairs] == ["support", "partial"]  # type: ignore[union-attr]


def test_irrelevant_candidate_is_a_valid_non_supporting_relation() -> None:
    unrelated_excerpt = UUID("10000000-0000-0000-0000-000000000002")
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(
                {
                    "s_fact": {
                        "statement_id": "s_fact",
                        "kind": "evidence",
                        "claim_type": "fact",
                        "pairs": [
                            {"excerpt_id": "E1", "relation": "support"},
                            {"excerpt_id": "E2", "relation": "irrelevant"},
                        ],
                        "conflict_keys": [],
                        "status": "pass",
                        "reason": "E1 支持该句，E2 与该句无关。",
                    }
                }
            )
        )
    )
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

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
                        {"excerpt_id": str(EXCERPT_ID), "text": "原文支持的事实。"},
                        {"excerpt_id": str(unrelated_excerpt), "text": "另一个无关主题。"},
                    ],
                )
            ],
            allowed_excerpt_ids=[EXCERPT_ID, unrelated_excerpt],
        )
    )

    assert result.findings.all_passed
    assert [pair.relation for pair in result.decisions[0].pairs] == [  # type: ignore[union-attr]
        "support",
        "irrelevant",
    ]


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
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]
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


def test_report_verifier_checks_direct_excerpts_on_a_derived_statement() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(
                {
                    "s_analysis": {
                        "statement_id": "s_analysis",
                        "kind": "derived",
                        "claim_type": "fact",
                        "inference_note": "直接综合原文",
                        "pairs": [{"excerpt_id": "E1", "relation": "support"}],
                        "status": "pass",
                        "reason": "原文支持该判断",
                    }
                }
            )
        )
    )
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

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
                    text="这些结果显示一个共同趋势。",
                    kind="derived",
                    candidate_excerpts=[
                        {"excerpt_id": str(EXCERPT_ID), "text": "多组结果呈现同一方向。"}
                    ],
                )
            ],
            allowed_excerpt_ids=[EXCERPT_ID],
        )
    )

    decision = result.decisions[0]
    assert result.findings.all_passed
    assert decision.kind == "derived"
    assert decision.pairs[0].excerpt_id == EXCERPT_ID  # type: ignore[union-attr]


def test_known_research_conflict_can_fail_a_one_sided_statement() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(
                {
                    "s_fact": {
                        "statement_id": "s_fact",
                        "kind": "evidence",
                        "claim_type": "fact",
                        "pairs": [{"excerpt_id": "E1", "relation": "support"}],
                        "conflict_keys": ["conflict:test"],
                        "status": "conflicted",
                        "reason": "正文把存在直接分歧的一方写成了无争议事实。",
                    }
                }
            )
        )
    )
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]
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
                    text="该指标已经得到证实。",
                    kind="evidence",
                    candidate_excerpts=[
                        {"excerpt_id": str(EXCERPT_ID), "text": "来源甲称该指标为 42%。"}
                    ],
                    known_conflicts=[
                        {
                            "conflict_key": "conflict:test",
                            "decision": "present_both",
                            "disputed_point": "该指标是否为 42%",
                        }
                    ],
                )
            ],
            allowed_excerpt_ids=[EXCERPT_ID],
        )
    )

    assert result.findings.failures[0].status == "conflicted"
    assert result.decisions[0].conflict_keys == ["conflict:test"]  # type: ignore[union-attr]


def test_derived_with_failed_premises_is_blocked_without_an_llm_call() -> None:
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
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]
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
    assert not result.findings.all_passed
    assert result.findings.failures[0].status == "unsupported"
    assert "前提硬闸门" in result.findings.failures[0].reason
    assert client.chat.completions.calls == 0


def test_deep_but_grounded_reasoning_is_left_to_semantic_verification() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(
                {
                    "s_deep": {
                        "statement_id": "s_deep",
                        "kind": "derived",
                        "claim_type": "fact",
                        "inference_note": "由多层已验证判断综合",
                        "status": "pass",
                        "reason": "证据链完整且归纳范围诚实",
                    }
                }
            )
        )
    )
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]
    snapshot = ReportVerifierSnapshot(
        job_id=UUID("20000000-0000-0000-0000-000000000001"),
        report_id=UUID("40000000-0000-0000-0000-000000000001"),
        revision=1,
        round=1,
        brief_question="q",
        statements=[
            ReportVerifierStatementInput(
                statement_id="s_deep",
                text="多层判断共同支持这一总结论。",
                kind="derived",
                premises=[
                    {
                        "statement_id": "s_one_below",
                        "text": "下一层推理。",
                        "kind": "derived",
                        "passed": True,
                    }
                ],
                premise_depth=12,
            )
        ],
    )

    result = verifier.verify(snapshot)

    assert result.findings.all_passed
    assert result.decisions[0].status == "pass"
    assert client.chat.completions.calls == 1


def test_verifier_marks_an_ungrounded_derived_statement_for_revision_without_an_llm_call() -> None:
    verifier = OpenAIReportVerifier(client=SimpleNamespace(), model="qwen-fake-model")  # type: ignore[arg-type]
    snapshot = ReportVerifierSnapshot(
        job_id=UUID("20000000-0000-0000-0000-000000000001"),
        report_id=UUID("40000000-0000-0000-0000-000000000001"),
        revision=1,
        round=1,
        brief_question="q",
        statements=[
            ReportVerifierStatementInput(
                statement_id="s_ungrounded",
                text="把衔接句推成事实结论。",
                kind="derived",
                premises=[
                    {
                        "statement_id": "s_bridge",
                        "text": "下文讨论。",
                        "kind": "elaboration",
                        "passed": True,
                    }
                ],
                premise_depth=1,
            )
        ],
    )

    result = verifier.verify(snapshot)

    assert [(failure.statement_id, failure.status) for failure in result.findings.failures] == [
        ("s_ungrounded", "unsupported")
    ]
    assert "s_bridge" in result.findings.failures[0].reason


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
    assembler = ReportPatchAssembler(
        snapshot=_snapshot(),
        allowed_statement_ids={"s_fact"},
        legal_premise_ids={"s_fact": set()},
    )
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


def test_patch_assembler_rejects_a_premise_that_comes_later_in_the_draft() -> None:
    """Revision shows the model the whole report, so "earlier" stops being self-evident.

    Grounding an early sentence on evidence further down reads fine to a model holding the
    finished draft, and produces a report whose reader meets the conclusion before its basis.
    """
    assembler = ReportPatchAssembler(
        snapshot=_snapshot(),
        allowed_statement_ids={"s_intro"},
        legal_premise_ids=preceding_statement_ids(_draft()),
    )
    outcome = assembler.consume(
        json.dumps(
            {
                "record": "patch_statement",
                "statement_id": "s_intro",
                "text": "引言直接下了结论。",
                "kind": "derived",
                "candidate_excerpt_ids": [],
                "premise_statement_ids": ["s_fact"],
            },
            ensure_ascii=False,
        )
    )
    assert outcome.error is not None
    assert "排在它之后" in outcome.error
    assert "s_fact" in outcome.error


def test_writer_revise_retries_when_the_assembled_draft_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed patch is retried before the report revision is rejected."""
    writer = OpenAIReportWriter(client=SimpleNamespace(), model="qwen-fake-model")  # type: ignore[arg-type]

    def patch(**fields: object) -> str:
        return "\n".join(
            [
                json.dumps({"record": "patch_statement", **fields}, ensure_ascii=False),
                '{"record":"end"}',
            ]
        )

    turns = iter(
        [
            patch(
                statement_id="s_fact",
                text="由引言推出的说法。",
                kind="derived",
                candidate_excerpt_ids=[],
                premise_statement_ids=[],
            ),
            patch(
                statement_id="s_fact",
                text="补丁事实",
                kind="evidence",
                candidate_excerpt_ids=["e_01"],
                premise_statement_ids=[],
            ),
        ]
    )
    prompts: list[list[dict[str, str]]] = []

    def fake_stream(messages: list[dict[str, str]]) -> str:
        prompts.append([dict(message) for message in messages])
        return next(turns)

    monkeypatch.setattr(writer, "_stream_content", fake_stream)
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
    assert len(prompts) == 2
    # A malformed stream record is corrected before the revision is rejected.
    restart = prompts[1][-1]["content"]
    assert "derived statement requires candidate_excerpt_ids or premise_statement_ids" in restart
    assert "已接受的最后一条记录" in restart


def test_writer_revise_applies_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = OpenAIReportWriter(client=SimpleNamespace(), model="qwen-fake-model")  # type: ignore[arg-type]
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


def test_reasoning_depth_is_observational_not_a_sentence_failure() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=_FakeCompletions(
                {
                    "s_at_cap": {
                        "statement_id": "s_at_cap",
                        "kind": "derived",
                        "claim_type": "fact",
                        "inference_note": "由分论点收束为全文论点",
                        "status": "pass",
                        "reason": "归纳范围已写明",
                    }
                }
            )
        )
    )
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

    result = verifier.verify(
        ReportVerifierSnapshot(
            job_id=UUID("20000000-0000-0000-0000-000000000001"),
            report_id=UUID("40000000-0000-0000-0000-000000000001"),
            revision=1,
            round=1,
            brief_question="q",
            statements=[
                ReportVerifierStatementInput(
                    statement_id="s_at_cap",
                    text="就本报告收集到的材料而言，这些线索指向同一个判断。",
                    kind="derived",
                    premises=[
                        {
                            "statement_id": "s_one_below",
                            "text": "下一层推理。",
                            "kind": "derived",
                            "passed": True,
                        }
                    ],
                    premise_depth=8,
                )
            ],
        )
    )

    assert result.findings.all_passed
    assert client.chat.completions.calls == 1


def test_derived_prompt_uses_only_declared_premises_and_their_excerpts() -> None:
    """Every fact allowed to support a judgement must be an auditable premise."""
    statement = ReportVerifierStatementInput(
        statement_id="s_pattern",
        text="这些案例显示落地集中在流程标准化的场景。",
        kind="derived",
        premises=[
            {
                "statement_id": "s_case_a",
                "text": "甲公司部署了订单处理 Agent。",
                "kind": "evidence",
                "passed": True,
                "excerpts": [
                    {
                        "text": "甲公司订单处理时间从 3 小时缩短到 15 分钟。",
                        "title": "甲公司案例",
                        "url": "https://example.test/a",
                    }
                ],
            },
            {
                "statement_id": "s_case_b",
                "text": "乙公司部署了客服 Agent。",
                "kind": "evidence",
                "passed": True,
                "excerpts": [
                    {
                        "text": "乙公司把客服 Agent 用于标准化工单分流。",
                        "title": "乙公司案例",
                        "url": "https://example.test/b",
                    }
                ],
            },
        ],
        premise_depth=1,
    ).model_dump(mode="json")

    messages = report_verifier_messages(statement)
    system, user = messages[0]["content"], messages[1]["content"]

    assert "没有被列为 premise 的其他句子不能补足依据" in system
    assert "不得写入 pairs" in system
    assert "乙公司部署了客服 Agent。" in user
    assert "甲公司订单处理时间从 3 小时缩短到 15 分钟。" in user
    assert "paragraph_statements" not in user


def test_evidence_prompt_stays_narrow() -> None:
    """An evidence sentence is judged against its Excerpt alone.

    Neighbouring sentences must never be able to stand in for a missing source, so the
    paragraph context that derived statements get is deliberately withheld here.
    """
    statement = ReportVerifierStatementInput(
        statement_id="s_fact",
        text="甲公司部署了订单处理 Agent。",
        kind="evidence",
        candidate_excerpts=[{"excerpt_id": "E1", "text": "甲公司上线了订单处理 Agent。"}],
    )

    user = report_verifier_messages(statement.model_dump(mode="json"))[1]["content"]
    assert "甲公司上线了订单处理 Agent。" in user
    assert "paragraph_statements" not in user


def test_evidence_prompt_checks_source_attribution_and_known_conflicts() -> None:
    statement = ReportVerifierStatementInput(
        statement_id="s_fact",
        text="该指标已经得到证实。",
        kind="evidence",
        candidate_excerpts=[
            {
                "excerpt_id": "E1",
                "text": "某媒体援引匿名人士称该指标为 42%。",
                "url": "https://example.test/report",
                "title": "媒体报道",
                "author": "记者甲",
            }
        ],
        known_conflicts=[
            {
                "conflict_key": "conflict:test",
                "decision": "present_both",
                "disputed_point": "该指标是否为 42%",
            }
        ],
    )

    system = report_verifier_messages(statement.model_dump(mode="json"))[0]["content"]

    assert "报道、机构表态或分析观点" in system
    assert "present_both" in system
    assert "conflict_keys" in system
    assert "known_conflicts 只用于判断分寸，不得写入 pairs" in system
    assert "winning_excerpt_ids" not in system


def test_missing_core_answer_blocks_verification_while_quality_reminders_do_not() -> None:
    completions = _StatementAndQualityCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]
    snapshot = _bridge_snapshot()
    snapshot.report_context = {
        "brief_question": "研究问题是什么？",
        "title": "报告",
        "scopes": [
            {
                "kind": "conclusion",
                "title": None,
                "paragraphs": [
                    {
                        "paragraph_id": "p_c",
                        "statements": [
                            {
                                "statement_id": "s_intro",
                                "text": "接下来讨论这一变化的影响。",
                                "kind": "elaboration",
                                "premise_statement_ids": [],
                                "premise_depth": 0,
                            }
                        ],
                    }
                ],
            }
        ],
    }

    result = verifier.verify(snapshot)

    assert not result.findings.all_passed
    assert len(result.findings.requirement_failures) == 1
    assert result.findings.requirement_failures[0].kind == "core_answer"
    assert result.findings.requirement_failures[0].repair_scope == "report"
    assert len(result.findings.quality_reminders) == 1
    assert result.findings.quality_reminders[0].kind == "repetition"
    assert '"statement_checks"' in completions.quality_user
    assert '"status": "pass"' in completions.quality_user
    assert completions.calls == 2


def test_valid_statement_failure_still_runs_whole_report_stage() -> None:
    completions = _FailedStatementThenQualityCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]
    snapshot = _bridge_snapshot()
    snapshot.report_context = {
        "brief_question": "研究问题是什么？",
        "title": "报告",
        "scopes": [
            {
                "kind": "introduction",
                "title": None,
                "paragraphs": [
                    {
                        "paragraph_id": "p_intro",
                        "statements": [
                            {
                                "statement_id": "s_intro",
                                "text": "接下来讨论这一变化的影响。",
                                "kind": "elaboration",
                                "premise_statement_ids": [],
                                "premise_depth": 0,
                            }
                        ],
                    }
                ],
            }
        ],
    }

    result = verifier.verify(snapshot)

    assert len(result.findings.failures) == 1
    assert completions.quality_seen
    assert completions.calls == 2


def test_stage_two_only_revision_skips_sentence_checks() -> None:
    class _QualityOnlyCompletions:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, **kwargs: Any) -> SimpleNamespace:
            self.calls += 1
            user = kwargs["messages"][1]["content"]
            if "请检查下面完整报告的组织质量" not in user:
                raise AssertionError("stage-two-only verification must not re-check sentences")
            return _completion(json.dumps({"requirement_failures": [], "reminders": []}))

    completions = _QualityOnlyCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIReportVerifier(client=client, model="qwen-fake-model")  # type: ignore[arg-type]
    reused = BridgeStatementDecision(
        statement_id="s_intro",
        kind="elaboration",
        contains_factual_claim=False,
        reason="上一轮已通过。",
    )
    snapshot = ReportVerifierSnapshot(
        job_id=UUID("20000000-0000-0000-0000-000000000001"),
        report_id=UUID("40000000-0000-0000-0000-000000000001"),
        revision=2,
        round=1,
        brief_question="q",
        skip_statement_verification=True,
        reused_statement_decisions=[reused],
        report_context={
            "brief_question": "q",
            "title": "t",
            "scopes": [
                {
                    "kind": "introduction",
                    "title": None,
                    "paragraphs": [
                        {
                            "paragraph_id": "p_intro",
                            "statements": [
                                {
                                    "statement_id": "s_intro",
                                    "text": "接下来讨论这一变化的影响。",
                                    "kind": "elaboration",
                                    "premise_statement_ids": [],
                                    "premise_depth": 0,
                                }
                            ],
                        }
                    ],
                }
            ],
        },
    )

    result = verifier.verify(snapshot)

    assert result.findings.all_passed
    assert result.findings.passed_statement_ids == ["s_intro"]
    assert result.raw_outputs["s_intro"] == {"reused_from_prior_revision": True}
    assert completions.calls == 1
