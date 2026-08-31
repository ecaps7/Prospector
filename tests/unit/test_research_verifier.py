"""Research Verifier schema, transport, and graph-control unit tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, ValidationError

from prospector.agents.prompts.research_verifier import (
    research_coverage_messages,
    research_verifier_messages,
)
from prospector.agents.research_verifier import (
    OpenAIResearchVerifier,
    VerifierModelResult,
    VerifierOutputError,
)
from prospector.deterministic.budget import limits_for_effort
from prospector.deterministic.gates import PlannerRejection, finish_rejection
from prospector.deterministic.model_refs import ResearchModelRefs
from prospector.flow.research_graph import (
    ResearchGraphServices,
    VerifierMajorGapError,
    _verifier_node,
)
from prospector.flow.state import initial_research_state
from prospector.runtime.timeline import ResearchTimelineRenderer
from prospector.schemas.verifier import (
    AssertionDisposition,
    ConflictJudgement,
    VerifierCoverageDecision,
    VerifierCoverageDecisionRefs,
    VerifierDecision,
    VerifierEvidenceReview,
    VerifierEvidenceReviewRefs,
    conflict_key,
    effective_unusable_assertion_ids,
    materialize_conflict_resolutions,
    materialize_verifier_decision,
    validate_verifier_references,
)

TASK_ID = UUID("10000000-0000-0000-0000-000000000001")
ASSERTION_ID = UUID("20000000-0000-0000-0000-000000000001")
ASSERTION_B = UUID("20000000-0000-0000-0000-000000000002")
EXCERPT_A = UUID("30000000-0000-0000-0000-000000000001")
EXCERPT_B = UUID("30000000-0000-0000-0000-000000000002")


def _verifier_snapshot() -> dict[str, object]:
    return {
        "brief": {},
        "plans": [],
        "tasks": [{"task_id": str(TASK_ID)}],
        "assertions": [
            {
                "assertion_id": str(ASSERTION_ID),
                "task_id": str(TASK_ID),
                "statement": "证据 A",
                "excerpt_ids": [str(EXCERPT_A)],
            },
            {
                "assertion_id": str(ASSERTION_B),
                "task_id": str(TASK_ID),
                "statement": "证据 B",
                "excerpt_ids": [str(EXCERPT_B)],
            },
        ],
        "excerpts": [
            {"excerpt_id": str(EXCERPT_A), "url": "https://example.com/a"},
            {"excerpt_id": str(EXCERPT_B), "url": "https://example.com/b"},
        ],
    }


def _gaps(
    *,
    severity: str = "minor",
    kind: str = "plan_coverage",
) -> list[dict[str, object]]:
    return [
        {
            "kind": kind,
            "severity": severity,
            "related_task_ids": [str(TASK_ID)],
            "related_assertion_ids": [str(ASSERTION_ID)],
            "description": "关键比较口径尚未核实",
            "evidence_needed": ("补充独立来源并统一口径" if severity == "major" else ""),
        }
    ]


def _coverage_decision(
    release: str = "pass",
    *,
    severity: str = "minor",
    kind: str = "plan_coverage",
) -> VerifierCoverageDecision:
    return VerifierCoverageDecision.model_validate(
        {
            "decision": release,
            "reason": (
                "现有证据存在影响结论的重大缺口"
                if release == "needs_research"
                else "现有证据足以履行 Plan"
            ),
            "answerability_checks": [
                (
                    {
                        "requirement": "回答核心问题",
                        "status": "blocked",
                        "answer": "",
                        "supporting_assertion_ids": [],
                        "evidence_bridge": "",
                        "evidence_needed": "补充能够回答核心问题的证据",
                    }
                    if release == "needs_research"
                    else {
                        "requirement": "回答核心问题",
                        "status": "answered",
                        "answer": "现有证据支持有边界的回答",
                        "supporting_assertion_ids": [str(ASSERTION_ID)],
                        "evidence_bridge": "该断言直接回答核心问题",
                        "evidence_needed": "",
                    }
                )
            ],
            "gaps": _gaps(severity=severity, kind=kind),
        }
    )


def _evidence_review(
    *,
    dispositions: list[dict[str, object]] | None = None,
    conflicts: list[dict[str, object]] | None = None,
) -> VerifierEvidenceReview:
    return VerifierEvidenceReview.model_validate(
        {
            "source_credibility_findings": [],
            "conflicts": conflicts or [],
            "assertion_dispositions": dispositions or [],
        }
    )


def _decision(
    release: str = "pass",
    *,
    severity: str = "minor",
    kind: str = "plan_coverage",
    dispositions: list[dict[str, object]] | None = None,
) -> VerifierDecision:
    return materialize_verifier_decision(
        _evidence_review(dispositions=dispositions),
        _coverage_decision(release, severity=severity, kind=kind),
        {},
    )


def _unusable_disposition(assertion_id: UUID = ASSERTION_ID) -> dict[str, object]:
    return {
        "assertion_id": str(assertion_id),
        "status": "unusable",
        "reason": "来源为伪学术 UGC，定量数字不可采信。",
    }


def _model_payload(value: object) -> object:
    refs = ResearchModelRefs.from_verifier_snapshot(_verifier_snapshot())
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return refs.alias_payload(value)


def test_decision_requires_major_gap_exactly_when_research_is_needed() -> None:
    with pytest.raises(ValidationError, match="pass must not contain major gaps"):
        _decision("pass", severity="major")
    with pytest.raises(ValidationError, match="needs_research requires"):
        VerifierCoverageDecision.model_validate(
            {
                **_coverage_decision().model_dump(mode="json"),
                "decision": "needs_research",
                "gaps": [],
            }
        )


def test_major_gap_requires_evidence_needed() -> None:
    payload = _coverage_decision().model_dump(mode="json")
    payload["decision"] = "needs_research"
    payload["gaps"] = [{**_gaps(severity="major")[0], "evidence_needed": ""}]
    with pytest.raises(ValidationError, match="major gap requires evidence_needed"):
        VerifierCoverageDecision.model_validate(payload)


def test_verifier_rejects_blank_reason_and_gap_description() -> None:
    payload = _coverage_decision().model_dump(mode="json")
    with pytest.raises(ValidationError, match="reason must not be blank"):
        VerifierCoverageDecision.model_validate({**payload, "reason": "\n"})

    payload["gaps"] = [{**_gaps()[0], "description": "\n"}]
    with pytest.raises(ValidationError, match="description must not be blank"):
        VerifierCoverageDecision.model_validate(payload)


def test_coverage_decision_requires_auditable_core_answers() -> None:
    payload = _coverage_decision().model_dump(mode="json")
    payload["answerability_checks"][0]["evidence_bridge"] = ""
    with pytest.raises(ValidationError, match="requires an evidence_bridge"):
        VerifierCoverageDecision.model_validate(payload)

    blocked = _coverage_decision("needs_research", severity="major").model_dump(mode="json")
    blocked["decision"] = "pass"
    blocked["gaps"] = []
    with pytest.raises(ValidationError, match="pass must not contain blocked"):
        VerifierCoverageDecision.model_validate(blocked)


def test_conflict_judgement_enforces_winner_contract() -> None:
    base = {
        "disputed_point": "市场规模口径",
        "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
        "rationale": "来源口径不同。",
    }
    payload = _evidence_review().model_dump(mode="json")
    payload["conflicts"] = [
        {**base, "decision": "present_both", "winning_assertion_ids": [str(ASSERTION_ID)]}
    ]
    with pytest.raises(ValidationError, match="must not select winners"):
        VerifierEvidenceReview.model_validate(payload)

    payload["conflicts"] = [{**base, "decision": "adjudicated", "winning_assertion_ids": []}]
    with pytest.raises(ValidationError, match="requires winning_assertion_ids"):
        VerifierEvidenceReview.model_validate(payload)


def test_materialize_conflict_binds_assertion_excerpts() -> None:
    judgement = ConflictJudgement.model_validate(
        {
            "disputed_point": "市场规模口径",
            "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
            "decision": "adjudicated",
            "winning_assertion_ids": [str(ASSERTION_ID)],
            "rationale": "官方披露优先。",
        }
    )
    resolutions = materialize_conflict_resolutions(
        [judgement],
        {ASSERTION_ID: [EXCERPT_A], ASSERTION_B: [EXCERPT_B]},
    )
    assert len(resolutions) == 1
    assert resolutions[0].excerpt_ids == [EXCERPT_A, EXCERPT_B]
    assert resolutions[0].winning_excerpt_ids == [EXCERPT_A]
    assert resolutions[0].decision == "adjudicated"


def test_materialize_conflict_rejects_unknown_assertion() -> None:
    judgement = ConflictJudgement.model_validate(
        {
            "disputed_point": "口径",
            "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
            "decision": "present_both",
            "winning_assertion_ids": [],
            "rationale": "并陈。",
        }
    )
    with pytest.raises(ValueError, match="assertion outside"):
        materialize_conflict_resolutions([judgement], {ASSERTION_ID: [EXCERPT_A]})


def test_materialize_conflict_rejects_fewer_than_two_excerpts() -> None:
    judgement = ConflictJudgement.model_validate(
        {
            "disputed_point": "口径",
            "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
            "decision": "present_both",
            "winning_assertion_ids": [],
            "rationale": "并陈。",
        }
    )
    with pytest.raises(ValueError, match="two distinct excerpts"):
        materialize_conflict_resolutions(
            [judgement],
            {ASSERTION_ID: [EXCERPT_A], ASSERTION_B: [EXCERPT_A]},
        )


def test_reference_validation_rejects_ids_outside_job() -> None:
    decision = _decision("needs_research", severity="major")
    with pytest.raises(ValueError, match="task outside"):
        validate_verifier_references(
            decision,
            task_ids=set(),
            assertion_ids={ASSERTION_ID},
            excerpt_ids={EXCERPT_A},
        )


def test_minor_credibility_gap_may_disclose_without_discarding_evidence() -> None:
    """A weak source worth disclosing, with the finding intact, must stay a legal judgement.

    Requiring disposals at every severity made the most ordinary credibility call illegal,
    and killed whole runs at the final gate over bookkeeping.
    """
    decision = _decision("pass", severity="minor", kind="source_credibility")

    assert decision.release_decision == "pass"
    assert decision.assertion_dispositions == []
    assert decision.gaps[0].related_assertion_ids == [ASSERTION_ID]


def test_major_credibility_gap_derives_unusable_dispositions() -> None:
    """Code supplies the mechanical consequence the model may have left out."""
    evidence_review = _evidence_review(dispositions=[])
    decision = materialize_verifier_decision(
        evidence_review,
        _coverage_decision("needs_research", severity="major", kind="source_credibility"),
        {},
    )

    assert evidence_review.assertion_dispositions == []
    assert [item.assertion_id for item in decision.assertion_dispositions] == [ASSERTION_ID]
    assert decision.assertion_dispositions[0].status == "unusable"


def test_major_credibility_gap_overrides_a_contradicting_restore() -> None:
    decision = materialize_verifier_decision(
        _evidence_review(
            dispositions=[
                {
                    "assertion_id": str(ASSERTION_ID),
                    "status": "restored",
                    "reason": "上一轮误判。",
                }
            ],
        ),
        _coverage_decision("needs_research", severity="major", kind="source_credibility"),
        {},
    )

    assert len(decision.assertion_dispositions) == 1
    assert decision.assertion_dispositions[0].status == "unusable"
    assert "上一轮误判。" in decision.assertion_dispositions[0].reason


def test_persisted_decision_still_asserts_major_credibility_invariant() -> None:
    """The strict rule survives where it is now guaranteed rather than gambled on."""
    payload = _decision("pass").model_dump(mode="json")
    payload["release_decision"] = "needs_research"
    payload["gaps"] = _gaps(severity="major", kind="source_credibility")
    with pytest.raises(ValidationError, match="must be marked unusable"):
        VerifierDecision.model_validate(payload)


def test_source_credibility_gap_rejects_excerpt_only_pointers() -> None:
    payload = _coverage_decision("needs_research", severity="major").model_dump(mode="json")
    payload["gaps"] = [
        {
            **_gaps(severity="major", kind="source_credibility")[0],
            "related_assertion_ids": [],
        }
    ]
    with pytest.raises(ValidationError, match="related_assertion_ids"):
        VerifierCoverageDecision.model_validate(payload)


def test_pass_may_include_unusable_dispositions_without_major_gap() -> None:
    decision = _decision(
        "pass",
        dispositions=[_unusable_disposition()],
    )
    assert decision.release_decision == "pass"
    assert decision.assertion_dispositions[0].status == "unusable"


def test_duplicate_disposition_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate assertion disposition"):
        _evidence_review(dispositions=[_unusable_disposition(), _unusable_disposition()])


def test_effective_unusable_last_write_wins() -> None:
    unusable = AssertionDisposition(
        assertion_id=ASSERTION_ID,
        status="unusable",
        reason="伪学术",
    )
    restored = AssertionDisposition(
        assertion_id=ASSERTION_ID,
        status="restored",
        reason="误判纠正",
    )
    assert effective_unusable_assertion_ids([(1, [unusable])]) == {ASSERTION_ID}
    assert effective_unusable_assertion_ids([(1, [unusable]), (2, [restored])]) == set()


def test_reference_validation_rejects_unknown_disposition_assertion() -> None:
    decision = VerifierDecision.model_validate(
        {
            **_decision("pass", dispositions=[]).model_dump(mode="json"),
            "gaps": [],
            "assertion_dispositions": [_unusable_disposition()],
        }
    )
    with pytest.raises(ValueError, match="Assertion disposition references"):
        validate_verifier_references(
            decision,
            task_ids={TASK_ID},
            assertion_ids=set(),
            excerpt_ids={EXCERPT_A},
        )


def test_conflict_key_is_order_independent() -> None:
    assert conflict_key([EXCERPT_A, EXCERPT_B]) == conflict_key([EXCERPT_B, EXCERPT_A])


def test_prompt_uses_source_metadata_without_tier_field_or_full_document() -> None:
    snapshot = {
        "brief": {"question": "问题"},
        "plans": [],
        "tasks": [],
        "planner_exit": {"trigger": "planner_finish"},
        "effective_unusable_assertion_ids": [],
        "assertions": [],
        "excerpts": [
            {
                "excerpt_id": str(EXCERPT_A),
                "url": "https://example.com/source",
                "title": "标题",
                "author": "作者",
                "published_at": "2026-07-01",
                "text": "原文片段",
            }
        ],
    }
    messages = research_verifier_messages(snapshot)
    prompt = "\n".join(message["content"] for message in messages)
    schema = json.dumps(VerifierEvidenceReviewRefs.model_json_schema(), ensure_ascii=False)

    assert all(field in prompt for field in ("url", "title", "author"))
    assert '"publisher"' not in prompt
    assert '"tier"' not in prompt
    assert "来源元数据" not in prompt or "来源" in prompt
    assert "不要判断 pass 或 needs_research" in prompt
    assert "document_text" not in prompt
    assert "worker_trace" not in prompt
    assert "conflicts" in prompt
    assert "冲突" in prompt
    assert "assertion_dispositions" in prompt
    assert "effective_unusable_assertion_refs" in prompt
    assert "restored" in prompt
    assert "decision" not in VerifierEvidenceReviewRefs.model_json_schema()["properties"]
    assert '"conflict_resolutions"' not in schema
    conflict_def = (
        VerifierEvidenceReviewRefs.model_json_schema()
        .get("$defs", {})
        .get("ConflictJudgementRefs", {})
    )
    conflict_props = conflict_def.get("properties", {})
    assert "assertion_refs" in conflict_props
    assert "winning_assertion_refs" in conflict_props
    assert "excerpt_ids" not in conflict_props
    assert "winning_excerpt_ids" not in conflict_props
    disposition_def = (
        VerifierEvidenceReviewRefs.model_json_schema()
        .get("$defs", {})
        .get("AssertionDispositionRef", {})
    )
    disposition_props = disposition_def.get("properties", {})
    assert "assertion_ref" in disposition_props
    assert "excerpt_id" not in disposition_props
    assert "excerpt_ids" not in disposition_props

    coverage_prompt = "\n".join(
        message["content"]
        for message in research_coverage_messages(
            {
                "brief": {"question": "问题"},
                "tasks": [],
                "usable_assertions": [],
                "source_credibility_findings": [],
                "conflicts": [],
            },
            ResearchModelRefs.build(),
        )
    )
    assert "reason 直接说明" in coverage_prompt
    assert "minor gap" in coverage_prompt
    assert "Excerpt" not in json.dumps(
        VerifierCoverageDecisionRefs.model_json_schema(), ensure_ascii=False
    )


class _FakeCompletions:
    def __init__(self, streamed: str | list[str], repaired: str | None = None) -> None:
        self.streamed = [streamed] if isinstance(streamed, str) else list(streamed)
        self.repaired = repaired
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            content = self.streamed[min(len(self.streamed) - 1, self._stream_count() - 1)]
            delta = SimpleNamespace(content=content)
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta)])])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.repaired or ""))]
        )

    def _stream_count(self) -> int:
        return sum(1 for request in self.requests if request.get("stream"))


def test_verifier_model_uses_short_refs_and_runtime_restores_storage_ids() -> None:
    review = json.dumps(
        {
            "source_credibility_findings": [],
            "conflicts": [],
            "assertion_dispositions": [
                {
                    "assertion_ref": "a1",
                    "status": "unusable",
                    "reason": "来源不足以承担该断言的事实强度。",
                }
            ],
        },
        ensure_ascii=False,
    )
    coverage = json.dumps(
        {
            "decision": "needs_research",
            "reason": "核心问题仍缺少可用证据。",
            "answerability_checks": [
                {
                    "requirement": "回答核心问题",
                    "status": "blocked",
                    "answer": "",
                    "supporting_assertion_refs": [],
                    "evidence_bridge": "",
                    "evidence_needed": "补充能够回答核心问题的证据",
                }
            ],
            "gaps": [
                {
                    "kind": "plan_coverage",
                    "severity": "major",
                    "related_task_refs": ["t1"],
                    "related_assertion_refs": [],
                    "description": "核心问题仍然受阻。",
                    "evidence_needed": "补充能够回答核心问题的证据",
                }
            ],
        },
        ensure_ascii=False,
    )
    completions = _FakeCompletions([review, coverage])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = OpenAIResearchVerifier(
        client=cast(Any, client), model="qwen-strong", repair_model="qwen-mid"
    ).verify(_verifier_snapshot())

    prompt = result.full_prompt[1]["content"]
    assert str(TASK_ID) not in prompt
    assert str(ASSERTION_ID) not in prompt
    assert str(EXCERPT_A) not in prompt
    assert '"task_ref": "t1"' in prompt
    assert '"assertion_ref": "a1"' in prompt
    coverage_prompt = cast(
        list[dict[str, str]], cast(dict[str, Any], result.raw_output)["coverage_prompt"]
    )[1]["content"]
    assert str(TASK_ID) not in coverage_prompt
    assert str(ASSERTION_ID) not in coverage_prompt
    assert '"task_ref": "t1"' in coverage_prompt
    assert result.decision.gaps[0].related_task_ids == [TASK_ID]
    assert result.decision.assertion_dispositions[0].assertion_id == ASSERTION_ID


def test_verifier_rejects_an_unknown_short_ref_after_contract_retry() -> None:
    invalid = json.dumps(
        {
            "source_credibility_findings": [],
            "conflicts": [],
            "assertion_dispositions": [
                {
                    "assertion_ref": "a999",
                    "status": "unusable",
                    "reason": "引用不存在。",
                }
            ],
        },
        ensure_ascii=False,
    )
    completions = _FakeCompletions([invalid, invalid])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(VerifierOutputError, match="unknown Assertion refs: a999"):
        OpenAIResearchVerifier(
            client=cast(Any, client), model="qwen-strong", repair_model="qwen-mid"
        ).verify(_verifier_snapshot())


def test_verifier_uses_strong_thinking_and_one_structural_repair() -> None:
    review = json.dumps(_model_payload(_evidence_review()), ensure_ascii=False)
    coverage = json.dumps(_model_payload(_coverage_decision()), ensure_ascii=False)
    completions = _FakeCompletions(["判断如下：" + review, coverage], repaired=review)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIResearchVerifier(
        client=cast(Any, client), model="qwen-strong", repair_model="qwen-mid"
    )

    result = verifier.verify(_verifier_snapshot())

    assert result.decision.release_decision == "pass"
    assert result.decision.conflict_resolutions == []
    first, second, third = completions.requests
    assert first["model"] == "qwen-strong" and first["stream"] is True
    assert first["extra_body"] == {"enable_thinking": True}
    assert second["model"] == "qwen-mid"
    assert second["response_format"] == {"type": "json_object"}
    assert second["extra_body"] == {"enable_thinking": False}
    assert third["model"] == "qwen-strong" and third["stream"] is True
    assert cast(dict[str, Any], result.raw_output)["coverage_prompt"]


def test_contract_violation_goes_back_to_the_verifier_not_the_repair_model() -> None:
    """Judgement errors are re-asked with the snapshot; only syntax goes to the cheap model.

    The repair model never sees the snapshot, so asking it to fix a judgement contract
    would be asking it to re-decide the evidence while looking at none of it.
    """
    snapshot = _verifier_snapshot()
    review = json.dumps(_model_payload(_evidence_review()), ensure_ascii=False)
    illegal = json.dumps(
        _model_payload(
            {
                **_coverage_decision().model_dump(mode="json"),
                "gaps": _gaps(severity="major", kind="plan_coverage"),
            }
        ),
        ensure_ascii=False,
    )
    legal = json.dumps(
        _model_payload(_coverage_decision("needs_research", severity="major")),
        ensure_ascii=False,
    )
    completions = _FakeCompletions([review, illegal, legal])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIResearchVerifier(
        client=cast(Any, client), model="qwen-strong", repair_model="qwen-mid"
    )

    result = verifier.verify(snapshot)

    assert result.decision.release_decision == "needs_research"
    assert [request["model"] for request in completions.requests] == [
        "qwen-strong",
        "qwen-strong",
        "qwen-strong",
    ]
    retry_messages = completions.requests[2]["messages"]
    assert retry_messages[:2][0]["content"].startswith("你是 Research Verifier 的覆盖核验步骤")
    assert retry_messages[-2] == {"role": "assistant", "content": illegal}
    assert "pass must not contain major gaps" in retry_messages[-1]["content"]
    assert "usable Assertion 短 ref" in retry_messages[-1]["content"]
    raw_output = cast(dict[str, Any], result.raw_output)
    assert cast(dict[str, Any], raw_output["coverage"])["retried_content"] == legal


def test_unknown_snapshot_reference_goes_back_to_the_verifier() -> None:
    snapshot = _verifier_snapshot()
    illegal_payload = cast(dict[str, Any], _model_payload(_coverage_decision()))
    illegal_payload["gaps"][0]["related_task_refs"] = ["t99"]
    illegal = json.dumps(illegal_payload, ensure_ascii=False)
    review = json.dumps(_model_payload(_evidence_review()), ensure_ascii=False)
    legal = json.dumps(_model_payload(_coverage_decision()), ensure_ascii=False)
    completions = _FakeCompletions([review, illegal, legal])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIResearchVerifier(
        client=cast(Any, client), model="qwen-strong", repair_model="qwen-mid"
    )

    result = verifier.verify(snapshot)

    assert result.decision.gaps[0].related_task_ids == [TASK_ID]
    assert [request["model"] for request in completions.requests] == [
        "qwen-strong",
        "qwen-strong",
        "qwen-strong",
    ]
    assert "unknown Task refs: t99" in completions.requests[2]["messages"][-1]["content"]


def test_verifier_binds_conflicts_to_excerpt_ids() -> None:
    conflicts = [
        {
            "disputed_point": "市场规模口径",
            "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
            "decision": "present_both",
            "winning_assertion_ids": [],
            "rationale": "口径不同，并陈。",
        }
    ]
    review = json.dumps(_model_payload(_evidence_review(conflicts=conflicts)), ensure_ascii=False)
    coverage = json.dumps(_model_payload(_coverage_decision()), ensure_ascii=False)
    completions = _FakeCompletions([review, coverage])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIResearchVerifier(
        client=cast(Any, client), model="qwen-strong", repair_model="qwen-mid"
    )

    result = verifier.verify(_verifier_snapshot())

    assert len(result.decision.conflict_resolutions) == 1
    resolution = result.decision.conflict_resolutions[0]
    assert resolution.excerpt_ids == [EXCERPT_A, EXCERPT_B]
    assert resolution.winning_excerpt_ids == []
    assert resolution.decision == "present_both"


def test_same_excerpt_conflict_goes_back_to_the_verifier_instead_of_killing_the_job() -> None:
    """Two assertions disagreeing while resting on one excerpt is a repairable slip.

    Binding used to run after the retry block, so a Job that had already finished its
    research died the first time the model filed a same-excerpt contradiction as a source
    conflict. It now returns to the Verifier like any other contract violation.
    """
    snapshot = _verifier_snapshot()
    cast(list[dict[str, object]], snapshot["assertions"])[1]["excerpt_ids"] = [str(EXCERPT_A)]
    conflicts = [
        {
            "disputed_point": "论文发表年份是 2024 还是 2025",
            "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
            "decision": "present_both",
            "winning_assertion_ids": [],
            "rationale": "两条断言的年份不一致。",
        }
    ]
    illegal = json.dumps(_model_payload(_evidence_review(conflicts=conflicts)), ensure_ascii=False)
    legal = json.dumps(
        _model_payload(_evidence_review(dispositions=[_unusable_disposition(ASSERTION_B)])),
        ensure_ascii=False,
    )
    coverage = json.dumps(_model_payload(_coverage_decision()), ensure_ascii=False)
    completions = _FakeCompletions([illegal, legal, coverage])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIResearchVerifier(
        client=cast(Any, client), model="qwen-strong", repair_model="qwen-mid"
    )

    result = verifier.verify(snapshot)

    assert result.decision.conflict_resolutions == []
    assert [item.assertion_id for item in result.decision.assertion_dispositions] == [ASSERTION_B]
    # Judgement errors go to the model that made them, never to the snapshot-blind repair model.
    assert [request["model"] for request in completions.requests] == [
        "qwen-strong",
        "qwen-strong",
        "qwen-strong",
    ]
    retry = completions.requests[1]["messages"][-1]["content"]
    # Named, so the model can tell which of several judgements to drop, and told where to put it.
    assert "论文发表年份是 2024 还是 2025" in retry
    assert "同一 Excerpt" in retry


def test_verifier_raises_when_structural_repair_is_still_invalid() -> None:
    completions = _FakeCompletions("not json", repaired='{"decision":"pass"}')
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIResearchVerifier(
        client=cast(Any, client), model="qwen-strong", repair_model="qwen-mid"
    )

    with pytest.raises(VerifierOutputError, match="repair failed"):
        verifier.verify({})


class _FakeRepository:
    def __init__(self, stored: VerifierDecision | None = None) -> None:
        self.run_id = uuid4()
        self.stored = stored
        self.synthesis_run: object = SimpleNamespace(
            synthesis_run_id=uuid4(),
            decision="needs_research",
            reason="来源分歧阻断核心比较。",
            evidence_needed="同口径的一手统计。",
        )
        self.completed: VerifierDecision | None = None
        self.failed: dict[str, object] | None = None
        self.outcomes: list[dict[str, object]] = []
        self.begin_count = 0
        self.snapshot_kwargs: dict[str, object] = {}

    def get_completed_verifier_run(self, job_id: UUID, plan_version: int, trigger: str) -> object:
        del job_id, plan_version, trigger
        if self.stored is None:
            return None
        return {"run_id": self.run_id, "decision": self.stored}

    def build_verifier_snapshot(self, job_id: UUID, **kwargs: object) -> dict[str, object]:
        del job_id
        self.snapshot_kwargs = kwargs
        return {"brief": {}, "plans": [{"version": 1}], "tasks": [], "excerpts": []}

    def begin_verifier_run(self, job_id: UUID, **kwargs: object) -> UUID:
        del job_id, kwargs
        self.begin_count += 1
        return self.run_id

    def complete_verifier_run(self, job_id: UUID, run_id: UUID, **kwargs: object) -> None:
        del job_id, run_id
        self.completed = cast(VerifierDecision, kwargs["decision"])

    def fail_verifier_run(self, job_id: UUID, run_id: UUID, **kwargs: object) -> None:
        del job_id, run_id
        self.failed = kwargs

    def record_phase_changed(self, job_id: UUID, phase: str, **kwargs: object) -> None:
        del job_id, phase, kwargs

    def set_research_outcome(self, job_id: UUID, **kwargs: object) -> None:
        del job_id
        self.outcomes.append(kwargs)

    def count_excerpts(self, job_id: UUID) -> int:
        del job_id
        return 1

    def get_job_effort(self, job_id: UUID) -> str:
        del job_id
        return "quick"

    def list_unusable_assertion_details(
        self, job_id: UUID, dispositions: object
    ) -> list[dict[str, str]]:
        del job_id
        items = cast(list[Any], dispositions or [])
        return [
            {
                "assertion_id": str(item.assertion_id),
                "statement": "伪学术定量断言",
                "reason": item.reason.splitlines()[0],
            }
            for item in items
            if getattr(item, "status", None) == "unusable"
        ]

    def get_latest_synthesis_run(self, job_id: UUID) -> object:
        del job_id
        return self.synthesis_run


class _FakeVerifier:
    def __init__(self, decision: VerifierDecision) -> None:
        self.decision = decision
        self.calls = 0

    def verify(self, snapshot: dict[str, Any]) -> VerifierModelResult:
        self.calls += 1
        return VerifierModelResult(
            full_prompt=research_verifier_messages(snapshot),
            raw_output={"decision": self.decision.model_dump(mode="json")},
            decision=self.decision,
        )


def _state(
    *, decision_round: int = 3, limit: int = 8, trigger: str = "planner_finish"
) -> dict[str, Any]:
    state = cast(dict[str, Any], initial_research_state(job_id=str(uuid4()), brief_id=str(uuid4())))
    state.update(
        {
            "plan_version": 1,
            "decision_round": decision_round,
            "research_decisions_used": decision_round,
            "decision_round_limit": limit,
            "verifier_trigger": trigger,
            "planner_messages": [],
        }
    )
    return state


def _services(repository: _FakeRepository, verifier: _FakeVerifier) -> ResearchGraphServices:
    return ResearchGraphServices(
        repository=cast(Any, repository),
        planner=cast(Any, object()),
        worker=cast(Any, object()),
        verifier=cast(Any, verifier),
    )


def test_verifier_node_passes_to_composition_pending() -> None:
    repository = _FakeRepository()
    verifier = _FakeVerifier(_decision())

    result = _verifier_node(_services(repository, verifier))(cast(Any, _state()))

    assert result["phase"] == "composition_pending"
    assert result["outcome"] == "ready_for_writer"
    assert result["route"] == "synthesis"
    assert repository.completed is not None
    assert repository.outcomes[-1]["phase"] == "composition_pending"


def _passing_decision_that_names_its_evidence() -> VerifierDecision:
    """A pass whose minor gap says what it could not find -- the shape that used to end research.

    One real Job closed after a single dispatch this way: the Verifier passed, then wrote
    a minor plan_coverage gap whose evidence_needed read as a ready-made research task,
    and that note reached nobody but the report's stated limits.
    """
    coverage = _coverage_decision().model_dump(mode="json")
    coverage["gaps"][0]["evidence_needed"] = (
        "需要在同一样本中同时记录公众自发讨论与平台排序变化的研究"
    )
    return materialize_verifier_decision(
        _evidence_review(),
        VerifierCoverageDecision.model_validate(coverage),
        {},
    )


def _follow_up_state(*, trigger: str = "planner_finish", **overrides: Any) -> dict[str, Any]:
    state = _state(trigger=trigger)
    state.update({"research_decisions_used": 2, "decision_round_limit": 12})
    state.update(overrides)
    return state


def _last_research_state(messages: list[dict[str, Any]]) -> dict[str, Any]:
    for message in reversed(messages):
        payload = json.loads(str(message["content"]))
        if payload.get("runtime_feedback") == "research_state":
            return payload
    raise AssertionError("no research_state feedback in the Planner thread")


def test_a_pass_that_names_its_missing_evidence_returns_one_round_to_the_planner() -> None:
    repository = _FakeRepository()
    verifier = _FakeVerifier(_passing_decision_that_names_its_evidence())

    result = _verifier_node(_services(repository, verifier))(cast(Any, _follow_up_state()))

    assert result["route"] == "planner"
    assert result["phase"] == "research"
    assert result["follow_up_research_used"] is True
    assert result["finish_withheld"] is True
    # Research is not closed on the way back, or the Job would be composing and
    # researching at once.
    assert repository.outcomes == []

    gap_feedback = json.loads(str(result["planner_messages"][-2]["content"]))
    assert gap_feedback["gap_origin"] == "verifier_follow_up"
    assert gap_feedback["follow_up_gaps"][0]["evidence_needed"]
    assert _last_research_state(result["planner_messages"])["available_decisions"] == ["dispatch"]


def test_the_follow_up_round_is_offered_once_per_job() -> None:
    repository = _FakeRepository()
    verifier = _FakeVerifier(_passing_decision_that_names_its_evidence())
    state = _follow_up_state(follow_up_research_used=True)

    result = _verifier_node(_services(repository, verifier))(cast(Any, state))

    assert result["route"] == "synthesis"
    assert result["outcome"] == "ready_for_writer"


def test_a_gap_the_verifier_cannot_say_how_to_close_stays_a_disclosure() -> None:
    repository = _FakeRepository()
    verifier = _FakeVerifier(_decision())

    result = _verifier_node(_services(repository, verifier))(cast(Any, _follow_up_state()))

    assert result["route"] == "synthesis"


def test_the_follow_up_round_is_not_bought_late_in_the_research_budget() -> None:
    repository = _FakeRepository()
    verifier = _FakeVerifier(_passing_decision_that_names_its_evidence())
    state = _follow_up_state(research_decisions_used=5, decision_round_limit=12)

    result = _verifier_node(_services(repository, verifier))(cast(Any, state))

    assert result["route"] == "synthesis"


def test_a_declined_synthesis_request_never_reopens_research() -> None:
    """Research is already closed there; reopening it would strand a written synthesis."""

    repository = _FakeRepository()
    verifier = _FakeVerifier(_passing_decision_that_names_its_evidence())
    state = _follow_up_state(trigger="synthesis_gap")

    result = _verifier_node(_services(repository, verifier))(cast(Any, state))

    assert result["route"] == "writer"


def test_finish_is_refused_by_the_gate_not_only_by_the_runtime_state() -> None:
    """available_decisions only advises the model, and this round exists because it had
    already argued itself into finishing."""

    assert finish_rejection(3) is None
    assert finish_rejection(3, finish_withheld=True) is PlannerRejection.FINISH_WITHHELD
    assert finish_rejection(0, finish_withheld=True) is PlannerRejection.EMPTY_FINISH


def test_verifier_node_keeps_the_raw_answer_and_stops_the_job_when_output_is_rejected() -> None:
    """The rejected answer is the whole diagnosis; losing it is how a run becomes unexplainable."""

    class _RejectingVerifier:
        def verify(self, snapshot: dict[str, Any]) -> VerifierModelResult:
            del snapshot
            raise VerifierOutputError("invalid Verifier decision: boom", {"content": "…"})

    repository = _FakeRepository()

    with pytest.raises(VerifierOutputError):
        _verifier_node(_services(repository, cast(Any, _RejectingVerifier())))(cast(Any, _state()))

    assert repository.failed is not None
    assert repository.failed["raw_output"] == {"content": "…"}
    assert repository.outcomes[-1] == {
        "outcome": "failed",
        "error_code": "verifier_output_invalid",
        "phase": "failed",
    }


def test_verifier_node_logs_decision_reason_after_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _FakeRepository()
    decision = _decision()
    calls: list[tuple[str, dict[str, object]]] = []

    def capture(event: str, **fields: object) -> None:
        assert repository.completed == decision
        calls.append((event, fields))

    monkeypatch.setattr(
        "prospector.flow.research_graph.log",
        SimpleNamespace(debug=capture, info=lambda *_a, **_k: None),
    )

    _verifier_node(_services(repository, _FakeVerifier(decision)))(cast(Any, _state()))

    assert calls[0][0] == "verifier.completed"
    assert calls[0][1]["message"] == decision.decision_reason
    assert calls[0][1]["outcome"] == "pass"
    assert calls[0][1]["reason_code"] == "pass"


@pytest.mark.parametrize("decision_round,used", [(3, 3), (8, 6)])
@pytest.mark.parametrize("replayed", [False, True])
def test_verifier_node_replans_when_major_gap_has_rounds_left(
    decision_round: int, used: int, replayed: bool
) -> None:
    decision = _decision(
        "needs_research",
        severity="major",
        kind="source_credibility",
        dispositions=[_unusable_disposition()],
    )
    repository = _FakeRepository(stored=decision if replayed else None)
    verifier = _FakeVerifier(decision)
    state = _state(decision_round=decision_round)
    state["research_decisions_used"] = used

    result = _verifier_node(_services(repository, verifier))(cast(Any, state))

    assert result["route"] == "planner"
    assert result["last_verifier_run_id"] == str(repository.run_id)
    content = result["planner_messages"][0]["content"]
    assert '"runtime_feedback": "verifier_gap"' in content
    assert "unusable_assertions" in content
    assert str(ASSERTION_ID) in content
    assert repository.outcomes == []
    assert verifier.calls == (0 if replayed else 1)


@pytest.mark.parametrize("decision_round", [8, 10])
def test_verifier_node_fails_when_major_gap_has_no_rounds_left(decision_round: int) -> None:
    repository = _FakeRepository()
    verifier = _FakeVerifier(_decision("needs_research", severity="major"))
    state = _state(decision_round=decision_round, limit=8)
    state["research_decisions_used"] = 8

    with pytest.raises(VerifierMajorGapError, match="关键比较口径"):
        _verifier_node(_services(repository, verifier))(cast(Any, state))

    assert repository.outcomes[-1] == {
        "outcome": "failed",
        "error_code": "verifier_major_gap",
        "phase": "failed",
    }


def test_verifier_node_reuses_completed_run_without_model_call() -> None:
    stored = _decision()
    repository = _FakeRepository(stored=stored)
    verifier = _FakeVerifier(stored)

    result = _verifier_node(_services(repository, verifier))(cast(Any, _state()))

    assert result["phase"] == "composition_pending"
    assert verifier.calls == 0
    assert repository.begin_count == 0


def test_timeline_renders_verifier_and_replan_events() -> None:
    renderer = ResearchTimelineRenderer(cast(Any, object()), limits_for_effort("quick"))

    assert renderer.render(
        {
            "event_type": "job.phase_changed",
            "payload": {
                "phase": "verifier",
                "plan_version": 1,
                "trigger": "planner_finish",
            },
        }
    ) == ["[研究] 研究阶段结束，等待核验（Plan v1，触发：Planner finish）"]

    assert renderer.render(
        {
            "event_type": "verifier.completed",
            "decision_round": 3,
            "payload": {
                "plan_version": 1,
                # The research budget is spent by decisions, not by storage rounds, so the
                # runtime reports it explicitly rather than letting the renderer infer it.
                "research_decisions_used": 3,
                "release_decision": "needs_research",
                "decision_reason": "关键比较口径仍缺证据",
                "major_gap_count": 2,
                "minor_gap_count": 0,
                "conflict_resolution_count": 0,
                "gap_summaries": [
                    {
                        "severity": "major",
                        "kind": "plan_coverage",
                        "description": "关键比较口径尚未核实",
                        "evidence_needed": "补充独立来源并统一口径",
                    },
                    {
                        "severity": "major",
                        "kind": "brief_alignment",
                        "description": "Brief 要求的反例仍缺",
                        "evidence_needed": "补充能够回答核心问题的反例证据",
                    },
                ],
            },
        }
    ) == [
        "[核验] Plan v1 不通过：2 个重大缺口（次要 0，冲突 0，废证 0）",
        "  ├─ 重大·覆盖：关键比较口径尚未核实",
        "  │     待补证据：补充独立来源并统一口径",
        "  └─ 重大·Brief对齐：Brief 要求的反例仍缺",
        "        待补证据：补充能够回答核心问题的反例证据",
        "[核验] 返回 Planner 补查（余 5 轮）：关键比较口径仍缺证据",
    ]
    assert renderer.render(
        {
            "event_type": "replan.triggered",
            "payload": {"verifier_run_id": "12345678-abcd", "plan_version": 2},
        }
    ) == ["[重规划] Verifier 12345678 触发 Plan v2"]
    assert renderer.render(
        {
            "event_type": "verifier.completed",
            "decision_round": 4,
            "payload": {
                "plan_version": 2,
                "release_decision": "pass",
                "decision_reason": "Plan 承诺已有充分证据",
                "major_gap_count": 0,
                "minor_gap_count": 0,
                "conflict_resolution_count": 0,
                "unusable_assertion_count": 1,
                "unusable_summaries": [
                    {
                        "assertion_id": "20000000-0000-0000-0000-000000000001",
                        "reason": "伪学术来源不可采信",
                    }
                ],
            },
        }
    ) == [
        "[核验] Plan v2 通过（重大缺口 0，冲突裁决 0，废证 1）",
        "[核验] 收工：Plan 承诺已有充分证据",
        "[核验] 废证 1 条：",
        "  └─ 20000000：伪学术来源不可采信",
    ]
    assert renderer.render(
        {
            "event_type": "job.phase_changed",
            "payload": {
                "phase": "composition_pending",
                "outcome": "ready_for_writer",
                "error_code": None,
            },
        }
    ) == ["[综合] Research Verifier 已放行，等待 Research Synthesis"]
    assert renderer.render(
        {
            "event_type": "job.phase_changed",
            "payload": {"phase": "verifying"},
        }
    ) == ["[核验] Report Verifier 正在逐句验证"]


def test_timeline_renders_completed_research_synthesis() -> None:
    renderer = ResearchTimelineRenderer(cast(Any, object()), limits_for_effort("quick"))

    assert renderer.render(
        {
            "event_type": "synthesis.completed",
            "payload": {
                "synthesis": "材料显示需求下降并非短期波动，而是供给约束与价格变化共同作用的结果。"
            },
        }
    ) == [
        "[综合] Research Synthesis：",
        "材料显示需求下降并非短期波动，而是供给约束与价格变化共同作用的结果。",
    ]


def test_timeline_distinguishes_total_unusable_assertions_from_displayed_summaries() -> None:
    renderer = ResearchTimelineRenderer(cast(Any, object()), limits_for_effort("quick"))
    summaries = [
        {
            "assertion_id": f"{index:08d}-0000-0000-0000-000000000000",
            "reason": f"废证原因 {index}",
        }
        for index in range(8)
    ]

    lines = renderer.render(
        {
            "event_type": "verifier.completed",
            "payload": {
                "plan_version": 1,
                "release_decision": "pass",
                "major_gap_count": 0,
                "minor_gap_count": 1,
                "conflict_resolution_count": 3,
                "unusable_assertion_count": 28,
                "unusable_summaries": summaries,
            },
        }
    )

    assert lines[0] == "[核验] Plan v1 通过（重大缺口 0，冲突裁决 3，废证 28）"
    assert lines[1] == "[核验] 废证 28 条（以下展示 8 条）："
    assert len(lines[2:]) == 8


def test_a_compound_assertion_is_noted_not_destroyed() -> None:
    """122 of 187 disqualifications were merged facts the bound Excerpt fully supported."""
    prompt = "\n".join(
        message["content"]
        for message in research_verifier_messages({"brief": {"question": "q", "brief_text": "b"}})
    )

    assert "多个可分别成立的事实合并成一条" in prompt
    # The packaging verdict and the truth verdict must be told apart in the instructions.
    assert "granularity" in prompt
    assert "仍然可用" in prompt
    assert "不得仅因为句子长而标为 unusable" in prompt

    coverage_prompt = "\n".join(
        message["content"]
        for message in research_coverage_messages(
            {
                "brief": {"question": "q", "brief_text": "b"},
                "tasks": [],
                "usable_assertions": [],
                "source_credibility_findings": [],
                "conflicts": [],
            },
            ResearchModelRefs.build(),
        )
    )
    assert "task 未完全达到 expected_evidence" in coverage_prompt
    assert "阻断\nBrief 核心回答" in coverage_prompt


def test_the_timeline_separates_destroyed_material_from_merely_packed_material() -> None:
    """A reader must be able to see the evidence pool did not shrink."""
    renderer = ResearchTimelineRenderer(cast(Any, object()), limits_for_effort("quick"))
    lines = renderer.render(
        {
            "event_type": "verifier.completed",
            "payload": {
                "plan_version": 1,
                "release_decision": "pass",
                "decision_reason": "放行。",
                "major_gap_count": 0,
                "minor_gap_count": 0,
                "conflict_resolution_count": 2,
                "unusable_assertion_count": 4,
                "granularity_assertion_count": 47,
            },
        }
    )
    assert any("废证 4，粒度备注 47" in line for line in lines)


def test_a_granularity_note_leaves_the_assertion_usable() -> None:
    packed = uuid4()
    fabricated = uuid4()
    unusable = effective_unusable_assertion_ids(
        [
            (
                1,
                [
                    AssertionDisposition(
                        assertion_id=packed,
                        status="granularity",
                        reason="合并了两项可分别核对的事实。",
                    ),
                    AssertionDisposition(
                        assertion_id=fabricated,
                        status="unusable",
                        reason="绑定摘录未给出该日期。",
                    ),
                ],
            )
        ]
    )
    assert unusable == {fabricated}


def test_declined_synthesis_gap_goes_straight_to_the_writer() -> None:
    """A gap the Verifier will not confirm must not restart research.

    The report is written from the limited analysis, and the Verifier's reason travels
    with it as a minor gap of this same run.
    """
    repository = _FakeRepository()

    result = _verifier_node(_services(repository, _FakeVerifier(_decision())))(
        cast(Any, _state(trigger="synthesis_gap"))
    )

    assert result["route"] == "writer"
    assert result["last_verifier_run_id"] == str(repository.run_id)


def test_confirmed_synthesis_gap_returns_to_the_planner_marked_as_such() -> None:
    repository = _FakeRepository()
    verifier = _FakeVerifier(_decision("needs_research", severity="major"))

    result = _verifier_node(_services(repository, verifier))(
        cast(Any, _state(trigger="synthesis_gap"))
    )

    assert result["route"] == "planner"
    content = result["planner_messages"][0]["content"]
    assert '"gap_origin": "research_synthesis"' in content
    request = cast(Any, repository.snapshot_kwargs["synthesis_request"])
    assert request["evidence_needed"] == "同口径的一手统计。"
    assert repository.snapshot_kwargs["trigger"] == "synthesis_gap"


def test_synthesis_gap_verification_refuses_a_missing_evidence_request() -> None:
    repository = _FakeRepository()
    repository.synthesis_run = None

    with pytest.raises(RuntimeError, match="evidence request"):
        _verifier_node(_services(repository, _FakeVerifier(_decision())))(
            cast(Any, _state(trigger="synthesis_gap"))
        )
