# ruff: noqa: E501
"""Whole-report review after Claim Attribution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prospector.agents.llm import get_openai_client, strong_model, thinking_extra_body
from prospector.agents.research_synthesis import synthesis_context_payload
from prospector.deterministic.markdown_report import parse_markdown
from prospector.schemas.claims import AttributionRun, ReportReviewRun, ReviewFinding
from prospector.schemas.report import ResearchSynthesisRun, WriterSnapshot


class _ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocking_findings: list[ReviewFinding] = Field(default_factory=list)
    key_block_ids: list[str] = Field(default_factory=list)
    audit_notes: list[dict[str, Any]] = Field(default_factory=list)


class ReportReviewOutputError(ValueError):
    def __init__(self, message: str, raw_output: object) -> None:
        super().__init__(message)
        self.raw_output = raw_output


@dataclass(frozen=True, slots=True)
class ReportReviewResult:
    full_prompt: list[dict[str, str]]
    raw_output: str
    run: ReportReviewRun


class ReportReviewModel(Protocol):
    def review(
        self,
        report_id: UUID,
        revision: int,
        markdown: str,
        synthesis: ResearchSynthesisRun,
        attribution: AttributionRun,
        snapshot: WriterSnapshot,
    ) -> ReportReviewResult: ...


def attribution_summary(attribution: AttributionRun) -> dict[str, Any]:
    """Expose verified anchors and reasoning links without sending Excerpts again.

    Whole-report Review needs to see what the report's analysis rests on, but it must not
    receive enough source material to repeat Claim Attribution.
    """
    claim_ref = {claim.claim_id: f"c{index}" for index, claim in enumerate(attribution.claims, 1)}
    evidence_claim_ids = {item.claim_id for item in attribution.claim_evidence}
    failed_claim_ids = {
        item.claim_id for item in attribution.blocking_findings if item.claim_id is not None
    }
    premise_by_claim = {item.claim_id: item for item in attribution.claim_premises}
    failed_by_block: dict[str, int] = {}
    verified_by_block: dict[str, int] = {}
    for claim in attribution.claims:
        if claim.claim_id in evidence_claim_ids and claim.claim_id not in failed_claim_ids:
            verified_by_block[claim.block_id] = verified_by_block.get(claim.block_id, 0) + 1
    for item in attribution.blocking_findings:
        failed_by_block[item.block_id] = failed_by_block.get(item.block_id, 0) + 1
    return {
        "blocks": [
            {
                "block_id": assessment.block_id,
                "status": assessment.status,
                "verified_retrieval_claims": verified_by_block.get(assessment.block_id, 0),
                "failed_claims": failed_by_block.get(assessment.block_id, 0),
            }
            for assessment in attribution.block_assessments
        ],
        "open_failures": [
            {
                "failure_ref": f"f{index}",
                "claim_ref": claim_ref.get(item.claim_id) if item.claim_id is not None else None,
                "block_id": item.block_id,
                "text": item.text,
                "reason": item.reason,
            }
            for index, item in enumerate(attribution.blocking_findings, 1)
        ],
        "evidence_anchors": [
            {
                "claim_ref": claim_ref[claim.claim_id],
                "block_id": claim.block_id,
                "text": claim.text,
                "status": "failed" if claim.claim_id in failed_claim_ids else "verified",
            }
            for claim in attribution.claims
            if claim.claim_id in evidence_claim_ids or claim.claim_id in failed_claim_ids
        ],
        "analysis_links": [
            {
                "claim_ref": claim_ref[claim.claim_id],
                "block_id": claim.block_id,
                "text": claim.text,
                "premise_claim_refs": [
                    claim_ref[premise_id]
                    for premise_id in premise_by_claim[claim.claim_id].premise_claim_ids
                    if premise_id in claim_ref
                ],
                "direct_assertion_ids": [
                    str(value) for value in premise_by_claim[claim.claim_id].direct_assertion_ids
                ],
                "known_conflict_keys": premise_by_claim[claim.claim_id].known_conflict_keys,
            }
            for claim in attribution.claims
            if claim.claim_id not in evidence_claim_ids
            and claim.claim_id not in failed_claim_ids
            and claim.claim_id in premise_by_claim
        ],
    }


REVIEW_ATTEMPTS = 2


class OpenAIReportReview:
    def __init__(self, client: OpenAI | None = None, model: str | None = None) -> None:
        self.client = client or get_openai_client()
        self.model = model or strong_model()

    def review(
        self,
        report_id: UUID,
        revision: int,
        markdown: str,
        synthesis: ResearchSynthesisRun,
        attribution: AttributionRun,
        snapshot: WriterSnapshot,
    ) -> ReportReviewResult:
        blocks = parse_markdown(markdown)
        used_assertion_ids = {
            assertion_id
            for premise in attribution.claim_premises
            for assertion_id in premise.direct_assertion_ids
        }
        system = """你负责判断这份深度研究报告作为整体是否成立。

检查报告是否实质回应 Brief、遵守用户明确限制、诚实处理会改变核心认识的反例、冲突和证据边界，以及主要结论能否从正文中的已核对事实、材料冲突或证据缺口形成可识别的推理链。

这不是材料覆盖检查。Writer 可以选择材料、组织顺序和表达方式；事实较密、结构复杂或文风不同，都不能单独构成阻断。Research Synthesis 只是背景。

unused_assertions 列出报告没有用到的研究材料。取舍是作者的权利，没用到本身不是问题，**不要**因为某条材料没被使用就提出阻断。只回答一个问题：这些没被用到的材料里，有没有哪一条如果写进去会改变报告的核心结论——例如它是核心结论的反例、它给出了与正文不同的关键数字或时间、或者它直接回答了 Brief 问到而正文回避了的方面。有这种情况才记 material_omission，并指明是哪一条。

但是，如果报告把研究材料、来源能力或证据边界变成主要叙述对象，因而挤占了对 Brief 所问对象本身的解释，这不是文风差异，而是没有实质完成核心回答，应记录为 brief_response。局部必要的来源限定和证据边界说明不属于这种情况。

只有会使报告无法实质回应 Brief，或会让读者对核心结论产生实质误解的问题，才进入 blocking_findings。kind 只使用 brief_response、user_constraint、material_omission、conclusion_integrity。每条必须引用相关 block_ids，并具体说明问题及其对核心回答的影响。

key_block_ids 只标识实际承载主要认识和推理的位置，不评价观点是否正确。**这是一份少数派清单**：只列出删掉它读者就拿不到核心回答的段落，通常是全文的少数。把大部分段落都标进来等于没有标，代码会因此忽略这份清单。不要重新匹配 Excerpt。最终只输出符合 output_schema 的单个 JSON 对象。"""
        payload = {
            "markdown": markdown,
            "blocks": [
                {"block_id": block.block_id, "kind": block.kind, "text": block.text}
                for block in blocks
            ],
            "brief": snapshot.brief.model_dump(mode="json"),
            "synthesis": synthesis_context_payload(synthesis),
            "attribution_summary": attribution_summary(attribution),
            "usable_assertions": [
                {
                    "assertion_id": str(card.assertion_id),
                    "statement": card.assertion_statement,
                }
                for card in snapshot.evidence_cards
            ],
            # Omission had no detector at all: the chain checked 186 spans for
            # overclaiming and nothing for what the report left out, which tilts the
            # incentive the whole redesign set out to correct.  Code can compute exactly
            # which material went unused; only the question of whether any of it matters
            # needs a reader.
            "unused_assertions": [
                {
                    "assertion_id": str(card.assertion_id),
                    "statement": card.assertion_statement,
                }
                for card in snapshot.evidence_cards
                if card.assertion_id not in used_assertion_ids
            ],
            "conflicts": snapshot.conflicts,
            "minor_gaps": snapshot.minor_gaps,
            "output_schema": _ReviewOutput.model_json_schema(),
        }
        prompt = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        known_blocks = {block.block_id for block in blocks}
        last: ReportReviewOutputError | None = None
        for _ in range(REVIEW_ATTEMPTS):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=cast(Any, prompt),
                temperature=0.1,
                extra_body=thinking_extra_body(self.model),
                max_tokens=4096,
            )
            raw = response.choices[0].message.content or ""
            try:
                output = _ReviewOutput.model_validate_json(raw)
            except (ValidationError, ValueError) as exc:
                last = ReportReviewOutputError(f"invalid Whole-report Review output: {exc}", raw)
                continue
            if set(output.key_block_ids) - known_blocks or any(
                set(item.block_ids) - known_blocks for item in output.blocking_findings
            ):
                last = ReportReviewOutputError("Whole-report Review referenced unknown block", raw)
                continue
            if not output.key_block_ids and not any(
                item.kind == "brief_response" for item in output.blocking_findings
            ):
                last = ReportReviewOutputError(
                    "Whole-report Review omitted the report's key blocks", raw
                )
                continue
            break
        else:
            raise cast(ReportReviewOutputError, last)
        run = ReportReviewRun(
            review_run_id=uuid4(),
            report_id=report_id,
            revision=revision,
            synthesis_run_id=synthesis.synthesis_run_id,
            blocking_findings=[
                item.model_copy(update={"block_ids": list(dict.fromkeys(item.block_ids))})
                for item in output.blocking_findings
            ],
            key_block_ids=list(dict.fromkeys(output.key_block_ids)),
            audit_notes=output.audit_notes,
            raw_output=raw,
        )
        return ReportReviewResult(full_prompt=prompt, raw_output=raw, run=run)
