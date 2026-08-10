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

无论是否需要澄清，都必须填写 assessment，用一两句话说明你的判断依据：
问题里哪部分已经足够明确，还有哪些缺口是可以通过展开研究方向来弥补、不必打扰用户的。
这句话会交给下一步撰写 Brief 的环节，避免它把你刚做过的判断重做一遍。
例：「研究对象是某新能源车企的海外扩张，很明确；用户没有说明是从投资角度还是
供应链角度关心这件事，这个缺口应该由 Brief 展开多个方向覆盖，不必追问。」

只输出一个 JSON 对象，键为：
"need_clarification" (boolean),
"question" (string),
"assessment" (string)。
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
{assessment_context}{clarification_context}
{revision_context}

写作要求：
0. 两类信息必须分开输出，不能混写

- **用户自己说过的限制** → 逐项填入 user_constraints 对应字段。
  用贴近用户原话的短句，不要改写、不要美化、不要扩展成更专业的说法。
  用户没提到的字段一律留空——**留空是常态，宁可少填也不要猜**。
  判断标准只有一条：这句话如果用户没说过，就不能出现在这里。
  例：用户说"别给我看那些自媒体的东西"，就写"不要自媒体内容"，
  不要写成"来源应限定为机构报告与学术文献"——那是你的扩展，会悄悄改变范围。

- **你补充的研究方向** → 全部写进 brief_text，用"可以关注""值得检验"
  "一种可能的解释是"这样的措辞。这些是供 Planner 取舍的建议，不是必须遵守的要求。

brief_text 里不要重复 user_constraints 中已写下的限制，也不要用"用户要求""必须"
这类词去描述你自己补充的方向。

user_constraints 各字段的含义：
- time_range：用户说的时间范围（如"近三年""2020 年以后"）；
- regions：用户说的地域范围；
- comparison_targets：用户点名要比较的对象；
- source_rules：用户对来源的要求（如"只要一手数据""不要媒体转述"）；
- exclusions：用户明确说不要研究或不要写的内容；
- deliverable_rules：用户对输出形式的额外要求。

1. 先把问题写具体
- 完整保留用户明确提出的研究对象与目的；限制类信息按第 0 条进 user_constraints。
- 说清用户真正想判断什么、这个判断服务于什么问题，以及哪些概念需要在研究中辨析。
- 输出前在内部逐项核对用户明确要求，不能遗漏；不要输出核对过程。

2. 主动打开研究空间（请根据用户问题的类型，动态采取不同的展开策略）：
- 若为【探索/梳理型问题】（如“XX是什么/发生了什么”）：不要强行制造竞争性假设。\
应着重拆解研究的“维度”，如：核心机制分解、关键时间节点与演变阶段、\
不同利益相关者的视角、以及该事物的上下游影响。帮助 Planner 看到该话题的全貌结构。
- 若为【分析/评判型问题】（如“XX为什么失败/A是否优于B”）：\
必须提出多种彼此竞争的假设或替代解释；展开可能改变答案的机制、地域差异、\
比较基准或边界条件；并主动指出哪些相反证据或异常现象可能推翻常见直觉。
- 无论何种题型，都应主动指出哪些常见的直觉或行业刻板印象是需要在这个研究中被重新检验的，\
避免过早收敛于单一叙事。

展开的宽度必须匹配用户选择的研究档位（本次档位：{effort}）：
- quick：只展开 1 到 2 个最关键的方向。这个档位的研究预算只够回答核心问题，\
展开太宽会导致每个方向都浅尝辄止。宁可窄而深。
- standard：展开 3 到 4 个方向，其中至少包含一个可能推翻常见直觉的角度。
- deep：可以充分展开，并列多个竞争性解释、多个比较基准与多层边界条件。
这不是硬性数量指标，而是提醒你：**Brief 的宽度就是后续的研究成本**。\
你在这里多开一个方向，Planner 就要多花决策轮去覆盖它。

3. 语言风格与边界感（区分用户要求与候选方向）
- 用户明确要求是必须原样保留的事实；\
你补充的内容应写成「可探索的维度」「值得重点关注的机制」或「需要检验的假设」。
- 绝对禁止使用晦涩的学术套话和执行指令语，\
例如“需要转化为可检验的研究命题”、“可探索的间接证据路径包括”等。\
请用平实、清晰、面向业务或直接阅读的专业语言描述。
- 不得把你补充的时间范围、来源偏好、评价标准、排除项或结论写成用户要求，\
也不得把它们填进 user_constraints。
- 不得预设结论，已有直觉只能作为待验证假设。

4. 输出形式
- question：能够准确概括核心研究问题的短问句标题。
- brief_text：连贯、具体的研究问题说明，可以使用自然段；不要写成可逐项打勾的 must_cover 清单。
- user_constraints：按第 0 条填写；用户没提到的字段留空（字符串用 ""，列表用 []）。
- 使用与用户问题相同的主要语言。

元数据：
- language 默认 "{language}"；若用户明确要求其它语言则使用用户要求。
- effort 使用 "{effort}"。
- output_format 使用 "report_with_citations"。

只输出一个 JSON 对象，键为：
"question", "brief_text", "user_constraints", "output_format", "language", "effort"。
其中 user_constraints 是一个对象，键为：
"time_range" (string), "regions" (array), "comparison_targets" (array),
"source_rules" (array), "exclusions" (array), "deliverable_rules" (array)。
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
请在保留用户明确要求的前提下，按修订指令改写 Brief；不要忽略指令中的具体改动。"""

    return WRITE_BRIEF_INSTRUCTIONS.format(
        today=today or date.today().isoformat(),
        question=question.strip(),
        assessment_context=assessment_context,
        clarification_context=clarification_context,
        revision_context=revision_context,
        language=language,
        effort=effort,
    )
