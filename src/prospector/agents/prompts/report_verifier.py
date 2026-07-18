"""Prompt construction for per-statement Report Verifier calls."""

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
            """
        ).strip()
    else:
        system = dedent(
            """
            你是研究报告的逐句审稿人。当前句子声明为衔接句（elaboration 或 limitation），
            声称不含需核验的事实主张。请检查是否夹带了具体事实、数字、因果结论或归因断言。

            只输出一个 JSON 对象，字段：
            {
              "statement_id": "...",
              "kind": "elaboration" | "limitation",
              "contains_factual_claim": true | false,
              "reason": "简短中文理由"
            }
            """
        ).strip()

    user = "请审阅下面句子：\n" + json.dumps(statement, ensure_ascii=False, default=str)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
