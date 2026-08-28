"""Scope prompts for clarifying and expanding a research question."""

from __future__ import annotations

from datetime import date

from prospector.schemas.brief import ResearchBrief

CLARIFY_INSTRUCTIONS = """\
你是一个**深度研究**智能体，现在判断用户问题是否需要向用户澄清一次。

今天的日期是 {today}。

用户问题：
<question>
{question}
</question>

只输出一个 JSON 对象，键为：
"need_clarification" (boolean),
"question" (string),
"assessment" (string)。
need_clarification 为 true 时，question 必须是写给用户的澄清问题；
为 false 时，question 必须是空字符串。
不要 markdown 围栏，不要其它说明。
"""

WRITE_BRIEF_INSTRUCTIONS = """\
你是一个**深度研究**智能体，现在把用户问题改写成一份研究纲要。

今天的日期是 {today}。

用户问题：
<question>
{question}
</question>
{assessment_context}{clarification_context}
{revision_context}

研究纲要的作用是把问题打开，让下游规划者看见足够宽的研究空间。

本次研究档位是 {effort}。研究纲要展开的宽度就是后续的研究成本，两者要匹配。

user_constraints 只装用户自己说过的限制，用贴近用户原话的短句；\
用户没提到的字段留空，留空是常态。你补充的研究方向一律写进 brief_text。
其中 time_range 只填用户为研究划定的时间窗口；用户用来指称研究对象的时间\
（如“2026 年初开始的 Agent 热潮”）是在给对象命名，不是窗口，留空。

只输出一个 JSON 对象，键为：
"question", "brief_text", "user_constraints", "output_format", "language", "effort"。
- question：概括核心研究问题的短问句标题；
- brief_text：详细、具体的研究问题说明与候选方向；
- user_constraints：一个对象，键为 "time_range" (string), "regions" (array),
  "comparison_targets" (array), "source_rules" (array), "exclusions" (array),
  "deliverable_rules" (array)；
- output_format 为 "report_with_citations"；
- language 默认为 "{language}"，用户明确要求其它语言时以用户为准；
- effort 为 "{effort}"。
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

用户要求对上一版研究纲要做一轮修订（仅此一轮；改完会交回用户复看）：
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
请在保留用户明确要求的前提下，按修订指令改写研究纲要；不要忽略指令中的具体改动。"""

    return WRITE_BRIEF_INSTRUCTIONS.format(
        today=today or date.today().isoformat(),
        question=question.strip(),
        assessment_context=assessment_context,
        clarification_context=clarification_context,
        revision_context=revision_context,
        language=language,
        effort=effort,
    )
