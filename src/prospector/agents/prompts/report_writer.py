# ruff: noqa: E501
"""Prompts for Markdown-only report writing."""

from __future__ import annotations

import json
from typing import Any

from prospector.agents.research_synthesis import (
    global_minor_gaps,
    source_caveats_by_assertion,
    synthesis_context_payload,
)
from prospector.deterministic.excerpt_text import clip_excerpt_text, writer_excerpt_limit
from prospector.deterministic.markdown_report import parse_markdown
from prospector.deterministic.model_refs import ResearchModelRefs
from prospector.schemas.claims import (
    AttributionRun,
    ReportReviewRun,
    core_attribution_finding_ids,
)
from prospector.schemas.report import (
    ReportRevisionPatch,
    ResearchSynthesisRun,
    WriterSnapshot,
)


def material_payload(snapshot: WriterSnapshot) -> dict[str, Any]:
    excerpt_count = sum(len(card.excerpts) for card in snapshot.evidence_cards)
    limit = writer_excerpt_limit(excerpt_count)
    source_caveats = source_caveats_by_assertion(snapshot)
    findings: list[dict[str, Any]] = []
    for card in snapshot.evidence_cards:
        finding = {
            "assertion_id": str(card.assertion_id),
            "task_id": str(card.task_id),
            "statement": card.assertion_statement,
            "excerpts": [
                {
                    "excerpt_id": str(excerpt.excerpt_id),
                    "text": clip_excerpt_text(excerpt.text, limit),
                    "source": excerpt.source.model_dump(mode="json"),
                }
                for excerpt in card.excerpts
            ],
        }
        caveat = source_caveats.get(str(card.assertion_id))
        if caveat is not None:
            finding["source_caveat"] = caveat
        findings.append(finding)
    payload = {
        "brief": snapshot.brief.model_dump(mode="json"),
        "research_synthesis": None,
        "findings": findings,
        "conflicts": snapshot.conflicts,
        "minor_gaps": global_minor_gaps(snapshot),
    }
    return ResearchModelRefs.from_writer_snapshot(snapshot).alias_payload(payload)


def report_writer_messages(
    snapshot: WriterSnapshot, synthesis: ResearchSynthesisRun
) -> list[dict[str, str]]:
    payload = material_payload(snapshot)
    payload["research_synthesis"] = synthesis_context_payload(synthesis)
    system = """你是深度研究报告的作者。基于 Brief、Research Synthesis 和输入的研究材料，写一篇完整、连贯的深度研究报告。

报告应实质回应 Brief，说明这些材料合起来意味着什么，而不是逐条转述研究材料。Research Synthesis 是分析底稿，不是提纲或标准答案；如何选择材料、组织结构、安排详略和表达判断由你决定。

正文直接讲 Brief 所问的对象、变化和解释。不要把“材料”“证据”“本报告”作为叙述主体，也不要用“材料称”“材料显示”“材料能够证明”代替具体事实、来源归属或你的分析。需要来源归属时直接写明来源主体。

深度来自解释关系、机制和转折，不来自使用更多 finding。不要为了展示覆盖面而依次展开 ResearchTask、厂商、产品或案例；只使用服务于核心回答的内容，也不要在开头、阶段总结和结论中反复表达同一套判断。

`source_caveat`、`conflicts` 和 `minor_gaps` 与 finding 一样按需选取，不必逐条交代。但用到其中一条时，它属于它所改变的那个判断：来源性质和适用范围写进使用该 finding 的表述，争议和缺口写在讨论该问题的地方。不要把它们从正文抽出来集中安置，也不要在文末统一交代研究本身。

不得加入材料未支持的具体事实，不得改变数字、时间、主体、范围、口径或来源归属，也不得隐藏会实质改变认识的冲突。材料不足以支持确定判断时，把判断写到它实际成立的条件和范围为止。遵守用户明确限制。

research_synthesis.decision 为 needs_research 时，按照当前能够支持的有限分析完成报告，在相关判断处写明它成立的条件，不要因此把整篇报告写成模糊的保守表述。

只输出完整的 GitHub Flavored Markdown，不要输出 JSON、写作说明、自我检查清单或自行生成的引用脚注，也不要用代码围栏包裹整篇报告。"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def report_writer_revision_messages(
    snapshot: WriterSnapshot,
    synthesis: ResearchSynthesisRun,
    markdown: str,
    attribution: AttributionRun,
    review: ReportReviewRun,
    readthrough: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    blocks = parse_markdown(markdown)
    block_text = {block.block_id: block.text for block in blocks}
    core_finding_ids = core_attribution_finding_ids(attribution, review)
    # Findings reach the Writer as concrete problems only.  The internal finding kinds are
    # the check taxonomy; handing them over turns revision into rule-guessing, and a model
    # that guesses at an unseen rule hedges rather than fixes.
    feedback = {
        "attribution_failures": [
            {"block_id": item.block_id, "text": item.text, "reason": item.reason}
            for item in attribution.blocking_findings
            if item.finding_id in core_finding_ids
        ],
        "whole_report_blockers": [
            {
                "affected_blocks": [
                    {"block_id": block_id, "text": block_text[block_id]}
                    for block_id in item.block_ids
                ],
                "reason": item.reason,
            }
            for item in review.blocking_findings
        ],
        # Read-through problems are about the prose, not the facts.  They arrive in the
        # same list because the repair is the same act -- replace a range of blocks --
        # and separating them would only invite the model to treat one kind as optional.
        "readthrough_problems": [
            {
                "affected_blocks": [
                    {"block_id": block_id, "text": block_text.get(block_id, "")}
                    for block_id in item.get("block_ids", [])
                ],
                "reason": item.get("reason"),
            }
            for item in (readthrough or {}).get("findings", [])
        ],
    }
    system = """你负责修订一份已经定稿的深度研究报告。

代码给出当前正文的分块和本轮必须解决的问题。你不重写整篇报告，而是提交若干段替换：每一项给出要替换的块区间（start_block_id 到 end_block_id）和这段的新 Markdown。

**替换范围由你决定，而且应当覆盖真正受影响的范围。** 如果改准一句话会让相邻段落的指代（“这一转变”）、连接词（“因此”“相比之下”）、小标题或开头的总述讲不通，就把它们一起放进同一个替换区间改掉。宁可多带一两段，也不要留下读不通的接缝。没有被列进替换的段落会原样保留，你无法间接改动它们。

markdown 留空表示删除该区间。合格修复是改准、换成有出处的说法，或连同依赖它的推理一起删除；不得仅把具体事实改写成笼统表述来隐藏问题，也不得新增材料未支持的事实。

新写的内容要与原文的语气、结构层级和详略保持一致；替换区间里的 Markdown 必须自带它需要的标题层级和列表标记。

最终只输出符合 output_schema 的单个 JSON 对象。"""
    payload = {
        "blocks": [
            {"block_id": block.block_id, "kind": block.kind, "text": block.text} for block in blocks
        ],
        "feedback": feedback,
        "research_synthesis": synthesis_context_payload(synthesis),
        "research_material": material_payload(snapshot),
        "output_schema": ReportRevisionPatch.model_json_schema(),
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
