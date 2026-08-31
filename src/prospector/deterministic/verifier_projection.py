"""Qualified evidence projection used by the Research Verifier coverage pass."""

from __future__ import annotations

from typing import Any

from prospector.deterministic.model_refs import ResearchModelRefs
from prospector.schemas.verifier import (
    AssertionDisposition,
    ConflictJudgement,
    CoreAnswerabilityCheck,
    SourceCredibilityFinding,
    VerifierCoverageDecision,
    VerifierCoverageDecisionRefs,
    VerifierEvidenceReview,
    VerifierEvidenceReviewRefs,
    VerifierGap,
)


def resolve_evidence_review_refs(
    review: VerifierEvidenceReviewRefs,
    refs: ResearchModelRefs,
) -> VerifierEvidenceReview:
    """Resolve one qualification decision from local refs to storage UUIDs."""
    return VerifierEvidenceReview(
        source_credibility_findings=[
            SourceCredibilityFinding(
                related_assertion_ids=refs.assertions(item.related_assertion_refs),
                description=item.description,
            )
            for item in review.source_credibility_findings
        ],
        conflicts=[
            ConflictJudgement(
                disputed_point=item.disputed_point,
                assertion_ids=refs.assertions(item.assertion_refs),
                decision=item.decision,
                winning_assertion_ids=refs.assertions(item.winning_assertion_refs),
                rationale=item.rationale,
            )
            for item in review.conflicts
        ],
        assertion_dispositions=[
            AssertionDisposition(
                assertion_id=refs.assertions([item.assertion_ref])[0],
                status=item.status,
                reason=item.reason,
            )
            for item in review.assertion_dispositions
        ],
    )


def resolve_coverage_decision_refs(
    decision: VerifierCoverageDecisionRefs,
    refs: ResearchModelRefs,
) -> VerifierCoverageDecision:
    """Resolve one coverage decision from the shared local namespace."""
    return VerifierCoverageDecision(
        decision=decision.decision,
        reason=decision.reason,
        answerability_checks=[
            CoreAnswerabilityCheck(
                requirement=item.requirement,
                status=item.status,
                answer=item.answer,
                supporting_assertion_ids=refs.assertions(item.supporting_assertion_refs),
                evidence_bridge=item.evidence_bridge,
                evidence_needed=item.evidence_needed,
            )
            for item in decision.answerability_checks
        ],
        gaps=[
            VerifierGap(
                kind=item.kind,
                severity=item.severity,
                related_task_ids=refs.tasks(item.related_task_refs),
                related_assertion_ids=refs.assertions(item.related_assertion_refs),
                description=item.description,
                evidence_needed=item.evidence_needed,
            )
            for item in decision.gaps
        ],
    )


def build_verifier_coverage_snapshot(
    snapshot: dict[str, Any],
    evidence_review: VerifierEvidenceReview,
) -> dict[str, Any]:
    """Project only currently usable Assertions after the qualification pass.

    The coverage model does not need Excerpt text: qualification already decided fidelity,
    source fitness and conflicts against that text. It receives source identity and caveats
    so it can judge whether the remaining pool can carry the Brief's actual answer.
    """

    unusable = {str(value) for value in snapshot.get("effective_unusable_assertion_ids") or []}
    for disposition in evidence_review.assertion_dispositions:
        assertion_id = str(disposition.assertion_id)
        if disposition.status == "unusable":
            unusable.add(assertion_id)
        elif disposition.status == "restored":
            unusable.discard(assertion_id)

    excerpts = {
        str(row["excerpt_id"]): row
        for row in snapshot.get("excerpts") or []
        if isinstance(row, dict) and row.get("excerpt_id") is not None
    }
    usable_assertions: list[dict[str, Any]] = []
    usable_ids: set[str] = set()
    for row in snapshot.get("assertions") or []:
        if not isinstance(row, dict) or row.get("assertion_id") is None:
            continue
        assertion_id = str(row["assertion_id"])
        if assertion_id in unusable:
            continue
        sources: list[dict[str, Any]] = []
        for excerpt_id_value in row.get("excerpt_ids") or []:
            excerpt_id = str(excerpt_id_value)
            excerpt = excerpts.get(excerpt_id)
            if excerpt is None:
                continue
            sources.append(
                {
                    "excerpt_id": excerpt_id,
                    "url": excerpt.get("url"),
                    "title": excerpt.get("title"),
                    "author": excerpt.get("author"),
                    "published_at": excerpt.get("published_at"),
                }
            )
        usable_ids.add(assertion_id)
        usable_assertions.append(
            {
                "assertion_id": assertion_id,
                "task_id": str(row["task_id"]),
                "statement": row.get("statement"),
                "sources": sources,
            }
        )

    source_findings: list[dict[str, Any]] = []
    for finding in evidence_review.source_credibility_findings:
        related = [
            str(assertion_id)
            for assertion_id in finding.related_assertion_ids
            if str(assertion_id) in usable_ids
        ]
        if related:
            source_findings.append(
                {
                    "related_assertion_ids": related,
                    "description": finding.description,
                }
            )

    result: dict[str, Any] = {
        "brief": snapshot.get("brief") or {},
        "plans": snapshot.get("plans") or [],
        "tasks": snapshot.get("tasks") or [],
        "planner_exit": snapshot.get("planner_exit") or {},
        "usable_assertions": usable_assertions,
        "source_credibility_findings": source_findings,
        "conflicts": [item.model_dump(mode="json") for item in evidence_review.conflicts],
    }
    if snapshot.get("synthesis_evidence_request") is not None:
        result["synthesis_evidence_request"] = snapshot["synthesis_evidence_request"]
    return result
