# ruff: noqa: E501
"""Research Synthesis: continuous analysis before prose composition."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol, cast

from openai import OpenAI
from pydantic import ValidationError

from prospector.agents.llm import get_openai_client, strong_model, thinking_extra_body
from prospector.deterministic.excerpt_text import clip_excerpt_text, writer_excerpt_limit
from prospector.schemas.report import (
    ResearchSynthesisResult,
    ResearchSynthesisReview,
    ResearchSynthesisRun,
    WriterSnapshot,
)

SYNTHESIS_ATTEMPTS = 2


class ResearchSynthesisOutputError(ValueError):
    def __init__(self, message: str, raw_output: object) -> None:
        super().__init__(message)
        self.raw_output = raw_output


@dataclass(frozen=True, slots=True)
class ResearchSynthesisResultEnvelope:
    full_prompt: list[dict[str, str]]
    raw_output: dict[str, Any]
    result: ResearchSynthesisResult


class ResearchSynthesisModel(Protocol):
    def synthesize(self, snapshot: WriterSnapshot) -> ResearchSynthesisResultEnvelope: ...


def synthesis_context_payload(run: ResearchSynthesisRun) -> dict[str, str | None]:
    """Project the adopted analysis without leaking its persisted audit record."""

    return {
        "decision": run.decision,
        "synthesis": run.synthesis,
        "reason": run.reason,
        "evidence_needed": run.evidence_needed,
    }


def synthesis_result_payload(result: ResearchSynthesisResult) -> dict[str, Any]:
    """Serialize only the model-facing result, even when passed a persisted subclass."""

    return {
        "decision": result.decision,
        "synthesis": result.synthesis,
        "assertion_ids": [str(value) for value in result.assertion_ids],
        "material_conflict_keys": result.material_conflict_keys,
        "reason": result.reason,
        "evidence_needed": result.evidence_needed,
    }


def source_caveats_by_assertion(snapshot: WriterSnapshot) -> dict[str, str]:
    """Attach each source warning to the finding whose wording it constrains."""

    caveats: dict[str, str] = {}
    for gap in snapshot.minor_gaps:
        if gap.get("kind") != "source_credibility":
            continue
        description = str(gap.get("description") or "").strip()
        if not description:
            continue
        for assertion_id in gap.get("related_assertion_ids") or []:
            caveats.setdefault(str(assertion_id), description)
    return caveats


def global_minor_gaps(snapshot: WriterSnapshot) -> list[dict[str, Any]]:
    """Keep non-source gaps global; source caveats are represented on their findings."""

    return [gap for gap in snapshot.minor_gaps if gap.get("kind") != "source_credibility"]


def synthesis_material_payload(snapshot: WriterSnapshot) -> dict[str, Any]:
    """Present the same evidence pool by research question instead of as one flat list."""

    excerpt_count = sum(len(card.excerpts) for card in snapshot.evidence_cards)
    limit = writer_excerpt_limit(excerpt_count)
    task_questions: dict[str, str | None] = {}
    task_details: dict[str, dict[str, Any]] = {}
    task_order: list[str] = []
    source_caveats = source_caveats_by_assertion(snapshot)
    for plan in snapshot.final_plan_summary:
        for task in plan.get("tasks", []):
            task_id = str(task["id"])
            task_questions.setdefault(task_id, task.get("question"))
    for card in snapshot.evidence_cards:
        task_id = str(card.task_id)
        if task_id not in task_details:
            task_order.append(task_id)
            task_details[task_id] = {
                "task_id": task_id,
                "question": task_questions.get(task_id),
                "findings": [],
            }
        finding = {
            "assertion_id": str(card.assertion_id),
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
        task_details[task_id]["findings"].append(finding)
    return {
        "brief": snapshot.brief.model_dump(mode="json"),
        "research_tasks": [task_details[task_id] for task_id in task_order],
        "conflicts": snapshot.conflicts,
        "minor_gaps": global_minor_gaps(snapshot),
    }


def research_synthesis_messages(snapshot: WriterSnapshot) -> list[dict[str, str]]:
    schema = json.dumps(ResearchSynthesisResult.model_json_schema(), ensure_ascii=False)
    material = json.dumps(synthesis_material_payload(snapshot), ensure_ascii=False)
    system = """你是研究综合者。通读 Brief 和输入中的可用研究材料，形成一份连贯的分析，说明这些材料合起来意味着什么。

分析应实质回应 Brief，而不是汇总材料或反复讨论“材料能证明什么”。证据能够支持明确认识时直接说清楚；边界、冲突或无法确定之处会改变回答时，再说明其影响。不得加入材料未支持的信息，也不得把带有来源归属、条件或不确定性的内容改写成无条件事实。

这是一份供 Writer 使用的分析底稿，不是文章提纲、标准答案或压缩版报告。ResearchTask 是材料采集路线，不是分析结构；不要依次复述各任务结果。只保留支撑核心解释所需的事实，不承担完整时间线、厂商名单或案例目录；Writer 仍会收到全部可用材料。没有进入底稿的可用材料仍然是合格材料。如何选择材料、组织分析和表达认识由你决定。

现有材料足以实质回应 Brief 时选择 ready。只有缺失证据导致无法实质回应 Brief 时才选择 needs_research；此时仍应写出当前材料能够支持的有限分析，并用 reason 说明为什么无法实质回应 Brief，用 evidence_needed 说明还需要什么证据。

assertion_ids 和 material_conflict_keys 只记录分析实际依据的材料，不要为了覆盖输入而罗列。

最终只输出符合给定 JSON Schema 的单个 JSON 对象。"""
    user = f"""请综合下面的研究材料。

JSON Schema：
{schema}

研究材料：
{material}"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def research_synthesis_review_messages(
    snapshot: WriterSnapshot, draft: ResearchSynthesisResult
) -> list[dict[str, str]]:
    schema = json.dumps(ResearchSynthesisReview.model_json_schema(), ensure_ascii=False)
    usable_task_ids = {str(card.task_id) for card in snapshot.evidence_cards}
    research_questions = [
        str(task["question"])
        for plan in snapshot.final_plan_summary
        for task in plan.get("tasks", [])
        if str(task.get("id")) in usable_task_ids and task.get("question")
    ]
    payload = {
        "json_schema": json.loads(schema),
        "brief": snapshot.brief.model_dump(mode="json"),
        "draft": synthesis_result_payload(draft),
        "research_shape": {
            "research_questions": list(dict.fromkeys(research_questions)),
            "usable_assertion_count": len(snapshot.usable_assertion_ids),
            "draft_assertion_count": len(set(draft.assertion_ids)),
        },
        "conflicts": snapshot.conflicts,
        "minor_gaps": global_minor_gaps(snapshot),
    }
    system = """你独立检查一份 Research Synthesis 初稿。检查只基于 Brief、初稿、研究任务问题与数量信息，以及已确认的冲突和缺口；不要重新做材料覆盖核对。

你必须分别判断下面四件事，只有确实成立时才写入对应缺陷，不要用一个笼统判断代替：

1. 初稿是否实质回答了 Brief 的核心问题。没有回答则写入 brief_not_answered。
2. 是否解释了材料之间的关系、变化机制或转折，而不是把事实并列。没有解释则写入 missing_relationships。
3. 是否完成了材料取舍。把可用材料压成完整时间线、产品/厂商/案例目录，或沿 ResearchTask 复述采集结果，写入 evidence_catalog；没有取舍、为了覆盖输入而堆材料，写入 missing_selection。`draft_assertion_count` 和 `usable_assertion_count` 只是背景，本身不构成缺陷。
4. 是否把有时间或范围边界的发现写成长期结局，或把有限认识写成无条件事实。若是，写入 unsupported_overreach。

有任一实质缺陷时必须给出完整 revised_result。修订仍是分析底稿，不是文章提纲，并遵守与初稿相同的 ready / needs_research 语义。候选方向未覆盖或还能找到更多资料，本身不构成 needs_research。

不要因为文风、段落安排、结论数量、篇幅、Assertion 数量或存在另一种同样合理的写法而改写，也不要规定文章结构。详细本身不是问题，关键在于事实是否服务于认识。defects 为空表示通过；是否采用初稿由代码根据缺陷列表计算，不要输出 decision 字段。

最终只输出符合给定 JSON Schema 的单个 JSON 对象。"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


class OpenAIResearchSynthesis:
    def __init__(self, client: OpenAI | None = None, model: str | None = None) -> None:
        self.client = client or get_openai_client()
        self.model = model or strong_model()

    def synthesize(self, snapshot: WriterSnapshot) -> ResearchSynthesisResultEnvelope:
        draft_prompt = research_synthesis_messages(snapshot)
        conflicts = {str(item.get("conflict_key")) for item in snapshot.conflicts}
        draft, draft_raw = self._complete_result(draft_prompt, snapshot, conflicts)
        review_prompt = research_synthesis_review_messages(snapshot, draft)
        review, review_raw = self._complete_review(review_prompt, snapshot, conflicts)
        final = (
            draft if not review.defects else cast(ResearchSynthesisResult, review.revised_result)
        )
        return ResearchSynthesisResultEnvelope(
            full_prompt=draft_prompt,
            raw_output={
                "draft": draft_raw,
                "review_prompt": review_prompt,
                "review": review_raw,
            },
            result=final,
        )

    def _request(self, prompt: list[dict[str, str]], *, temperature: float) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=cast(Any, prompt),
            temperature=temperature,
            extra_body=thinking_extra_body(self.model),
        )
        return response.choices[0].message.content or ""

    @staticmethod
    def _validate_references(
        result: ResearchSynthesisResult, snapshot: WriterSnapshot, conflicts: set[str]
    ) -> None:
        if set(result.assertion_ids) - snapshot.usable_assertion_ids:
            raise ValueError("Synthesis referenced unusable Assertion")
        if set(result.material_conflict_keys) - conflicts:
            raise ValueError("Synthesis referenced unknown conflict")

    def _complete_result(
        self,
        prompt: list[dict[str, str]],
        snapshot: WriterSnapshot,
        conflicts: set[str],
    ) -> tuple[ResearchSynthesisResult, str]:
        last: ResearchSynthesisOutputError | None = None
        for _ in range(SYNTHESIS_ATTEMPTS):
            raw = self._request(prompt, temperature=0.2)
            try:
                result = ResearchSynthesisResult.model_validate_json(raw)
                self._validate_references(result, snapshot, conflicts)
            except (ValidationError, ValueError) as exc:
                last = ResearchSynthesisOutputError(
                    f"invalid Research Synthesis output: {exc}", raw
                )
                continue
            return result, raw
        raise cast(ResearchSynthesisOutputError, last)

    def _complete_review(
        self,
        prompt: list[dict[str, str]],
        snapshot: WriterSnapshot,
        conflicts: set[str],
    ) -> tuple[ResearchSynthesisReview, str]:
        last: ResearchSynthesisOutputError | None = None
        for _ in range(SYNTHESIS_ATTEMPTS):
            raw = self._request(prompt, temperature=0.1)
            try:
                review = ResearchSynthesisReview.model_validate_json(raw)
                if review.revised_result is not None:
                    self._validate_references(review.revised_result, snapshot, conflicts)
            except (ValidationError, ValueError) as exc:
                last = ResearchSynthesisOutputError(
                    f"invalid Research Synthesis review output: {exc}", raw
                )
                continue
            return review, raw
        raise cast(ResearchSynthesisOutputError, last)
