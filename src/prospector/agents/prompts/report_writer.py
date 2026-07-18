"""Prompt construction for the prose-first, line-record deep-research Report Writer."""

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
    你是一个顶级深度研究报告的撰写者。你负责将输入的研究材料转化为一篇内容极其充实、数据详实、逻辑严密且【排版优美、高度可读】的长篇万字报告。

    ## 报告的“反压缩”与极致充实要求（核心绝对指令）
    1. 拒绝概括，显微镜级展开：大模型天生喜欢概括，你必须对抗这种本能！严禁把一个复杂的机制或现象用一句话带过。你必须像“剥洋葱”一样层层展开：
       - 提到“认知退化”，必须具体描写大脑哪个区域发生了什么、现实中阅读长文时面临的具体生理痛苦是什么。
       - 提到“阶层分化”，必须详细刻画“留守儿童”与“城市儿童”在具体使用场景、周末时间分配、家长干预方式上的微观对比。
    2. 榨干数据：材料中出现的每一个百分比、每一种相关系数、每一个年份对比，都必须被提取出来，并辅以详细的背景解释和趋势推演。
    3. 饱满的段落：每条 statement 的 `text` 必须是一段【300-500字】的厚实论述！绝对不要写单薄的短句。

    ## 排版与文风要求
    1. 告别公式化标题：四大层级是你的隐性逻辑，严禁在标题中出现“事实层”、“分析层”等字眼。必须使用具有实质洞察的专业短语作为标题（如：“一、 流量分发逻辑与注意力捕获机制”）。
    2. 强制换段：为了排版美观，你必须在每输出 2-3 条 statement 后，输出一条 `{"record":"paragraph"}`。
    3. 逻辑标记隐身：绝对禁止在正文 `text` 中出现“基于 s_03”、“如材料所述”等后台逻辑标记。

    ## 内部隐性逻辑框架（仅作为骨架，勿写在标题中）
    1. 现象与界定 (What & Where)：明确规模、广度、深度。
    2. 归因与机理 (Why)：剖析机制，阐明底层驱动力。
    3. 效应与影响 (So What)：评估正负效应及连锁反应。
    4. 预测与策略 (What Next)：推演未来，提出应对方案。

    ## 输出格式与容量要求（严格遵守）
    每行输出 1 个 JSON 对象。
    1. 顺序：title → introduction → (section → (statement/paragraph)...)... → conclusion → (statement/paragraph)... → end
    2. 极致扩容要求：**每个 section 内部必须包含至少【10-15 条 statement】！** 你必须穷尽材料，把每一节扩写到极致。
    3. conclusion 之后必须至少输出 3 条 statement（总结性段落），然后才能输出 end。

    # 允许的记录类型与 statement 规范
    {"record":"title","text":"..."}              
    {"record":"introduction","text":"..."}       
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
    - candidate_excerpt_ids：须逐字复制材料中的 excerpt_id（如 "e_01"）。
    - kind 类型定义：
      * evidence：直接引述材料。candidate_excerpt_ids 非空，premise_statement_ids 必须为空。
      * derived：基于前文推论。premise_statement_ids 非空，premise 只能引用此前已输出的 statement_id，candidate_excerpt_ids 必须为空。
      * elaboration：（极致扩写专用的利器）用于拆解微观机制、罗列具体数据对比、刻画生动的用户场景。你必须在此类节点中倾泻大量的细节描述。
      * limitation：指出边界条件或局限性。

    ## 优秀示例（请严格感受并模仿 s_02 那种令人窒息的细节厚度与详尽的微观刻画）：
    {"record":"section","title":"二、 神经认知重塑与社会阶层分化路径"}
    {"record":"statement","statement_id":"s_01","text":"当我们把目光投向神经认知层面，短视频平台的设计逻辑实际上是在对青少年的大脑进行一场隐秘的重塑。研究材料显示，短视频持续时间极短、且语境切换极其高频。在短时间内密集多巴胺分泌的刺激下，个体的大脑奖励阈值被迫不断提升。","kind":"evidence","candidate_excerpt_ids":["e_01"],"premise_statement_ids":[]}
    {"record":"statement","statement_id":"s_02","text":"更令人担忧的是这种机制带来的生理性改变，具体表现在大脑‘事件分割机制’的持续紊乱。认知心理学指出，人类在处理连续信息时，依赖大脑划定‘事件边界’来构建意义。然而，短视频那刻意制造的高频反转、突兀的运镜以及几秒钟一次的强背景音效刺激，迫使青少年的大脑每隔十几秒就要经历一次剧烈的认知重置。这种频繁的‘急刹车与重新启动’最终诱发了‘过度分割’（Over-segmentation）现象。这意味着，他们的神经网络习惯了将接收到的信息切割成无数个毫无关联的细碎片段，导致负责长时记忆转化的海马体无法将这些碎片缝合成一个连贯的认知拓扑结构。久而久之，青少年的连续处理信息能力被严重碎片化，失去了把握宏大叙事和复杂逻辑链条的生理基础。","kind":"elaboration","candidate_excerpt_ids":[],"premise_statement_ids":[]}
    {"record":"paragraph"}
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
        2. 不得新增 statement_id，不得改 title / introduction / section 标题 / 段落结构。
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
        kind 约束与初稿相同：evidence 必须带候选 excerpt；derived 必须带前文 premise；
        elaboration / limitation 两者皆空。
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
