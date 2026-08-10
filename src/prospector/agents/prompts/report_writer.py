"""Prompt construction for the prose-first, line-record deep-research Report Writer."""

# JSONL contract examples intentionally remain on one physical line.
# ruff: noqa: E501

from __future__ import annotations

import json
from textwrap import dedent

from prospector.schemas.claims import ReportVerifierFindings
from prospector.schemas.report import ReportDraft, WriterSnapshot, excerpt_alias_map


def _aliased_material(snapshot: WriterSnapshot) -> str:
    """Serialize the snapshot with every excerpt UUID replaced by its short alias.

    Excerpt ids can surface in loosely-typed fields too (e.g. minor_gaps), so the
    replacement walks the whole payload instead of naming individual fields.
    """
    aliases = {
        str(excerpt_id): alias for excerpt_id, alias in excerpt_alias_map(snapshot).items()
    }

    def replace(value: object) -> object:
        if isinstance(value, str):
            return aliases.get(value, value)
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    payload = replace(snapshot.model_dump(mode="json"))
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

    ## 写作要求
    1. 论证优先于篇幅。该展开的地方展开到位；材料撑不住的地方，宁可写短，不要用措辞把它填满。
       报告的长度应当是研究深度的结果，不是目标。
    2. 榨干材料：材料中出现的每一个数字、对比、时间线索都值得被用上，并放回它自己的语境中解释清楚。
    3. 标题必须是有实质内容的专业短语，严禁出现“事实层”“分析层”“现象与归因”一类框架术语。
    4. 正文 `text` 中绝对禁止出现“基于 s_03”“如材料所述”等后台逻辑标记。
    5. 段落切分服从语义：写完一个完整的意思就输出一条 {"record":"paragraph"} 另起一段，
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
      evidence 或 derived 句；candidate_excerpt_ids 必须为空。
      推理链最多两层（evidence → derived → derived），不得更深，也不得以 elaboration
      或 limitation 作为前提——那样整条推理就落不到任何出处上。
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


def report_writer_revision_messages(
    snapshot: WriterSnapshot,
    draft: ReportDraft,
    findings: ReportVerifierFindings,
) -> list[dict[str, str]]:
    material = _aliased_material(snapshot)
    aliases = {
        str(excerpt_id): alias for excerpt_id, alias in excerpt_alias_map(snapshot).items()
    }

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
        derived 必须以此前的 evidence 或 derived 句为 premise 且不带 excerpt，推理链最多两层；
        elaboration / limitation 两个引用字段皆空。

        ## 怎么改
        elaboration / limitation 被判不合格，说明它承载了需要核对的事实或判断。
        具体事实改为 evidence 并附上 excerpt；基于前文形成的判断改为 derived 并附上 premise；
        真正的 elaboration 只保留转折、预告或收束作用。
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
