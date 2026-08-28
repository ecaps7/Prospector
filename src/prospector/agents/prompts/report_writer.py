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


def _source_caveats(snapshot: WriterSnapshot) -> dict[UUID, str]:
    """Map each Assertion named by a minor source-credibility gap to that gap's finding.

    Only ``source_credibility`` gaps qualify: they are the ones whose subject is "this
    evidence rests on a weak carrier", which is a fact about the sentence that will cite
    it. The other gap kinds describe what the research missed, which belongs to the
    report's limitations rather than to any one finding.
    """
    caveats: dict[UUID, str] = {}
    for gap in snapshot.minor_gaps:
        if gap.get("kind") != "source_credibility":
            continue
        description = str(gap.get("description") or "").strip()
        if not description:
            continue
        for assertion_id in gap.get("related_assertion_ids") or []:
            caveats.setdefault(UUID(str(assertion_id)), description)
    return caveats


def _aliased_material(snapshot: WriterSnapshot) -> str:
    """Render the material as research strands over a deduplicated Excerpt library.

    Two shapes matter here. Assertions are grouped by the task that collected them, so
    the Writer sees what each strand of the research was asking rather than one flat
    chronological pile. Excerpt text is rendered once in a library the cards point into:
    Assertions outnumber Excerpts and share them, so inlining passages per card would
    pay for the same text several times over.

    A source-credibility caveat is attached to the finding it applies to, not left for
    the Writer to resolve out of the gap list by id. Research Verifier already knows
    which Assertions rest on a weak carrier; a warning the Writer has to cross-reference
    is a warning that reaches the report as a disclaimer in the last paragraph instead
    of as attribution in the sentence that uses the number.
    """
    alias_map = excerpt_alias_map(snapshot)
    replace = _alias_replacer(snapshot)
    caveats = _source_caveats(snapshot)

    cards_by_task: dict[str, list[dict[str, object]]] = {}
    for card in snapshot.evidence_cards:
        finding: dict[str, object] = {
            "assertion_id": str(card.assertion_id),
            "statement": card.assertion_statement,
            "excerpt_ids": [alias_map[excerpt.excerpt_id] for excerpt in card.excerpts],
        }
        caveat = caveats.get(card.assertion_id)
        if caveat is not None:
            finding["source_caveat"] = caveat
        cards_by_task.setdefault(str(card.task_id), []).append(finding)

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
            "brief": snapshot.brief.model_dump(mode="json"),
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
    你是**深度研究**报告的撰写者。请根据输入的冻结研究材料写成完整详细的报告。
    报告的内容取舍、结构、详略和表达由你决定。

    报告必须直接回答 `brief.question`，并遵守 `brief.user_constraints` 中所有非空要求。
    `brief.brief_text` 是可自由取舍的研究方向，不是必须逐项覆盖的清单。

    正文中的事实、数字、引述和来源归属必须与研究材料一致，不得编造材料中没有的内容。

    ## 研究材料的形状
    * `research_groups`：研究是分成若干条线索做的，每组是一条线索——`research_question`
      是这条线索要回答的问题，`findings` 是它查到的结果。
    * `source_caveat`：某条 finding 带这个字段，表示审稿已经查出它的来源有问题
      （单一来源、聚合站转述、无独立佐证等）。用到这条 finding 时，必须在相关句子中写明转述关系。
    * `excerpt_library`：每条 `findings` 指向的原文片段，按 `excerpt_id` 去重后集中列出。
      `findings` 是压缩结果，原文片段是事实、数字口径和来源限定的依据。

    ## 输出格式
    每行只输出 1 个完整、合法的 JSON 对象，不要输出 Markdown 代码块或 JSON 之外的文字。
    `paragraph` 表示开始一个新段落，必须放在该段第一条 statement 之前。
    报告完成后才输出 `end`。

    ## kind 是核验路径
    kind 只说明这句话应该如何核验，不规定句子应该怎么写。
    先写出自然、完整的正文，再根据这句话的实际内容和依据选择 kind；
    不要为了区分事实与判断，把原本完整的句子拆开。

    * evidence：整句都在直接转述材料事实；必须输出 `candidate_excerpt_ids`，
      不能输出 `premise_statement_ids`。
    * derived：句子包含分析、概括、比较、解释或判断；可输出 `candidate_excerpt_ids`、此前已输出的
      `premise_statement_ids`，或同时输出两者，但至少要有一种依据。结论依赖的每一条关键前文
      事实都必须列入 premise；物理上位于同一段不能代替显式绑定。
    * elaboration：不承载需要外部材料核对的内容。
    * limitation：只说明现有材料的边界或未覆盖之处。

    引言、正文和结论中的句子都按同一格式输出。`statement_id` 必须全文唯一。
    引用只能使用研究材料中的 excerpt_id 短编号和此前已输出的 statement_id。
    没有使用的引用字段直接省略，不要输出空数组。

    ## 完整格式示例
    {"record":"title","text":"……"}
    {"record":"introduction"}
    {"record":"paragraph"}
    {"record":"statement","statement_id":"s_01","text":"……","kind":"derived","candidate_excerpt_ids":["e_01"]}
    {"record":"section","title":"……"}
    {"record":"paragraph"}
    {"record":"statement","statement_id":"s_02","text":"……","kind":"evidence","candidate_excerpt_ids":["e_02"]}
    {"record":"statement","statement_id":"s_03","text":"……","kind":"derived","candidate_excerpt_ids":["e_03"],"premise_statement_ids":["s_02"]}
    {"record":"conclusion"}
    {"record":"paragraph"}
    {"record":"statement","statement_id":"s_04","text":"……","kind":"derived","premise_statement_ids":["s_01","s_03"]}
    {"record":"end"}
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
    if findings.report_rewrite_required:
        return report_writer_full_revision_messages(snapshot, draft, findings)

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
    paragraph_ids = sorted(findings.paragraph_repair_ids)
    revision_findings = replace(
        {
            "round": findings.round,
            "revision": findings.revision,
            "failures": [item.model_dump(mode="json") for item in findings.failures],
            "requirement_failures": [
                item.model_dump(mode="json") for item in findings.requirement_failures
            ],
        }
    )
    system = dedent(
        """
        你是研究报告的修订写作者。审稿人指出了单句或局部段落问题。
        你只能输出获准范围的补丁，不得重写全文。

        ## 硬约束
        1. patch_statement 只能替换 findings 中列出的 statement_id；不得新增 statement_id。
        2. patch_paragraph 只能替换 findings 中 repair_scope=paragraph 点名的完整段落；
           可以重写该段全部句子，但 paragraph_id 不变。
        3. 同一段不能同时使用 patch_statement 和 patch_paragraph。
        4. 不得改 title、section 标题或未点名段落。
        5. 段落外仍被后文 premise 引用的旧 statement_id 必须保留；否则完整补丁集会被拒绝。
        6. 换证只能使用材料中已有的 excerpt_id 短编号（如 e_01）；禁止暗示去搜新来源。
        7. 每行一个 JSON；全部补丁结束后输出 {"record":"end"}。

        ## 补丁记录
        单句替换输出：
        {"record":"patch_statement","statement_id":"s_...","text":"...","kind":"derived","candidate_excerpt_ids":["e_01"],"premise_statement_ids":["s_01"]}
        完整段落替换输出：
        {"record":"patch_paragraph","paragraph_id":"p_...","statements":[{"statement_id":"s_...","text":"...","kind":"derived","premise_statement_ids":["s_01"]}]}
        kind 约束与初稿相同：evidence 必须带候选 excerpt 且不带 premise；
        derived 可带 excerpt、此前 statement 的 premise 或两者，但至少要有一种；
        elaboration / limitation 不带引用字段。没有使用的字段直接省略。
        derived 必须列出结论依赖的所有关键前文句子，同段相邻不能代替 premise。
        推理是否越界由后续报告核验者核对。
        kind 只决定核验路径，不决定句子的写法；修订时也不要为了分开事实与判断而拆句。

        ## premise 的顺序（修订时最容易踩的坑）
        premise 只能引用在草稿中排在该句之前的句子。你看到的是完整草稿，
        但读者是顺序读下来的：把根据挂在后面才出现的句子上，读者读到该句时它还不存在。
        给某句找依据时只在它前面的句子里找；前面没有合适依据，
        derived 可以直接挂 excerpt，或改为 evidence / limitation。

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
        "请根据审稿意见，只输出获准的句子或段落补丁。\n\n"
        f"需要修订的 statement_id：{json.dumps(failure_ids, ensure_ascii=False)}\n\n"
        f"需要重写的 paragraph_id：{json.dumps(paragraph_ids, ensure_ascii=False)}\n\n"
        f"审稿 findings：\n{json.dumps(revision_findings, ensure_ascii=False)}\n\n"
        f"当前草稿：\n{json.dumps(aliased_draft, ensure_ascii=False)}\n\n"
        f"可用研究材料：\n{material}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def report_writer_full_revision_messages(
    snapshot: WriterSnapshot,
    draft: ReportDraft,
    findings: ReportVerifierFindings,
) -> list[dict[str, str]]:
    """Request a complete rewrite when the report as a whole missed its contract."""

    aliases = {str(excerpt_id): alias for excerpt_id, alias in excerpt_alias_map(snapshot).items()}

    def replace(value: object) -> object:
        if isinstance(value, str):
            return aliases.get(value, value)
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        return value

    system = report_writer_messages(snapshot)[0]["content"] + dedent(
        """

        ## 本轮是整份报告修订
        当前草稿没有履行核心问题或用户明确要求，因此必须重新输出完整报告，
        从 title 开始，到 end 结束。可以调整标题、章节、段落和句子；不要输出 patch_statement。
        同时修正 findings 中的逐句核验失败。保留仍有材料支持且不妨碍修订的内容。
        """
    )
    revision_findings = replace(
        {
            "round": findings.round,
            "revision": findings.revision,
            "requirement_failures": [
                item.model_dump(mode="json") for item in findings.requirement_failures
            ],
            "statement_failures": [item.model_dump(mode="json") for item in findings.failures],
        }
    )
    user = (
        "请依据审稿结果重写完整报告。\n\n"
        f"审稿 findings：\n{json.dumps(revision_findings, ensure_ascii=False)}\n\n"
        f"当前草稿：\n{json.dumps(replace(draft.model_dump(mode='json')), ensure_ascii=False)}\n\n"
        f"冻结研究材料：\n{_aliased_material(snapshot)}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
