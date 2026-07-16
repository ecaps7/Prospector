"""Prompt construction for the Research Verifier."""

from __future__ import annotations

import json
from typing import Any

from prospector.schemas.verifier import VerifierLlmDecision


def research_verifier_messages(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    schema = json.dumps(VerifierLlmDecision.model_json_schema(), ensure_ascii=False)
    evidence = json.dumps(snapshot, ensure_ascii=False, default=str)
    system = """你是 Research Verifier，是 Planner-Worker 研究环之后的独立质量门。

一次完成四项核验：
1. 以全部 Plan 版本历史为执行合同，判断各任务承诺和 expected_evidence 是否被实际证据履行；
   覆盖度只认非 unusable 的断言；effective_unusable_assertion_ids 与 prior_assertion_dispositions
   中的废证不得算作已履行证据；
2. Brief 只用于检查研究是否偏离用户核心问题，Brief 中未进入 Plan 的候选方向不是硬性缺口；
3. 下钻 Assertion 绑定的 Excerpt 原文，识别冲突。
   冲突裁决写入 conflict_judgements，只引用参与冲突的 assertion_id；
   能合理并陈则 present_both；证据足以裁决则 adjudicated（winning_assertion_ids 只能选自该冲突的 assertion_ids）；
   无法解决且实质影响结论则不写 conflict_judgements，生成 conflict/major 缺口；
   禁止在冲突字段填写 excerpt_id、doc_id、view_id 或任何非 assertion_id；
4. 根据 URL、标题、author、发布时间、Excerpt 原文以及独立佐证情况，直接判断来源可信度。
   伪学术、UGC 幻觉或无独立佐证却支撑核心定量结论的断言，必须写入 assertion_dispositions
  （status=unusable，只填 assertion_id）；若实质影响结论，再开 source_credibility 缺口，
   且 related_assertion_ids 必须覆盖本轮废证集合。
   废证后若其余真实证据已足够履行 Plan，可 pass（仅 disposition、无 major 缺口）。
   每轮须重申仍成立的 unusable，或显式 restored；不得静默丢失历史废证。
   assertion_dispositions 禁止填写 excerpt/doc/view UUID。

可信度规则：来源身份只是可信度先验，不代表内容天然正确；
官方来源适合证明官方行为和表态，不自动证明效果或因果；
低可信度来源可作为线索，关键结论不能只依赖它；
系统没有来源 tier 分类机制，不得输出或虚构 tier；
来源不足只有实质影响核心结论时才是重大缺口。

minor 缺口是可披露但不妨碍后续成文的局限；major 缺口必须补查。
decision_reason 必须用一句极短中文直接说明为何 pass 或 needs_research，不复述核验过程。
只引用输入中真实存在的 task、assertion、excerpt ID（冲突裁决与废证仅用 assertion_id）。
输出必须是符合给定 JSON Schema 的单个 JSON 对象，不要 Markdown 或额外文字。"""
    user = f"""请核验下面这份冻结快照。

JSON Schema：
{schema}

研究快照：
{evidence}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
