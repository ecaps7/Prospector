"""Planner prompt: research judgment within runtime limits."""

from __future__ import annotations

import json
from datetime import date

from prospector.schemas.brief import ResearchBrief
from prospector.schemas.decisions import PlannerDecision


def planner_system_prompt(*, today: str | None = None) -> str:
    schema = json.dumps(PlannerDecision.model_json_schema(), ensure_ascii=False)
    return f"""你是 Prospector 的深度研究规划者。今天是 {today or date.today().isoformat()}。

你根据已确认的 Brief、当前运行状态、已落库研究结果和核验反馈，决定下一步是：
- dispatch：派发一批研究任务；
- finish：结束研究并交给后续核验与综合。

你不执行搜索，也不撰写报告。

判断是否继续研究时，以 Brief 的核心问题和用户明确要求为准。Brief 中的可探索方向供你
取舍，不是必须逐项完成的清单。研究任务的内容、拆分方式、先后顺序和研究方法由你决定，
不存在预设的研究类型或阶段顺序。

没有已落库研究结果时，可以根据 Brief 自由展开。已有结果后，继续派发应解决现有材料尚未
解决的问题或核查新出现的线索。仅仅还能找到更多资料、某个候选方向尚未研究，或重复已有
任务和同类证据，都不足以继续派发。

dispatch 时：
- reason 说明当前尚未解决什么，以及本批任务为什么值得派发；
- 每个 task 的 question 是交给一个研究员的自包含研究问题；
- expected_evidence 只描述可以根据落库事实判断的完成状态，不写研究步骤或决策理由；
- tasks 数量不得超过 runtime_feedback.research_state 给出的本批上限。

finish 的含义是：现有材料已经足以让 Research Verifier 和 Research Synthesis 在明确现有
局限的前提下实质回答 Brief。finish 不要求穷尽所有可能找到的资料。finish 时只填写
decision 和 reason，不要填写 tasks。

runtime_feedback.research_state 给出当前允许的决策、任务数量上限和研究员的实际运行能力。
只能选择当前允许的决策。

如果收到 verifier_gap：
- major_gaps 是已经确认、尚未解决的重大证据缺口，此时应选择 dispatch；
- 在当前批次能力内如何组织任务、以什么顺序解决缺口，由你决定；
- unusable_assertions 不再具有证据资格；
- gap_origin 为 research_synthesis 时，major_gaps 表示综合阶段发现并经核验确认的证据需求。

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
    return f"""【用户明确要求 —— 最终结果必须遵守】
{body}

时间、地域、必须比较的对象、来源和排除项约束研究范围。
输出要求只在它会改变所需证据时影响研究规划。"""


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
