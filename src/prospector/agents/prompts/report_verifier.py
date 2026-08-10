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
            - reason 保持简洁，只说明最主要的判定依据，不要逐条罗列或复述原文。
            """
        ).strip()
    elif kind == "derived":
        system = dedent(
            """
            你是研究报告的逐句审稿人。当前句子声明为 derived（推理句）。
            前提句是否已通过验证由字段 premises_all_passed 给出；若为 false，
            请在 reason 中注明前提风险，但仍须根据推理本身的论证质量独立判定 status，
            不得仅因前提未通过而一律判 unsupported。

            只输出一个 JSON 对象，字段：
            {
              "statement_id": "...",
              "kind": "derived",
              "claim_type": "fact"|"number"|"causal"|"opinion_attributed",
              "inference_note": "一句话说明推理步骤",
              "status": "pass"|"unsupported"|"conflicted"|"overreach"|"miscalibrated",
              "reason": "简短中文理由"
            }
            重点拦截：把相关说成因果、把个例说成规律、把多家报道说成确凿事实（miscalibrated）、
            推理跳跃（overreach）。
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
