from __future__ import annotations

import json
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import pytest

from prospector.agents.prompts.report_writer import (
    material_payload,
    report_writer_messages,
    report_writer_revision_messages,
)
from prospector.agents.report_attribution import (
    ClaimAttributionOutputError,
    _filter_selection,
    build_attribution_run,
    expand_selected_sources,
    plan_incremental_attribution,
    prepare_attribution_plan,
    run_attribution,
)
from prospector.agents.report_review import OpenAIReportReview, attribution_summary
from prospector.agents.research_synthesis import (
    OpenAIResearchSynthesis,
    research_synthesis_review_messages,
    synthesis_material_payload,
)
from prospector.deterministic.citation_render import render_final_report, report_health
from prospector.deterministic.markdown_report import (
    MarkdownContractError,
    apply_block_replacements,
    build_entity_whitelist,
    parse_markdown,
    partition_attribution_batches,
    scan_markers,
    serialized_batch_size,
    text_hash,
)
from prospector.flow import research_graph
from prospector.flow.research_graph import ResearchGraphServices, _synthesis_node
from prospector.flow.state import initial_research_state
from prospector.schemas.brief import ResearchBrief
from prospector.schemas.claims import (
    AttributionBatchSelection,
    AttributionBatchVerification,
    AttributionFinding,
    AttributionRun,
    AttributionSummary,
    BlockAssessment,
    ClaimEvidence,
    ClaimPremise,
    ClaimSpan,
    ReportReviewRun,
    ReviewFinding,
    core_attribution_finding_ids,
    final_report_status,
    has_core_problem,
)
from prospector.schemas.report import (
    BlockReplacement,
    ResearchSynthesisResult,
    ResearchSynthesisReview,
    ResearchSynthesisRun,
    SynthesisReviewDefect,
    WriterEvidenceCard,
    WriterExcerptRef,
    WriterSnapshot,
    WriterSource,
)


def test_synthesis_contract_keeps_gap_fields_out_of_ready_results() -> None:
    with pytest.raises(ValueError, match="ready"):
        ResearchSynthesisResult(
            decision="ready",
            synthesis="材料支持一项认识。",
            reason="还可以研究更多方向。",
        )
    result = ResearchSynthesisResult(
        decision="needs_research",
        synthesis="目前只能确认来源存在分歧。",
        reason="分歧阻断核心问题。",
        evidence_needed="同口径的一手统计。",
    )
    assert result.decision == "needs_research"


def test_synthesis_review_decision_is_computed_from_defects() -> None:
    with pytest.raises(ValueError, match="revised_result"):
        ResearchSynthesisReview(
            defects=[SynthesisReviewDefect(kind="evidence_catalog", reason="初稿只是罗列材料。")],
            reason="初稿只是罗列材料。",
        )
    accepted = ResearchSynthesisReview(reason="初稿已实质回应 Brief。")
    assert accepted.decision == "accept"
    assert accepted.defects == []
    revised = ResearchSynthesisReview(
        defects=[SynthesisReviewDefect(kind="missing_relationships", reason="没有解释形成机制。")],
        reason="没有解释形成机制。",
        revised_result=ResearchSynthesisResult(
            decision="ready",
            synthesis="材料之间的关系说明需求被推迟，而不是消失。",
        ),
    )
    assert revised.decision == "revise"


def test_synthesis_review_rejects_a_model_accept_flag() -> None:
    with pytest.raises(Exception, match="decision"):
        ResearchSynthesisReview.model_validate(
            {"decision": "accept", "reason": "看起来可以。", "defects": []}
        )


def test_synthesis_material_is_grouped_by_research_question_without_empty_tasks() -> None:
    snapshot = _snapshot("出货量下降。", "同口径数据显示出货量下降 12%。")
    task_id = snapshot.evidence_cards[0].task_id
    snapshot.final_plan_summary = [
        {
            "version": 1,
            "tasks": [
                {"id": task_id, "question": "出货量发生了什么变化？"},
                {"id": uuid4(), "question": "没有形成可用证据的任务"},
            ],
        }
    ]

    payload = synthesis_material_payload(snapshot)

    assert len(payload["research_tasks"]) == 1
    assert payload["research_tasks"][0]["question"] == "出货量发生了什么变化？"
    assert payload["research_tasks"][0]["findings"][0]["statement"] == "出货量下降。"


def test_synthesis_review_context_excludes_the_full_research_material() -> None:
    snapshot = _snapshot("不应进入检查上下文的断言。", "不应进入检查上下文的原文。")
    snapshot.final_plan_summary = [
        {
            "version": 1,
            "tasks": [
                {
                    "id": snapshot.evidence_cards[0].task_id,
                    "question": "出货量变化由什么推动？",
                }
            ],
        }
    ]
    snapshot.conflicts = [{"conflict_key": "c1", "disputed_point": "统计口径不同"}]
    snapshot.minor_gaps = [{"kind": "source_credibility", "description": "仅有单一来源"}]
    draft = ResearchSynthesisRun(
        synthesis_run_id=uuid4(),
        job_id=snapshot.job_id,
        version=1,
        decision="ready",
        synthesis="不同统计口径不能直接比较，但共同显示出货量下降。",
        assertion_ids=list(snapshot.usable_assertion_ids),
        material_conflict_keys=["c1"],
        raw_output={"review_prompt": "AUDIT_MATERIAL_MUST_NOT_REACH_SYNTHESIS_REVIEW"},
    )

    messages = research_synthesis_review_messages(snapshot, draft)
    payload = json.loads(messages[1]["content"])

    assert set(payload) == {
        "json_schema",
        "brief",
        "draft",
        "research_shape",
        "conflicts",
        "minor_gaps",
    }
    assert payload["brief"]["question"] == snapshot.brief.question
    assert payload["conflicts"] == snapshot.conflicts
    assert payload["minor_gaps"] == []
    assert payload["research_shape"] == {
        "research_questions": ["出货量变化由什么推动？"],
        "usable_assertion_count": 1,
        "draft_assertion_count": 1,
    }
    assert "不应进入检查上下文的断言" not in messages[1]["content"]
    assert "不应进入检查上下文的原文" not in messages[1]["content"]
    assert "AUDIT_MATERIAL_MUST_NOT_REACH_SYNTHESIS_REVIEW" not in messages[1]["content"]


def test_writer_receives_only_the_adopted_synthesis_not_its_audit_record() -> None:
    snapshot = _snapshot("出货量下降 12%。", "同口径数据显示出货量下降 12%。")
    synthesis = ResearchSynthesisRun(
        synthesis_run_id=uuid4(),
        job_id=snapshot.job_id,
        version=1,
        decision="ready",
        synthesis="同口径数据共同显示出货量下降。",
        assertion_ids=list(snapshot.usable_assertion_ids),
        raw_output={
            "draft": "AUDIT_DRAFT_MUST_NOT_REACH_WRITER",
            "review_prompt": [{"role": "user", "content": "AUDIT_MATERIAL_MUST_NOT_REACH_WRITER"}],
            "review": "AUDIT_REVIEW_MUST_NOT_REACH_WRITER",
        },
    )

    messages = report_writer_messages(snapshot, synthesis)
    payload = json.loads(messages[1]["content"])

    assert payload["research_synthesis"] == {
        "decision": "ready",
        "synthesis": "同口径数据共同显示出货量下降。",
        "reason": None,
        "evidence_needed": None,
    }
    serialized = messages[1]["content"]
    assert "AUDIT_" not in serialized
    assert str(synthesis.synthesis_run_id) not in serialized
    assert str(next(iter(snapshot.usable_assertion_ids))) in serialized


def test_source_credibility_gaps_are_attached_to_the_affected_finding() -> None:
    snapshot = _snapshot("出货量下降 12%。", "同口径数据显示出货量下降 12%。")
    assertion_id = snapshot.evidence_cards[0].assertion_id
    snapshot.minor_gaps = [
        {
            "kind": "source_credibility",
            "description": "该数字来自厂商案例，只能作为趋势信号。",
            "related_assertion_ids": [str(assertion_id)],
        },
        {
            "kind": "coverage",
            "description": "没有同口径地区数据。",
            "related_assertion_ids": [],
        },
    ]

    writer_payload = material_payload(snapshot)
    synthesis_payload = synthesis_material_payload(snapshot)

    assert writer_payload["findings"][0]["source_caveat"] == (
        "该数字来自厂商案例，只能作为趋势信号。"
    )
    assert synthesis_payload["research_tasks"][0]["findings"][0]["source_caveat"] == (
        "该数字来自厂商案例，只能作为趋势信号。"
    )
    assert writer_payload["minor_gaps"] == [snapshot.minor_gaps[1]]
    assert synthesis_payload["minor_gaps"] == [snapshot.minor_gaps[1]]


def test_writer_prompt_keeps_research_process_out_of_the_report_narrative() -> None:
    snapshot = _snapshot("出货量下降 12%。", "同口径数据显示出货量下降 12%。")
    synthesis = ResearchSynthesisRun(
        synthesis_run_id=uuid4(),
        job_id=snapshot.job_id,
        version=1,
        decision="ready",
        synthesis="同口径数据共同显示出货量下降。",
    )

    system = report_writer_messages(snapshot, synthesis)[0]["content"]

    assert "不要把“材料”“证据”“本报告”作为叙述主体" in system
    assert "不要单设证据边界" in system
    assert "不要为了展示覆盖面" in system


def test_markdown_parser_rejects_writer_footnotes_and_scans_surface_markers() -> None:
    with pytest.raises(MarkdownContractError, match="footnotes"):
        parse_markdown("# 标题\n\n数字是 42%。[^1]")
    blocks = parse_markdown("# 标题\n\n2025 年 OpenAI 的采用率为 42%，变化显著。")
    markers = scan_markers(blocks[1].text, {"OpenAI"})
    assert {marker.family for marker in markers} == {"retrieval", "candidate", "advisory"}
    assert any(marker.kind == "number" for marker in markers)


def test_year_marker_is_not_also_a_number() -> None:
    markers = scan_markers("2025 年出货量下降 12%。")
    assert any(marker.kind == "date" and marker.text == "2025 年" for marker in markers)
    assert not any(marker.kind == "number" and "2025" in marker.text for marker in markers)
    assert any(marker.kind == "number" and "12" in marker.text for marker in markers)


def test_batches_run_concurrently_and_a_premise_may_cross_them() -> None:
    """Running batches in order was the pipeline's largest cost; a premise still crosses."""
    import threading

    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    filler = "该季度的渠道调整与采购节奏变化在报告中被反复讨论并作为背景说明。" * 80
    markdown = (
        "\n\n".join(f"第 {index} 段出货量下降 {index}%。{filler}" for index in range(1, 12)) + "\n"
    )
    plan = prepare_attribution_plan(markdown, snapshot)
    assert len(plan.batches) > 1

    overlapped = threading.Event()
    inside = threading.Semaphore(0)
    started = threading.Lock()
    running = {"count": 0}

    class Concurrent:
        def select_materials(self, prompt):
            with started:
                running["count"] += 1
                if running["count"] > 1:
                    overlapped.set()
            inside.release()
            if not overlapped.wait(timeout=5):
                raise AssertionError("batches did not overlap")
            return AttributionBatchSelection(assertion_refs=["a1"]), "{}"

        def verify_batch(self, prompt):
            payload = json.loads(prompt[1]["content"])
            claims = [
                _model_claim(
                    claim_ref=f"c{index}",
                    block_id=item["block_id"],
                    start_offset=item["start_offset"],
                    end_offset=item["end_offset"],
                    status="analysis",
                    candidate_refs=[item["candidate_ref"]],
                    # Rest on a position from another batch, which only resolves globally.
                    premise_claim_refs=[payload["report_index"][0]["candidate_ref"]]
                    if payload["report_index"]
                    else [],
                    assertion_refs=["a1"],
                )
                for index, item in enumerate(payload["candidates"], start=1)
            ]
            return AttributionBatchVerification.model_validate({"claims": claims}), "{}"

        def summarize(self, prompt):
            return AttributionSummary(), "{}"

    result = run_attribution(Concurrent(), uuid4(), 1, markdown, snapshot, None, None)
    assert overlapped.is_set()
    crossing = [item for item in result.run.claim_premises if item.premise_claim_ids]
    assert crossing, "a premise naming a position outside its batch must resolve"


def test_a_heading_demands_no_evidence_of_its_own() -> None:
    """A heading is a label the section restates; it carries no statement to source."""
    markers = scan_markers("二、起源：2024 年 10 月的能力铺垫", block_kind="heading")
    assert markers
    assert not [marker for marker in markers if marker.family == "retrieval"]


def test_a_bare_year_is_the_topic_and_a_full_date_is_an_anchor() -> None:
    bare = scan_markers("2026 年初开始的热潮并非单次发布点燃。")
    assert [marker.family for marker in bare if marker.kind == "date"] == ["candidate"]
    full = scan_markers("Anthropic 在 2024 年 10 月推出 computer use。")
    assert [marker.family for marker in full if marker.kind == "date"] == ["retrieval"]


def test_a_quotation_needs_an_attribution_cue_to_demand_a_source() -> None:
    """Chinese uses the same marks for emphasis; 39 of 52 quoted spans were the writer's."""
    emphasis = scan_markers("模型开始从“会回答”走向“会做事”。")
    assert {marker.family for marker in emphasis if marker.kind == "quote"} == {"candidate"}
    quoted = scan_markers("Gartner 报告称“agent washing”现象普遍。")
    assert {marker.family for marker in quoted if marker.kind == "quote"} == {"retrieval"}


def test_a_list_ordinal_is_not_a_quantity() -> None:
    ordinal = scan_markers("1. 计算机使用：模型获得通用操作界面")
    assert [marker.family for marker in ordinal if marker.kind == "number"] == ["candidate"]
    quantity = scan_markers("出货量下降 12%。")
    assert [marker.family for marker in quantity if marker.kind == "number"] == ["retrieval"]


def test_a_spaced_chinese_date_is_one_date_not_a_year_plus_two_numbers() -> None:
    """Without space tolerance the month and day become separate phantom quantities."""
    markers = scan_markers("Anthropic 在 2024 年 10 月 22 日发布。")
    assert [marker.text for marker in markers if marker.kind == "date"] == ["2024 年 10 月 22 日"]
    assert not [marker for marker in markers if marker.kind == "number"]


def test_attribution_batches_split_consecutive_blocks_by_serialized_size() -> None:
    markdown = "\n\n".join(f"段落 {index} 的数字是 {index}%。" for index in range(1, 12))
    blocks = parse_markdown(markdown)
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    from prospector.agents.report_attribution import candidate_specs

    candidates = candidate_specs(blocks, snapshot)
    batches = partition_attribution_batches(blocks, candidates, char_budget=500)
    assert len(batches) > 1
    assert [item.batch_index for item in batches] == list(range(len(batches)))
    covered_blocks = [block_id for batch in batches for block_id in batch.block_ids]
    assert covered_blocks == [block.block_id for block in blocks]
    by_id = {block.block_id: block for block in blocks}
    by_ref = {item["candidate_ref"]: item for item in candidates}
    for batch in batches[:-1]:
        batch_blocks = [by_id[block_id] for block_id in batch.block_ids]
        batch_candidates = [by_ref[ref] for ref in batch.candidate_refs]
        assert serialized_batch_size(batch_blocks, batch_candidates) <= 500
        next_block = by_id[batches[batch.batch_index + 1].block_ids[0]]
        grown = serialized_batch_size(
            [*batch_blocks, next_block],
            [
                *batch_candidates,
                *[item for item in candidates if item["block_id"] == next_block.block_id],
            ],
        )
        assert grown > 500


def test_final_status_allows_partial_only_for_non_core_failed_claim() -> None:
    report_id = uuid4()
    claim = ClaimSpan(
        claim_id=uuid4(),
        block_id="b_0001",
        start_offset=0,
        end_offset=2,
        text="42",
        text_hash="sha256:" + "0" * 64,
        markers=[],
    )
    attribution = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=report_id,
        revision=3,
        block_assessments=[BlockAssessment(block_id="b_0001", status="assessed")],
        claims=[claim],
        blocking_findings=[
            AttributionFinding(
                kind="attribution",
                claim_id=claim.claim_id,
                block_id="b_0001",
                text="42",
                reason="材料口径不同",
            )
        ],
        marker_lexicon_version="v1",
    )
    review = ReportReviewRun(
        review_run_id=uuid4(), report_id=report_id, revision=3, synthesis_run_id=uuid4()
    )
    assert final_report_status(attribution, review, repairs_used=2) == "partial"
    attribution.claim_premises = [
        ClaimPremise(claim_id=uuid4(), premise_claim_ids=[claim.claim_id])
    ]
    assert final_report_status(attribution, review, repairs_used=2) == "failed"


def _snapshot(statement: str, excerpt_text: str) -> WriterSnapshot:
    return WriterSnapshot(
        job_id=uuid4(),
        brief=ResearchBrief(
            question="服务器市场发生了什么？",
            brief_text="考察出货量变化。",
        ),
        evidence_cards=[
            WriterEvidenceCard(
                assertion_id=uuid4(),
                task_id=uuid4(),
                assertion_statement=statement,
                excerpts=[
                    WriterExcerptRef(
                        excerpt_id=uuid4(),
                        text=excerpt_text,
                        source=WriterSource(source_uri="https://example.com/a", document_version=1),
                    )
                ],
            )
        ],
    )


def test_entity_whitelist_keeps_real_entities_and_drops_sliced_prose() -> None:
    material = (
        "根据 IDC 于 2025 年 3 月发布的报告，中国大陆企业级服务器市场出货量同比下降 12%，"
        "其中浪潮信息的份额降至 25.1%。分析师认为，这一变化主要由采购周期推迟造成。"
        "国家统计局与中国信息通信研究院联合发布的《数字经济发展报告》提到，全球最大的芯片公司投入增加。"
    )
    names = build_entity_whitelist([material])
    assert names == {"IDC", "国家统计局", "中国信息通信研究院", "数字经济发展报告"}
    # A sentence carrying no number, date, quote or scope word must not be pulled into
    # source retrieval merely because it reuses common phrasing from the material.
    judgement = "分析师认为这一变化并不意味着需求消失，采购周期推迟是更合理的解释。"
    assert scan_markers(judgement, names) == []


def test_parsed_blocks_locate_repeated_table_cells_separately() -> None:
    markdown = (
        "## 结论\n\n采购推迟是更合理的解释。\n\n"
        "| 年份 | 份额 |\n|---|---|\n| 2025 | 是 |\n| 2024 | 是 |\n"
    )
    blocks = parse_markdown(markdown)
    cells = [block for block in blocks if block.text == "是"]
    assert len(cells) == 2
    assert cells[0].source_start != cells[1].source_start
    for block in blocks:
        assert markdown[block.source_start : block.source_end] == block.text


def test_citation_lands_on_the_claimed_cell_not_the_first_matching_text() -> None:
    snapshot = _snapshot("2024 年的答案是肯定的。", "2024 年的答案是肯定的。")
    excerpt_id = snapshot.evidence_cards[0].excerpts[0].excerpt_id
    markdown = (
        "## 结论\n\n采购推迟是更合理的解释。\n\n"
        "| 年份 | 结论 |\n|---|---|\n| 2025 | 是 |\n| 2024 | 是 |\n"
    )
    blocks = parse_markdown(markdown)
    target = [block for block in blocks if block.text == "是"][1]
    claim = ClaimSpan(
        claim_id=uuid4(),
        block_id=target.block_id,
        start_offset=0,
        end_offset=1,
        text="是",
        text_hash=text_hash("是"),
        markers=[],
    )
    run = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=uuid4(),
        revision=1,
        block_assessments=[BlockAssessment(block_id=target.block_id, status="assessed")],
        claims=[claim],
        claim_evidence=[ClaimEvidence(claim_id=claim.claim_id, excerpt_id=excerpt_id)],
        marker_lexicon_version="v3",
    )
    review = ReportReviewRun(
        review_run_id=uuid4(),
        report_id=run.report_id,
        revision=1,
        synthesis_run_id=uuid4(),
        key_block_ids=[target.block_id],
    )
    rendered = render_final_report(markdown, blocks, run, review, snapshot, status="verified")
    assert "| 2024 | 是[^1] |" in rendered.markdown
    assert "解释。[^1]" not in rendered.markdown


def test_revision_feedback_hides_the_check_taxonomy_from_the_writer() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    synthesis = ResearchSynthesisRun(
        synthesis_run_id=uuid4(),
        job_id=snapshot.job_id,
        version=1,
        decision="ready",
        synthesis="材料支持出货量下降。",
        raw_output={"review_prompt": "AUDIT_MATERIAL_MUST_NOT_REACH_REVISION"},
    )
    report_id = uuid4()
    attribution = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=report_id,
        revision=1,
        blocking_findings=[
            AttributionFinding(
                kind="in_place_downgrade",
                block_id="b_0001",
                text="出货量大幅下降",
                reason="上一版写的是 12%，这一版改成了大幅，材料里是 12%。",
            ),
            AttributionFinding(
                kind="attribution",
                claim_id=uuid4(),
                block_id="b_0001",
                text="边角事实",
                reason="这是一处非核心失败。",
            ),
        ],
        marker_lexicon_version="v3",
    )
    review = ReportReviewRun(
        review_run_id=uuid4(),
        report_id=report_id,
        revision=1,
        synthesis_run_id=synthesis.synthesis_run_id,
        blocking_findings=[
            ReviewFinding(
                kind="conclusion_integrity",
                reason="主要结论没有落在材料上。",
                block_ids=["b_0001"],
            )
        ],
    )
    messages = report_writer_revision_messages(
        snapshot, synthesis, "# 报告\n\n出货量大幅下降。", attribution, review
    )
    payload = "".join(message["content"] for message in messages)
    for taxonomy in ("in_place_downgrade", "conclusion_integrity", "brief_response"):
        assert taxonomy not in payload
    assert "上一版写的是 12%" in payload
    assert "出货量大幅下降" in payload
    assert "材料支持出货量下降" in payload
    assert "这是一处非核心失败" not in payload
    assert "AUDIT_MATERIAL_MUST_NOT_REACH_REVISION" not in payload


def _model_claim(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "claim_ref": "c1",
        "block_id": "b_0001",
        "start_offset": 0,
        "end_offset": 4,
        "status": "analysis",
        "candidate_refs": [],
        "excerpt_refs": [],
        "assertion_refs": [],
        "premise_claim_refs": [],
        "known_conflict_keys": [],
        "reason": None,
        "audit_note": None,
    }
    base.update(kwargs)
    return base


def test_attribution_resolves_premise_refs_into_a_real_premise_graph() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "出货量下降 12%，因此采购在推迟。"
    blocks = parse_markdown(markdown)
    body = blocks[0].text
    split = body.index("，")
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=split,
                    status="verified",
                    candidate_refs=["k1"],
                    excerpt_refs=["a1e1"],
                    assertion_refs=["a1"],
                ),
                _model_claim(
                    claim_ref="c2",
                    start_offset=split + 1,
                    end_offset=len(body),
                    status="analysis",
                    premise_claim_refs=["c1"],
                ),
            ]
        }
    )
    run = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    supporting = {claim.text: claim.claim_id for claim in run.claims}[body[:split]]
    assert any(premise.premise_claim_ids == [supporting] for premise in run.claim_premises)


def test_a_span_with_no_verdict_is_reported_instead_of_discarding_the_report() -> None:
    """An unanswered span is a hole in one span, not grounds for losing the whole Job."""
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    blocks = parse_markdown("出货量下降 12%。")
    run = build_attribution_run(
        uuid4(),
        1,
        AttributionBatchVerification.model_validate({"claims": []}),
        blocks,
        snapshot,
        None,
        None,
        "{}",
    )
    assert [item.reason for item in run.blocking_findings] == ["这处正文没有得到任何核对结论。"]
    assert run.blocking_findings[0].claim_id is None
    assert not run.claims


def test_attribution_rejects_unbound_verified_claims() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    blocks = parse_markdown("出货量下降 12%。")
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=len(blocks[0].text) - 1,
                    status="verified",
                    candidate_refs=["k1"],
                )
            ]
        }
    )
    with pytest.raises(ClaimAttributionOutputError, match="malformed"):
        build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")


def test_health_counts_a_paragraph_that_both_states_and_reasons_in_both_columns() -> None:
    """Checked and reasoning blocks overlap; subtracting one reported zero reasoning."""
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "出货量下降 12%，因此采购在推迟。"
    blocks = parse_markdown(markdown)
    body = blocks[0].text
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=body.index("，"),
                    status="verified",
                    candidate_refs=["k1"],
                    excerpt_refs=["a1e1"],
                    assertion_refs=["a1"],
                ),
                _model_claim(
                    claim_ref="c2",
                    start_offset=body.index("，") + 1,
                    end_offset=len(body),
                    status="analysis",
                    premise_claim_refs=["c1"],
                ),
            ]
        }
    )
    run = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    health = report_health(run, snapshot, blocks)
    assert health.checked_blocks == 1
    assert health.reasoned_blocks == 1
    assert health.assertions_collected == len(snapshot.evidence_cards)
    assert health.assertions_used == 1


def test_a_replacement_leaves_every_unnamed_block_byte_identical() -> None:
    """The property a full rewrite cannot offer: untouched prose is provably untouched."""
    markdown = (
        "# 标题\n\n## 一、起源\n\n"
        "2024 年 10 月，Anthropic 推出 computer use。\n\n模型第一次获得通用操作界面。\n"
    )
    blocks = parse_markdown(markdown)
    applied = apply_block_replacements(
        markdown,
        blocks,
        [
            BlockReplacement(
                start_block_id="b_0004",
                end_block_id="b_0004",
                markdown="Claude 3.5 Sonnet 成为首个提供该功能的前沿模型。",
                reason="收窄首次性口径",
            )
        ],
    )
    assert not applied.rejected
    assert "2024 年 10 月，Anthropic 推出 computer use。" in applied.markdown
    assert "模型第一次获得通用操作界面" not in applied.markdown
    assert [applied.markdown[start:end] for start, end in applied.new_regions] == [
        "Claude 3.5 Sonnet 成为首个提供该功能的前沿模型。"
    ]


def test_a_replacement_may_span_blocks_so_a_fix_can_carry_its_neighbours() -> None:
    markdown = "# 标题\n\n## 一、起源\n\n模型第一次获得通用操作界面。\n\n这一转变改变了产品形态。\n"
    blocks = parse_markdown(markdown)
    applied = apply_block_replacements(
        markdown,
        blocks,
        [
            BlockReplacement(
                start_block_id="b_0002",
                end_block_id="b_0004",
                markdown=(
                    "## 一、起源：能力铺垫\n\n"
                    "Claude 3.5 Sonnet 是首个提供该功能的前沿模型。\n\n它改变了产品形态。"
                ),
                reason="改口径后指代要跟着调",
            )
        ],
    )
    assert not applied.rejected
    assert "这一转变" not in applied.markdown
    assert applied.markdown.startswith("# 标题")


def test_a_replacement_that_would_swallow_a_neighbouring_table_cell_is_refused() -> None:
    markdown = "# 标题\n\n| 厂商 | 时间 |\n| --- | --- |\n| OpenAI | 2025 |\n"
    blocks = parse_markdown(markdown)
    cell = [block for block in blocks if block.kind == "table_cell"][-1]
    applied = apply_block_replacements(
        markdown,
        blocks,
        [
            BlockReplacement(
                start_block_id=cell.block_id,
                end_block_id=cell.block_id,
                markdown="2026",
                reason="改年份",
            )
        ],
    )
    assert applied.markdown == markdown
    assert "共用行" in applied.rejected[0]["reason"]


def test_a_revision_reuses_the_verdicts_of_blocks_it_did_not_rewrite() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "出货量下降 12%。\n\n采购在推迟。\n"
    blocks = parse_markdown(markdown)
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    block_id="b_0001",
                    start_offset=0,
                    end_offset=len(blocks[0].text) - 1,
                    status="verified",
                    candidate_refs=["k1"],
                    excerpt_refs=["a1e1"],
                    assertion_refs=["a1"],
                ),
                _model_claim(
                    claim_ref="c2",
                    block_id="b_0002",
                    start_offset=0,
                    end_offset=len(blocks[1].text),
                    status="analysis",
                    premise_claim_refs=["c1"],
                ),
            ]
        }
    )
    previous = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    revised = parse_markdown("出货量下降 12%。\n\n采购节奏没有变化。\n")
    carried = plan_incremental_attribution(previous, blocks, revised)
    assert carried.dirty_block_ids == {"b_0002"}
    assert [claim.block_id for claim in carried.claims] == ["b_0001"]


def test_carrying_a_revision_keeps_the_notes_and_unanswered_spans_of_kept_text() -> None:
    """Inheriting verdicts without their records made a revision look healthier than it was."""
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "2026 年 3 月的回落更像是渠道调整；出货量下降 12%。\n\n采购在推迟。\n"
    blocks = parse_markdown(markdown)
    first = blocks[0].text.index("；")
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                # Only the first clause is answered; the second is left with no verdict.
                _model_claim(
                    claim_ref="c1",
                    block_id="b_0001",
                    start_offset=0,
                    end_offset=first,
                    status="analysis",
                    candidate_refs=["k1"],
                    assertion_refs=["a1"],
                )
            ]
        }
    )
    previous = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    assert any(note["kind"] == "unchecked_marker_in_analysis" for note in previous.audit_notes)
    assert any(item.claim_id is None for item in previous.blocking_findings)

    revised = parse_markdown(
        "2026 年 3 月的回落更像是渠道调整；出货量下降 12%。\n\n采购节奏没有变化。\n"
    )
    carried = plan_incremental_attribution(previous, blocks, revised)
    assert carried.dirty_block_ids == {"b_0002"}
    assert any(note["kind"] == "unchecked_marker_in_analysis" for note in carried.audit_notes)
    # The span nobody answered for in b_0002... belongs to the rewritten block and is gone;
    # the one in the kept block must survive.
    assert all(item.block_id == "b_0001" for item in carried.blocking_findings)


def test_rewriting_a_fact_invalidates_the_analysis_that_rested_on_it() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "出货量下降 12%。\n\n采购在推迟。\n"
    blocks = parse_markdown(markdown)
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    block_id="b_0001",
                    start_offset=0,
                    end_offset=len(blocks[0].text) - 1,
                    status="verified",
                    candidate_refs=["k1"],
                    excerpt_refs=["a1e1"],
                    assertion_refs=["a1"],
                ),
                _model_claim(
                    claim_ref="c2",
                    block_id="b_0002",
                    start_offset=0,
                    end_offset=len(blocks[1].text),
                    status="analysis",
                    premise_claim_refs=["c1"],
                ),
            ]
        }
    )
    previous = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    # The premise is rewritten; the untouched conclusion must not keep its inherited pass.
    revised = parse_markdown("出货量下降 8%。\n\n采购在推迟。\n")
    carried = plan_incremental_attribution(previous, blocks, revised)
    assert carried.dirty_block_ids == {"b_0001", "b_0002"}
    assert not carried.claims


def test_the_entity_whitelist_does_not_depend_on_the_interpreter_hash_seed() -> None:
    """The plan is a contract; it may not differ between two processes reading one report."""
    material = [
        f"{prefix}Corp 与 {prefix}Labs 在 2025 年发布了 {prefix}Agent。"
        for prefix in (chr(code) for code in range(65, 91))
    ]
    names = build_entity_whitelist(material * 40)
    for _ in range(3):
        assert build_entity_whitelist(material * 40) == names
    # Ties on length must resolve by name, not by set order.
    trimmed = sorted(build_entity_whitelist(material * 40))
    assert trimmed == sorted(names)


def test_a_heading_carries_no_footnotes_and_a_span_shows_at_most_three() -> None:
    """519 marks with a 21-mark run buried the first real report; the chain stays in audit."""
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    card = snapshot.evidence_cards[0]
    extra = [
        WriterExcerptRef(
            excerpt_id=uuid4(),
            text=f"另一来源 {index} 同样报告出货量下降 12%。",
            source=WriterSource(source_uri=f"https://example.com/{index}", document_version=1),
        )
        for index in range(5)
    ]
    card.excerpts = [*card.excerpts, *extra]
    markdown = "# 出货量下降 12%\n\n出货量下降 12%。\n"
    blocks = parse_markdown(markdown)
    heading, body = blocks[0], blocks[1]
    claims = [
        ClaimSpan(
            claim_id=uuid4(),
            block_id=heading.block_id,
            start_offset=0,
            end_offset=len(heading.text),
            text=heading.text,
            text_hash=text_hash(heading.text),
        ),
        ClaimSpan(
            claim_id=uuid4(),
            block_id=body.block_id,
            start_offset=0,
            end_offset=len(body.text) - 1,
            text=body.text[:-1],
            text_hash=text_hash(body.text[:-1]),
        ),
    ]
    run = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=uuid4(),
        revision=1,
        block_assessments=[
            BlockAssessment(block_id=block.block_id, status="assessed") for block in blocks
        ],
        claims=claims,
        claim_evidence=[
            ClaimEvidence(claim_id=claim.claim_id, excerpt_id=excerpt.excerpt_id)
            for claim in claims
            for excerpt in card.excerpts
        ],
        marker_lexicon_version="v5",
    )
    review = ReportReviewRun(
        review_run_id=uuid4(),
        report_id=run.report_id,
        revision=1,
        synthesis_run_id=uuid4(),
    )
    rendered = render_final_report(markdown, blocks, run, review, snapshot, status="verified")
    heading_line = next(line for line in rendered.markdown.splitlines() if line.startswith("# "))
    assert "[^" not in heading_line
    body_line = next(
        line for line in rendered.markdown.splitlines() if line.startswith("出货量下降")
    )
    assert body_line.count("[^") == 3
    # Nothing is lost: every binding is still in the audit output.
    assert len(json.loads(rendered.json_text)["claim_evidence"]) == 12
    assert report_health(run, snapshot, blocks).spans_over_citation_cap == 2


def test_a_citation_never_lands_inside_a_latin_word() -> None:
    """A span may end mid-token; the mark it produces may not sit inside the word."""
    snapshot = _snapshot("IBM 发布了 foundation 服务。", "IBM 发布了 foundation 服务。")
    markdown = "e&与IBM宣布企业级agentic AI foundation，随后扩展。"
    blocks = parse_markdown(markdown)
    body = blocks[0]
    # The model ended its span in the middle of "foundation".
    end = body.text.index("foundation") + len("foundat")
    claim = ClaimSpan(
        claim_id=uuid4(),
        block_id=body.block_id,
        start_offset=0,
        end_offset=end,
        text=body.text[:end],
        text_hash=text_hash(body.text[:end]),
    )
    run = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=uuid4(),
        revision=1,
        block_assessments=[BlockAssessment(block_id=body.block_id, status="assessed")],
        claims=[claim],
        claim_evidence=[
            ClaimEvidence(
                claim_id=claim.claim_id,
                excerpt_id=snapshot.evidence_cards[0].excerpts[0].excerpt_id,
            )
        ],
        marker_lexicon_version="v5",
    )
    review = ReportReviewRun(
        review_run_id=uuid4(),
        report_id=run.report_id,
        revision=1,
        synthesis_run_id=uuid4(),
    )
    rendered = render_final_report(markdown, blocks, run, review, snapshot, status="verified")
    assert "foundation[^1]，" in rendered.markdown
    assert "foundat[^1]ion" not in rendered.markdown


def test_a_citation_against_a_separator_is_left_alone() -> None:
    """Enumerated citations already sit well; the fix must not drag them anywhere."""
    snapshot = _snapshot("MCP 是连接标准。", "MCP 是连接标准。")
    markdown = "函数调用、计算机操作、MCP、A2A等机制让模型连接外部系统。"
    blocks = parse_markdown(markdown)
    body = blocks[0]
    end = body.text.index("、A2A") + len("、A2A")
    claim = ClaimSpan(
        claim_id=uuid4(),
        block_id=body.block_id,
        start_offset=0,
        end_offset=end,
        text=body.text[:end],
        text_hash=text_hash(body.text[:end]),
    )
    run = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=uuid4(),
        revision=1,
        block_assessments=[BlockAssessment(block_id=body.block_id, status="assessed")],
        claims=[claim],
        claim_evidence=[
            ClaimEvidence(
                claim_id=claim.claim_id,
                excerpt_id=snapshot.evidence_cards[0].excerpts[0].excerpt_id,
            )
        ],
        marker_lexicon_version="v5",
    )
    review = ReportReviewRun(
        review_run_id=uuid4(),
        report_id=run.report_id,
        revision=1,
        synthesis_run_id=uuid4(),
    )
    rendered = render_final_report(markdown, blocks, run, review, snapshot, status="verified")
    assert "A2A[^1]等机制" in rendered.markdown


def test_claims_ending_together_share_one_capped_run_of_citations() -> None:
    """Nesting spans stacked nine marks on one full stop when the cap was per claim."""
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    card = snapshot.evidence_cards[0]
    card.excerpts = [
        *card.excerpts,
        *[
            WriterExcerptRef(
                excerpt_id=uuid4(),
                text=f"另一来源 {index}。",
                source=WriterSource(source_uri=f"https://example.com/{index}", document_version=1),
            )
            for index in range(8)
        ],
    ]
    markdown = "出货量下降 12%。\n"
    blocks = parse_markdown(markdown)
    body = blocks[0]
    end = len(body.text) - 1
    # Three claims over the same passage, all ending at the same offset.
    claims = [
        ClaimSpan(
            claim_id=uuid4(),
            block_id=body.block_id,
            start_offset=start,
            end_offset=end,
            text=body.text[start:end],
            text_hash=text_hash(body.text[start:end]),
        )
        for start in (0, 1, 2)
    ]
    run = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=uuid4(),
        revision=1,
        block_assessments=[BlockAssessment(block_id=body.block_id, status="assessed")],
        claims=claims,
        claim_evidence=[
            ClaimEvidence(claim_id=claim.claim_id, excerpt_id=excerpt.excerpt_id)
            for claim, excerpt in zip(claims, card.excerpts, strict=False)
        ],
        marker_lexicon_version="v5",
    )
    review = ReportReviewRun(
        review_run_id=uuid4(),
        report_id=run.report_id,
        revision=1,
        synthesis_run_id=uuid4(),
    )
    rendered = render_final_report(markdown, blocks, run, review, snapshot, status="verified")
    body_line = next(
        line for line in rendered.markdown.splitlines() if line.startswith("出货量下降")
    )
    assert body_line == "出货量下降 12%[^1][^2][^3]。"


def test_material_reaches_the_model_only_as_short_refs() -> None:
    """No UUID in a prompt means no UUID to splice, and a quarter less output to write."""
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    sources = expand_selected_sources(snapshot, [snapshot.evidence_cards[0].assertion_id])
    serialized = json.dumps(sources, ensure_ascii=False)
    assert str(snapshot.evidence_cards[0].assertion_id) not in serialized
    assert str(snapshot.evidence_cards[0].excerpts[0].excerpt_id) not in serialized
    assert sources[0]["ref"] == "a1"
    # An Excerpt ref carries its Assertion, so the belongs-to rule is visible up front.
    assert sources[0]["excerpts"][0]["ref"] == "a1e1"


def test_a_claim_written_in_refs_is_stored_against_real_ids() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    blocks = parse_markdown("出货量下降 12%。")
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=len(blocks[0].text) - 1,
                    status="verified",
                    candidate_refs=["k1"],
                    excerpt_refs=["a1e1"],
                    assertion_refs=["a1"],
                )
            ]
        }
    )
    run = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    assert [item.excerpt_id for item in run.claim_evidence] == [
        snapshot.evidence_cards[0].excerpts[0].excerpt_id
    ]
    assert run.claim_premises[0].direct_assertion_ids == [snapshot.evidence_cards[0].assertion_id]


def test_an_unknown_catalog_ref_is_dropped_rather_than_ending_the_job() -> None:
    """A ref the catalog never issued is a slip, not grounds for losing the report."""
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    selection = AttributionBatchSelection.model_validate({"assertion_refs": ["a1", "a99"]})
    filtered, notes = _filter_selection(selection, snapshot)
    assert filtered.assertion_refs == ["a1"]
    assert [note["kind"] for note in notes] == ["dropped_selection_ids"]


def test_analysis_over_a_retrieval_marker_is_recorded_not_discarded() -> None:
    """The marker lexicon cannot tell a topic year from a fact anchor; it may only observe."""
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "2026 年 3 月的这轮回落更像是渠道调整的结果。"
    blocks = parse_markdown(markdown)
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=len(blocks[0].text) - 1,
                    status="analysis",
                    candidate_refs=["k1"],
                    assertion_refs=["a1"],
                )
            ]
        }
    )
    run = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    assert len(run.claims) == 1
    assert not run.blocking_findings
    unchecked = [note for note in run.audit_notes if note["kind"] == "unchecked_marker_in_analysis"]
    assert [marker["text"] for marker in unchecked[0]["markers"]] == ["2026 年 3 月"]


def test_analysis_that_reaches_no_checked_fact_becomes_a_finding() -> None:
    """Analysis is the verdict a model can award itself, so it has to bottom out."""
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "采购在推迟。"
    blocks = parse_markdown(markdown)
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=len(blocks[0].text),
                    status="analysis",
                )
            ]
        }
    )
    run = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    assert [item.reason for item in run.blocking_findings] == [
        "这段分析没有落到任何已核对的事实或材料上：它声明的依据要么为空，要么本身也没有依据。"
    ]


def test_analysis_resting_on_a_verified_claim_is_grounded() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "出货量下降 12%，因此采购在推迟。"
    blocks = parse_markdown(markdown)
    body = blocks[0].text
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=body.index("，"),
                    status="verified",
                    candidate_refs=["k1"],
                    excerpt_refs=["a1e1"],
                    assertion_refs=["a1"],
                ),
                _model_claim(
                    claim_ref="c2",
                    start_offset=body.index("，") + 1,
                    end_offset=len(body),
                    status="analysis",
                    premise_claim_refs=["c1"],
                ),
            ]
        }
    )
    run = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    assert not run.blocking_findings


def test_a_premise_cycle_grounds_nothing() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "采购在推迟，因此库存在上升。"
    blocks = parse_markdown(markdown)
    body = blocks[0].text
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=body.index("，"),
                    status="analysis",
                    premise_claim_refs=["c2"],
                ),
                _model_claim(
                    claim_ref="c2",
                    start_offset=body.index("，") + 1,
                    end_offset=len(body),
                    status="analysis",
                    premise_claim_refs=["c1"],
                ),
            ]
        }
    )
    run = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    assert len(run.blocking_findings) == 2


def test_an_invented_premise_ref_is_dropped_rather_than_ending_the_job() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "出货量下降 12%，因此采购在推迟。"
    blocks = parse_markdown(markdown)
    body = blocks[0].text
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=body.index("，"),
                    status="verified",
                    candidate_refs=["k1"],
                    excerpt_refs=["a1e1"],
                    assertion_refs=["a1"],
                ),
                _model_claim(
                    claim_ref="c2",
                    start_offset=body.index("，") + 1,
                    end_offset=len(body),
                    status="analysis",
                    premise_claim_refs=["c1", "b7_c99"],
                ),
            ]
        }
    )
    run = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    assert not run.blocking_findings
    dropped = [note for note in run.audit_notes if note["kind"] == "dropped_premise_refs"]
    assert dropped[0]["premise_claim_refs"] == ["b7_c99"]


def test_named_entity_candidate_can_remain_analysis_without_a_fact_failure() -> None:
    snapshot = _snapshot("OpenAI 扩大了渠道投入。", "OpenAI 扩大了渠道投入。")
    markdown = "OpenAI 的扩张更像是渠道优势的结果。"
    blocks = parse_markdown(markdown)
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=len(blocks[0].text) - 1,
                    status="analysis",
                    candidate_refs=["k1"],
                    assertion_refs=["a1"],
                )
            ]
        }
    )
    run = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    assert run.blocking_findings == []
    assert {marker.family for marker in run.claims[0].markers} == {"candidate"}
    assert run.claim_premises[0].direct_assertion_ids == [snapshot.evidence_cards[0].assertion_id]


def test_advisory_without_a_number_only_creates_an_audit_note() -> None:
    snapshot = _snapshot("采购链条发生变化。", "采购链条发生变化。")
    blocks = parse_markdown("这个变化显著改变了采购决策链条。")
    run = build_attribution_run(
        uuid4(),
        1,
        AttributionBatchVerification.model_validate({"claims": []}),
        blocks,
        snapshot,
        None,
        None,
        "{}",
    )
    assert run.blocking_findings == []
    assert run.audit_notes == [
        {
            "kind": "advisory_without_quantification",
            "block_id": "b_0001",
            "text": "显著",
        }
    ]


def test_attribution_drops_one_bad_span_but_fails_when_most_are_bad() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "采购正在推迟，因此需要重新判断需求。这一点值得关注。"
    blocks = parse_markdown(markdown)
    size = len(blocks[0].text)
    good = [
        _model_claim(claim_ref=f"c{index}", start_offset=11, end_offset=size, status="analysis")
        for index in range(9)
    ]
    output = AttributionBatchVerification.model_validate(
        {"claims": [*good, _model_claim(claim_ref="bad", start_offset=0, end_offset=size + 50)]}
    )
    run = build_attribution_run(uuid4(), 1, output, blocks, snapshot, None, None, "{}")
    assert len(run.claims) == 9
    assert any(note["kind"] == "skipped_claim" for note in run.audit_notes)
    with pytest.raises(ClaimAttributionOutputError, match="malformed"):
        build_attribution_run(
            uuid4(),
            1,
            AttributionBatchVerification.model_validate(
                {"claims": [_model_claim(claim_ref="bad", end_offset=size + 50)]}
            ),
            blocks,
            snapshot,
            None,
            None,
            "{}",
        )


def test_in_place_downgrade_is_located_in_the_current_revision() -> None:
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "出货量大幅下降，因此采购在推迟。"
    blocks = parse_markdown(markdown)
    report_id = uuid4()
    previous = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=report_id,
        revision=1,
        blocking_findings=[
            AttributionFinding(
                kind="in_place_downgrade",
                block_id="b_0001",
                text="出货量下降 42%",
                reason="材料里是 12%。",
            )
        ],
        marker_lexicon_version="v3",
    )
    previous_review = ReportReviewRun(
        review_run_id=uuid4(),
        report_id=report_id,
        revision=1,
        synthesis_run_id=uuid4(),
        key_block_ids=["b_0001"],
    )
    output = AttributionBatchVerification.model_validate(
        {
            "claims": [
                _model_claim(
                    claim_ref="c1",
                    start_offset=0,
                    end_offset=len(blocks[0].text),
                    assertion_refs=["a1"],
                )
            ],
        }
    )
    summary = AttributionSummary.model_validate(
        {
            "dispositions": [
                {
                    "prior_ref": "p1",
                    "outcome": "in_place_downgrade",
                    "reason": "具体比例被改成了大幅下降。",
                    "current_block_id": "b_0001",
                    "current_start_offset": 0,
                    "current_end_offset": 7,
                }
            ]
        }
    )
    run = build_attribution_run(
        report_id, 2, output, blocks, snapshot, previous, previous_review, "{}", summary=summary
    )
    assert [item.kind for item in run.blocking_findings] == ["in_place_downgrade"]
    assert run.blocking_findings[0].text == "出货量大幅下降"
    current_review = previous_review.model_copy(update={"revision": 2})
    rendered = render_final_report(markdown, blocks, run, current_review, snapshot, status="failed")
    assert rendered.markdown.endswith(markdown)


def _runs(
    *,
    kind: Literal["attribution", "in_place_downgrade"],
    key_blocks: list[str],
    block_id: str = "b_0001",
) -> tuple[AttributionRun, ReportReviewRun, ClaimSpan]:
    report_id = uuid4()
    claim = ClaimSpan(
        claim_id=uuid4(),
        block_id=block_id,
        start_offset=0,
        end_offset=2,
        text="42",
        text_hash=text_hash("42"),
        markers=[],
    )
    attribution = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=report_id,
        revision=1,
        block_assessments=[BlockAssessment(block_id=block_id, status="assessed")],
        claims=[claim],
        blocking_findings=[
            AttributionFinding(
                kind=kind,
                claim_id=claim.claim_id,
                block_id=block_id,
                reason="材料口径不同",
                text="42",
            )
        ],
        marker_lexicon_version="v3",
    )
    review = ReportReviewRun(
        review_run_id=uuid4(),
        report_id=report_id,
        revision=1,
        synthesis_run_id=uuid4(),
        key_block_ids=key_blocks,
    )
    return attribution, review, claim


def test_last_repair_round_is_reserved_for_core_problems() -> None:
    peripheral, review, _ = _runs(kind="attribution", key_blocks=["b_0009"])
    # A peripheral detail never re-exposes the whole document to another generation.
    assert final_report_status(peripheral, review, repairs_used=0) == "partial"
    assert final_report_status(peripheral, review, repairs_used=1) == "partial"
    core, core_review, _ = _runs(kind="attribution", key_blocks=["b_0001"])
    assert final_report_status(core, core_review, repairs_used=1) == "revising"
    assert final_report_status(core, core_review, repairs_used=2) == "failed"


def test_a_review_that_marks_most_of_the_report_key_is_ignored() -> None:
    """59 of 72 blocks marked key made six failures core when only one was depended upon."""
    blocks = [f"b_{index:04d}" for index in range(1, 21)]
    peripheral, review, _ = _runs(kind="attribution", key_blocks=blocks[:15])
    peripheral.block_assessments = [
        BlockAssessment(block_id=block_id, status="assessed") for block_id in blocks
    ]
    # b_0001 is in the list, but the list covers three quarters of the report.
    assert not core_attribution_finding_ids(peripheral, review)
    assert final_report_status(peripheral, review, repairs_used=0) == "partial"

    selective, selective_review, _ = _runs(kind="attribution", key_blocks=blocks[:4])
    selective.block_assessments = [
        BlockAssessment(block_id=block_id, status="assessed") for block_id in blocks
    ]
    assert core_attribution_finding_ids(selective, selective_review)
    assert final_report_status(selective, selective_review, repairs_used=0) == "revising"


def test_a_failure_something_rests_on_is_core_however_the_review_marks_blocks() -> None:
    blocks = [f"b_{index:04d}" for index in range(1, 21)]
    run, review, claim = _runs(kind="attribution", key_blocks=blocks[:15])
    run.block_assessments = [
        BlockAssessment(block_id=block_id, status="assessed") for block_id in blocks
    ]
    # Another claim reasons from the failed one, which is structural, not a judgement.
    run.claim_premises = [ClaimPremise(claim_id=uuid4(), premise_claim_ids=[claim.claim_id])]
    assert core_attribution_finding_ids(run, review)


def test_in_place_downgrade_counts_as_a_core_problem() -> None:
    attribution, review, _ = _runs(kind="in_place_downgrade", key_blocks=[])
    assert has_core_problem(attribution, review) is True
    assert final_report_status(attribution, review, repairs_used=1) == "revising"


def test_review_sees_counts_and_open_failures_but_not_every_span() -> None:
    attribution, _review, claim = _runs(kind="attribution", key_blocks=[])
    summary = attribution_summary(attribution)
    assert summary["blocks"] == [
        {
            "block_id": "b_0001",
            "status": "assessed",
            "verified_retrieval_claims": 0,
            "failed_claims": 1,
        }
    ]
    assert summary["open_failures"][0]["reason"] == "材料口径不同"
    assert str(claim.claim_id) not in json.dumps(summary)


class _SynthesisRepository:
    """Just enough repository for the Research Synthesis node's routing."""

    def __init__(self, run: ResearchSynthesisRun | None) -> None:
        self.run = run
        self.begun: list[UUID] = []
        self.phases: list[tuple[UUID, str]] = []

    def record_phase_changed(self, job_id: UUID, phase: str, **kwargs: object) -> None:
        del kwargs
        self.phases.append((job_id, phase))

    def get_synthesis_run_for_verifier(
        self, job_id: UUID, verifier_run_id: UUID
    ) -> ResearchSynthesisRun | None:
        del job_id
        if self.run is None or self.run.verifier_run_id != verifier_run_id:
            return None
        return self.run

    def build_writer_snapshot(self, job_id: UUID, verifier_run_id: UUID) -> WriterSnapshot:
        del verifier_run_id
        return WriterSnapshot(job_id=job_id, brief=ResearchBrief(question="Q", brief_text="B"))

    def begin_synthesis_run(
        self, job_id: UUID, prompt: list[dict[str, str]], verifier_run_id: UUID
    ) -> tuple[UUID, int]:
        del job_id, prompt
        self.begun.append(verifier_run_id)
        return uuid4(), len(self.begun)


def _synthesis_state(verifier_run_id: UUID) -> dict[str, Any]:
    state = cast(dict[str, Any], initial_research_state(job_id=str(uuid4()), brief_id=str(uuid4())))
    state.update({"last_verifier_run_id": str(verifier_run_id), "planner_messages": []})
    return state


def _stored_synthesis(
    verifier_run_id: UUID, decision: Literal["ready", "needs_research"]
) -> ResearchSynthesisRun:
    return ResearchSynthesisRun(
        synthesis_run_id=uuid4(),
        job_id=uuid4(),
        version=1,
        verifier_run_id=verifier_run_id,
        decision=decision,
        synthesis="目前只能确认来源存在分歧。",
        reason="分歧阻断核心问题。" if decision == "needs_research" else None,
        evidence_needed="同口径的一手统计。" if decision == "needs_research" else None,
    )


def test_synthesis_evidence_request_goes_to_the_verifier_not_the_planner() -> None:
    """Major-gap admission belongs to the Research Verifier alone.

    Appending the request straight onto the closed Planner thread would let the synthesis
    spend research budget on a gap nobody confirmed blocks the Brief.
    """
    verifier_run_id = uuid4()
    repository = _SynthesisRepository(_stored_synthesis(verifier_run_id, "needs_research"))
    services = ResearchGraphServices(
        repository=cast(Any, repository),
        planner=cast(Any, object()),
        worker=cast(Any, object()),
        verifier=cast(Any, object()),
        synthesis=cast(Any, object()),
    )

    result = _synthesis_node(services)(cast(Any, _synthesis_state(verifier_run_id)))

    assert result["route"] == "verifier"
    assert result["verifier_trigger"] == "synthesis_gap"
    assert "planner_messages" not in result


def test_synthesis_reruns_after_new_research_instead_of_reusing_a_stale_run() -> None:
    stale = _stored_synthesis(uuid4(), "needs_research")
    repository = _SynthesisRepository(stale)
    fresh_verifier_run = uuid4()
    synthesis = SimpleNamespace(
        synthesize=lambda snapshot: SimpleNamespace(
            raw_output="{}",
            result=ResearchSynthesisResult(
                decision="ready",
                synthesis="材料支持一项认识。",
            ),
        )
    )
    repository.complete_synthesis_run = lambda run_id, stored, raw: None  # type: ignore[attr-defined]
    services = ResearchGraphServices(
        repository=cast(Any, repository),
        planner=cast(Any, object()),
        worker=cast(Any, object()),
        verifier=cast(Any, object()),
        synthesis=cast(Any, synthesis),
    )

    result = _synthesis_node(services)(cast(Any, _synthesis_state(fresh_verifier_run)))

    assert repository.begun == [fresh_verifier_run]
    assert repository.phases[-1][1] == "synthesizing"
    assert result["route"] == "writer"


def test_report_nodes_announce_each_external_stage_before_invoking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local follower can only display phases that nodes persist before blocking."""

    job_id = uuid4()
    verifier_run_id = uuid4()
    report_id = uuid4()
    synthesis = _stored_synthesis(verifier_run_id, "ready")
    snapshot = WriterSnapshot(job_id=job_id, brief=ResearchBrief(question="Q", brief_text="B"))
    phases: list[tuple[str, int | None]] = []

    def record_phase_changed(_job_id: UUID, phase: str, **kwargs: object) -> None:
        assert _job_id == job_id
        revision = kwargs.get("revision")
        phases.append((phase, None if revision is None else int(cast(int, revision))))

    state = cast(Any, _synthesis_state(verifier_run_id))
    state["job_id"] = str(job_id)

    writer_repository = SimpleNamespace(
        get_latest_synthesis_run=lambda _job_id: synthesis,
        build_writer_snapshot=lambda _job_id, _verifier_run_id: snapshot,
        get_markdown_revision=lambda _job_id: None,
        begin_markdown_revision=lambda *_args, **_kwargs: (report_id, 1),
        record_phase_changed=record_phase_changed,
    )
    monkeypatch.setattr(
        "prospector.agents.prompts.report_writer.report_writer_messages", lambda *_args: []
    )
    writer = SimpleNamespace(write=lambda *_args: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        research_graph._writer_node(
            ResearchGraphServices(
                repository=cast(Any, writer_repository),
                planner=cast(Any, object()),
                worker=cast(Any, object()),
                verifier=cast(Any, object()),
                writer=writer,
            )
        )(state)
    assert phases == [("writing", 1)]

    stored = {"report_id": report_id, "revision": 1, "markdown": "# 标题\n\n正文。"}
    attribution_repository = SimpleNamespace(
        get_markdown_revision=lambda *_args, **_kwargs: stored,
        get_attribution_run=lambda *_args: None,
        build_writer_snapshot=lambda _job_id, _verifier_run_id: snapshot,
        record_phase_changed=record_phase_changed,
    )
    monkeypatch.setattr(
        research_graph,
        "run_attribution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop")),
    )
    with pytest.raises(RuntimeError, match="stop"):
        research_graph._attribution_node(
            ResearchGraphServices(
                repository=cast(Any, attribution_repository),
                planner=cast(Any, object()),
                worker=cast(Any, object()),
                verifier=cast(Any, object()),
                attribution=cast(Any, object()),
            )
        )(state)
    assert phases[-1] == ("attributing", 1)

    review_repository = SimpleNamespace(
        get_markdown_revision=lambda _job_id: stored,
        get_latest_synthesis_run=lambda _job_id: synthesis,
        get_attribution_run=lambda *_args: object(),
        get_report_review_run=lambda *_args: None,
        build_writer_snapshot=lambda _job_id, _verifier_run_id: snapshot,
        record_phase_changed=record_phase_changed,
    )
    review = SimpleNamespace(review=lambda *_args: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        research_graph._review_node(
            ResearchGraphServices(
                repository=cast(Any, review_repository),
                planner=cast(Any, object()),
                worker=cast(Any, object()),
                verifier=cast(Any, object()),
                review=cast(Any, review),
            )
        )(state)
    assert phases[-1] == ("reviewing", 1)

    render_repository = SimpleNamespace(
        get_markdown_revision=lambda _job_id: {**stored, "report_status": "verified"},
        get_attribution_run=lambda *_args: object(),
        get_report_review_run=lambda *_args: object(),
        build_writer_snapshot=lambda _job_id, _verifier_run_id: snapshot,
        get_readthrough=lambda *_args: None,
        record_phase_changed=record_phase_changed,
    )
    monkeypatch.setattr(
        research_graph,
        "render_final_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop")),
    )
    with pytest.raises(RuntimeError, match="stop"):
        research_graph._render_node(
            ResearchGraphServices(
                repository=cast(Any, render_repository),
                planner=cast(Any, object()),
                worker=cast(Any, object()),
                verifier=cast(Any, object()),
                object_store=cast(Any, object()),
            )
        )(state)
    assert phases[-1] == ("rendering", 1)


class _QueuedCompletions:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.outputs.pop(0)))]
        )


def test_whole_report_review_sees_only_the_adopted_synthesis() -> None:
    snapshot = _snapshot("出货量下降 12%。", "同口径数据显示出货量下降 12%。")
    synthesis = ResearchSynthesisRun(
        synthesis_run_id=uuid4(),
        job_id=snapshot.job_id,
        version=1,
        decision="ready",
        synthesis="同口径数据共同显示出货量下降。",
        raw_output={"review_prompt": "AUDIT_MATERIAL_MUST_NOT_REACH_REVIEW"},
    )
    report_id = uuid4()
    markdown = "# 报告\n\n出货量下降，因此采购正在推迟。"
    body_block_id = parse_markdown(markdown)[1].block_id
    attribution = AttributionRun(
        attribution_run_id=uuid4(),
        report_id=report_id,
        revision=1,
        marker_lexicon_version="v3",
    )
    output = json.dumps(
        {"blocking_findings": [], "key_block_ids": [body_block_id], "audit_notes": []},
        ensure_ascii=False,
    )
    completions = _QueuedCompletions([output])
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = OpenAIReportReview(client=cast(Any, client), model="qwen-test").review(
        report_id, 1, markdown, synthesis, attribution, snapshot
    )
    payload = json.loads(result.full_prompt[1]["content"])

    assert payload["synthesis"] == {
        "decision": "ready",
        "synthesis": "同口径数据共同显示出货量下降。",
        "reason": None,
        "evidence_needed": None,
    }
    assert "AUDIT_MATERIAL_MUST_NOT_REACH_REVIEW" not in result.full_prompt[1]["content"]
    assert "主要叙述对象" in result.full_prompt[0]["content"]
    assert "brief_response" in result.full_prompt[0]["content"]


def test_synthesis_runs_an_independent_review_and_uses_its_revision() -> None:
    snapshot = _snapshot("出货量下降。", "同口径数据显示出货量下降 12%。")
    assertion_id = str(snapshot.evidence_cards[0].assertion_id)
    draft = {
        "decision": "ready",
        "synthesis": "材料一称出货量下降。",
        "assertion_ids": [assertion_id],
        "material_conflict_keys": [],
    }
    revised = {
        "decision": "ready",
        "synthesis": "同口径数据共同显示出货量下降，变化并非仅来自来源表述差异。",
        "assertion_ids": [assertion_id],
        "material_conflict_keys": [],
    }
    review = {
        "defects": [
            {
                "kind": "missing_relationships",
                "reason": "初稿只转述了材料，没有形成分析。",
            }
        ],
        "reason": "初稿只转述了材料，没有形成分析。",
        "revised_result": revised,
    }
    completions = _QueuedCompletions(
        [json.dumps(draft, ensure_ascii=False), json.dumps(review, ensure_ascii=False)]
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = OpenAIResearchSynthesis(client=cast(Any, client), model="qwen-test").synthesize(
        snapshot
    )

    assert result.result.synthesis == revised["synthesis"]
    assert len(completions.calls) == 2
    assert result.raw_output["draft"]
    assert result.raw_output["review"]
    assert result.raw_output["review_prompt"]


def test_attribution_selection_prompt_omits_excerpt_text() -> None:
    snapshot = _snapshot("出货量下降 12%。", "这段原文不得进入选择阶段。")
    markdown = "出货量下降 12%。"
    from prospector.agents.report_attribution import prepare_attribution_plan, selection_messages

    plan = prepare_attribution_plan(markdown, snapshot)
    prompt = selection_messages(plan, plan.batches[0], snapshot)
    serialized = prompt[1]["content"]
    assert "这段原文不得进入选择阶段。" not in serialized
    # The catalog names material by short ref; a UUID must never reach the model.
    assert str(snapshot.evidence_cards[0].assertion_id) not in serialized
    assert '"ref": "a1"' in serialized
    assert "excerpts" not in serialized


class _MemoryAttributionStore:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.batches: dict[int, dict[str, Any]] = {}
        self.summary: dict[str, Any] = {}
        self.completed: object | None = None
        self.errors: list[str] = []

    def begin_attribution_run(self, report_id: UUID, revision: int, plan: dict[str, Any]) -> UUID:
        del report_id, revision, plan
        return self.run_id

    def list_attribution_batches(self, run_id: UUID) -> list[dict[str, Any]]:
        del run_id
        return [dict(row) for _, row in sorted(self.batches.items())]

    def begin_attribution_batch(
        self,
        run_id: UUID,
        batch_index: int,
        *,
        block_ids: Sequence[str],
        candidate_refs: Sequence[str],
        selection_prompt: object,
    ) -> dict[str, Any]:
        del run_id
        existing = self.batches.get(batch_index)
        if existing is not None and existing["status"] in {"selected", "completed"}:
            return dict(existing)
        if (
            existing is not None
            and existing["status"] == "failed"
            and existing.get("selection_result")
        ):
            return dict(existing)
        self.batches[batch_index] = {
            "batch_index": batch_index,
            "block_ids": list(block_ids),
            "candidate_refs": list(candidate_refs),
            "selection_prompt": selection_prompt,
            "status": "prompted",
        }
        return dict(self.batches[batch_index])

    def save_attribution_batch_selection(
        self,
        run_id: UUID,
        batch_index: int,
        *,
        raw_output: object,
        result: dict[str, Any],
        verify_prompt: object,
    ) -> None:
        del run_id
        self.batches[batch_index].update(
            {
                "selection_raw": raw_output,
                "selection_result": result,
                "verify_prompt": verify_prompt,
                "status": "selected",
            }
        )

    def complete_attribution_batch(
        self,
        run_id: UUID,
        batch_index: int,
        *,
        raw_output: object,
        result: dict[str, Any],
    ) -> None:
        del run_id
        self.batches[batch_index].update(
            {"verify_raw": raw_output, "verify_result": result, "status": "completed"}
        )

    def fail_attribution_batch(
        self, run_id: UUID, batch_index: int, *, raw_output: object, error: str
    ) -> None:
        del run_id, raw_output
        self.batches[batch_index]["status"] = "failed"
        self.batches[batch_index]["contract_error"] = error

    def get_attribution_attempt(self, report_id: UUID, revision: int) -> dict[str, Any] | None:
        del report_id, revision
        return {"id": self.run_id, "raw_output": self.summary}

    def begin_attribution_summary(self, run_id: UUID, prompt: object) -> dict[str, Any]:
        del run_id, prompt
        return dict(self.summary)

    def complete_attribution_summary(
        self, run_id: UUID, *, raw_output: object, result: dict[str, Any]
    ) -> None:
        del run_id
        self.summary = {"summary_raw": raw_output, "summary_result": result}

    def fail_attribution_run(self, run_id: UUID, *, raw_output: object, error: str) -> None:
        del run_id, raw_output
        self.errors.append(error)

    def complete_attribution_run(self, run: AttributionRun) -> None:
        self.completed = run


class _CoveringAttributionModel:
    def __init__(self, snapshot: WriterSnapshot) -> None:
        self.snapshot = snapshot
        self.select_calls = 0
        self.verify_prompts: list[list[str]] = []
        self.fail_next_verify = False

    def select_materials(
        self, prompt: list[dict[str, str]]
    ) -> tuple[AttributionBatchSelection, str]:
        del prompt
        self.select_calls += 1
        return (
            AttributionBatchSelection(assertion_refs=["a1"]),
            "{}",
        )

    def verify_batch(
        self, prompt: list[dict[str, str]]
    ) -> tuple[AttributionBatchVerification, str]:
        payload = json.loads(prompt[1]["content"])
        self.verify_prompts.append([item["candidate_ref"] for item in payload["candidates"]])
        if len(self.verify_prompts) == 2 and self.fail_next_verify:
            self.fail_next_verify = False
            raise ClaimAttributionOutputError("batch verify failed", "{}")
        claims = [
            _model_claim(
                claim_ref=item["candidate_ref"],
                block_id=item["block_id"],
                start_offset=item["start_offset"],
                end_offset=item["end_offset"],
                status="verified",
                candidate_refs=[item["candidate_ref"]],
                excerpt_refs=["a1e1"],
                assertion_refs=["a1"],
            )
            for item in payload["candidates"]
        ]
        return AttributionBatchVerification.model_validate({"claims": claims}), "{}"

    def summarize(self, prompt: list[dict[str, str]]) -> tuple[AttributionSummary, str]:
        del prompt
        return AttributionSummary(), "{}"


def test_completed_attribution_batches_survive_a_later_batch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from prospector.agents.report_attribution import run_attribution
    from prospector.deterministic import markdown_report as markdown_mod

    real_partition = markdown_mod.partition_attribution_batches
    monkeypatch.setattr(
        "prospector.agents.report_attribution.partition_attribution_batches",
        lambda blocks, candidates, char_budget=80: real_partition(
            blocks, candidates, char_budget=80
        ),
    )
    snapshot = _snapshot("出货量下降 12%。", "出货量下降 12%。")
    markdown = "第一段数字是 11%。\n\n第二段数字是 22%。\n\n第三段数字是 33%。"
    store = _MemoryAttributionStore()
    model = _CoveringAttributionModel(snapshot)
    model.fail_next_verify = True
    with pytest.raises(ClaimAttributionOutputError, match="batch verify failed"):
        run_attribution(model, uuid4(), 1, markdown, snapshot, store=store)
    assert store.errors
    completed_before = [
        row["batch_index"]
        for row in store.list_attribution_batches(store.run_id)
        if row["status"] == "completed"
    ]
    assert completed_before
    first_group = model.verify_prompts[0]
    result = run_attribution(model, uuid4(), 1, markdown, snapshot, store=store)
    assert result.run.claims
    assert model.verify_prompts.count(first_group) == 1
    assert store.completed is result.run
