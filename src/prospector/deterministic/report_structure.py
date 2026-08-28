"""Deterministic structure metrics for a finished draft.

These measure whether a report argues or merely lists. They are **recorded, not
enforced**: the statement-level revision loop can only replace named sentences, so it
has no way to repair "this paragraph is a run of nineteen facts" — gating on that would
only produce failed Jobs. Thresholds also have no observed distribution yet. Collect
first, decide where the line goes once several Jobs have reported.

Measured on the report that motivated them: 137 evidence to 36 derived, a 10-statement
evidence run, and 3 of 20 paragraphs reaching no judgement at all — while every one of
its 9 top-level scopes held at least one derived statement somewhere. Section
granularity is too coarse to catch a chronicle; the paragraph is where it shows.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from prospector.schemas.report import ReportDraft, ReportParagraph


@dataclass(frozen=True, slots=True)
class ReportStructure:
    statement_count: int
    evidence_count: int
    derived_count: int
    paragraph_count: int
    # Longest run of consecutive evidence statements inside one paragraph. A long run is
    # the signature of a list: facts accumulate with nothing claiming what they show.
    longest_evidence_run: int
    # Paragraphs carrying no derived statement at all. This is the sensitive one: a
    # chronicle shows up as whole paragraphs of accumulated facts long before a section
    # runs out of judgement, because one analytic sentence anywhere redeems the section.
    paragraphs_without_derived: int
    # Top-level scopes (introduction, each section, conclusion) with no derived
    # statement. This is a coarse signal for the quality reviewer, not a verdict.
    scopes_without_derived: int
    scope_count: int

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


def _longest_evidence_run(paragraphs: list[ReportParagraph]) -> int:
    longest = 0
    for paragraph in paragraphs:
        run = 0
        for statement in paragraph.statements:
            # Runs break on any non-evidence statement, not just derived ones: a
            # limitation or a bridge also interrupts an undigested pile of facts.
            run = run + 1 if statement.kind == "evidence" else 0
            longest = max(longest, run)
    return longest


def measure_report_structure(draft: ReportDraft) -> ReportStructure:
    statements = draft.statements()
    groups = draft.statement_groups()
    return ReportStructure(
        statement_count=len(statements),
        evidence_count=sum(1 for item in statements if item.kind == "evidence"),
        derived_count=sum(1 for item in statements if item.kind == "derived"),
        paragraph_count=len(draft.paragraphs()),
        longest_evidence_run=_longest_evidence_run(draft.paragraphs()),
        paragraphs_without_derived=sum(
            1
            for paragraph in draft.paragraphs()
            if not any(item.kind == "derived" for item in paragraph.statements)
        ),
        scopes_without_derived=sum(
            1 for group in groups if not any(item.kind == "derived" for item in group)
        ),
        scope_count=len(groups),
    )
