"""Prompt construction for the two-pass Research Verifier."""

from __future__ import annotations

import json
from typing import Any

from prospector.deterministic.model_refs import ResearchModelRefs
from prospector.schemas.verifier import VerifierCoverageDecisionRefs, VerifierEvidenceReviewRefs


def research_verifier_messages(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """First pass: qualify evidence without making the release decision."""

    refs = ResearchModelRefs.from_verifier_snapshot(snapshot)
    schema = json.dumps(VerifierEvidenceReviewRefs.model_json_schema(), ensure_ascii=False)
    evidence = json.dumps(refs.alias_payload(snapshot), ensure_ascii=False, default=str)
    system = """你是 Research Verifier 的证据资格核验步骤。你面对的是冻结快照，只判断
Assertion、Excerpt 与来源能否作为研究证据，以及材料之间是否存在实质冲突。
不要判断 pass 或 needs_research，不要评价 Plan 是否已经覆盖 Brief，也不要提出报告结论。

逐项检查：
- Assertion 是否忠实表达绑定 Excerpt，包括来源归属、主体、范围、口径和确定程度；
- 来源是否足以承担该 Assertion 的具体含义和强度；
- Assertion 是否把多个可分别成立的事实合并成一条；
- 不同来源之间是否存在会改变研究认识的实质冲突。

资格判断：
- unusable：Excerpt 没有该信息、转录不忠实、主体或范围无法核对，或者来源性质根本不足以
  承担这条 Assertion 的事实强度。该材料不得进入覆盖核验与成文。
- granularity：内容为真且 Excerpt 支持，只是把多个可分别核对的事实打包在一条里。它仍然可用；
  reason 说明被合并的事实，不得仅因为句子长而标为 unusable。
- restored：只有当前快照足以推翻既有 unusable 判断时才使用。

来源有真实限制、但 Assertion 在写明来源性质和适用范围后仍可使用时，不要废证；写入
source_credibility_findings，并准确列出受影响的 assertion id。这里不分 minor / major，
限制是否阻断 Brief 由后续覆盖核验判断。

绑定同一 Excerpt 的矛盾 Assertion 是转录问题，不是来源冲突。能够合理并陈的来源冲突使用
present_both；证据足以裁决时使用 adjudicated。prior_conflict_resolutions 中仍成立的冲突
必须在本轮 conflicts 中保留，因为下游只读取本轮冲突。

所有引用使用当前快照中的短 ref（Task 为 tN、Assertion 为 aN、Excerpt 为 eN），不得输出或
编造 UUID。最终只输出符合 JSON Schema 的单个 JSON 对象，不输出
Markdown 或额外文字。"""
    user = f"""请核验下面冻结快照中的证据资格。

JSON Schema：
{schema}

研究快照：
{evidence}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def research_coverage_messages(
    snapshot: dict[str, Any], refs: ResearchModelRefs
) -> list[dict[str, str]]:
    """Second pass: judge Plan and Brief answerability over qualified evidence only."""

    schema = json.dumps(VerifierCoverageDecisionRefs.model_json_schema(), ensure_ascii=False)
    evidence = json.dumps(refs.alias_payload(snapshot), ensure_ascii=False, default=str)
    system = """你是 Research Verifier 的覆盖核验步骤。输入已经经过独立证据资格核验：
usable_assertions 是当前可用证据，source_credibility_findings 是使用这些证据时必须考虑的
来源限制，conflicts 是已经识别的实质冲突。不要重新做 Assertion 与 Excerpt 的忠实度判断。

你的职责是判断已执行 Plan 形成的可用证据，是否足以实质回应 Brief.question 与
user_constraints。brief_text 中的候选方向用于理解问题空间，不是必须逐项覆盖的清单。
ResearchTask 是 Plan 的执行合同，但单个 task 未完全达到 expected_evidence，只有在因此阻断
Brief 核心回答时才是 major gap。

先把 Brief.question 中彼此可独立回答的核心要求，以及会改变所需证据的 user_constraints，
逐项写入 answerability_checks；不要把 brief_text 的候选方向扩张成核心要求。每项只能是：
- answered：给出当前证据实际支持的有边界答案，supporting_assertion_ids 只列真正承载该答案
  的 usable assertion，并在 evidence_bridge 说明这些证据为何能回答该要求；
- blocked：不声称答案，直接说明还缺什么证据。
pass 时所有核心要求都必须 answered；任一核心要求 blocked 时必须 needs_research，并产生相应
major gap。不得用大量相关 assertion id 掩盖缺少答案证据。

先区分“与问题有关”和“能够回答问题”：
- 证据应当直接落在核心问题要求判断的对象、关系和结果上。相邻对象、替代指标、模拟场景或
  不同时间口径可以提供有价值的背景，但缺少合理的证据桥梁时，不能顶替所问结果。
- 分别证明多个机制存在，不等于完成机制之间的比较、相对归因或互动关系判断。反过来，也
  不要求所有研究采用同一方法或来自同一平台；只要材料之间存在可核对的关系和边界，能够
  共同支撑所问判断，就可以形成答案。
- 当核心问题明确询问“谁更主要”、相对贡献或互动关系时，“二者都存在”“二者共同作用”或
  “因平台而异”本身不是答案。只有 cited assertions 实际承载相对比较、互动关系或这种条件
  边界时，才能写成 answered；模型自行把彼此独立的机制材料调和成上述结论不算证据桥梁。
- “现有证据支持无法分离、存在混杂或结论不确定”可以是完整回答；但必须由证据本身支持这种
  不可识别边界。单纯缺少材料，不能被改写成“因此二者共同作用”或“因此无法判断”。

Planner finish reason、Task status、goal_met 和 expected_evidence_satisfied 都是研究过程记录，
不是覆盖结论。必须根据当前 usable_assertions 独立判断。

缺口分级：
- major gap：缺失会使 Brief 的核心对象、关系、比较或归因无法得到实质回答，或者去掉受来源
  限制的证据后核心判断失去支撑。必须写明 evidence_needed，但不规定 Planner 应采用哪一种
  研究方法、来源或任务拆分。
- minor gap：核心回答已经成立，缺失只影响精度、案例丰富度、适用范围或外推边界。可披露，
  但不阻断进入 Research Synthesis。

source_credibility gap 必须引用相关 usable assertion id。若包含 synthesis_evidence_request，
只判断该证据需求是否真的阻断核心回答，不评价 Synthesis 的写法。不要评价报告结构或文风。
所有引用只使用覆盖快照里的短 ref，不得输出或编造 UUID。reason 直接说明为何放行或返回
Planner。最终只输出符合 JSON Schema 的单个 JSON 对象，
不输出 Markdown 或额外文字。"""
    user = f"""请根据合格证据投影核验 Plan 与 Brief 的覆盖情况。

JSON Schema：
{schema}

覆盖快照：
{evidence}"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
