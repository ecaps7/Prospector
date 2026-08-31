"""Contracts that keep research completion tied to answerable evidence."""

from __future__ import annotations

from uuid import UUID

from prospector.agents.prompts.planner import planner_system_prompt
from prospector.agents.prompts.research_verifier import (
    research_coverage_messages,
    research_verifier_messages,
)
from prospector.deterministic.model_refs import ResearchModelRefs
from prospector.deterministic.verifier_projection import build_verifier_coverage_snapshot
from prospector.schemas.claims import AttributionBatchVerification
from prospector.schemas.report import ResearchSynthesisModelResult, ResearchSynthesisModelReview
from prospector.schemas.verifier import (
    AssertionDisposition,
    VerifierCoverageDecisionRefs,
    VerifierEvidenceReview,
    VerifierEvidenceReviewRefs,
)

TASK_ID = UUID("10000000-0000-0000-0000-000000000001")
ASSERTION_A = UUID("20000000-0000-0000-0000-000000000001")
ASSERTION_B = UUID("20000000-0000-0000-0000-000000000002")
EXCERPT_A = UUID("30000000-0000-0000-0000-000000000001")
EXCERPT_B = UUID("30000000-0000-0000-0000-000000000002")


def _contains_uuid_format(value: object) -> bool:
    if isinstance(value, dict):
        return value.get("format") == "uuid" or any(
            _contains_uuid_format(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_uuid_format(item) for item in value)
    return False


def _snapshot() -> dict[str, object]:
    return {
        "brief": {
            "question": "机制 A 还是机制 B 主导结果？二者如何互动？",
            "brief_text": "可探索多个平台和案例，但不预设结论。",
            "user_constraints": {},
        },
        "plans": [{"version": 1, "task_ids": [str(TASK_ID)]}],
        "tasks": [
            {
                "task_id": str(TASK_ID),
                "question": "比较 A 与 B 对同一结果的作用",
                "expected_evidence": "能够判断两种机制关系的证据",
                "status": "done",
                "stop_reason": "expected_evidence_satisfied",
                "finish_reason": "已达到数量要求",
            }
        ],
        "planner_exit": {"trigger": "planner_finish", "finish_reason": "材料很多"},
        "assertions": [
            {
                "assertion_id": str(ASSERTION_A),
                "task_id": str(TASK_ID),
                "statement": "A 改变了结果。",
                "excerpt_ids": [str(EXCERPT_A)],
                "topic_tags": [],
            },
            {
                "assertion_id": str(ASSERTION_B),
                "task_id": str(TASK_ID),
                "statement": "B 改变了另一个结果。",
                "excerpt_ids": [str(EXCERPT_B)],
                "topic_tags": [],
            },
        ],
        "excerpts": [
            {
                "excerpt_id": str(EXCERPT_A),
                "text": "A 原文",
                "url": "https://example.com/a",
                "title": "A",
                "author": "作者 A",
                "published_at": "2026-01-01",
            },
            {
                "excerpt_id": str(EXCERPT_B),
                "text": "B 原文",
                "url": "https://example.com/b",
                "title": "B",
                "author": "作者 B",
                "published_at": "2026-01-02",
            },
        ],
        "prior_assertion_dispositions": [],
        "prior_conflict_resolutions": [],
        "effective_unusable_assertion_ids": [],
    }


def test_planner_contract_ties_task_completion_to_the_question_not_material_counts() -> None:
    prompt = planner_system_prompt(today="2026-08-30")

    assert "数量、平台数、案例数或机制类别" in prompt
    assert "不能单独构成完成条件" in prompt
    assert "局部 ResearchTask 已收工" in prompt
    assert "不同对象或不同结果指标" in prompt
    assert "不规定必须采用哪一种研究方法" in prompt


def test_research_model_output_contracts_never_ask_the_model_to_copy_a_uuid() -> None:
    model_outputs = (
        VerifierEvidenceReviewRefs,
        VerifierCoverageDecisionRefs,
        ResearchSynthesisModelResult,
        ResearchSynthesisModelReview,
        AttributionBatchVerification,
    )

    assert all(not _contains_uuid_format(model.model_json_schema()) for model in model_outputs)


def test_qualification_and_coverage_are_separate_prompts() -> None:
    snapshot = _snapshot()
    qualification = "\n".join(
        message["content"] for message in research_verifier_messages(snapshot)
    )

    assert "不要判断 pass 或 needs_research" in qualification
    assert "Assertion 是否忠实表达绑定 Excerpt" in qualification
    assert "source_credibility_findings" in qualification

    review = VerifierEvidenceReview(
        assertion_dispositions=[
            AssertionDisposition(
                assertion_id=ASSERTION_B,
                status="unusable",
                reason="绑定原文不支持该结果。",
            )
        ]
    )
    coverage_snapshot = build_verifier_coverage_snapshot(snapshot, review)
    refs = ResearchModelRefs.from_verifier_snapshot(snapshot)
    coverage = "\n".join(
        message["content"] for message in research_coverage_messages(coverage_snapshot, refs)
    )

    assert '"assertion_ref": "a1"' in coverage
    assert '"assertion_ref": "a2"' not in coverage
    assert str(ASSERTION_A) not in coverage
    assert "分别证明多个机制存在" in coverage
    assert "answerability_checks" in coverage
    assert "模型自行把彼此独立的机制材料调和" in coverage
    assert "缺少材料" in coverage and "不可识别" in coverage
    assert "不规定 Planner 应采用" in coverage


def test_coverage_projection_applies_current_dispositions_before_answerability() -> None:
    snapshot = _snapshot()
    snapshot["effective_unusable_assertion_ids"] = [str(ASSERTION_A)]
    review = VerifierEvidenceReview(
        assertion_dispositions=[
            AssertionDisposition(
                assertion_id=ASSERTION_A,
                status="restored",
                reason="新快照足以推翻旧判断。",
            ),
            AssertionDisposition(
                assertion_id=ASSERTION_B,
                status="unusable",
                reason="绑定原文不支持该结果。",
            ),
        ]
    )

    projected = build_verifier_coverage_snapshot(snapshot, review)

    assert [row["assertion_id"] for row in projected["usable_assertions"]] == [str(ASSERTION_A)]
    source = projected["usable_assertions"][0]["sources"][0]
    assert source == {
        "excerpt_id": str(EXCERPT_A),
        "url": "https://example.com/a",
        "title": "A",
        "author": "作者 A",
        "published_at": "2026-01-01",
    }
    assert "text" not in source
