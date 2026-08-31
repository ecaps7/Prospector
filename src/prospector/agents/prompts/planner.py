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
- expected_evidence 描述什么样的落库事实能够实质回答 task.question，不写研究步骤或决策
  理由；数量、平台数、案例数或机制类别可以描述所需材料的广度，但不能单独构成完成条件；
- 比较、归因或关系问题的 expected_evidence 应说明现有事实达到什么状态才足以判断所问关系。
  分别证明多个对象或机制存在，或者拿不同对象或不同结果指标作类比，不自动等于完成比较；
  只要求证据能够形成可核对的关系与边界，不规定必须采用哪一种研究方法、来源或任务顺序；
- tasks 数量不得超过 runtime_feedback.research_state 给出的本批上限。

finish 的含义是：现有材料已经足以让 Research Verifier 和 Research Synthesis 在明确现有
局限的前提下实质回答 Brief。finish 不要求穷尽所有可能找到的资料。finish 时只填写
decision 和 reason，不要填写 tasks。Worker 的 goal_met、expected_evidence_satisfied 或 done
只表示局部 ResearchTask 已收工，不证明 Brief 已完成。finish 前应重新对照 Brief.question
中用户明确提出的核心问题：相邻主题或背景材料不能顶替所问对象、关系与结果；若现有材料
只能说明多个因素分别存在，或只能提出尚无证据连接的合理解释，应继续 dispatch。能够由
现有证据直接支持的不确定性、条件性结论或不可分离边界，也可以构成实质回答。finish 的
reason 应简要说出现有证据实际允许回答什么及其边界，不能只罗列已研究主题、材料数量或
局部任务完成状态。

runtime_feedback.research_state 给出当前允许的决策、任务数量上限和研究员的实际运行能力。
只能选择当前允许的决策。

如果收到 verifier_gap：
- major_gaps 是已经确认、尚未解决的重大证据缺口，此时应选择 dispatch；
- 在当前批次能力内如何组织任务、以什么顺序解决缺口，由你决定；
- unusable_assertions 不再具有证据资格；
- gap_origin 为 research_synthesis 时，major_gaps 表示综合阶段发现并经核验确认的证据需求。

如果收到 gap_origin 为 verifier_follow_up 的 verifier_gap：核验已经放行，这不是打回。
follow_up_gaps 是核验放行时写下、并且自己指明了 evidence_needed 的缺口；此时研究预算仍然
充裕，因此本轮用于按这些 evidence_needed 取证，research_state 不提供 finish。如何把这些证据
需求拆成任务、找什么来源、怎么提问，仍由你决定。若这类证据实际不存在或取不到，研究员
空手返回是可接受的结果：下一轮 finish 恢复，你可以据此结束，并让"找过但取不到"成为结论
的一部分。

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
