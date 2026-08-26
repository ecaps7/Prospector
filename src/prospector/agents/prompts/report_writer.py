"""Prompt construction for the prose-first, line-record deep-research Report Writer."""

# JSONL contract examples intentionally remain on one physical line.
# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Callable
from textwrap import dedent
from uuid import UUID

from prospector.deterministic.excerpt_text import clip_excerpt_text, writer_excerpt_limit
from prospector.schemas.claims import ReportVerifierFindings
from prospector.schemas.report import ReportDraft, WriterSnapshot, excerpt_alias_map


def _alias_replacer(snapshot: WriterSnapshot) -> Callable[[object], object]:
    """Swap every excerpt UUID for its short alias, anywhere in a payload.

    Excerpt ids surface in loosely-typed fields too (e.g. minor_gaps), so the
    replacement walks the whole payload instead of naming individual fields.
    """
    aliases = {str(excerpt_id): alias for excerpt_id, alias in excerpt_alias_map(snapshot).items()}

    def replace(value: object) -> object:
        if isinstance(value, str):
            return aliases.get(value, value)
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    return replace


def _aliased_material(snapshot: WriterSnapshot) -> str:
    """Render the material as research strands over a deduplicated Excerpt library.

    Two shapes matter here. Assertions are grouped by the task that collected them, so
    the Writer sees what each strand of the research was asking rather than one flat
    chronological pile. Excerpt text is rendered once in a library the cards point into:
    Assertions outnumber Excerpts and share them, so inlining passages per card would
    pay for the same text several times over.
    """
    alias_map = excerpt_alias_map(snapshot)
    replace = _alias_replacer(snapshot)

    cards_by_task: dict[str, list[dict[str, object]]] = {}
    for card in snapshot.evidence_cards:
        cards_by_task.setdefault(str(card.task_id), []).append(
            {
                "statement": card.assertion_statement,
                "excerpt_ids": [alias_map[excerpt.excerpt_id] for excerpt in card.excerpts],
            }
        )

    # Plan order, so the material arrives in the shape the research actually took.
    groups: list[dict[str, object]] = []
    for plan in snapshot.final_plan_summary:
        for task in plan.get("tasks", []):
            findings = cards_by_task.pop(str(task.get("id")), None)
            if not findings:
                continue
            groups.append(
                {
                    "research_question": task.get("question"),
                    "research_stage": task.get("research_stage"),
                    "research_mode": task.get("research_mode"),
                    "expected_evidence": task.get("expected_evidence"),
                    "stop_reason": task.get("stop_reason"),
                    "findings": findings,
                }
            )
    groups.extend({"research_question": None, "findings": rest} for rest in cards_by_task.values())

    limit = writer_excerpt_limit(len(alias_map))
    library: list[dict[str, object]] = []
    seen: set[UUID] = set()
    for card in snapshot.evidence_cards:
        for excerpt in card.excerpts:
            if excerpt.excerpt_id in seen:
                continue
            seen.add(excerpt.excerpt_id)
            library.append(
                {
                    "excerpt_id": alias_map[excerpt.excerpt_id],
                    "source": excerpt.source.model_dump(mode="json"),
                    "text": clip_excerpt_text(excerpt.text, limit),
                }
            )

    payload = replace(
        {
            "brief": snapshot.brief,
            "research_groups": groups,
            "excerpt_library": library,
            "conflicts": [conflict.model_dump(mode="json") for conflict in snapshot.conflicts],
            "minor_gaps": snapshot.minor_gaps,
        }
    )
    return json.dumps(payload, ensure_ascii=False, default=str)


def report_writer_messages(snapshot: WriterSnapshot) -> list[dict[str, str]]:
    material = _aliased_material(snapshot)
    system = dedent(
        """
    你是深度研究报告的撰写者。你负责把输入的研究材料写成一篇论证充分、结构清晰、读起来顺畅的研究报告。

    ## 唯一的硬性红线
    报告中的任何一句话都不得引入研究材料之外的内容：
    材料里没有的数字、比例、金额、时间点，材料里没有出现过的机构、人物、地区、研究、事件或专有名词，
    以及材料无法支撑的因果结论和归因断言，一律不得出现。
    也不要用“研究表明”“专家指出”这类说法引述材料中并不存在的来源。

    这条红线之内你是自由的：解释机制、复述细节、把分散在多处的材料串联起来、指出趋势与张力、
    说明边界与局限——都可以充分展开，篇幅和信息密度不设上限，也不设下限。

    ## 研究材料的形状
    * `research_groups`：研究是分成若干条线索做的，每组是一条线索——`research_question`
      是这条线索要回答的问题，`findings` 是它查到的结论。这是材料的结构，不是报告的结构：
      不要一组写一节，也不要照抄这些问题当章节标题。
    * `excerpt_library`：每条 `findings` 指向的原文片段，按 `excerpt_id` 去重后集中列出。
      `findings` 里的一句话是压缩过的结论，**原文才是你真正的写作材料**：数字的口径、
      事实的前因后果、来源自己的措辞与限定，都只在原文里。写到某条结论时先读它的原文。

    ## 写作要求
    1. 论证优先于篇幅。该展开的地方展开到位；材料撑不住的地方，宁可写短，不要用措辞把它填满。
       报告的长度应当是研究深度的结果，不是目标。
    2. **每段先立论点，再用材料支撑它**。段落的第一句应当说出这一段要说明什么，
       后续句子用事实、数字、对比和边界把它撑起来，段末不必再复述一遍。
       严禁把一段写成"事实、事实、事实……以上事实同时发生"这种清单加收尾的形状。
    3. **一条 finding 不等于一句正文**。材料条数和正文句数没有对应关系：
       同一条原文的不同细节可以拆到不同地方用；多条讲同一件事的结论应当合并成一句；
       与报告主线无关的条目**不必写进正文**。不要为了用完材料而罗列。
    4. 罗列同类案例时，先说清楚你要用这批案例说明什么，再举其中最有说服力的几个，
       并写明这是一批什么样的案例（覆盖哪些行业、来自什么口径的来源）。
       把十几个案例平铺成十几句，读者得不到任何判断。
    5. 来源强弱要写进正文。同一个数字来自机构原始报告还是转载聚合站，
       在句子里就要说清楚（"据某站转述的某机构调查"），不要留到最后统一免责。
    6. 标题必须是有实质内容的专业短语，严禁出现"事实层""分析层""现象与归因"一类框架术语。
    7. 正文 `text` 中绝对禁止出现"基于 s_03""如材料所述"等后台逻辑标记。
    8. 段落切分服从语义：写完一个完整的意思就输出一条 {"record":"paragraph"} 另起一段，
       不按条数机械换段。

    ## 内部组织思路（仅用于你规划结构，禁止写进标题）
    现象与界定 → 归因与机理 → 效应与影响 → 判断与展望。
    章节数量、顺序和详略由问题本身和材料的厚度决定，不必强行套用这四段。

    ## 输出格式
    每行输出 1 个 JSON 对象。
    顺序：title → introduction → (statement/paragraph)... → (section → (statement/paragraph)...)...
          → conclusion → (statement/paragraph)... → end

    {"record":"title","text":"..."}
    {"record":"introduction"}                    # 之后直接输出引言的 statement 与 paragraph
    {"record":"section","title":"..."}
    {"record":"paragraph"}
    {"record":"conclusion"}
    {"record":"end"}                             # 报告未完成禁止输出 end
    {
      "record": "statement",
      "statement_id": "s_...",
      "text": "...",
      "kind": "evidence" | "derived" | "elaboration" | "limitation",
      "candidate_excerpt_ids": [],
      "premise_statement_ids": []
    }

    ## kind 的选择
    * evidence：直接依据材料原文陈述事实。candidate_excerpt_ids 须逐字复制材料中的 excerpt_id
      （如 "e_01"），premise_statement_ids 必须为空。
    * derived：在已写出的句子之上做推理。premise_statement_ids 非空，且只能引用此前已输出的
      statement_id；candidate_excerpt_ids 必须为空。推理是否越界、是否最终落到证据，由
      后续 Report Verifier 逐句核对，不在写作阶段拒绝。
      **归纳是本职工作，不要因为怕被打回就不写判断**；要写的是**带范围的判断**：
      写明这个概括依据的是什么（"这些案例显示""在本报告收集到的材料范围内"
      "至少五套评测均显示"），避免"普遍""必然""所有""标志着"这类超出材料的表述。
      材料只能支持到同时发生或先后关系时，就写成同时发生或先后关系，不要写成因果。
      章节论点可以架在分论点之上，全文论点可以架在章节论点之上，不必把每句都直接挂到事实。
    * elaboration：只承担章节转折、下文预告和前文收束，两个引用字段都为空。
      含有具体数字、年份、机构、人物、地点或事件的句子必须写成 evidence 并绑定 Excerpt；
      在已有事实之上形成的解释或判断必须写成 derived 并绑定 premise。
      不得因为一句话是在复述材料，就把本应是 evidence 的事实写成无引用的 elaboration。
    * limitation：只说明现有材料的边界或未覆盖之处，两个引用字段都为空；
      不得借 limitation 声称未经材料验证的外部事实。

    引言和结论同样逐句输出 statement，遵循与正文完全相同的 kind 规则，没有例外。
    引言同样先写 evidence，再在这些已输出的事实之上写 derived；不能把需要来源的核心判断
    伪装成 elaboration。全文任何位置都没有无引用的事实通道。

    ## 格式示例（只示范记录形式与引用关系，与你的实际主题无关）
    {"record":"section","title":"……有实质内容的章节标题……"}
    {"record":"statement","statement_id":"s_11","text":"……直接依据某条材料原文作出的事实陈述……","kind":"evidence","candidate_excerpt_ids":["e_01"],"premise_statement_ids":[]}
    {"record":"statement","statement_id":"s_12","text":"……下一节将讨论这一事实可能带来的影响……","kind":"elaboration","candidate_excerpt_ids":[],"premise_statement_ids":[]}
    {"record":"paragraph"}
    {"record":"statement","statement_id":"s_13","text":"……在 s_11 的事实之上得出的判断……","kind":"derived","candidate_excerpt_ids":[],"premise_statement_ids":["s_11"]}
    {"record":"statement","statement_id":"s_14","text":"……材料未覆盖的边界或反例风险……","kind":"limitation","candidate_excerpt_ids":[],"premise_statement_ids":[]}
    """
    ).strip()
    user = f"""请根据下面冻结的研究材料，按系统提示中的逐行 JSON 记录流格式撰写深度研究报告。

研究材料：
{material}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def continuation_message(last_accepted: str) -> str:
    return (
        f"已完整接收至：{last_accepted}。之前的内容已全部保存。"
        "请从断点继续输出后续记录，不要重复任何已输出的记录；"
        '全文完成时以 {"record": "end"} 单独一行收尾。'
    )


def retry_message(error: str, last_accepted: str) -> str:
    return (
        f"你最近一轮输出存在问题：{error}\n"
        f"已接受的最后一条记录是：{last_accepted}，其之前的内容已全部保存。"
        "请从该记录之后继续输出，修正上述问题，不要重复任何已接受的记录。"
    )


def patch_restart_message(error: str) -> str:
    """Ask for the whole patch set again after the assembled draft was rejected.

    Unlike ``retry_message`` this throws away what was already accepted: the rejection is a
    property of the patches taken together, so resuming from a checkpoint inside the set
    would only rebuild the same illegal draft.
    """
    return (
        f"你提交的补丁逐条看都合法，但整体应用到草稿后不通过：{error}\n"
        "这一轮补丁已全部作废。请重新输出完整的补丁集并修正上述问题，"
        '同样以 {"record": "end"} 单独一行收尾。'
    )


def report_writer_revision_messages(
    snapshot: WriterSnapshot,
    draft: ReportDraft,
    findings: ReportVerifierFindings,
) -> list[dict[str, str]]:
    material = _aliased_material(snapshot)
    aliases = {str(excerpt_id): alias for excerpt_id, alias in excerpt_alias_map(snapshot).items()}

    def replace(value: object) -> object:
        if isinstance(value, str):
            return aliases.get(value, value)
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    aliased_draft = replace(draft.model_dump(mode="json"))
    failure_ids = [item.statement_id for item in findings.failures]
    system = dedent(
        """
        你是研究报告的修订写作者。审稿人指出了若干未通过句子。你只能输出补丁，不得重写全文。

        ## 硬约束
        1. 只输出 findings 中列出的 statement_id 的替换；未点名句子一个字都不能改。
        2. 不得新增 statement_id，不得改 title / section 标题 / 段落结构。
        3. 换证只能使用材料中已有的 excerpt_id 短编号（如 e_01）；禁止暗示去搜新来源。
        4. 每行一个 JSON；全部补丁结束后输出 {"record":"end"}。

        ## 补丁记录
        {
          "record": "patch_statement",
          "statement_id": "s_...",
          "text": "...",
          "kind": "evidence" | "derived" | "elaboration" | "limitation",
          "candidate_excerpt_ids": [],
          "premise_statement_ids": []
        }
        kind 约束与初稿相同：evidence 必须带候选 excerpt 且不带 premise；
        derived 必须带此前 statement 的 premise 且不带 excerpt；
        elaboration / limitation 两个引用字段皆空。推理是否越界由后续 Report Verifier 核对。

        ## premise 的顺序（修订时最容易踩的坑）
        premise 只能引用在草稿中排在该句之前的句子。你看到的是完整草稿，
        但读者是顺序读下来的：把根据挂在后面才出现的句子上，读者读到该句时它还不存在。
        给某句找依据时只在它前面的句子里找；前面没有合适依据，
        就改用 evidence 直接挂 excerpt，或降级为 limitation 如实说明。

        ## 怎么改
        elaboration / limitation 被判不合格，说明它承载了需要核对的事实或判断。
        具体事实改为 evidence 并附上 excerpt；基于前文形成的判断改为 derived 并附上 premise；
        真正的 elaboration 只保留转折、预告或收束作用。

        derived 被判 overreach 时，正确的修法是**给判断补上它的依据范围**，
        而不是把判断改软、改空或换成复述。
        - 归纳被判越界：写明这个归纳依据的是什么（"这些案例显示""在本报告收集到的
          材料范围内""至少五套评测均显示"），删掉"普遍""必然""所有""标志着"
          一类超出材料的表述。判断本身要保留。
        - 因果被判越界：材料只能支持到同时发生或先后关系时，就如实写成同时发生或
          先后关系，不要保留因果动词；不要因此把整句删成一句无信息的复述。
        - 前提不足：改挂到能支撑该判断的前文句子上，或收窄到材料支持得住的范围。
        判为 miscalibrated 时，把"事实如此"改成"某来源如此报道"，并保留该判断。

        修订后的句子必须仍然是一个判断。把被点名的句子改成"以上事实同时发生"
        这类没有信息量的复述，等同于修订失败。
        """
    ).strip()
    user = (
        "请根据审稿意见，只输出需要替换的句子补丁。\n\n"
        f"需要修订的 statement_id：{json.dumps(failure_ids, ensure_ascii=False)}\n\n"
        f"审稿 findings：\n{json.dumps(findings.model_dump(mode='json'), ensure_ascii=False)}\n\n"
        f"当前草稿：\n{json.dumps(aliased_draft, ensure_ascii=False)}\n\n"
        f"可用研究材料：\n{material}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
