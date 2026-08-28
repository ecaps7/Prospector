"""Prompt construction for per-statement Report Verifier calls."""

# JSONL contract examples intentionally remain on one physical line.
# ruff: noqa: E501

from __future__ import annotations

import json
from textwrap import dedent
from typing import Any

from prospector.schemas.claims import ReportQualityDecision


def report_verifier_messages(statement: dict[str, Any]) -> list[dict[str, str]]:
    kind = statement["kind"]
    if kind == "evidence":
        system = dedent(
            """
            你是研究报告的逐句审稿人。当前句子声明为 evidence（事实句）。
            只判断这句话是否忠实于它明确绑定的候选原文片段，不使用常识补全。

            核对句中的每项关键内容：谁、做了什么、数字、时间、对象、适用范围和来源关系。
            多条原文可以分别支持不同部分；只有关键内容全部被它们联合覆盖，整句才能 pass。
            报道、机构表态或分析观点只能证明该来源如此声称，不能直接升级为已证实事实。

            每条候选原文都要给出关系：
            - support：精确支持句中至少一个关键命题，不要求单独覆盖整句；
            - partial：与命题相关，但原文比正文更弱、含糊或缺少关键限定；
            - contradict：原文明确信息与正文相反；原文只是更弱或不确定时不是 contradict；
            - irrelevant：与本句没有关系。

            整句状态与单条原文关系是两个层次。复合句可以有局部 support，
            同时因为另一项关键内容没有依据而判 unsupported。

            状态选择：
            - unsupported：关键内容没有依据，但现有依据没有明确说反话；
            - conflicted：依据明确断言相反，或正文没有遵守相关 known_conflicts；
            - overreach：事实基础存在，但范围、因果或结论强度超过原文；
            - miscalibrated：来源归属或确定程度被升级；这类升级优先使用本状态，
              不因原文措辞更弱而改判 conflicted；
            - pass：所有关键内容均被联合覆盖，且表达分寸忠实。

            known_conflicts 只用于判断分寸，不得写入 pairs。
            present_both 不得被写成无争议事实；
            adjudicated 可以采用标记 winning 的一方，不得反用未标记 winning 的一方。
            冲突条目仅在同时也是本句候选时带 excerpt_id，且与 candidate_excerpts 使用同一短代码。

            只输出一个 JSON 对象，字段：
            {
              "statement_id": "...",
              "kind": "evidence",
              "claim_type": "fact" | "number" | "causal" | "opinion_attributed",
              "pairs": [{"excerpt_id": "<code>", "relation": "support"|"contradict"|"partial"|"irrelevant"}],
              "conflict_keys": ["..."],
              "status": "pass"|"unsupported"|"conflicted"|"overreach"|"miscalibrated",
              "reason": "简短中文理由"
            }
            - excerpt_id 只能使用 candidate_excerpts 中的短代码（如 E1, E2），禁止修改、编造或从 known_conflicts 抄写其他 id。
            - pairs 恰好覆盖每一个候选 excerpt，不得包含任何非候选条目；仅 conflicted 可以填写 conflict_keys。
            - 复合句中，片段精确支持一个命题、但另一命题缺依据时，该片段必须标 support，
              整句标 unsupported；不得因为片段不覆盖整句而把它降为 partial。
            - reason 保持简洁，只说明最主要的判定依据，不要逐条罗列或复述原文。
            """
        ).strip()
    elif kind == "derived":
        system = dedent(
            """
            你是研究报告的逐句审稿人。当前句子声明为 derived（判断句）。
            只判断这句话是否忠实于它明确列出的 candidate_excerpts 和 premises。
            同段中没有被列为 premise 的其他句子不能补足依据。进入本次语义核验的 premise
            均已通过；未通过的 premise 由代码提前拦截。

            先找出结论所需的关键事实，再判断直接原文和 premises 能否联合覆盖它们。
            研究报告可以归纳和综合；问题不在于用了几个例子，而在于结论是否诚实限定在这些材料
            能支持的范围内。因果判断必须有因果依据，只有先后或相关性不能写成导致关系。
            比较判断必须具备双方事实并使用一致口径。

            candidate_excerpts 的关系：
            - support：精确支持至少一个关键事实或判断步骤；
            - partial：相关但比正文更弱、含糊或缺少关键限定；
            - contradict：明确相反；依据只是更弱或不确定时不是 contradict；
            - irrelevant：无关。

            状态选择：
            - unsupported：结论所需的关键事实在明确依据中不存在；
            - conflicted：结论与明确依据相反，或没有遵守相关 known_conflicts；
            - overreach：事实基础存在，但归纳范围、因果、比较或结论强度超过依据；
            - miscalibrated：把报道、观点、单一来源或不确定判断升级为已证实事实；
              这类升级优先使用本状态，不因依据措辞更弱而改判 conflicted；
            - pass：结论由明确依据联合支持，且范围与确定程度相称。

            known_conflicts 只用于判断分寸，不得写入 pairs。
            present_both 不得被写成无争议结论；
            adjudicated 可以采用标记 winning 的一方，不得反用未标记 winning 的一方。
            冲突条目仅在同时也是本句候选时带 excerpt_id，且与 candidate_excerpts 使用同一短代码。

            只输出一个 JSON 对象，字段：
            {
              "statement_id": "...",
              "kind": "derived",
              "claim_type": "fact"|"number"|"causal"|"opinion_attributed",
              "inference_note": "一句话说明依据如何支持或不足以支持判断",
              "pairs": [{"excerpt_id": "<code>", "relation": "support"|"contradict"|"partial"|"irrelevant"}],
              "conflict_keys": ["..."],
              "status": "pass"|"unsupported"|"conflicted"|"overreach"|"miscalibrated",
              "reason": "简短中文理由"
            }
            pairs 只能包含 candidate_excerpts 中的短代码，必须恰好覆盖每一条；
            没有 candidate_excerpts 时输出空数组，不得从 known_conflicts 抄写 id；
            仅 conflicted 可以填写 conflict_keys，其他状态必须输出空数组。
            本句只依据 candidate_excerpts 时，pass 至少需要一条 support；
            inference_note 与 reason 保持简洁，只说明最主要的推理或判定依据，
            不要逐条罗列或复述原文。
            """
        ).strip()
    else:
        system = dedent(
            """
            你是研究报告的逐句审稿人。当前句子声明为衔接句
            （elaboration 或 limitation），因此不得承载需要外部材料核对的事实。

            contains_factual_claim 判 true（不合格）的情形：
            - 出现具体数字、比例、金额、年份或时间点；
            - 陈述具体机构、人物、地点、研究或事件做了什么；
            - 给出因果结论、归因断言或可独立核验的事实判断；
            - 以"研究表明""专家指出"等方式引述外部来源。

            判 false（合格）的情形：
            - 只承担章节转折、下文预告或前文收束；
            - 只说明本报告接下来如何组织讨论；
            - limitation 只说明现有材料未覆盖什么，不声称外部世界的真实情况。

            若句子复述材料中的具体事实，它仍然是事实句，必须改为 evidence 并绑定原文片段；
            若句子在前文事实之上得出判断，必须改为 derived 并绑定 premise。

            只输出一个 JSON 对象，字段：
            {
              "statement_id": "...",
              "kind": "elaboration" | "limitation",
              "contains_factual_claim": true | false,
              "reason": "简短中文理由"
            }
            reason 只说明最主要的判定依据。
            判 true 时只指出最主要的一处事实表达，不要逐条罗列或复述整句。
            """
        ).strip()

    user = "请审阅下面句子：\n" + json.dumps(statement, ensure_ascii=False, default=str)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def report_quality_messages(report_context: dict[str, Any]) -> list[dict[str, str]]:
    """Build the whole-report requirement and non-blocking quality review prompt."""

    schema = json.dumps(ReportQualityDecision.model_json_schema(), ensure_ascii=False)
    system = dedent(
        """
        你是深度研究报告的整体审稿人。这是第二阶段：第一阶段逐句结果已经在
        statement_checks 中给出。不要重新核对数字或复述单句失败，而要判断这些句子组合起来
        是否形成诚实、完整、与研究材料相称的答案。

        ## 必须进入修订的问题
        - core_answer：整份报告，尤其结论，没有直接回答 brief_question；
        - user_constraint：报告违反 user_constraints 中任一非空要求。
        - conclusion_support：主要结论在正文中没有必要的事实和中间判断支撑；
        - internal_consistency：引言、章节判断或结论对关键问题互相矛盾；
        - material_omission：遗漏 research_context 中足以改变答案的重要反例、冲突或局限；
        - overall_calibration：最终答案的确定程度超过整份研究实际达到的程度。

        只有实质影响答案的问题写入 requirement_failures。为每项选择最小充分修订范围：
        - repair_scope=paragraph：问题集中在一个或少数段落，paragraph_ids 必须列出真实编号；
        - repair_scope=report：中心答案或整体结构需要改变，paragraph_ids 必须为空。
        statement_ids 只引用直接相关的现有句子；缺失内容无法定位时可以为空。
        不得把“还可以写得更好”或个人文风偏好写成 requirement failure。
        不要求写入全部 research_context，也不得把 brief_text 的候选方向当作覆盖清单；
        只拦截会让读者对答案产生实质误解的遗漏。

        ## 只记录、不阻止通过的组织提醒
        - evidence_listing：整段主要在堆放事实，没有说明这些事实对于问题意味着什么；
        - repetition：多处重复前文，没有增加新的综合、解释或结论；
        - section_without_judgement：某一章节只有材料摘要，没有形成实质判断；
        - long_reasoning_chain：推理链虽然有依据，但组织得过长、跳步过多，已经影响读者理解。

        重要边界：
        - 推理层数多本身不是问题。事实→局部判断→章节判断→总结论是正常的深度研究写法。
          只有链条的表达方式已经让读者难以看清中间关系时，才记录 long_reasoning_chain。
        - 一个段落没有 derived 标签不等于必然有问题；要看它是否承担必要的背景、限定或过渡。
        - 不评价文风偏好，不要求固定段落模板，不把“还可以写得更好”写成提醒。
        - reminder 只是质量记录，不能重复逐句核验中的 unsupported、conflicted、overreach
          或 miscalibrated 问题。
        - 第一阶段失败只有在导致中心答案无法成立时才形成整篇问题，不能原样重复一次。
        - reminder 的 location 用简短中文写明位置；所有 statement_ids 和 paragraph_ids
          只能引用输入中真实存在的编号。
        - 没有明确问题时，requirement_failures 和 reminders 都输出空数组。

        只输出符合给定 JSON Schema 的单个 JSON 对象，不要 Markdown 或额外文字。
        """
    ).strip()
    user = f"请检查下面完整报告的组织质量。\n\nJSON Schema：\n{schema}\n\n报告：\n" + json.dumps(
        report_context, ensure_ascii=False, default=str
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
