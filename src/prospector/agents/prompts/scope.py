"""Scope prompts for clarifying and expanding a research question."""

from __future__ import annotations

from datetime import date

from prospector.schemas.brief import ResearchBrief

CLARIFY_INSTRUCTIONS = """\
你负责判断用户问题是否必须经过一次澄清，才能形成可靠的 Research Brief。

今天的日期是 {today}。

用户问题：
<question>
{question}
</question>

只有在问题存在会导向实质不同研究对象、范围或用户意图的关键歧义，
并且任何一种自行假设都有明显偏题风险时，才选择澄清。

如果未说明的内容可以由 Brief 作为候选方向展开，或者可以由后续 Planner 自主决定，
则不需要澄清。不要为了询问研究侧重点、研究方法、报告结构或更多背景而打断用户。

需要澄清时，只提出一个聚焦于最关键歧义的问题，不要把多个问题合并成需求访谈。
不需要澄清时，在 assessment 中简要说明问题为什么已经足以形成 Brief，
以及哪些未指定内容可以由 Brief 自由展开。

只输出一个 JSON 对象，键为：
"need_clarification" (boolean),
"question" (string),
"assessment" (string)。

- need_clarification 为 true 时，question 是写给用户的澄清问题，assessment 为空字符串；
- need_clarification 为 false 时，question 为空字符串，assessment 写上述判断。

不要输出 Markdown 围栏或其它说明。
"""

WRITE_BRIEF_INSTRUCTIONS = """\
你负责根据用户输入生成一份待用户确认的 Research Brief。

今天的日期是 {today}。

用户问题：
<question>
{question}
</question>
{assessment_context}{clarification_context}
{revision_context}

Research Brief 是后续研究使用的输入快照。它需要准确保留用户的核心问题和明确要求，
并帮助 Planner 看见可以探索的研究空间。

brief_text 可以补充有助于理解问题的背景、边界和候选方向。
候选方向由 Planner 自由取舍，不是必须逐项完成的清单。

不要在 Brief 中：
- 规定研究任务、任务顺序或研究方法；
- 规定报告章节或文章结构；
- 把候选方向写成强制覆盖要求；
- 预设答案、结论数量或必须得到单一结论；
- 自行扩大、缩小或改变用户的研究对象。

本次研究档位是 {effort}。effort 只影响候选方向展开的程度和细致度，不改变核心问题和用户明确要求。

user_constraints 只装用户自己说过的限制，用贴近用户原话的短句；\
用户没提到的字段留空，留空是常态。你补充的研究方向一律写进 brief_text。
其中 time_range 只填用户为研究划定的时间窗口；用户用来指称研究对象的时间\
（如“2026 年初开始的 Agent 热潮”）是在给对象命名，不是窗口，留空。

只输出一个 JSON 对象，键为：
"question", "brief_text", "user_constraints", "output_format", "language", "effort"。
- question：准确概括核心研究问题的短问句，不改变研究对象；
- brief_text：核心问题的具体说明，以及供 Planner 自由取舍的候选方向；
- user_constraints：一个对象，键为 "time_range" (string), "regions" (array),
  "comparison_targets" (array), "source_rules" (array), "exclusions" (array),
  "deliverable_rules" (array)；
- output_format 为 "report_with_citations"；
- language 默认为 "{language}"，用户明确要求其它语言时以用户为准；
- effort 为 "{effort}"。
不要输出 Markdown 围栏或其它说明。
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
    assessment: str | None = None,
    previous_brief: ResearchBrief | None = None,
    revision_note: str | None = None,
    language: str = "zh",
    effort: str = "standard",
    today: str | None = None,
) -> str:
    # The clarify step already worked out what is settled and what should be opened by
    # the Brief rather than asked about. Passing that verdict on saves this step from
    # deriving it a second time, with less information than the first.
    assessment_context = ""
    if assessment and assessment.strip():
        assessment_context = f"""

澄清环节的判断：
<assessment>
{assessment.strip()}
</assessment>"""

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

用户要求对上一版 Research Brief 做一轮修订（仅此一轮；改完会交回用户复看）：
<previous_brief>
question: {previous_brief.question}
brief_text:
{previous_brief.brief_text}
user_constraints:
{previous_brief.user_constraints.model_dump_json()}
</previous_brief>
<revision_note>
{note}
</revision_note>
修订指令代表用户的最新要求；它与上一版内容冲突时，以修订指令为准。
未被修改的用户明确要求继续保留。改完后仍需遵守 Research Brief 的职责边界。"""

    return WRITE_BRIEF_INSTRUCTIONS.format(
        today=today or date.today().isoformat(),
        question=question.strip(),
        assessment_context=assessment_context,
        clarification_context=clarification_context,
        revision_context=revision_context,
        language=language,
        effort=effort,
    )
