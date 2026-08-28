"""Planner prompt: research judgment within runtime limits."""

from __future__ import annotations

import json
from datetime import date

from prospector.schemas.brief import ResearchBrief
from prospector.schemas.decisions import PlannerDecision


def planner_system_prompt(*, today: str | None = None) -> str:
    schema = json.dumps(PlannerDecision.model_json_schema(), ensure_ascii=False)
    return f"""你是 Prospector 的深度研究规划者。今天是 {today or date.today().isoformat()}。

你根据已确认的研究纲要、当前运行状态、研究员结果和核验反馈，决定下一步是派发一批
研究任务（dispatch），还是结束研究并交给核验者（finish）。你不搜索，也不撰写报告。

- dispatch：填写 reason 和至少一个 task；
- finish：只填写 decision 和 reason，不要填写 tasks。

每个任务用 question 告诉一个研究员要解决什么，用 expected_evidence 说明获得什么证据
即可认为完成。宽范围探索、深入追踪、事实核查、冲突裁决或反例搜索等研究策略，应直接
写入 question 和 expected_evidence；任务内容、拆分方式和研究取舍由你判断。

runtime_feedback.research_state 给出当前可用决策、本批最多任务数、研究员可用动作与工具、
研究员轮次和并行工具上限、自动抓取数量，以及是否允许 finish；只能选择当前允许的决策。
verifier_gap 中的 major_gaps 是尚未解决的问题，unusable_assertions 已不具备证据资格。

最终回答只输出符合下面 JSON Schema 的单个 JSON 对象，不加代码围栏或其他文字：
{schema}"""


def _user_constraint_lines(brief: ResearchBrief) -> str:
    """Render the user's own limits as a labelled block, apart from Scope's suggestions.

    The two kinds of information carry different authority — one is binding, the other
    is a menu — so they are presented as two blocks rather than one paragraph the
    Planner has to re-read and re-classify every round.
    """
    constraints = brief.user_constraints
    if constraints.is_empty():
        return """【用户明确要求】
（本次用户没有提出额外限制。）"""

    rows: list[str] = []
    if constraints.time_range:
        rows.append(f"- 时间范围：{constraints.time_range}")
    labelled = (
        ("地域", constraints.regions),
        ("必须比较的对象", constraints.comparison_targets),
        ("来源要求", constraints.source_rules),
        ("排除", constraints.exclusions),
        ("输出要求", constraints.deliverable_rules),
    )
    rows.extend(f"- {label}：{'、'.join(values)}" for label, values in labelled if values)
    body = "\n".join(rows)
    return f"""【用户明确要求 —— 不可协商，违反即为错误】
{body}

以上是用户本人提出的限制，不是可选建议。派发任务时必须遵守：
不要研究被排除的内容，不要越出声明的时间与地域范围，来源要求同样适用于研究员。"""


def planner_brief_message(brief: ResearchBrief) -> str:
    return f"""已确认研究纲要（不可改写）：
<brief>
question: {brief.question}

{_user_constraint_lines(brief)}

【可探索的研究方向 —— 供你取舍，不必全做】
{brief.brief_text}

output_format: {brief.output_format}
language: {brief.language}
effort: {brief.effort}
</brief>"""
