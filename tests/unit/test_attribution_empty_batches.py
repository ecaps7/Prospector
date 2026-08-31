from __future__ import annotations

from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from prospector.agents.report_attribution import (
    AttributionPersistence,
    ClaimAttributionModel,
    ClaimAttributionOutputError,
    plan_incremental_attribution,
    run_attribution,
)
from prospector.deterministic.markdown_report import (
    MARKER_LEXICON_VERSION,
    apply_block_replacements,
    parse_markdown,
)
from prospector.schemas.brief import ResearchBrief
from prospector.schemas.claims import (
    AttributionDisposition,
    AttributionFinding,
    AttributionRun,
    AttributionSummary,
    ClaimEvidence,
    ClaimPremise,
    ClaimSpan,
    ReportReviewRun,
    final_report_status,
)
from prospector.schemas.report import (
    BlockReplacement,
    ReportRevisionPatch,
    WriterEvidenceCard,
    WriterExcerptRef,
    WriterSnapshot,
    WriterSource,
)


@pytest.mark.parametrize(
    "change,complete_summary",
    [
        ("empty", True),
        ("delete_heading", True),
        ("delete_failure", True),
        ("delete_failure", False),
    ],
    ids=["empty-patch", "deletion-only", "core-failure-removed", "missing-core-disposition"],
)
def test_zero_batches_preserve_results_and_still_account_for_core_failures(
    change: str, complete_summary: bool
) -> None:
    markdown = "# Revenue\n\nRevenue fell by 12%.\n\nRevenue will double next year.\n"
    blocks = parse_markdown(markdown)
    snapshot = WriterSnapshot(
        job_id=uuid4(),
        brief=ResearchBrief(question="How did revenue change?", brief_text="Use reported figures."),
        evidence_cards=[
            WriterEvidenceCard(
                assertion_id=uuid4(),
                task_id=uuid4(),
                assertion_statement=blocks[1].text,
                excerpts=[
                    WriterExcerptRef(
                        excerpt_id=uuid4(),
                        text=blocks[1].text,
                        source=WriterSource(
                            source_uri="https://source.test/report", document_version=1
                        ),
                    )
                ],
            )
        ],
    )
    claims = [
        ClaimSpan(
            claim_id=uuid4(),
            block_id=block.block_id,
            start_offset=0,
            end_offset=len(block.text),
            text_hash=block.text_hash,
            text=block.text,
        )
        for block in blocks[1:]
    ]
    previous = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=uuid4(),
        revision=1,
        claims=claims,
        claim_evidence=[
            ClaimEvidence(
                claim_id=claims[0].claim_id,
                excerpt_id=snapshot.evidence_cards[0].excerpts[0].excerpt_id,
            )
        ],
        claim_premises=[
            ClaimPremise(
                claim_id=claims[0].claim_id,
                direct_assertion_ids=[snapshot.evidence_cards[0].assertion_id],
            )
        ],
        blocking_findings=[
            AttributionFinding(
                kind="attribution",
                claim_id=claims[1].claim_id,
                block_id=claims[1].block_id,
                text=claims[1].text,
                reason="The source has no forecast for next year.",
            )
        ],
        audit_notes=[{"block_id": blocks[1].block_id, "note": "Annual comparison only."}],
        marker_lexicon_version=MARKER_LEXICON_VERSION,
    )
    removes_failure = change == "delete_failure"
    previous_review = ReportReviewRun(
        review_run_id=uuid4(),
        report_id=previous.report_id,
        revision=1,
        synthesis_run_id=uuid4(),
        key_block_ids=[blocks[2].block_id] if removes_failure else [],
    )
    patch = ReportRevisionPatch(replacements=[])
    if change != "empty":
        target = blocks[2] if removes_failure else blocks[0]
        patch.replacements.append(
            BlockReplacement(
                start_block_id=target.block_id,
                end_block_id=target.block_id,
                markdown="",
                reason="Remove the unsupported forecast or redundant heading.",
            )
        )
    revised = apply_block_replacements(markdown, blocks, patch.replacements).markdown
    carried = plan_incremental_attribution(previous, blocks, parse_markdown(revised))
    assert not carried.dirty_block_ids

    model = create_autospec(ClaimAttributionModel, instance=True)
    summary = AttributionSummary()
    if removes_failure and complete_summary:
        summary.dispositions = [
            AttributionDisposition(prior_ref="p1", outcome="removed", reason="Forecast deleted.")
        ]
    model.summarize.return_value = (summary, summary.model_dump_json())
    store = create_autospec(AttributionPersistence, instance=True)
    run_id = uuid4()
    store.begin_attribution_run.return_value = run_id
    store.begin_attribution_summary.return_value = {}

    def run() -> AttributionRun:
        return run_attribution(
            model,
            previous.report_id,
            2,
            revised,
            snapshot,
            previous=previous,
            previous_review=previous_review,
            store=store,
            carried=carried,
        ).run

    if not complete_summary:
        with pytest.raises(ClaimAttributionOutputError, match="every prior core failure"):
            run()
        store.fail_attribution_run.assert_called_once()
        store.complete_attribution_run.assert_not_called()
        return

    result = run()
    model.select_materials.assert_not_called()
    model.verify_batch.assert_not_called()
    assert model.summarize.call_count == int(removes_failure)
    store.begin_attribution_batch.assert_not_called()
    store.complete_attribution_run.assert_called_once_with(result)
    assert result.attribution_run_id == run_id
    assert result.revision == 2
    assert result.claims == list(carried.claims)
    assert result.claim_evidence == previous.claim_evidence
    assert result.claim_premises == previous.claim_premises
    assert result.blocking_findings == list(carried.blocking_findings)
    assert all(note in result.audit_notes for note in carried.audit_notes)
    assert len(result.claims) == (1 if removes_failure else 2)
    assert len(result.blocking_findings) == (0 if removes_failure else 1)
    assert len(result.dispositions) == int(removes_failure)
    review = previous_review.model_copy(update={"revision": 2, "key_block_ids": []})
    assert final_report_status(result, review, repairs_used=2) == (
        "verified" if removes_failure else "partial"
    )
