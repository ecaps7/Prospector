"""Prompt construction for per-statement Report Verifier calls."""

# JSONL contract examples intentionally remain on one physical line.
# ruff: noqa: E501

from __future__ import annotations

import json
from textwrap import dedent
from typing import Any


def report_verifier_messages(statement: dict[str, Any]) -> list[dict[str, str]]:
    kind = statement["kind"]
    if kind == "evidence":
        system = dedent(
            """
            你是研究报告的逐句审稿人。当前句子声明为 evidence（事实句）。
            输入只含句子文本与候选 Excerpt 原文。你必须判断原文是否真正支持该句。

            只输出一个 JSON 对象，字段：
            {
              "statement_id": "...",
              "kind": "evidence",
              "claim_type": "fact" | "number" | "causal" | "opinion_attributed",
              "pairs": [{"excerpt_id": "<code>", "relation": "support"|"contradict"|"partial"}],
              "status": "pass"|"unsupported"|"conflicted"|"overreach"|"miscalibrated",
              "reason": "简短中文理由"
            }
            规则：
            - excerpt_id 使用输入中的短代码（如 E1, E2），禁止修改或编造新代码。
            - pairs 必须覆盖输入中的每一个候选 excerpt 代码，数量与顺序不限，但不得遗漏。
            - pass 至少要有一条 support；unsupported 不得保留 support；conflicted 至少一条 contradict。
            - 原文未给出句子中的关键数字/事实时判 unsupported，不要猜测。
            - 一句话由多条 Excerpt 分别支撑其不同部分时，只要每一部分都有原文支持即可 pass；
              不要求任何单独一条 Excerpt 覆盖整句，覆盖不到的那几条标 partial 即可。
            - reason 保持简洁，只说明最主要的判定依据，不要逐条罗列或复述原文。
            """
        ).strip()
    elif kind == "derived":
        system = dedent(
            """
            你是研究报告的逐句审稿人。当前句子声明为 derived（推理句）。
            归纳是研究报告的本职工作，不是需要被消灭的风险；你的任务是分清
            哪些推理如实交代了自己的依据，哪些悄悄越过了材料。

            ## 输入中可用的上下文
            - premises：本句直接依据的前提句；前提是事实句时，附带它绑定的 Excerpt 原文。
            - paragraph_statements：本句所在段落的全部句子（按正文顺序，含本句）。
              归纳句概括的通常是整段事实，不止 premises 点名的那几条。
              判断"以偏概全"之前，必须先看整段实际列举了多少条同类事实。
            - premises_all_passed：前提是否已通过验证。为 false 时在 reason 中注明前提风险，
              但仍须根据推理本身独立判定 status，不得仅因前提未通过就一律判不通过。

            ## 先分类，再按各自的标准判定
            inference_type 说明本句在做哪一种推理：

            * generalization —— 从若干具体事实概括出模式、趋势或共同点。
              判定标准是**归纳范围是否如实标注**，不是"例子够不够多"：
              - 句子写明了依据范围（"这些案例显示""在已收集到的材料范围内"
                "至少五套评测均显示"等）→ pass；
              - 句子使用"普遍""必然""所有""全行业""标志着"等超出材料的全称或定性表述
                → overreach，且 reason 必须点明该补哪一处限定，不要只说"推理跳跃"。
              段落里已经列举了同类事实时，概括它们是正常写作；
              不得仅因为"只有三个例子"或"事件各自独立"就判 overreach。

            * causal —— 声称 A 导致 B、A 是 B 的原因，或用"因此""源于""驱动"表达因果。
              标准最严：材料必须给出因果证据。只有时间先后、同时发生或相关性 → overreach。

            * comparison —— 对比两个或多个对象、时期或地区。
              双方事实都在材料中且比较口径一致 → pass；口径不一致或一方缺事实 → unsupported。

            * restatement —— 只把前文几句重新罗列一遍，没有新增判断。
              这类句子判 pass，但 reason 必须写明"复述型，无新增判断"。

            ## status 的含义
            - unsupported：推理所需的事实在前提和所在段落中都不存在。
            - miscalibrated：把媒体报道、分析师观点或单一来源写成确凿事实。
              判这一档前先读 premises 里的 Excerpt 原文，确认原文本身的口径。
            - conflicted：推理与前提或 Excerpt 原文互相矛盾。
            - overreach：按上面各类型的标准越界。

            只输出一个 JSON 对象，字段：
            {
              "statement_id": "...",
              "kind": "derived",
              "claim_type": "fact"|"number"|"causal"|"opinion_attributed",
              "inference_type": "generalization"|"causal"|"comparison"|"restatement",
              "inference_note": "一句话说明推理步骤",
              "status": "pass"|"unsupported"|"conflicted"|"overreach"|"miscalibrated",
              "reason": "简短中文理由"
            }
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

            若句子复述材料中的具体事实，它仍然是事实句，必须改为 evidence 并绑定 Excerpt；
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
