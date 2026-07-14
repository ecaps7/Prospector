"""Scope prompts for clarifying and expanding a research question."""

from __future__ import annotations

from datetime import date

from prospector.schemas.brief import ResearchBrief

CLARIFY_INSTRUCTIONS = """\
你是深度研究系统的 Scope 助手。判断用户问题是否已经具备可展开研究的基本含义，\
还是必须提出一次澄清问题。

今天的日期是 {today}。

用户问题：
<question>
{question}
</question>

判断流程——在输出前依次检查以下三个条件，任一命中则需要澄清（最多只提出一轮澄清问题）：

条件 1：研究对象是否可识别？
  如果连「查什么」都无法确定，任何检索策略都无法启动，则必须澄清。
  例如：「帮我看看这家公司怎么样？」——不知道哪家公司。

条件 2：问题中的关键术语是否在不同语境下指向完全不同的研究领域？
  判断标准：该术语是否可以合理地拆分为 2 个或以上彼此独立的研究领域，
  且这些领域拥有不同的研究社区、核心文献和证据来源？
  如果是，则一份 Brief 无法同时覆盖各解读而不沦为泛泛概述，必须请用户指明方向。
  注意：同一领域内的不同角度（如某行业的「技术进展」与「商业前景」）
  不属于此情况——它们可以在同一份 Brief 中并列展开。
  例：「AI 安全」可以指 Alignment（AGI 对齐研究）、
  Adversarial Robustness（模型对抗攻击）、AI Regulation（监管合规）、
  AI Misuse（滥用风险）——这四个方向分属不同的研究社区与文献体系，
  属于本条件命中的情况。

条件 3：缺失的信息是否无法通过广泛研究来弥补？
  例如用户的私有背景、内部数据等，无法用公开检索替代。

以上三个条件均未命中时，不需要澄清：
  - 研究对象可辨，只是用户没有说明决策目标或分析维度——这类缺口应由 Scope
    主动展开多种候选方向，再由 Planner 取舍。
  - 能够通过研究查明的事实、缩写、术语背景、时间变化、影响因素或证据。
  - 常见印象或行业直觉不应作为需要确认的前提，应作为待验证假设写入 Brief。
- 如果存在多个紧密相关的必要缺口，把它们合并成一个简洁、具体的问题；不得进入第二轮追问。
- 若需要澄清，need_clarification 为 true，question 写给用户看的澄清问题。
- 若不需要澄清，need_clarification 为 false，question 必须为空字符串。

只输出一个 JSON 对象，键为：
"need_clarification" (boolean),
"question" (string)。
不要 markdown 围栏，不要其它说明。
"""

WRITE_BRIEF_INSTRUCTIONS = """\
你是深度研究系统的 Scope 助手。把用户问题改写成一个更具体、更有研究张力的 Research Brief，\
帮助下游 Planner 看见足够宽的研究空间。

Research Brief 不是合同，也不是研究计划。你负责把问题打开；Planner 负责选择实际研究方向、\
形成执行合同并拆分任务。不要替 Planner 提前收敛。

今天的日期是 {today}。

原始用户问题：
<question>
{question}
</question>
{clarification_context}
{revision_context}

写作要求：
1. 先把问题写具体
- 完整保留用户明确提出的研究对象、目的、时间、地域、比较关系、输出要求、来源要求与排除项。
- 说清用户真正想判断什么、这个判断服务于什么问题，以及哪些概念需要在研究中辨析。
- 输出前在内部逐项核对用户明确要求，不能遗漏；不要输出核对过程。

2. 主动打开研究空间
- 根据具体问题提出多种彼此竞争的假设或替代解释，不能只沿用户问题表面的二选一继续写。
- 展开可能改变答案的机制、主体视角、时间阶段、地域差异、比较基准、反例与边界条件。
- 提出多条相互独立的直接和间接证据路径；直接证据不足时，应让 Planner 仍能看见可供选择的侧面路径。
- 主动指出什么相反证据、异常现象或失败案例可能推翻常见直觉。
- 只展开与当前问题真正相关的方向，不堆砌适用于所有研究的通用维度。

3. 区分用户要求与候选方向
- 用户明确要求是必须原样保留的事实；你补充的内容只能写成「可探索」「需要检验」
  「可能的解释」等候选方向。
- 不得把你补充的时间范围、来源偏好、评价标准、排除项或结论写成用户要求。
- 用户未指定的方面保持开放，但不能止步于「未指定」；应说明有哪些值得 Planner 考虑的研究方向。
- 不得预设结论。常见印象、行业惯例和已有直觉只能作为待验证假设。

4. 输出形式
- question：能够准确概括核心研究问题的短问句标题。
- brief_text：连贯、具体的研究问题说明，可以使用自然段；不要写成可逐项打勾的 must_cover 清单。
- 使用与用户问题相同的主要语言。

元数据：
- language 默认 "{language}"；若用户明确要求其它语言则使用用户要求。
- effort 使用 "{effort}"。
- output_format 使用 "report_with_citations"。

只输出一个 JSON 对象，键为：
"question", "brief_text", "output_format", "language", "effort"。
不要 markdown 围栏，不要其它说明。
"""


def clarify_prompt(question: str, *, today: str | None = None) -> str:
    return CLARIFY_INSTRUCTIONS.format(
        today=today or date.today().isoformat(),
        question=question.strip(),
    )


def write_brief_prompt(
    question: str,
    *,
    clarification_question: str | None = None,
    clarification_answer: str | None = None,
    previous_brief: ResearchBrief | None = None,
    revision_note: str | None = None,
    language: str = "zh",
    effort: str = "standard",
    today: str | None = None,
) -> str:
    clarification_context = ""
    if clarification_question is not None and clarification_answer is not None:
        clarification_context = f"""

一次澄清对话：
<clarification>
助手提问：{clarification_question.strip()}
用户回答：{clarification_answer.strip()}
</clarification>"""

    revision_context = ""
    if previous_brief is not None and revision_note is not None:
        note = revision_note.strip()
        if not note:
            raise ValueError("revision_note must not be blank when previous_brief is set")
        revision_context = f"""

用户要求对上一版 Research Brief 做一轮修订（仅此一轮，改完即定稿）：
<previous_brief>
question: {previous_brief.question}
brief_text:
{previous_brief.brief_text}
</previous_brief>
<revision_note>
{note}
</revision_note>
请在保留用户明确要求的前提下，按修订指令改写 Brief；不要忽略指令中的具体改动。"""

    return WRITE_BRIEF_INSTRUCTIONS.format(
        today=today or date.today().isoformat(),
        question=question.strip(),
        clarification_context=clarification_context,
        revision_context=revision_context,
        language=language,
        effort=effort,
    )
