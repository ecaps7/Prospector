# Prospector 设计文档

Prospector 是查证型深度调研系统：把一个研究问题展开成研究简报（Brief），在公开网络上采集证据，
检查证据是否可靠、能否回答问题，再生成 Markdown 报告和审计 JSON。
报告中的事实陈述由 Attribution 核对出处，引用由代码生成。

本文说明系统应遵守的规则、各环节的职责，以及数据如何保存和交付，供实现和测试共同参考。
精确字段与类型见 `src/prospector/schemas/`，数据库结构见 `migrations/versions/`，
实现入口在相应章节列出，不在这里逐项复制。

代码与文档不一致时，需要判断哪一侧应当修改，不能默认代码正确。
本文用“实现差异”标明已发现的问题，用“待确认”标明仍需决定的行为；
这些说明不代表问题已经修复，也不能作为保留错误行为的理由。

## 1. 产品范围与领域概念

### 1.1 系统做什么

从提问到获得报告，流程包括：

1. 把自然语言问题展开为 Brief，必要时向用户澄清一次，并经人工确认冻结；
2. 由 Planner 规划研究任务、Worker 并行采集公开网页证据；
3. Research Verifier 检查证据是否可用、能否回答问题，必要时要求 Planner 补研；
4. Research Synthesis 在成文前形成分析底稿；
5. Writer 写出完整报告，Attribution 对正文逐句归因到证据，
   Review / Readthrough 做整体与行文检查，未达标则在限额内修订；
6. 由代码生成带脚注引用的报告 `.md` 与审计 `.json`。

用户确认 Brief 后才创建 Job，开始执行研究。Scope 和确认过程发生在 Job 之前。

报告核验结果与运行成败是两回事。当前实现用 `verification_status`
（`verified / partial / failed`）保存报告核验结果；报告未完全通过核验时仍可交付，
但运行异常导致没有完成报告时，不能称为已交付。修订耗尽后的结果规则见 §2.8。

### 1.2 系统不做什么

- **不接受附件与私有语料**。当前只采集公开网页，没有上传入口。
- **不支持执行中修改研究要求**。运行开始后可以取消，但不能改写已经确认的 Brief（§5.5）。
- **不把抓取全文交给任何 Prospector LLM**（D12，见 §3.1）。全文只写对象存储归档。
- **不因工具调用总数或总运行时长达到某值而停止研究**。研究次数限制见 §4。
- **不产出 HTML / PDF / 图表**。交付形态固定为 Markdown 与 JSON。
- **不是通用 Agent 框架**。Data Worker、私有知识库、分布式调度和多用户调度
  不属于当前实现范围，`docs/future/` 中的草案不能作为现有功能的验收要求。

创建 Job 的 API 接收完整的 `ResearchBrief`；自然语言问题先经过 Scope（§2.1）。

### 1.3 投入档位（effort）

`effort ∈ {quick, standard, deep}`，写入 Brief，决定以下研究次数与并发上限
（`src/prospector/deterministic/budget.py`）：

| effort | Planner 决策轮上限 | 每批并发任务 | 每任务 Worker 轮上限 |
|---|---|---|---|
| quick | 8 | 6 | 12 |
| standard | 12 | 5 | 20 |
| deep | 24 | 6 | 32 |

这些数值限制一次研究最多投入多少执行轮次，不保证在限额内一定能找到足够证据。
预算用尽后仍需按 §4 接受核验，不能直接跳到报告交付。

### 1.4 领域概念

Planner 根据 Brief 制定 Plan，并拆出 ResearchTask。Worker 抓取材料，保存 Document
与 DocumentView，再通过 `save_findings` 保存 Excerpt 与 Assertion。
Verifier 判断哪些 Assertion 可用并处理冲突；Synthesis 与 Writer 使用通过核验的材料。
Attribution 为报告中的陈述建立 Claim，已验证陈述通过 ClaimEvidence 关联 Excerpt。

| 概念 | 含义 | 定义位置（相对于 `src/prospector/`） |
|---|---|---|
| Job | 用户确认 Brief 后的一次研究执行，包含研究、成文、核验和交付。生命周期见 §5.1 | `schemas/brief.py`、`store/repositories/jobs.py` |
| Brief | 已确认且不再修改的研究输入。问题展开与用户明确限制分开保存；候选方向不是必做清单 | `schemas/brief.py` |
| ScopeOutcome | Scope 的结果：需要澄清，或已有待确认的 Brief | `schemas/brief.py` |
| Plan | 有版本的执行计划，说明本轮派发哪些研究任务，以及响应哪次核验 | `schemas/plan.py` |
| ResearchTask | 一个具体研究问题、应取得的证据要求（`expected_evidence`）及 Worker 次数上限 | `schemas/plan.py` |
| Document | 工作区共享的网页全文快照，内容变化时保存新版本。全文不进入模型上下文 | `schemas/evidence.py` |
| DocumentView | 属于某个 Job 和 Task 的材料视图，保存 Exa 返回的任务相关高亮及其编号 | `schemas/evidence.py` |
| Excerpt | Worker 选用的精确原文片段，属于某个 Job，记录来源版本和所选高亮。原文对应要求及实现差异见 §3.1 | `schemas/evidence.py` |
| Assertion | 绑定一个或多个 Excerpt 的结构化断言。同一任务内重复保存相同陈述时，追加证据绑定 | `schemas/evidence.py` |
| VerifierRun / Gap | 一次研究核验及其发现的缺口。核验给出是否继续研究、冲突处理和断言是否可用；重大缺口必须说明还需什么证据 | `schemas/verifier.py` |
| ResearchSynthesisRun | 基于一次研究核验结果形成的分析底稿，以及是否需要补充研究的判断 | `schemas/report.py` |
| WriterSnapshot | 成文使用的材料集合：Brief、计划摘要、全部可用断言及其 Excerpt、相关冲突和次要缺口 | `schemas/report.py` |
| Report / Revision | 一个 Job 的报告及其修订版本。正文按 Markdown 块保存文本、哈希和字符位置 | `schemas/report.py` |
| Claim（ClaimSpan） | Attribution 对一段正文的核验记录，包含原文位置和核验结果 | `schemas/claims.py` |
| ClaimEvidence | 已验证 Claim 与支撑它的 Excerpt 的关联，是生成脚注的依据 | `schemas/claims.py` |
| ClaimPremise | 分析性陈述依赖的其他 Claim、Assertion 或已知冲突 | `schemas/claims.py` |
| Finding | 核验或审查发现的具体问题。是否触发修订由问题类型和预算决定，不能把所有问题都当作报告失败 | `schemas/claims.py`、`agents/report_readthrough.py` |
| verification_status | 报告核验结果，与 Job 是否执行完成分开保存（§2.8、§5.1） | `schemas/claims.py` |

## 2. 完整研究与报告流程

主要执行路径如下。节点定义见 `src/prospector/flow/research_graph.py`；
拒绝决策、取消和错误处理分别见 §2.2、§4、§5.5。

| 当前环节 | 条件 | 下一环节 |
|---|---|---|
| initialize | 初始化完成 | planner |
| planner | 接受派发决策 | workers，执行后回 planner |
| planner | 接受结束决策，或预算用尽但已有证据 | verifier |
| verifier | 需要补充研究，且仍有预算 | planner |
| verifier | 通过常规核验且不再安排跟随轮 | synthesis |
| synthesis | 分析足以回答问题 | writer |
| synthesis | 请求补充证据 | verifier，重新判断是否确实需要补研 |
| verifier | 否决 Synthesis 的补研请求 | writer，带上核验说明 |
| writer | 正文生成或修订完成 | attribution，再到 review |
| review | 需要修订且还有预算 | writer |
| review | 不再修订，Readthrough 也未请求修订 | render，再到 END |

节点进入与退出时检查取消请求。恢复执行时，哪些结果可以复用由 §5.4 说明；
不能把“恢复执行”理解成所有模型和工具都恰好只调用一次。

`schemas` 定义数据结构，`store` 保存数据，`agents` 调用模型，`flow` 组织执行顺序，
`api / runtime / cli` 提供入口。`flow` 不导入 `runtime`，图状态中不能放连接、锁或协程。
`deterministic` 与 `reporting` 只执行代码，不调用模型。

### 2.1 输入确认（Scope + 人工确认）

**职责**：把问题变成可执行的 Brief；不规划、不采集。

- `decide_clarification`：只有问题歧义会实质改变研究方向时才澄清，
  **至多一次**；`run_scope` 收到澄清答案后不再二次澄清（`src/prospector/agents/scope.py`）。
- `write_research_brief`：思考模式流式生成；解析失败时用关闭思考的 `json_object` 调用修复一次。
  支持「上一版 Brief + 一条修订指令」重写；调用方 `effort` 强制覆盖模型输出。
- Web 页面先展示 Brief；直接编辑或要求模型修订后，仍需用户确认才能创建 Job。
  修订响应本身不能启动研究（`web/src/pages/AskPage.tsx`）。
- CLI 入口（`prospector` 与 `prospector-local ask`）进入确认循环
  （`src/prospector/runtime/hitl/brief_confirm.py`）：`c` 原样确认；`e` 打开编辑器直接改
  （不限轮数，改到通过校验为止）；`i` 一条指令让模型修订，**仅限一轮**，且修订后的 Brief
  回到循环顶部重新确认。非交互终端直接拒绝。
- 确认后保存不可修改的 Brief、创建 Job，并记录 `brief.confirmed` 事件。
  CLI 的确认方式记录为 `c / e / i`；这些按键不是 Web 或 API 的通用交互规则。

**模型上下文**：只有问题文本（加澄清对 / 上一版 Brief）。

### 2.2 Planner 决策轮

**职责**：唯一的研究策略决策者——决定继续派发任务还是宣布研究完成。

输入是只追加、不改写的消息记录 `planner_messages`。代码限制可以追加的内容
（`src/prospector/agents/planner.py`）：只允许五类运行时反馈——
`worker_projection`（Worker 完成任务后的摘要）、`rejection`、`schema_error`、
`verifier_gap`、`research_state`；递归拒绝 `document_text`、`worker_trace` 等键。
初始消息把 `user_constraints` 渲染为「用户明确要求」，与「可探索的研究方向」分开
（`src/prospector/agents/prompts/planner.py`）。

每轮通过 `research_state` 明确告知模型（`flow/research_graph.py:_research_state_message`）：
可用决策、剩余决策轮、每批并发上限、Worker 轮上限、动作集 `[search, save, finish]`、
工具集、`max_parallel_tool_calls=8`、`search_auto_fetch_top_n=2`、`finish_allowed`。

决策只有 `dispatch`（至少一个任务）或 `finish`（不能带任务），见 `schemas/decisions.py`。

- `dispatch` 超过并发上限时整批拒绝（`OVER_CONCURRENCY`）；接受后由代码给任务设置
  预算，保存 Plan，再执行 Worker。
- `finish` 在 Job 尚无 Excerpt（`EMPTY_FINISH`）或跟随轮暂不允许结束时
  （`FINISH_WITHHELD`，§4.3）被拒绝；接受后以 `planner_finish` 触发研究核验。
- 输出格式不合法时尝试修复一次，仍失败则记录 `schema_error`。

**模型上下文**：Brief、决策记录和上述反馈。Planner 看不到 Document 全文与 Worker
逐轮执行记录，只接收断言摘要、拒绝原因、缺口和预算余额。

### 2.3 Worker 采集

**职责**：围绕一个研究任务搜集并保存证据；不决定其他任务的研究策略。
同一批任务由 `asyncio.gather` 并行执行（`flow/research_graph.py:_workers_node`）。

每轮一个严格 JSON 动作（`json_object` 而非供应商 Function Calling）：

- **search**：`web_search` 只回元数据（标题 / URL / 日期 / 作者）。运行时自动对前 2 条
  结果执行 `web_fetch`（`AUTO_FETCH_TOP_N`）；Worker 不能手动调用 `web_fetch`。
- **save**：`save_findings` 以 `sN:hM` source_ref 指明「第 N 次抓取的第 M 条高亮」，
  由 `EvidenceSourceRegistry` 解析；代码检查视图属于当前任务、文档版本一致，
  且所选高亮编号都存在于视图中
  （`src/prospector/tools/save_findings.py`）。
- **finish**：声明任务结束。

每次成功保存后，运行时不带此前对话，单独调用 `assess_coverage` 检查任务是否完成。
`expected_evidence` 规定证据需要满足的条件；同时还要实质回答任务问题，
且没有仍可补齐的重要缺口，才算完成。

停止条件见 `src/prospector/agents/research_worker.py` 和 `deterministic/gates.py`：

- 模型声明无法继续的理由：`no_public_evidence` / `low_information_gain` /
  `blocked_by_scope`；
- 运行时停止：`expected_evidence_satisfied`、`worker_rounds_exhausted`、
  `low_information_gain`（连续 2 次空保存）、`repeating_without_progress`
  （连续 3 轮执行相同动作却未保存新证据；比较动作时不计自动抓取的附加调用）、`tool_error`。

其他机制：每轮最多 8 个并行工具调用。超过 2 轮未用的抓取高亮会从上下文中移除，
只留下提示；需要时应重新抓取，不能凭印象保存证据。任务结束后，
`submit_worker_summary` 在不带此前对话的上下文中生成摘要，每条断言最多 1000 字符，
再把摘要反馈给 Planner。Worker 动作仍是 `search / save / finish`，摘要调用由运行时负责。

**模型上下文**：任务问题、`expected_evidence`、运行时消息、此前各轮的动作与结果
（含任务级高亮，并定期移除旧高亮）、已保存的证据记录。不包含 Document 全文。

### 2.4 Research Verifier（研究核验）

**职责**：证据资格与研究覆盖的唯一裁决者。它不做报告级判断。

触发有三种（`verifier_runs.trigger`）：`planner_finish`（Planner 宣布完成）、
`budget_exhausted`（决策预算耗尽但尚有证据）、`synthesis_gap`（Synthesis 请求补研，§2.5）。
键为 `(job, plan_version, trigger)`，所以同一个 plan 版本可以因不同触发被核验多次。

两段式（`src/prospector/agents/research_verifier.py`）：

1. **资格核验**：冻结快照包含 Brief、全部 Plan/Task、全部 Assertion 与其 Excerpt 原文、
   来源元数据、历史冲突裁决与历史处置。模型逐项检查断言是否忠实于原文、来源是否足以
   支持它、是否合并了多个事实，以及证据是否冲突。
   输出 `unusable` / `granularity`（合并多个事实但仍可使用，保留相关说明）/ `restored`
   （仅当本轮材料足以推翻此前的不可用判断）。来源可信度问题不在此分级，写入
   `source_credibility_findings`，是否阻断由覆盖核验判断。此阶段不判放行。
2. **覆盖核验**：输入由代码整理，**不含 Excerpt 原文**，只有可用断言、
   来源身份与可信度提示（`deterministic/verifier_projection.py`）。模型把
   `Brief.question` 的核心要求逐项写入 `answerability_checks`，每项只能 `answered`
   （给出答案、支持答案的断言，以及从证据得出答案的解释）或 `blocked`（说明缺什么）。
   全部 answered 才能 pass。

代码检查并保存结果：

- 保存冲突记录要求至少两条不同 Excerpt，否则按同一原文的转录问题拒绝
  （`schemas/verifier.py:materialize_conflict_resolutions`）；
  `conflict_key = sha256(排序后的 excerpt id)` 作跨轮去重键。
- 重大来源可信度缺口由**代码**强制相应断言为 unusable
  （`derive_credibility_dispositions`），不依赖模型自觉。
- 引用校验：所有短编号必须能找到对应记录，否则按输出错误处理。

裁决去向：

- **pass**：先判断是否需要「跟随轮」（§4.3）；否则将研究结果记为 `ready_for_writer`。
  `planner_finish` / `budget_exhausted` → synthesis；`synthesis_gap` 放行则直接 → writer
  （Synthesis 的补研请求被否决，否决理由作为本轮次要缺口传给 Writer，
  报告按已有分析和明确的证据限制成文）。
- **needs_research**：重大缺口与不可用断言清单以 `verifier_gap` 反馈回 Planner
  （`gap_origin` 区分 `verifier` / `research_synthesis` / `verifier_follow_up`）。
  若剩余决策轮不大于零，则记录 `verifier_major_gap` 并抛出 `VerifierMajorGapError`，
  由执行入口结束 Job。预算计算见 §4.1。

输出 JSON 语法错误时交给不带研究材料的模型修复；内容不符合约定时，Verifier 重试一次。
仍失败则记录 `verifier_output_invalid` 并抛出异常，保留原始输出供诊断。
记录错误与写入 Job 终止事件不是同一步，见 §4.5。

### 2.5 Research Synthesis（研究综合）

**职责**：成文前的连续分析——材料合起来意味着什么。不是提纲，不是压缩版报告。
键为 `(job, verifier_run_id)`，一次核验对应一次综合。

流程见 `src/prospector/agents/research_synthesis.py`：

1. 初稿：通读 Brief 与按研究任务分组的材料，形成实质回应 Brief 的分析；
   材料足以回应时 `ready`，只有缺证据导致无法实质回应时才 `needs_research`
   （仍写出有限分析并说明还缺什么）。
2. 独立审查：只基于 Brief、初稿、各任务的问题与断言数量、冲突和缺口，
   分别检查四类问题：`brief_not_answered`、`missing_relationships`、
   `evidence_catalog` / `missing_selection`、`unsupported_overreach`。
   有实质问题时必须给出完整修订稿；是否采用由代码根据问题列表判断，模型不直接决定。
   不因文风、篇幅或「另一种同样合理的写法」改写。

去向：`ready` → writer；`needs_research` → verifier（触发 `synthesis_gap`）。
只有 Research Verifier 确认的重大缺口才反馈给 Planner；被否决的请求作为次要缺口传给 Writer。
输出不符合约定时记录 `synthesis_contract_error` 并抛出异常。

**模型上下文**：与 Writer 使用同一组可用材料，按任务分组，Excerpt 按字符限制裁剪。
来源可信度说明附在对应断言上，模型用 `aN / xN` 等短编号引用材料（§3.3）。

### 2.6 Writer（成文与修订）

**职责**：写出（或修订出）完整报告正文。不判事实、不生成脚注。

输入是 `WriterSnapshot`，只包含可用材料和裁剪后的 Excerpt 原文；还包括 Synthesis
最终采用的分析（`decision / synthesis / reason / evidence_needed`），不包含内部审查记录。

- **首写**：输出完整 GFM Markdown。代码用 `parse_markdown` 检查格式：拒绝原始 HTML 与
  Writer 自造脚注；块按 `b_0001..` 编号，记录 `text_hash` 与字符偏移
  （表格单元格文本会重复，因此不能只靠搜索文字定位）。输出不符合约定时重试一次，
  仍失败则记录 `writer_contract_error` 并抛出异常。
- **修订**（审查判 `revising` 时）：Writer **不重写全文**，只提交
  `ReportRevisionPatch`——若干「块区间 + 新 Markdown」替换（空文本即删除）。
  代码应用替换（`apply_block_replacements`）：拒绝越界与重叠，计算 `new_regions`
  （新写文本的字符区间）供增量归因使用。替换是模型唯一能改动正文的方式，
  未列入替换的块原样保留。是否能复用旧归因结果，还要检查它依赖的内容有无变化（§2.7）。
- 修订反馈说明具体问题：影响主要结论的归因失败、整体审查问题和行文问题，
  不把内部缺陷类型名直接当作给 Writer 的解释。

开始修订前，上一轮 Attribution 与 Review 必须完成；若已有 Readthrough 结果，一并提供。
`begin_markdown_revision(bump=True)` 递增 revision。

### 2.7 Attribution（后置句级归因）

**职责**：对当前版本正文中的事实陈述核对出处。这是报告链中唯一对照证据判断事实的环节。
当前版本完成核验后仍可能修订，不能把 Writer 输出称为最终定稿。

代码先整理核验输入（`deterministic/markdown_report.py`）：

1. 扫描数字、日期、带出处线索的引语、范围词和建议词，区分必须核对、待判断和建议性标记。
   用于识别人名、机构等实体的名单从研究材料生成，数量上限由代码规定。
2. 将相关子句标为候选位置（`kN`）。修订版先判断哪些结果可以复用，再处理需要重新核验的块。
3. 按正文块和候选位置的字符数安排批次，模型不能自行改变批次范围。
   当前分批预算不包含随后展开的 Excerpt，单个过长的块也不会被拆开；
   因此这个数值不是完整模型输入的硬上限。

每个批次分两步（`src/prospector/agents/report_attribution.py`；批次之间可并行）：

1. **选材**：给模型本批正文块、候选位置和只有陈述文本的断言目录（短编号 `aN`），
   选出可能相关的断言。当前实现会过滤目录中不存在的编号，并写入审计记录；
   这不表示所选材料已经支持正文，仍需下一步核对。
2. **核对**：展开所选断言的 Excerpt，模型提取最小可核对陈述并判定：

   - `verified`：必须绑定真正支持该句的 Assertion 与 Excerpt，且 Excerpt 属于所选 Assertion；
   - `failed`：必须说明正文写了什么、材料支持什么、差异在哪；
   - `analysis`：分析性内容，声明它依赖的本批 Claim、其他批次的位置（`kN`，由代码解析）、
     Assertion 或冲突；不要求 Excerpt 表达相同观点。

代码检查并汇总结果：

- Claim 的文本与 `text_hash` 必须与保存的正文一致，引用候选位置时不能超出其范围；
  没有任何核对结果的候选位置记录为“无核对结论”问题；
- 分析性 Claim 必须有可追溯的依据：沿依赖关系能找到已验证的 Claim 或直接 Assertion，
  否则记录“依据落空”问题（`ungrounded_claim_ids`）；
- 单批无效 Claim 超过 20%（`MAX_INVALID_CLAIM_RATIO`）时按输出错误处理；
  比例未超过上限的无效记录被丢弃，并保留原因；
- 修订轮必须对上一版每条核心失败逐一交代去向：`corrected` / `replaced_source` /
  `removed` / `in_place_downgrade`（把具体事实改成笼统说法，作为新问题记录）；
- **复用未变部分**：`plan_incremental_attribution` 按文本哈希和块的顺序匹配新旧版本。
  文本相同且依据未变化的块复用旧结果；改写的块及依赖被改写内容的陈述重新核验。
  审计说明和没有绑定具体 Claim 的问题记录也要保留，不能因复用而丢失问题。
  没有需要重新核验的块时，不创建批次线程池、不调用批次选材与核对；仍处理旧核心问题的去向，
  合并并保存可复用的结果，再进入整体审查。没有新批次不等于报告通过核验。
- 批次持久化状态 `prompted → selected → completed`（失败 `failed`）；
  某一批失败不丢弃其他已经完成的批次。

**实现差异：** 提示词要求 `requires_evidence=true` 的候选必须得到 `verified` 或 `failed`
结论，但当前覆盖检查也把引用该候选的 `analysis` 算作已有结果，只另记未核对标记。
因此该要求尚未由代码完整保证，不能用“有 Claim 记录”代替“具体事实已经核对”的验收。

### 2.8 Review 与 Readthrough

**Whole-report Review**（`src/prospector/agents/report_review.py`）：
判断报告作为整体是否成立——是否实质回应 Brief、是否遵守 `user_constraints`、
是否诚实处理会改变核心认识的反例/冲突/证据边界、主要结论是否有可识别的推理链。
它**不是材料覆盖检查**：代码算出 `unused_assertions`，模型只回答其中有没有
「写进去会改变核心结论」的（`material_omission`）。
`key_block_ids` 只列直接承载核心回答的少量正文块；超过 `max(3, 50%×块数)` 时，
代码丢弃该清单。模型只拿到归因结果摘要及其依据关系，不重新接收 Excerpt 原文，
也不重复 Attribution 的事实核对。
核心块的筛选阈值是当前实现方式，仍需用真实报告案例验证，不能据此认定核心问题的判断已经可靠。

报告核验结果由代码根据归因与整体审查记录计算，不能由模型直接决定是否通过。
规则如下，计算实现见 `schemas/claims.py:final_report_status`，修订预算见 §4.4。

| 条件 | 结果 | 含义 |
|---|---|---|
| 归因与整体审查都没有发现阻断问题 | `verified` | 通过现有核验 |
| 有核心问题，且还有修订预算 | `revising` | 返回 Writer 修订，尚未形成最终核验结果 |
| 修订预算耗尽后仍有核心问题 | `failed` | 核心内容未通过核验 |
| 只有非核心问题 | `partial` | 仍有未解决的问题，但未被判定为影响核心回答 |

`verified` 不保证报告绝对正确，`failed` 也不表示全文全部错误。

核心问题包括整体审查未通过、`in_place_downgrade`、被其他 Claim 依赖的失败 Claim，
或落在核心正文块中的失败 Claim。非核心问题保存在审计记录中，产生 `partial`，
但不单独触发事实修订。

无论最终核验结果为 `verified`、`partial` 还是 `failed`，都继续生成报告文件和审计记录。
正常完成交付与收尾后，Job 状态为 `completed`，报告核验结果单独保留。
这不代表运行异常导致交付未完成时，也可以把 Job 标为完成。

修订预算耗尽后交付最后一个报告版本，保留未解决问题的记录，不恢复为首稿，
也不把失败陈述自动改判为通过。未验证陈述不得生成已验证脚注。

**Readthrough**（`src/prospector/agents/report_readthrough.py`）：只读正文，检查行文是否通顺。
当前版本未被 Attribution 与 Review 要求修订时才运行（`status != revising`）。
这不要求事实核验全部通过：预算耗尽、报告核验结果为 `failed` 时，也进行行文检查。
只报告读者会实际卡住的四类问题：
`dangling_reference` / `broken_transition` / `summary_mismatch` / `orphaned_passage`。
它不提出风格偏好，不要求补充材料，只能请求修改文字，不直接改变报告的事实核验结果。
结果按报告版本保存；修改正文后可以重新检查，修订次数与预算规则见 §4.4。

### 2.9 生成并交付报告文件

`render` 节点按以下顺序交付。恢复时若发现该版本已标为 `report_rendered`，
直接返回已有引用，不重新发布交付事件。

1. `render_final_report`（`deterministic/citation_render.py`）产出两份交付物（§6.1）。
2. 写入对象存储 `{workspace}/reports/{job_id}/{revision}/report.md` 与 `report.json`。
3. `complete_v2_report_render` 在同一个数据库事务中保存文件引用、哈希、报告已渲染状态、
   `report.draft_rendered` 事件和 `jobs.outcome=report_rendered`。
4. 图返回后，由执行入口调用 `finalize_success`，另开事务写入
   `jobs.status=completed` 和 `job.stopped`。

第 3、4 步之间可能中断：报告已经就绪，但 Job 仍显示运行中。
恢复执行时应复用已保存的报告，再完成 Job 收尾，不能重新生成报告或重复发布交付事件。
对象存储写入也不属于数据库事务；具体边界见 §5.3。

## 3. 证据链与引用规则

### 3.1 采集路径（D12）

```
web_search（仅元数据）
  → 运行时自动 web_fetch 前 2 条
      → 媒体探测（%PDF- 魔数）
      → Exa /contents：全文 + 面向任务问题的高亮
          全文   → Document 快照：content_hash 去重、版本递增、对象存储归档（永不进模型上下文）
          高亮   → DocumentView（任务级，items 带 source_ids h1..hN）
  → Worker 以 sN:hM 选证据
      → save_findings：校验视图归属/文档版本/来源集合后，原子写入 Excerpt + Assertion
```

保存时机与原文规则：

- 抓取成功时，`web_fetch` 必须保存 Document 全文快照和任务级 DocumentView 高亮，
  不要求此时创建 Excerpt。Document 记录抓到了什么，DocumentView 记录给 Worker 看了什么。
- Worker 选用材料并提交断言后，由代码解析 `source_ref`，从已保存的高亮中提取文字，
  通过 `save_findings` 原子保存 Excerpt、Assertion 及其关联。已有相同记录时复用。
  未选中的材料仍保存在 Document 和 DocumentView 中，不自动创建 Excerpt。
- “已抓取并留档”“已选用为研究证据”和“已通过核验”是不同阶段。
  保存 Excerpt 与 Assertion 不代表断言已经得到证据支持，仍须由后续核验判断。
- Excerpt 必须能对应到指定版本 Document 快照中的精确原文，不能仅凭高亮编号存在
  就认定符合这一要求。
- 全文不进任何 Prospector LLM 上下文。模型只见任务相关高亮与 Excerpt，不能用自己
  生成的摘要代替 Excerpt。Excerpt 保存后不改写，提示词裁剪也不修改数据库原文。
- 提示词中的长 Excerpt 可以省略中间部分，但要保留首尾并明确标出省略位置。
  `deterministic/excerpt_text.py` 根据本次输入的 Excerpt 数量计算每条的字符上限，
  上限范围为 400–1500 字符；160,000 是用于分配字符数的目标值，不是整个 Job 的硬上限。
  Synthesis、Writer 与 Attribution 使用同一裁剪函数，但材料数量不同可能导致裁剪长度不同，
  不能保证每次看到的片段完全相同。
- 全文或高亮为空时，本次抓取报错，不向 Worker 返回可选证据。
  当前顺序可能先保存 Document、后发现高亮为空；报错不表示此前没有留下快照。

**实现差异：** 当前 `save_findings` 直接采用已保存的 Exa 高亮，没有逐字比对它是否出现在
对应版本的归档全文中。`locator` 记录的是视图编号和高亮编号，不是全文中的字符位置。
因此，目前能回查高亮及其来源版本，但尚未由代码保证高亮就是快照中的精确原文。
这与 Excerpt 的创建时机是两个问题，提前创建记录不能代替原文对应检查。

### 3.2 材料归属与可用性

- DocumentView 属于 `(job, task)`；`save_findings` 校验 `view.job_id/task_id`、
  `doc_id/doc_version` 一致、请求的 `source_ids` 全部在视图内，否则拒绝。
- 汇总多轮 Verifier 结果时，按计划版本采用最新处置；`restored` 可以恢复此前被判断为
  不可用的断言。相关计算见 `effective_unusable_assertion_ids`。
- Writer / Synthesis / Attribution 的 `WriterSnapshot` 只含有效可用的断言；
  冲突取放行那次核验的裁决；`source_credibility` 缺口不作为全局缺口，
  而是作为 `source_caveat` 附在相关断言上，要求使用该材料时说明它的限制。

### 3.3 模型使用的短编号

模型引用任务、断言、证据和冲突时使用短编号，不使用数据库 UUID。
代码在发送输入前建立映射，在解析输出时还原，见 `deterministic/model_refs.py`。

| 编号 | 指代 | 使用者 |
|---|---|---|
| `tN` | ResearchTask | Verifier |
| `aN` | Assertion | Verifier / Synthesis / Writer 侧 / Attribution |
| `eN` / `aNeM` | Excerpt（前缀即所属断言） | Verifier / Attribution |
| `xN` | 冲突 | Verifier / Synthesis / Attribution |
| `kN` | 报告候选位置 | Attribution 批次间引用 |
| `sN:hM` | 抓取结果的高亮 | Worker 保存证据 |

未知编号通常作为输出错误处理。Attribution 的选材阶段目前会过滤不存在的目录编号并记录，
但核对阶段仍须严格检查 Assertion 与 Excerpt 的关联，不能把过滤后的结果直接当作验证通过。
`aNeM` 还让模型能从编号看出 Excerpt 属于哪个 Assertion。

### 3.4 脚注如何生成与回查

脚注是**代码在已知偏移上拼接**的，不经过任何模型：

1. 当前版本正文按块保存（`block_id`、`text_hash` 和字符位置）。模型提交的 Claim
   必须通过 `validate_claim_span`：文本位置不能越界，文本和哈希必须匹配保存的正文；
   带检索标记的 Claim 还要覆盖对应标记。需要证据却没有核对结果的位置另记问题，见 §2.7。
2. `ClaimEvidence` 只记录已验证 Claim 与其 Excerpt 的绑定；
   Excerpt 的 `locator` 指向所选高亮，Document 记录来源与快照版本。保存时机见 §3.1。
3. 渲染时脚注只插在拥有 `ClaimEvidence` 的 Claim 上，位置是
   `block.source_start + 块内偏移` 的换算；每个位置至多 3 个角标，避免脚注堆积影响阅读。
4. 来源编号按 `(source_uri, document_version)` 编号：同一来源的不同版本是不同脚注源。
5. 标题块不插脚注。报告开头的核对情况摘要由保存的记录计算，不采用模型自报的计数。

可回查性：`GET /api/jobs/{job_id}/excerpts?ids=...` 可按 Excerpt id 回查原文与来源元数据；
审计 JSON 保存脚注、Claim、Excerpt 和来源版本之间的关联（§6.1）。
这些记录让核对过程可以回查，但“记录可追溯”不等于“模型的事实判断绝不出错”。
Excerpt 与快照原文的对应检查尚未实现，见 §3.1，不能把来源版本关联描述成已经完成这种检查。
网页全文快照不通过现有 API 暴露给读者。

## 4. 循环、预算与终止

本节定义循环次数、预算计算和停止条件。具体档位数值见 §1.3。

### 4.1 两个计数器

- `decision_round`：Planner 的执行轮次编号。每走完一轮，包括输出格式错误和决策被拒绝，
  都增加一次，用来定位和复用该轮记录。
- `research_decisions_used`：真实研究预算。只在**解析出合法决策**后 +1
  （被拒的合法决策也计数；`schema_error` 不计数）。

因此，所有研究阶段都应以 `decision_round_limit − research_decisions_used` 计算剩余预算。

- Planner 开始下一轮之前，若 `research_decisions_used ≥ decision_round_limit`，执行
  `_end_for_budget`——无 Excerpt 则失败 `research_budget_exhausted_without_evidence`，
  否则以 `budget_exhausted` 触发 Verifier，让已有证据接受覆盖裁决。
- 格式错误连续 3 次时，以 `planner_schema_error_limit` 结束。
  格式失败不消耗研究预算，但不能因此无限重试。
- Verifier 判断需要继续研究时，也应使用同一剩余预算；没有余额才以 `verifier_major_gap` 结束。

Verifier 的输入快照与执行判断使用同一预算口径。例如上限为 8、已使用 6 次合法研究决策、
另有 2 次格式错误时，执行轮次为 8，剩余研究机会仍为 2；补研后若合法决策达到 8 次，
即使执行轮次为 10，也只有研究预算耗尽这一种判断，不因格式错误提前停止。

拒绝原因见 `deterministic/gates.py`：`OVER_CONCURRENCY`、`EMPTY_FINISH`、
`FINISH_WITHHELD`、`SCHEMA_ERROR`。拒绝原因反馈给 Planner；除格式错误外都消耗一次研究决策。

### 4.2 Worker 级预算

每任务 `max_worker_rounds`（按 effort 注入，写进任务合同）。轮上限之外，
§2.3 的空保存、重复动作和任务完成检查可以更早停止任务。
每轮 ≤8 个并行工具调用；工具调用总数不设硬上限。

### 4.3 何时返回 Planner 补充研究

1. **Verifier 要求补研（needs_research）**：把重大缺口和不可用断言清单反馈给 Planner。
2. **跟随轮（`planner_finish` 专属，每 Job 至多一次）**：放行的核验若仍写着
   `evidence_needed`，且 `research_decisions_used × 3 ≤ decision_round_limit`
   （预算还剩三分之二以上），把这些缺口反馈给 Planner，再安排一次补研。
   这一安排称为跟随轮；期间代码拒绝 `finish`（`FINISH_WITHHELD`），只允许派发任务，
   避免 Planner 未尝试补研就再次宣布结束。任务没有找到新材料也可以正常结束，
   但之后仍要经过研究核验，不能直接视为证据充足。
3. **synthesis → verifier（synthesis_gap）→（确认后）planner**：综合认为缺证据时，
   由 Verifier 复核该请求；确认则按 needs_research 回 Planner（`gap_origin=research_synthesis`），
   否决则按已有分析成文，并保留证据限制说明。它使用同一研究预算，不增加额外轮数。

### 4.4 报告循环

- 一份报告最多修订两轮（`MAX_WRITER_REPAIRS = 2`），首稿不计入修订次数；
  `repairs_used = revision − 1`。事实问题与行文问题共享该上限，没有额外的行文修订预算。
  不另设“每个 Job 最多一次行文修订”的限制。
- Attribution 与 Review 只有发现核心问题才请求修订（§2.8）；非核心事实问题保留在审计中。
  当前版本已被要求返回 Writer 时，暂不运行 Readthrough。
- Readthrough 按 `(report, revision)` 保存结果。同一版本已有保存结果时复用；
  满足 §2.8 的调用条件且尚无结果时才检查。有明确的行文问题且总预算未耗尽时返回 Writer，
  即使此前已经因行文问题修订过一次，也可以使用剩余预算。
- 每次修订都按块替换，之后重新经过 Attribution 和 Review，包括仅为行文问题所做的修改。
  新版本不再返回事实修订时，再进行 Readthrough，不能用旧版本的行文检查结果代替。
  未改动且依据未变化的归因结果可复用，但整体审查不能随块一起跳过。
- 预算耗尽后仍检查最后版本的行文，问题保存在审计中，不再触发修订，
  也不单独改变核验结果或阻止交付。修改文字若引入新的事实问题，按最后版本的核验结果
  判定并交付（§2.8），不因此追加修订次数。

检查次数不等于修订次数。例如，首稿和第一次修订稿都发现行文问题，并分别使用一轮修订，
第二次修订稿仍会接受行文检查；这是三个版本的检查、两轮修订，没有增加总预算。

### 4.5 终止条件汇总

| 结局 | 条件 | 错误码 / 说明 |
|---|---|---|
| 报告就绪 | 文件引用与渲染结果已提交 | `report_rendered`；Job 的 `completed` 由入口另行提交 |
| 研究失败 | 预算耗尽且无证据 | `research_budget_exhausted_without_evidence` |
| 研究失败 | 重大缺口且无剩余决策轮 | `verifier_major_gap` |
| 研究失败 | Planner 连续 3 次格式错 | `planner_schema_error_limit` |
| 模型输出错误 | 对应环节按自身重试规则仍未得到合法结果 | 记录对应的 `*_contract_error` 或 `verifier_output_invalid` 并抛出异常；不能概括为统一“两次修复” |
| 取消 | 人工请求，执行到取消检查点时停止 | `cancelled`；尚未开始的排队任务直接终结 |

“记录失败原因”和“终结 Job”要分开理解。当前入口会为 `VerifierMajorGapError` 写入
终止事件；其他未处理异常通常直接抛出或记录日志，不补写 `job.stopped`。
图内可能已经写入失败状态，所以没有终止事件也不代表 Job 一定仍是 `running`。
能否恢复要检查状态、终止事件和 checkpoint，见 §5.5。

## 5. 状态、持久化与恢复

### 5.1 运行状态、执行阶段与核验结果

| 信息 | 回答的问题 | 保存位置 |
|---|---|---|
| Job 生命周期 | 正在排队、执行、取消，还是已经结束？ | `app.jobs.status` |
| 执行阶段与研究结果 | 当前做到哪一步，研究产生了什么结果？ | `job.phase_changed` 事件、`app.jobs.outcome` |
| 报告处理状态 | 正在写作、归因、审查、修订，还是已经渲染？ | `report_runs_v2.status` |
| 报告核验结果 | 正文是否通过核验？ | `report_runs_v2.verification_status` |

Job 可以直接以 `running` 创建，也可以先进入 `queued`。主要转换如下：

| 起点 | 条件 | 结果 |
|---|---|---|
| queued | 调度器开始执行 | running |
| running | 报告已交付且入口完成收尾 | completed |
| running | 执行被判定为终止失败 | failed |
| running | 收到取消请求 | cancelling，到可停止的位置后进入 cancelled |
| queued | 尚未执行即被取消 | cancelled |

正常完成不需要经过 `cancelling`。中断恢复与遗留取消请求的处理见 §5.5。
`report_failed` 是时间线中的报告核验结果，不是 Job 终止事件；客户端不能看到它就停止
等待报告。报告渲染后，处理状态变为 `report_rendered`，原核验结果仍单独保留。

### 5.2 存储分工

- **PostgreSQL `app` schema**：保存 Job、Brief、计划、任务、证据关系、核验结果、
  报告版本、事件和用量。Document 在工作区内共享；DocumentView 属于某个 Job 和 Task；
  Excerpt 属于某个 Job。当前报告处理使用 `report_*_v2` 等表，精确结构见迁移，
  读写实现见 `src/prospector/store/repositories/`。
- **对象存储**：保存 Document 全文快照和最终报告文件。当前研究流程不读取归档全文，
  报告下载接口会读取报告文件。数据库保存对象引用和哈希，而不是在 checkpoint 中携带文件。
- **LangGraph checkpoint（`langgraph` schema）**：保存图的执行位置与状态，
  `thread_id = str(job_id)`。显式恢复时用 `graph.invoke(None, config)` 继续原执行。
  Verifier 和 Writer 所需的业务材料通过 `build_verifier_snapshot`、`build_writer_snapshot`
  从业务表重新整理，不能只凭 checkpoint 判断某份证据是否仍可用。

### 5.3 事务边界

下列数据库写入各自在同一事务中提交；失败时该组写入一起回滚：

- `save_findings`：Excerpt、Assertion 及相关事件；
- `complete_verifier_run`：核验记录 + 冲突裁决 + 断言处置 + `verifier.completed`；
- `complete_markdown_revision`：revision → `generated` + report → `attributing`；
- `complete_attribution_run`：run `completed` + revision `attributed`；
- `save_report_review_run`：review run `completed` + revision `reviewed`；
- `complete_v2_report_render`：对象引用 + 哈希 + 交付事件 + `jobs.outcome`；
- Job 终结（`_finalize`）：锁定 Job 行，检查是否已有 `job.stopped`，再更新最终状态、
  处理未完成任务并记录终止事件，避免重复收尾。

这些保证只适用于列出的事务，不是整个研究流程的一个大事务。对象存储、模型调用、
单独写入的阶段事件和 checkpoint 都不与上述业务写入共享事务。
例如报告文件写入后、数据库引用提交前中断，可能留下尚未被引用的文件；引用提交后、
Job 收尾前中断，则可能出现“报告已就绪、Job 尚未完成”。测试应分别验证这两个位置，
不能只验证所有调用都成功的情况。

### 5.4 重复执行时复用什么

重复执行时，应根据所属 Job、版本和已保存状态识别既有结果，避免重复保存或误用旧结果。
数据库唯一约束只是其中一部分，各阶段还需检查结果是否完成、输入是否一致。

| 阶段 | 识别同一次工作的依据 | 复用行为 |
|---|---|---|
| Document | `(workspace, uri, content_hash)` | 相同来源和内容复用已有快照 |
| Excerpt | `(job, doc, excerpt_hash)` | 复用已保存的原文记录 |
| Assertion | `(job, task, statement_hash)` | 复用相同陈述，追加 Excerpt 绑定 |
| Planner 决策 | `(job, decision_round)` | 重放校验提示词一致，不一致报错 |
| Plan | `(job, decision_round)` | 已存在则返回既有 |
| Verifier | `(job, plan_version, trigger)` | 已完成则复用；未完成且提示词变化时重新保存输入 |
| Synthesis | `(job, verifier_run_id)` | 只复用对应核验轮的综合结果 |
| Writer revision | `(report, revision)` | `generated` 则跳过直达 attribution |
| Attribution | `(report, revision)` + 批次 `(run, batch_index)` | 批次的选材/核对结果逐级复用 |
| Review / Readthrough | `(report, revision)` | 完成则复用 |
| Render | `report_rendered` 状态 | 重放不再交付第二次 |

这里的“完成”指结果已经按该阶段要求保存，不是远端模型已经返回。
模型返回后、结果入库前中断，恢复时仍可能再次调用模型。不能承诺所有外部调用只发生一次。

复用还必须考虑依赖关系。例如某段正文虽未改动，但它依赖的前提已经变化，
就不能直接复用旧归因结果；相关规则见 §2.7。

### 5.5 恢复与取消

- **服务启动不自动恢复研究。** `JobScheduler` 的 `recover_on_start` 默认为 `False`，
  当前服务入口使用这个默认值。启动会处理此前遗留的 `cancelling` 请求，
  但这不等于把所有中断任务重新排队执行。
- **显式恢复。** `prospector-local job resume <id>` 使用同一 Job 的 checkpoint 继续执行。
  调用前必须确认原执行进程已经停止，不能同时运行原任务和恢复任务。
  已有 `job.stopped` 的 Job 不应再次执行；具体恢复条件还需检查保存的状态。
- **取消。** 正在执行的 Job 先进入 `cancelling`，执行到取消检查点后停止。
  取消检查在节点和模型、工具调用之间进行，不保证立刻终止已经发出的外部请求。
  已保存证据不会回滚，仍保留核验和回查记录；排队中尚未开始的 Job 可直接取消。
- **删除。** 仅已停止的 Job 可以软删除（设置 `deleted_at`）。不连带删除共享的 Document
  或报告对象，避免损坏已有引用；这不是清空全部底层数据的操作。

**入口差异：** 调度器的候选查询只选 `running / queued` 且没有 `job.stopped` 的任务；
本地 `job resume` 则在检查没有终止事件后尝试继续 checkpoint，不额外排除 `failed` 状态。
图内报错可能留下 `failed` 且没有终止事件的记录，因此两个入口的候选范围并不相同。
允许尝试恢复也不保证能成功，仍需处理原错误并检查 checkpoint；不能宣称所有异常都能恢复。

### 5.6 计量

`app.usage` append-only：`component`（planner / research_worker / research_verifier /
report_writer / …）× `model` × token 数；工具调用按 `DISTINCT tool_call_id` 计。
汇总后通过 Job 详情与 `job status` 提供，不由模型估算。

## 6. 对外交付与展示边界

### 6.1 两类报告文件

通过 `GET /api/jobs/{id}/report?format=md|json` 获取：

- **`report.md`（给读者）**：由代码生成。开头一段核对情况摘要（核验结果及材料收集、使用计数，
  全部从记录计算）；正文只对已验证句插脚注角标；结尾「## 来源」清单。
  `Content-Disposition: attachment`。CLI `report show` 终端渲染；
  `job attach` 完成后自动落盘。
- **`report.json`（给审计）**：`verification_status`、核对数量统计、逐句
  `claims`、`claim_evidence`（Excerpt 全文 + 断言 + 来源可信度提示）、
  `claim_premises`、核验问题及其处理结果、整体审查、冲突、次要缺口、
  readthrough、各类计数。`Content-Disposition: inline`。
  报告未就绪时端点返回 409 `report_not_ready`。

Job 列表和详情 API 也返回 `verification_status`，但返回字段不等于 Web 页面必须展示它。
报告是否就绪取决于是否已保存文件引用，不能只看 Job 是否 `completed`：
文件引用可能先于 Job 收尾提交；若 API 能确定报告引用尚不存在，返回 409 `report_not_ready`。
引用已存在但文件读取失败属于读取错误，不能当作报告仍在生成。

### 6.2 证据回查（给核对引用的读者）

`GET /api/jobs/{id}/excerpts?ids=...`：按 Excerpt id 批量回查原文与来源元数据。
读者可以用它核对脚注引用的原文，相关关联规则见 §3.4。

### 6.3 业务事件与 SSE（给监控与诊断）

- 执行事件保存于只追加的 `app.events`，按递增 id 读取。
  SSE（`GET /api/jobs/{id}/events`）从数据库读取新增事件，不依赖消息中间件。
  CLI 和 Web 时间线据此显示进度；Job 详情还使用业务表快照，不是只靠事件猜测状态。
- 主要事件：`job.phase_changed` / `job.stopped`、`brief.confirmed`、
  `planner.started|decided|rejected`、`task.started|round_advanced|tool_used|
  evidence_saved|finished`、`verifier.completed`、`synthesis.completed`、
  `replan.triggered`、`report.draft_rendered`。
- **断线续传**：`Last-Event-ID` 表示最后已收到的事件，只返回 id 更大的事件；省略则从头读取。
  已越过 `job.stopped` 的重连得到空流即关闭。客户端断线按指数退避从最后已收 id 续传。
- 事件不能携带密钥、连接串、Document 全文或完整模型提示词。
  当前 `synthesis.completed` 会携带最终分析文本，用于展示综合结果；
  因此不能笼统地说“事件中没有研究内容”。事件、普通日志和审计文件是不同的输出，
  允许展示分析文本不代表允许把原始资料或敏感配置写入日志。

### 6.4 Web 页面、下载文件与审计内容

| 入口 | 展示或提供的内容 | 不应混淆的内容 |
|---|---|---|
| Web 报告页 | 标题、正文、脚注、来源清单、引用原文抽屉 | 当前不显示下载文件开头的核对情况摘要，也不展示内部审查详情 |
| Markdown 下载或 CLI 报告显示 | 核对情况摘要、正文和来源 | 不是 Web 页面的逐字复制 |
| 引用回查接口 | Excerpt 原文及来源元数据 | 不提供归档的 Document 全文 |
| 审计 JSON | Claim、证据、依赖关系、问题记录与核验结果 | 前端可为引用抽屉读取它，不代表需要展示所有审计字段 |
| 任务列表、详情与时间线 | 生命周期、进度、用量和执行事件 | Job 已完成不等于报告全部核验通过 |

Web 报告页按来源网址和快照版本，把脚注与审计 JSON 中的 Excerpt 对应起来，
不重新计算脚注编号（`web/src/pages/ReportPage.tsx`）。
当前状态标签只表达 Job 生命周期，报告核验结果不能替代运行状态。

### 6.5 运行入口

- **Web UI**：通过服务 API 完成提问、Brief 编辑与确认、查看进度、取消任务和阅读报告。
- **`prospector`**：`serve` 启动服务，默认端口 7620；其他用户命令通过 API 操作任务和报告。
- **`prospector-local`**：直接连接存储，提供本地运行、初始化、事件查看和显式恢复入口。
  本地运行也使用同一研究图，不另设跳过核验的执行路径。

## 7. 关键设计理由

本节解释规则的目的，不重复定义次数和状态。

1. **Brief 展开问题，Plan 安排研究。** 候选研究方向不等于必须逐项完成的清单。
   Verifier 既要检查计划完成情况，也要判断证据是否真正回答了用户的问题，不能只数任务数量。

2. **保存原文，不用模型摘要代替证据。** Document 留下抓取时的全文快照，
   Excerpt 保留后续核对所依据的文字。模型只读取任务相关片段，既限制输入规模，
   也使断言能够回查到具体材料。保存时机和目前尚未完成的核对见 §3。

3. **事实核对与引用排版分开。** Attribution 判断正文得到哪些材料支持；
   代码负责位置、编号和来源版本。这样可以分别验证事实判断、证据关联与脚注渲染，
   不把这几类问题混在一个模型输出里。

4. **修订只修改指定部分。** 块替换避免无关内容被重新生成，并允许复用仍然有效的归因结果。
   但局部修改可能影响前提和行文，所以仍需检查依赖关系、整体结论与上下文衔接。
   两轮预算是成本限制，不是保证两轮一定修好的证明。

5. **交付完成与核验通过分开。** 是否有报告可读、报告是否通过核验、Job 是否结束，
   是三个不同问题。文件引用、核验结果和运行状态分别保存，让客户端能够正确说明当前情况。

6. **限制由代码执行。** 模型需要知道剩余预算与可用动作，但提示词不是执行保证。
   超过上限的派发、不允许的结束决策，应由代码拒绝；所有研究环节使用同一预算计算。

7. **模型使用短编号。** 短编号便于选择材料，也能表达 Assertion 与 Excerpt 的所属关系。
   数据库标识由代码映射，模型不能通过猜测或拼写一个编号创建新的证据关联。

实现上述规则后，需要用具体场景验证，而不是只检查函数能否返回预期结构。
例如：格式错误后预算仍正确；恢复已落库报告时不重复交付；改动前提后重新核验依赖它的陈述；
失败陈述不生成已验证脚注；Web 页面对“报告已就绪但 Job 未收尾”显示正确。
文档说明了预期，不代表这些场景已经有测试或已经验证通过。
