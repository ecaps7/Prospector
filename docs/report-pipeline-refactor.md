# 报告全链路重构方案：从约束写作转向后置归因

## 0. 文档状态

- **性质**：新一代报告主链的完整重构方案。
- **目标**：让模型有足够空间形成观点并写出自然文章，同时保留 Prospector 的事实准确性、证据血缘和可审计性。
- **替换范围**：整体替换当前 `Report Writer → Report Verifier → statement patch → render` 成文合同，不在旧合同上增加兼容分支。
- **不变边界**：Brief、Plan、Document、DocumentView、Excerpt、Assertion、Research Worker 动作合同、D12 联网路径和 Research Verifier 的证据资格判断继续成立。
- **实现状态**：已实现，本文已按实现同步（落地时超出原方案的调整见 §0.2）。`docs/design.md` 与 `docs/implementations/m1.md` 尚未改写，与本文冲突时以本文和代码为准。

本文解决四个相互关联的问题：

1. 说过头会失败，说不足却没有代价，模型会自然选择最保守的表达；
2. 当前链路没有一个阶段负责通读材料并形成整体认识；
3. Writer 一边写作一边给句子编号、分类和绑定依据，注意力被记账占用；
4. 判断无法像事实一样被客观批准，逐句审批只会把报告不断改弱。

新方案把职责重新分开：

- 研究阶段取得可信、精确、可定位的材料；
- Research Synthesis 把材料之间的关系整理成连续的分析底稿；
- Writer 只负责写完整报告；
- Claim Attribution 在成文后提取 Claim、查找依据并记录关系；
- Whole-report Review 检查整篇报告是否回应 Brief、处理重大冲突并保持一致；
- 确定性代码生成引用、审计视图和最终状态。

Research Synthesis 不生成候选答案，不选择“获胜结论”，也不输出结论清单。它只回答一个问题：**这些材料合起来说明什么。**

### 0.1 阅读约定

本文保留少量实现字段名，但业务含义统一如下：

- **major gap**：缺少它就无法实质回应 Brief 的重大研究缺口；
- **minor gap**：需要说明，但不妨碍报告在现有材料范围内完成；
- **阻断项**：当前正文必须修改，不能直接定稿的问题；
- **audit note**：提供给读者或开发者查看的审计说明，不触发正文修订；
- **revision**：一份已经冻结、等待完整核验的 Markdown 版本；
- **检索标记 / 候选标记 / 提示标记**：检索标记必须形成事实锚点，候选标记只说明附近可能有具体事实，提示标记只表达程度或意义（§3.7.2）；
- **事实锚点 / 分析片段**：前者表达可以从材料中取回的具体内容，后者表达材料合起来意味着什么；
- **核心片段**：被推理依赖、或位于主要认识块内的检索片段（§5.2）。

其余实现字段使用代码格式标出，正文说明尽量使用中文。

### 0.2 落地时的调整

实现过程中做了四项超出原方案的调整，均已写进对应章节，这里只作索引：

- **修订决策按核心性分流**（§4、§5.2）：只有核心问题触发 Writer 修订；非核心事实失败直接以 `partial` 收口，不让一个边角细节重新暴露整篇正文；
- **不合格的报告照样交付**（§3.8、§3.9、§5.3）：整体审阅不再有任何一条路径能终止 Job，核验结论改由数据库字段承载，正文不加整篇级别的标注；
- **一次模型格式错误不再杀掉 Job**（§4）：综合、成文、归因、审阅各自允许一次重试，归因还能逐条丢弃畸形 Claim，只有丢弃比例过高才算合同失败；
- **整体审阅收到的是归因摘要**（§3.8）：原方案写的是完整 AttributionRun，实际会把同一句话送进第二轮出处核对，还挤占输出预算。

另有四处实现缺陷在同一轮里修掉，对应约束写在 §3.7.2（专名白名单怎么建）、§3.7.4（依赖关系用什么编号表达）、§3.9（角标怎么定位）和 §8.2（检查规则不得从 JSON 字段漏给 Writer）。

---

## 1. 设计原则

### 1.1 Writer 只写文章

Writer 的唯一正文输出是普通 GitHub Flavored Markdown。它不再输出：

- `statement_id`；
- `kind`；
- `candidate_excerpt_ids`；
- `premise_statement_ids`；
- 一行一条的 JSONL 记录；
- 句级或段落级修订补丁。

Writer 自行决定标题、章节、段落、叙述顺序、详略、列表和表格。系统只规定 Markdown 必须能够被统一解析和安全渲染，不规定文章应当怎样组织。

表格在本方案中只是一种正文排版。单元格中的事实与普通正文一样进入后置归因。本次重构不引入 Data Worker、Computation、FigureSpec、计算型表格或图表渲染，这些仍不属于 M1。

### 1.2 Research Synthesis 是分析底稿，不是文章提纲

Research Synthesis 通读全部 usable Assertion、对应 Excerpt、来源信息、冲突和缺口，形成一段连续分析。

它不输出：

- 固定数量的结论；
- 编号论点；
- 章节建议；
- 材料使用顺序；
- 唯一获批答案；
- Writer 必须逐项展开的清单。

它可以自然包含一项或多项认识，也可以明确说明现有证据无法判断什么。结论的数量和形态由 Brief 与材料决定，不由输出格式预设。

Writer 同时接收 Research Synthesis 和全部 usable 材料。Synthesis 帮助 Writer 理解材料之间的关系，但不替 Writer 决定文章结构和表达。

### 1.3 表面标记只框候选，归因只核对最小事实锚点

具体内容必须对得上材料，连接和判断只记录来源。但触发检索的依据不是“这句话是事实还是判断”。

这条界线没有可执行的定义规则，而跨材料综合出来的句子恰好落在界线上：它讲的是数字和时间，看起来像事实，却没有任何单条材料这么说，于是被判成事实并阻断。结果是琐碎事实全过、有价值的综合全挂。

因此确定性代码只负责框出候选位置，不用表面标记替代最终的语义边界。标记分成三族：

- **检索标记**（数字与金额、日期、引语与明确来源归属、明确范围量词）：这些标记必须由 `verified` 或 `failed` 的事实锚点覆盖；
- **候选标记**（专名）：它只说明附近可能出现了关于具体主体的外部事实。归因模型必须检查该位置，但专名本身不能把相邻解释自动变成事实核验对象；
- **提示标记**（显著、大幅、全面、标志着一类的程度与意义词）：这类词不指向任何可以取回的东西，没有任何材料会写“这标志着一个转折”，对它们做出处检索必然返回 `failed`。它们只生成审计说明，不进入检索，不阻断；
- **分析片段**：不接受 `verified / failed` 审批，只记录它实际依赖的事实 Claim、Assertion、材料冲突或证据缺口；
- **报告结论**：Whole-report Review 检查报告是否回应 Brief、是否隐藏重大冲突、正文与结论是否一致，但不批准某一种观点，也不要求 Writer 复述 Research Synthesis。

代码验证每个候选都得到处理、每个强制检索标记都被事实锚点覆盖。归因模型只在候选内部划出最小事实锚点；它不能漏报候选，也不能把带数字、日期、引语或明确范围的内容标成普通分析。这样既不让模型任意扩大检查范围，也不让专名把整项研究判断拖进出处检索。具体规则见 §3.7.2。

Writer 可以基于同一批材料重新组织、合并或进一步限定 Synthesis 中的认识。只要检索片段的内容准确、推理可追溯、重大冲突得到处理，并且报告真正回应 Brief，就不应因为它与 Synthesis 的措辞或结论形态不同而失败。

### 1.4 事实忠实度不能因后置归因而放松

归因结果只有两种：

| 结果 | 含义 | 当前 revision 的处理 |
|---|---|---|
| `verified` | 找到材料，正文与材料一致 | 通过，生成角标 |
| `failed` | 找不到材料，或正文与材料不一致 | 不生成角标；核心时触发修订，非核心时标记后以 `partial` 交付 |

每条 `failed` 记录必须带一个 `reason` 自由文本字段，说清三件事：材料原文是什么、正文写的是什么、差在哪。

三种失败——找不到出处、口径走样、数字相反——修起来的成本差着数量级，前两种常常改几个字就行。只给“失败”两个字，Writer 只能整篇重写，第二稿会比第一稿更平。

`failed` 不能伪装成已验证引用。将厂商说法写成行业事实、将局部样本写成普遍结果、改变数字口径，都会留下可定位的失败标记；只有它影响报告核心回答时，才值得让 Writer 重新生成整篇正文。

归因结果的 `verified` / `failed` 与 §5 的 Job 终态同名但不同层：前者针对单个片段，后者针对整份报告。

### 1.5 被核对的正文就是最终正文

每一版 Markdown 都先冻结，再做归因和整体审阅。引用只在最后由代码插入。最后一次完成归因和审阅的正文不得再经过任何 LLM 改写。

任一修订产生新 Markdown revision 后，必须完整重跑：

```text
Markdown 解析 → 确定性标记扫描 → Claim 提取 → 出处检索 → 不检索片段依赖记录 → 失败片段去向核对（修订版本）→ 整体审阅
```

不按文本相似度复用旧 revision 的归因结果，不保留局部免检路径。

### 1.6 研究材料的证据资格保持不变

- `web_search` 仍只提供元数据；
- `web_fetch` 仍先冻结 Document，再生成任务级 DocumentView；
- Worker 仍只选择真实存在的 `source_ref`；
- `save_findings` 仍原子写入精确 Excerpt 与 Assertion；
- Document 全文仍不进入任何 Prospector LLM 上下文；
- 只有 Research Verifier 判定 usable 的 Assertion 和对应 Excerpt 才能进入综合、成文和归因。

---

## 2. 全链路：九个阶段

```text
用户问题
   │
① Brief ────────── 确认研究问题和用户限制
   │
   ├──────────────────────────────────┐
   ▼                                  │
② Planner ── 决定下一步需要什么证据    │ 研究循环
   ▼                                  │
③ Worker ─── 冻结材料并保存精确事实     │
   ▼                                  │
④ 研究审核 ─ 材料是否可信、是否够用 ────┘
   │
   ▼
⑤ 研究综合 ─ 材料合起来说明什么
   │                         │
   │                         └── 重大缺口 → ④确认 → ②补研究
   ▼
⑥ Markdown 成文 ─ Writer 只写文章 ──────┐
   ▼                                    │ 成文循环
⑦ 后置归因 ─ 提取 Claim 并匹配 Excerpt   │
   ▼                                    │
⑧ 整体审阅 ─ 是否回应 Brief、处理冲突 ───┘
   ▼
⑨ 确定性定稿 ─ 角标、审计视图、最终状态
```

研究循环和成文循环都有硬上限。预算触顶不能绕过 Research Verifier、Claim Attribution、Whole-report Review 或确定性定稿。

---

## 3. 阶段合同

### 3.1 阶段①：Brief

#### 输入

用户原始问题，以及必要时的一次澄清回答。

#### 职责

形成双方确认的研究输入快照：

- 核心问题；
- 用户明确限制；
- 可探索方向；
- 输出语言、格式和 effort。

只有在原始问题存在会导向实质不同研究对象、范围或用户意图的关键歧义，并且 Scope 无法在不偏题的前提下自行形成 Brief 时，才向用户澄清一次。能够作为候选方向展开的未指定内容，或能够由 Planner 自主决定的研究侧重点、研究方法和组织方式，不触发澄清。

effort 只影响候选方向展开的程度和细致度，不改变核心问题和用户明确要求。

#### 输出

现有 `ResearchBrief`。Brief 准确保留用户的核心问题和明确要求，并可补充供 Planner 自由取舍的候选方向；它不是覆盖清单，也不预设答案。

#### 不负责

- 不规定最终章节；
- 不生成 `must_cover`；
- 不把可探索方向变成强制任务；
- 不预设报告必须得到单一结论。

### 3.2 阶段②：Planner

#### 输入

- 冻结 Brief；
- 持久 Planner 消息线程；
- 已落库 Assertion 投影和 Worker 收工声明；
- Research Verifier 确认的 gap；
- 当前 Planner 决策轮、批次并发和 Worker 轮次余额。

#### 职责

判断当前证据相对于 Brief 是否还需要继续研究，然后输出 `dispatch` 或 `finish`。

判断以 Brief 的核心问题和用户明确要求为准。Brief 中的可探索方向供 Planner 取舍，不是必须逐项完成的清单。研究任务的内容、拆分方式、先后顺序和研究方法由 Planner 决定，不预设研究类型或阶段顺序。

没有已落库研究结果时，Planner 可以根据 Brief 自由展开。已有结果后，继续派发应解决现有材料尚未解决的问题或核查新出现的线索；仅仅还能找到更多资料、某个候选方向尚未研究，或重复已有任务与同类证据，不足以继续派发。

`dispatch` 时，Planner 说明当前继续研究和派发本批任务的理由。每个任务包含一个自包含研究问题，以及可以根据落库事实判断的完成状态。任务完成状态不混入研究步骤或决策理由。

`finish` 表示现有材料已经足以让 Research Verifier 和 Research Synthesis 在明确现有局限的前提下实质回答 Brief，不表示已经穷尽所有可能找到的资料。

Research Verifier 确认了 `major_gaps` 时，Planner 必须继续派发研究任务；在当前批次能力内如何组织任务、以什么顺序解决缺口，仍由 Planner 决定。来自 Research Synthesis 的 gap 也必须先经 Research Verifier 确认，才能取得这一权威性。

#### 输出

沿用现有 Plan 和 ResearchTask 合同：

```json
{
  "decision": "dispatch",
  "tasks": [
    {
      "question": "自包含研究问题",
      "expected_evidence": "什么可核对的落库事实表示任务已经完成"
    }
  ],
  "reason": "为什么当前应当继续研究并派发本批任务"
}
```

不增加固定研究类型枚举。任务价值写在决策级 `reason` 中；`question` 只定义研究问题，`expected_evidence` 只定义任务完成状态。

#### Planner 线程准入

Planner 线程继续保持封闭，只追加：

- Planner 决策对象；
- Assertion 投影摘要和 Worker 收工声明；
- 拒绝或格式错误反馈；
- Research Verifier gap；
- 预算和运行能力余额。

Research Synthesis 发现证据不足时，不把综合原始输出塞入 Planner。它输出的 `evidence_needed` 必须先交给 Research Verifier；只有被确认会影响 Brief 回应的 major gap 才能进入 Planner 线程。

### 3.3 阶段③：Worker

#### 输入

自包含 ResearchTask、用户限制和具体运行能力。

#### 职责

保持当前唯一动作合同：每轮只能选择 `search`、`save` 或 `finish`。

Worker 保存的是可以回到原文核对的 Assertion：

- 可以是外部世界的事实；
- 可以是“某机构或某人作出某项判断”的来源归属事实；
- 不得把 Worker 自己综合出的判断冒充原文事实。

例如，原文写“某公司认为 MCP 推动了生态成熟”，Worker 可以保存“该公司认为 MCP 推动了生态成熟”；不能去掉来源主体后保存为“MCP 推动了生态成熟”。

#### 输出

Document 快照、DocumentView、精确 Excerpt、Assertion 及其完整血缘。

### 3.4 阶段④：Research Verifier

#### 输入

Brief、Plan 历史、Task、Assertion、Excerpt、来源元数据、既有冲突、Planner finish reason，以及可选的 Research Synthesis 补研究请求。

#### 职责

1. 判断 Assertion 是否忠实于 Excerpt；
   一个 Assertion 必须是单一、可独立核对的陈述，合并多个可分别成立的事实属于不合格 Assertion；
2. 判断来源是否足以承担该 Assertion；
3. 识别需要并陈或裁决的来源冲突；
4. 判断 Plan 是否履约、材料是否足以进入研究综合；
5. 判断 Research Synthesis 提出的补研究请求是否确实构成 major gap。

某个 task 未完全达到 `expected_evidence`，或某个候选方向尚未研究，不会自动成为 major gap；只有由此缺失使现有材料无法实质回应 Brief 时，才返回 Planner。

Research Verifier 不提出报告结论，也不评价文章写法。

两段核验共享由冻结快照一次生成的局部引用命名空间：Task 使用 `tN`、Assertion 使用
`aN`、Excerpt 使用 `eN`。模型输入和输出都不暴露存储 UUID；代码先严格还原短引用，再执行
领域引用校验、冲突物化和持久化。未知短引用属于输出合同错误，不能静默忽略。

#### 输出

- usable 或 unusable Assertion；
- ConflictResolution；
- minor gap；
- major verifier gap；
- `pass` 或 `needs_research`。

#### 路由

- `needs_research` 且 Planner 决策预算仍有余额：回阶段②；
- `needs_research` 且预算耗尽：Job 失败；
- `pass`：进入阶段⑤。

如果本轮只是在核查 Research Synthesis 的补研究请求：

- 确认为 major gap：回阶段②；
- 不构成 major gap：不得进入 Planner，Research Verifier 的理由作为 minor gap，与现有 Synthesis 一起进入 Writer。

### 3.5 阶段⑤：Research Synthesis

Research Synthesis 的唯一职责是把全部可用材料转化为一份连续的分析底稿。它不写文章，也不审批答案。

每次运行固定包含“初稿—独立检查”两步。检查分别判断核心问题、关系机制、材料取舍和时间边界，并返回分析缺陷列表；代码据此决定是否采用初稿。有实质缺陷时必须给出完整修订结果。初稿、检查输入、检查输出和最终采用的分析一起落库为一个版本化的 `ResearchSynthesisRun`。

#### 输入

- 完整 Brief；
- 按 ResearchTask 分组的全部 usable Assertion；
- Assertion 对应的精确 Excerpt 和来源元数据；
- ConflictResolution；
- minor gap。

Document 全文仍不进入输入。Excerpt 可以使用现有确定性去重和裁剪机制，但不能只给 Assertion 摘要而不给原文。

#### 输出合同

第一步自由形成分析初稿。材料足以进入成文时输出：

```json
{
  "decision": "ready",
  "synthesis": "对全部材料的连续分析，说明整体上能够形成什么认识、不同事实如何关联、哪些解释相互支持或限制，以及现有证据最多能够说到什么程度。",
  "assertion_refs": ["a1", "a2", "a5"],
  "material_conflict_refs": ["x1"]
}
```

其中：

- `synthesis` 是连贯的分析文本，不是标题、提纲、编号论点或结论列表；
- `assertion_refs` 表示这份分析实际使用了哪些 usable Assertion；
- `material_conflict_refs` 表示分析已经考虑的重大冲突；
- 两者都只能引用本次输入中的 `aN / xN`。运行时代码严格还原并校验，落库后仍保存
  `assertion_ids / material_conflict_keys`；
- 证据边界、无法判断的部分和冲突影响直接写入 `synthesis`，不另列成 Writer 容易逐项照抄的清单。

`synthesis` 必须做到：

1. 回应 Brief，而不是复述材料；
2. 说明材料之间的关系以及这些关系为什么重要；
3. 在证据允许时给出具体认识，不能只写无法证伪的空泛结论；
4. 在证据不允许时明确说明不能判断什么以及原因，不能制造确定性；
5. 如实吸收会改变整体认识的冲突和限制。

它不要求单一 `direct_answer`，也不要求固定数量的结论。问题能够明确回答时，直接答案自然写入连续分析；问题涉及多个方面时，多项认识在分析中自然连接；证据无法定论时，无法定论本身必须有明确的证据原因。

如果缺失证据会阻断对 Brief 的实质回应，则输出：

```json
{
  "decision": "needs_research",
  "synthesis": "基于当前材料已经能够形成的有限分析，以及为什么关键问题仍无法确定。",
  "assertion_refs": ["a1", "a2"],
  "material_conflict_refs": ["x1"],
  "reason": "缺失证据为什么会阻断对 Brief 的实质回应",
  "evidence_needed": "需要补充什么具体证据"
}
```

`needs_research` 仍必须产出当前材料已经支持的有限分析，不能只返回缺口。这样 Research Verifier 如果判断该缺口不构成 major gap，Writer 仍能在明确边界内继续成文。

第二步使用新的消息上下文，只读取 Brief、初稿、已确认冲突和 minor gap，分别判断：

- 是否实质回应 Brief 的核心问题；
- 是否解释了材料之间的关系、变化机制或转折；
- 是否完成了材料取舍，而不是压成时间线、产品/厂商/案例目录或沿 ResearchTask 复述；
- 是否把有时间或范围边界的发现写成长期结局，或把有限认识写成无条件事实。

模型不输出 `accept` / `revise`。它列出实际成立的分析缺陷：`brief_not_answered`、`missing_relationships`、`evidence_catalog`、`missing_selection`、`unsupported_overreach`。代码根据缺陷列表决定是否采用初稿；有任一实质缺陷时必须返回完整 `revised_result`。检查不是风格审批，也不设置固定字数、Assertion 数量、结论数量或文章结构。

检查不重复读取全部 Assertion 和 Excerpt。它负责判断分析是否成立，不重新做覆盖和逐项事实核对；最终正文的事实忠实性仍由 Writer 原始材料、Claim Attribution 和 Whole-report Review 共同保证。

检查结果由代码计算：

- 缺陷列表为空：初稿可以直接交给 Writer；
- 有缺陷：同时返回一份完整的修订后 `ResearchSynthesisResult`，作为该次运行的最终分析。

检查不是风格审批。段落安排、结论数量、措辞偏好或存在另一种同样合理的写法，都不能写入缺陷。它也不要求固定的反证字段；反例、限制和冲突只有在确实影响回答时才进入分析。第二步不增加业务重试轮次，不回 Planner；如果修订结果为 `needs_research`，仍按既有路线交 Research Verifier 判断。

#### 对 Writer 的关系

Research Synthesis 是输入材料之一，不是必须复述的答案：

- Writer 同时接收最终采用的 `decision / synthesis / reason / evidence_needed` 和全部 usable Assertion、Excerpt；
- 还原后的 `assertion_ids`、`material_conflict_keys`、运行 id、状态、错误以及 `raw_output` 只用于持久化和审计，不进入 Writer、修订 Writer 或 Whole-report Review；
- Writer 可以重新组织、合并、限定或用不同语言表达其中的认识；
- Writer 可以基于同一批材料形成更准确的综合；
- Writer 不需要逐条使用 `assertion_ids` 中的材料；
- Whole-report Review 不得仅因正文与 Synthesis 的措辞、顺序或结论数量不同而阻断。

Research Synthesis 不生成标题、章节、段落安排、篇幅要求、材料顺序或正文草稿。

#### 路由

- `ready`：进入阶段⑥；
- `needs_research`：交阶段④确认；
- Research Verifier 确认为 major gap：回阶段②；
- Research Verifier 不确认该 gap：将 verifier 理由作为 minor gap，进入阶段⑥；
- Planner 决策预算耗尽后 major gap 仍存在：Job 失败。

### 3.6 阶段⑥：Markdown Writer

#### 输入

- 完整 Brief；
- 当前 ResearchSynthesisRun 的最终分析投影；
- 全部 usable Assertion；
- 对应 Excerpt 原文和来源元数据；
- ConflictResolution；
- minor gap。

Writer 接收全部 usable 材料，不设置“选中材料”和“未选材料摘要”。上游筛选正文材料会形成新的隐形限制，也会让遗漏检查依赖有损摘要。

#### 职责

写一篇完整报告。Writer 自行决定：

- 从研究结论、背景、案例还是冲突切入；
- 分多少章节和段落；
- 是否使用表格、列表、引用块或代码块；
- 哪些材料值得写入；
- 判断和叙事如何组合；
- 边界与局限放在什么位置最清楚。

#### 真实性边界

Writer 必须：

- 不编造材料中没有的具体事实；
- 不改变数字、时间、主体、范围、口径或来源归属；
- 不把来源观点升级成无归属事实；
- 不隐藏会让研究结论产生实质误导的冲突；
- 回应 Brief；
- 遵守用户明确限制。

归属、可信度提醒和整体研究边界分三件事处理：

- **归属留在句子里**：某项事实来自厂商声明、聚合站或二手转述时，说话的主体必须写在使用该事实的位置，例如“OpenAI 称其企业客户增长三倍”。去掉主体就变成另一个命题，因此这一项不能挪走；
- **可信度提醒附着到 finding**：`source_credibility` minor gap 不再作为全局 gap 输入，而是以 `source_caveat` 附着到相关 Assertion。Writer 使用该 finding 时在同一处写准来源性质和适用范围，不另写一份全局免责声明；
- **争议和缺口按需选取，但归属固定**：`conflicts` 和 `minor_gaps` 与 finding 一样不构成覆盖义务，Writer 没有用到某条冲突或缺口本身不是缺陷（会实质改变认识的遗漏由 Whole-report Review 的 `material_omission` 兜底）。一旦使用，就写在它所改变的那个判断处，不得从正文抽出来集中安置或在文末统一交代研究本身。

正文以 Brief 所问对象为叙述主体。不得用“材料称”“材料显示”“材料能够证明”代替具体事实、来源归属或分析，也不得为了展示覆盖面依次展开 ResearchTask、厂商、产品或案例。深度来自关系、机制和转折，不来自 finding 使用数量。

#### 输出合同

输出一份完整 GFM Markdown 字符串。允许标题、自然段、列表、表格、引用块和代码块；禁止原始 HTML，因为它会绕过统一 AST、引用插入和前端安全渲染。

Writer 不输出引用角标。Markdown 中如果自行生成脚注编号，运行时按格式错误拒绝，并要求重新输出完整 Markdown，避免模型脚注与确定性引用系统形成两个编号源。

### 3.7 阶段⑦：Claim Attribution

Claim Attribution 独立于 Writer，在一个流程节点中完成 Claim 提取、证据匹配，以及修订版本上的失败片段去向核对。一次调用不得吞下整篇报告、全部候选和全部 Excerpt。执行顺序由代码决定：

```text
Markdown
  → 确定性生成候选并按连续 block 与序列化字符预算分批
  → 每批用精简 Assertion 目录选择相关材料
  → 代码展开所选 Assertion 对应的 Excerpt
  → 每批完成事实核验
  → 汇总并做全局完整性校验
  → AttributionRun
```

批次边界不能由模型决定。已完成批次从数据库恢复，后续批次失败不得重跑它们。最终完整汇总成功后，才写入 completed AttributionRun。合同失败仍是 `attribution_contract_error`，必须先持久化具体错误，不得降级为 `partial`。

#### 3.7.1 Markdown 解析与稳定锚点

确定性代码使用统一 GFM parser 将 Markdown 解析成 AST，并枚举可见文本块：

- 标题；
- 段落；
- 列表项；
- 引用块；
- 表格表头和单元格；
- 代码块说明文字。

代码块内容默认不作为研究事实正文，不在代码文本内部插入角标。如果报告对代码本身作出事实判断，该判断必须出现在普通正文或表格单元格中。

每个可见文本块由代码分配 `block_id`。Claim 不依赖 Writer 给出的 statement id，而是锚定：

```text
report_id + revision + block_id + start_offset + end_offset + text_hash
```

offset 基于文本块解析后的可见 Unicode 文本，不基于原始 Markdown 字节位置。运行时必须验证 span 非空、边界合法、hash 匹配并准确对应当前 revision。

#### 3.7.2 Claim 提取：按表面标记选片段

确定性代码扫描表面标记并框出候选分句，归因模型只在候选内部确定最小事实锚点。标记分成三族。

**检索标记**——指向材料里存在或不存在的东西，检索有明确的成功条件：

| 标记类型 | 抓什么 |
|---|---|
| 数字、比例、金额 | 编造、口径走样 |
| 日期、时间点 | 编造 |
| 引语与明确来源归属 | 编造、张冠李戴 |
| 范围量词（所有、全部、多数、大部分、普遍、主流、无一例外） | 过度概括 |

**候选标记**——要求归因模型检查附近内容，但不自动要求整个分句通过出处检索：

| 标记类型 | 处理 |
|---|---|
| 专名（公司、产品、人名、机构） | 若附近表达了具体外部事实，提取最小事实锚点；若只是分析对象，记录分析依赖，不核验相邻判断 |

**提示标记**——不指向可取回的东西，只生成审计说明：

| 标记类型 | 处理 |
|---|---|
| 程度与意义词（显著、大幅、全面、根本性、标志着、转折点） | 不进检索。同一片段内没有任何数字时，记一条 audit note 说明该评价没有量化依据；有数字时，数字本身已按检索标记核对，评价留给读者 |

- 检索标记所在候选：必须由 `verified` 或 `failed` 的事实 Claim 覆盖；
- 只有专名候选标记的候选：必须得到处理，但可以是事实 Claim，也可以是带依据关系的分析片段；
- 只含提示标记、或不含前三类标记的分析：不做出处核验，按 §3.7.4 记录依据。

##### 程度词为什么不进检索

没有任何 Excerpt 会写“这标志着一个转折”。把这类片段送进 §3.7.3，第 1 步就找不到语义候选，结果必然是 `failed`——不是因为正文写错了，而是因为它根本不在检索的定义域里。

而显著、大幅、全面、标志着恰好是一份报告有话要说时使用的词汇。把它们整类划进阻断区，等于给判断加一道词汇税，逐句削弱判断的老问题会从这个口子原样回来。归因模型对这类片段唯一能做的“核对”，是否决作者对一个它自己也看得见的数字的评价——那是内容审批，不是出处检索。

范围量词不同：“行业普遍支持”中的“普遍”指着一个可以在材料里数出来的范围，因此它是强制检索标记；单独的“行业”只是研究对象，不是范围断言。分界仍然是：**能数、能取回的留下，只表达程度和意义的移出。**

##### 标记扫描必须由确定性代码完成

**表面位置扫描由代码完成，事实锚点边界由模型确定。** 模型不能自行把候选外的文字送进出处检索，但专名只提供候选位置，不预先批准“整句都是事实”这一判断。

各类标记都有确定性识别方式：

- 数字、比例、金额、日期、时间点：正则；
- 范围量词与程度词：固定词表，与 prompt、代码一同版本化，词表版本计入 AttributionRun 以便复现；
- 专名：以本 Job 全部 usable Assertion 与 Excerpt 中出现过的实体名字为白名单扫描，命中后形成候选标记。它不能单独构成检索失败。

##### 白名单怎么建

专名候选无法靠通用正则可靠识别，因此白名单的构造方式是硬约束。中文没有词边界，从材料里按固定字宽滑窗切出来的字串大多不是专名，而是「分析师认为」「根据」「降至」这类普通行文。它们一旦进了白名单，会制造大量无意义候选并挤占归因注意力。

所以白名单只收边界能被结构线索证明的名字：

- 机构后缀（公司、集团、研究院、委员会、交易所……）前接 2–6 个汉字，且整串不含只会出现在行文里的虚词；
- 书名号内的作品名；
- 大写字母开头的拉丁字串，扣除常见句首虚词。

两边的代价不对称：漏掉一个名字可能少检查一处，收进一个假名字却会给分析增加无意义负担。因此规则宁窄勿宽。白名单上限 400 条，超出时保留较长的名字。这套规则与词表一同版本化，当前为 `v4`。日期命中不再同时生成普通数字标记。

##### 片段边界

候选分句由代码给出，事实锚点边界由归因模型确定，规则是：

- 以标记为中心向外扩，扩到构成一个可以独立核对的命题为止；
- 不得跨过分句边界，把相邻的评价性内容纳入片段；
- 每个 Claim span 只表达一个命题。复合句包含多个主体、数字或事件时，必须提取多个 span。

边界扩得过宽，会把作者判断卷进检索范围；扩得过窄，会漏掉限定条件。运行时校验每个候选都有 Claim 覆盖、每个强制检索标记都落在 `verified` 或 `failed` 的事实锚点中。专名候选可以由 `analysis` 覆盖，但必须记录实际依据或审计说明。

检索以片段为单位，不以句子为单位。同一句话里，有的片段通过、有的片段失败是正常情况；通过的照常生成角标，失败的只影响它自己那一段。

同一句可以同时存在检索片段和不检索片段，不要求 Writer 拆句。例如：

```text
2025 年企业采用率升至 42%，这更像是采购门槛下降，而不是需求突然出现。
```

应拆为：

- 检索片段：“2025 年企业采用率升至 42%”（日期、比例）；
- 不检索片段：“这更像是采购门槛下降，而不是需求突然出现”（无标记）。

只含提示标记的片段同样不进检索：

```text
这个变化显著改变了企业采购的决策链条。
```

“显著”是提示标记，片段内没有任何数字，因此只生成一条 audit note 说明该评价没有量化依据。它不进入出处检索，也不会阻断任何 revision。

范围量词一类的失败有一条专门要求：反馈必须要求 Writer 写明材料实际支持的范围，例如把“行业普遍支持”改成“五家平台中有三家支持”。不允许通过添加“在本报告收集到的材料范围内”“据现有资料”这类免责措辞来过关——这类措辞不改变命题的范围，只是把过度概括藏进免责声明，读起来像满页免责条款。该要求同时写入 §8.2 的 Writer 真实性边界和 §8.3 的归因 prompt。

这条修法只对范围量词成立，这也是程度词必须留在检索之外的另一个理由：“行业普遍”能换成“五家里三家”，“大幅增长”换不成什么——句子里通常已经有那个数字，剩下的“大幅”就是作者对它的评价。给一类失败设阻断却给不出改法，只会逼 Writer 把话删掉。

每个非空文本块必须产生一份 block assessment。只有代码确认该块没有候选且模型没有记录分析 Claim 时才能记为 `no_claims`；存在候选却没有对应 Claim 属于归因输出合同错误，不能当作检查完成。

#### 3.7.3 检索片段的出处检索

对每个检索片段：

1. 在 usable Assertion 中寻找语义候选；
2. 由代码下钻到 Assertion 绑定的 Excerpt；
3. 比对正文内容和 Excerpt 的主体、动作、数字、时间、范围、口径和来源关系；
4. 输出 `verified` 或 `failed`；`failed` 必须附 `reason`，说清材料原文是什么、正文写的是什么、差在哪；
5. `verified` 必须绑定至少一个属于所选 Assertion 的 Excerpt 并落 ClaimEvidence；`failed` 进入当前 revision 的失败记录。

归因模型不能引用 unusable Assertion，也不能引用研究材料之外的 Excerpt。Assertion 使用 `aN`，Excerpt 使用所属 Assertion 下的 `aNeN`，已知冲突使用 `xN`；Claim 及其依赖关系使用本轮的 `cN`。代码对四类局部引用分别做白名单校验并还原落库关系，模型不填写 UUID 或完整冲突键。

#### 3.7.4 不检索片段的依赖记录

不检索片段包括只含提示标记的片段和完全无标记的片段。对它们，归因阶段不做出处检索，只记录：

- 它依赖的当前 revision 中已通过的检索片段；
- 它直接使用的 Assertion；
- 已知冲突和证据边界；
- 对推理分寸的非阻断审计说明；
- 提示标记片段在同片段内没有数字时的无量化依据说明。

这些关系进入 ClaimPremise 和审计视图。分析片段不会因为没有逐字对应的 Excerpt 而生成句级失败，也不会伪造 ClaimEvidence。

依赖关系用模型自己的编号表达，不用数据库 ID。模型给本次输出里的每个 span 起一个短编号（`c1`、`c2`……），依赖写成这些短编号之间的指向，代码落库时统一还原成真实 ID。指不到任何本轮 span、指向自身或重复的关系属于输出合同错误，不能静默丢弃，否则核心性判定会失真。

第一条同时是 §5.2 判定核心片段的数据来源：一个检索片段被任何不检索片段记录为依赖，就说明有推理建立在它之上。

不检索就放行不等于什么都不拦：真正的编造仍然会被抓住——“某公司 CEO 称 MCP 是转折点”含专名和引语，找不到出处就是 `failed`。被豁免的只是“转折点”这个评价本身，不是说话的人和他说过这句话。

不检索片段构成报告主要结论，或者静默绕开 material conflict 时，交给 Whole-report Review 从整篇报告层面处理。

#### 3.7.5 失败片段的去向核对

只在修订版本上运行。初稿没有上一版，跳过这一步。

只有核心问题会触发修订。因此修订版本的归因阶段额外收到上一版的核心归因失败，逐条核对它们在新版里的去向；非核心失败不会触发 Writer，也不进入去向核对：

| 去向 | 判定 |
|---|---|
| 还在讲同一件事，现在有出处 | 改准了，通过 |
| 还在讲同一件事，绑到了别的出处 | 换出处，通过 |
| 连同建立其上的说法一起消失了 | 删掉了，通过 |
| 还在讲同一件事，但具体内容没了 | **原地降级，进入失败记录** |

最后一行是这一步唯一要抓的东西：把“42%”改成“大幅上升”、把“OpenAI 称”改成“有厂商称”、把“五家里三家”改成“部分平台”。这些改法不违反任何真实性边界，也不会被其他任何一环拦下，但它们不是修复，是把问题藏起来。

前三行都是正当修法，全部放行。**这一步不惩罚删除，也不惩罚改准，只抓原地把话改笼统。**

实现要求：

- **按内容比对，不按位置比对**。问的是“上一版这条失败讲的是 X，新版还讲不讲 X、还带不带具体内容”，因此报告被整体重排不影响判定；
- **使用提示词内短编号**。代码把上一版核心失败编号为 `p1`、`p2`……，模型逐条返回去向，代码再还原真实 finding id；每条必须恰好出现一次；
- **在全局汇总中核对**。去向判定不进入各批核验；全部批次完成后由一次汇总调用按 prior_ref 返回；
- 判定结果写入 AttributionRun。`原地降级` 进入 `blocking_findings`，必须同时给出新版文字的 block 和 span，使反馈始终指向当前 revision。

这一步不违反 §1.5：§1.5 禁止的是复用旧 revision 的归因结果来跳过检查，这里是拿旧结果多加一道检查，方向相反——新版仍然完整重跑全部标记扫描和出处检索，去向核对是在此之外附加的。

##### 为什么需要这一步

出处检索是整条链路上唯一一股往笼统推的力，而且它触发得最频繁。Writer 面对的实际梯度是：写得具体可能被拦，写得笼统永远安全——而把具体表述换成笼统表述是完全合法的，不编造、不改数字、不藏冲突、照样回应 Brief，其余任何一环都拦不住。两轮修订下来，模型会自然漂到那一侧，报告因此变平。

这一步只盯住真正承载报告回答的失败。边角事实不触发修订，也就不需要用一轮额外检查约束 Writer；核心事实一旦要求修复，则不能通过保留原结论、只删除具体性的方式蒙混过关。

#### 3.7.6 归因输出

- 完整 Claim 列表、span anchor 和 `markers[]`（每项含族别）；
- 通过检索的 ClaimEvidence；
- ClaimPremise；
- 失败记录（`blocking_findings`），每条带 `reason`；
- 失败片段去向核对结果（仅修订版本）；
- 不检索片段的 audit notes，含提示标记片段的无量化依据说明；
- 本次使用的标记词表版本；
- 每个 Markdown block 的检查完成记录；
- 被丢弃的畸形 Claim 记录（§4）。

### 3.8 阶段⑧：Whole-report Review

Whole-report Review 不重复逐项匹配 Excerpt，也不把普通文风偏好变成失败。

只要 AttributionRun 自身合同有效，阶段⑧就必须运行，即使其中已有归因失败记录。这样 Writer 下一轮可以一次收到片段问题和整篇问题。Attribution 解析或合同失败时不进入阶段⑧，直接按系统输出合同错误处理。

#### 输入和模型预算

- 完整 Markdown；
- Brief；
- 当前 ResearchSynthesisRun；
- AttributionRun 摘要（见下）；
- usable Assertion 摘要；
- ConflictResolution 和 minor gap。

整体审阅使用 `strong_model`，开启 thinking，并使用独立输出预算。首版设置 `MAX_WHOLE_REVIEW_TOKENS = 4096`，不复用出处检索的短判定预算。

摘要而不是完整 AttributionRun：每个 block 的通过数与失败数、已核对事实锚点的短文本和位置、分析片段与事实锚点之间的依赖、尚未解决的失败原文和原因。摘要不含 Excerpt，因此 Reviewer 能判断推理是否有落点，却不能重新做出处匹配。

usable Assertion 摘要只用于发现会改变核心认识的反例、冲突或证据边界，不是覆盖清单。Writer 没有使用某条 Assertion 不能单独构成遗漏。

#### 阻断项

1. `brief_response`：报告没有实质回应 Brief；
2. `user_constraint`：违反用户明确限制；
3. `material_omission`：遗漏足以改变读者理解的重要反例、冲突或局限；
4. `conclusion_integrity`：主要结论与正文实际推理不一致，或者无法从已核对事实、材料冲突或证据缺口形成可识别的依据链。

“报告根本没有形成可识别的回答”归入 `brief_response`，不再单列文风色彩较强的 `unreadable_reasoning`。结论是否落地看跨 block 的 ClaimPremise 和整体推理，不要求结论所在段落紧邻数字或角标，也不要求每一项分析都能在单条 Excerpt 中找到同样措辞。

留一个出口：结论是“现有材料无法判断”时，只要写清楚这个判断基于哪些来源冲突或证据缺口（例如三份来源的统计口径互不兼容，分别是什么），就算落地。这一条交给审阅模型判断而不是由代码计算，正是为了这个情形。

普通重复、章节长短、事实较密、叙事顺序、结构复杂和文风问题只形成 audit note，不触发修订。只有这些问题已经使读者无法知道报告对 Brief 的实质回答时，才进入 `brief_response`。如果报告把研究材料、来源能力或证据边界变成主要叙述对象，挤占了对 Brief 所问对象本身的解释，也属于 `brief_response`；局部必要的来源限定不属于这种情况。

Research Synthesis 只是审阅上下文，不是通过标准。以下情况不能单独构成阻断：

- Writer 没有复述 Synthesis 原文；
- Writer 调整了认识的顺序或数量；
- Writer 没有逐项使用 Synthesis 引用的 Assertion；
- Writer 基于同一批材料形成了不同但不失实的组织和综合。

#### 主要认识块

除阻断项外，Whole-report Review 还必须输出 `key_block_ids[]`：承载报告主要认识和推理的 Markdown block。只要报告形成了可识别的回答，该数组就不得为空；若根本找不到主要认识，Reviewer 必须给出 `brief_response` 阻断项。

这不是对观点的批准，只是指认位置——不评价这些块里的观点对不对，只说明报告的主要认识写在哪几块。它与 ClaimPremise 一起作为 §5.2 判定核心片段的输入，让最终状态可以由代码算出来，不必在定稿时临时引入新的模型判断。

#### 路由

- 归因无失败记录、整体审阅无阻断项：进入阶段⑨，核验结论 `verified`；
- 还有修订额度，且存在核心归因失败或 Whole-report blocker：合并反馈交给 Writer，生成下一份完整 Markdown；
- 其余情况：进入阶段⑨，核验结论为 `partial` 或 `failed`。

**没有一条路径通向终止。** 整体审阅无论判出什么，报告都会渲染并交给用户；`failed` 描述的是这份报告的核验结果，不是任务失败。理由在 §5.3。

合并反馈交给 Writer 时只带具体问题——当前 block 的原文、材料实际支持什么、差在哪，以及为什么影响核心回答——不带阻断项分类代号。运行时必须把 block id 展开成当前正文，不能把 Writer 看不懂的内部编号单独交出去。完整理由见 §8.2。

Whole-report Review 不回到 Research Synthesis。Synthesis 不是需要重新批准的答案，Writer 已经持有全部 usable 材料，可以直接根据整篇反馈重写。

### 3.9 阶段⑨：Deterministic Finalization

只有最终 revision 完成归因和整体审阅后，代码才能定稿。

#### 引用生成

- 只从通过的归因记录生成角标；
- 角标插入通过的 Claim span 末尾，位置由该 block 在原始 Markdown 中的绝对起点加上 span 偏移算出，从后往前插入；
- 同一来源按正文首次出现顺序编号；
- 表格引用插入对应单元格；
- 不检索片段可以在审计视图展示依据，但没有直接 ClaimEvidence 时不伪造角标；
- 引用编号、脚注、来源列表和 Excerpt 映射全部由代码生成。

定位必须用绝对偏移，不能靠在 Markdown 里搜索 block 原文再替换。一篇报告里重复出现的短块很常见——表格里相同的单元格值、列表里重复的小标题——按文本搜索会一律插到第一处，也就是插错行。因此解析阶段在分配 `block_id` 的同时记下每个块在原始 Markdown 中的起止位置，并用一个只前进的游标保证重复文本依次落到各自的位置。

#### 审计视图

读者可以从正文中的 Claim 打开审计视图，查看：

- Claim 原文、命中的标记及其族别；
- 通过时匹配的 Excerpt，失败时的 `reason`；
- Document 来源、版本、作者、日期和 URI；
- 来源 caveat；
- 不检索片段所依赖的已通过检索片段；
- 已知冲突和证据边界；
- 非阻断 audit note。

审计视图另有一个报告级摘要：检索片段总数、通过数、失败数，以及主要认识块内已通过的检索片段。读者不必逐条点开，就能看出这份报告的地基有多厚。

不检索片段不在正文后附“未通过核对”。审计视图明确标记“作者分析”及其依据，让读者自行判断。

修订结束后仍为 `failed` 的检索片段保留在正文中，不生成已验证引用角标；失败原因保留在审计视图。核心与非核心在这里一样处理，差别只体现在核验结论上，不体现在正文里。

整篇级别的结论不进正文。报告不管判成 `verified`、`partial` 还是 `failed`，交付的正文都是同一份，没有横幅也没有整篇声明；结论写在 `report_runs_v2.verification_status` 和审计视图里，想看的读者随时能看到，不想看的也不会被一段免责声明挡在报告前面。

---

## 4. 循环、预算和终止

| 循环 | 触发 | 上限 | 上限后的结果 |
|---|---|---:|---|
| 研究循环 ②→③→④→② | Research Verifier 确认 major gap | 沿用 effort 对应 Planner 决策轮 | major gap 仍存在则失败 |
| 综合触发补研究 ⑤→④→② | Synthesis 发现阻断 Brief 回应的缺口，Verifier 确认 | 消耗现有 Planner 决策轮，不设独立额度 | 预算耗尽后 major gap 仍存在则失败 |
| Writer 修订 ⑥→⑦→⑧→⑥ | 存在核心归因失败或 Whole-report blocker | 2 次 | 按最终状态规则收口，报告照常交付 |

`writer_repairs_used ≤ 2`，是 Job 全局计数，不因补研究或重新生成 ResearchSynthesis 而重置。

初稿不计入 `writer_repairs_used`，因此一个 Job 最多产生三份完整 Markdown：初稿和两次 Writer 修订。

所有修订都按核心性设闸。整篇重写会让已经通过的部分重新面对全部检查，为一个边角日期赌上正文里已经站住的内容并不划算；而一个因为任何细枝末节都要重写整篇的 Writer，学到的是少写细节。非核心失败保留明确标记并以 `partial` 交付，不消耗修订预算。

循环之间不能走捷径：

- 补研究后必须重新经过 Research Verifier 和 Research Synthesis；
- 任一 Markdown 修改后必须重跑完整 Claim Attribution 和 Whole-report Review；
- Planner、Worker 或研究预算耗尽不能让系统跳过后续核验；
- verifier、synthesis、attribution 或 review 自身输出合同失败属于系统失败，不得伪装成正文失败并驱动 Writer 改写。

Research Synthesis、Markdown Writer、Claim Attribution 和 Whole-report Review 的结构化输出各允许一次基于原始输入的独立合同重试。第二次仍无法解析或违反输出格式时，Job 以对应的输出合同错误失败。合同重试不改变业务循环计数。

归因阶段另有一层更细的容错：单条 Claim 越界、hash 不匹配或编号重复时可以丢弃并记入审计，但丢弃后仍必须满足候选完整覆盖。畸形比例超过 20%、一个候选无人处理、强制检索标记没有事实锚点、`verified` 没有对应证据，或上一版核心失败没有逐条交代去向，都属于本轮输出合同错误。

---

## 5. 最终状态

### 5.1 `verified`

同时满足：

- 报告实质回应 Brief；
- 所有检索片段均为 `verified`；
- 没有与材料明确矛盾的事实；
- 去向核对没有留下未解决的原地降级记录；
- material conflict 和重要局限得到诚实处理；
- 正文推理与报告结论一致；
- Whole-report Review 无阻断项。

不检索片段存在 audit note 不影响 `verified`。`verified` 表示事实链完整、报告履行研究任务，不表示系统替读者批准了所有观点。

### 5.2 `partial`

只允许以下情况：

- 剩余问题全部非核心，因此不触发修订；
- 剩余 `failed` 只涉及非核心检索片段；
- 这些片段被明确标出且不生成角标；
- Whole-report Review 没有留下阻断项。

核心推理依赖的事实没有精确支持时，不能记为 `partial`，必须记为 `failed`。整体审阅只要留下任何一条阻断项，报告就不是 `partial`——整篇级别的问题按定义就是核心问题。

#### 核心片段的判定

“非核心”不能留成一句无人执行的描述。一篇长报告会有数百个检索片段，两轮修订后全部通过的概率不高，因此**多数 Job 实际会落在这条判据上**；而 §9 要求终态由代码计算，代码判断不了“删掉这句会不会改变主要推理”。

因此核心性由两份已经落库的数据确定，不引入新的模型判断。一个检索片段是**核心片段**，当且仅当满足任一条件：

1. 它被任何 ClaimPremise 引用——即至少有一个不检索片段把它记录为依赖（§3.7.4）；
2. 它落在 Whole-report Review 输出的 `key_block_ids[]` 之内（§3.8）。

其余检索片段为非核心。

第一条对应“有推理建立在它之上”，第二条对应“它在承载主要认识的段落里”。两者都是最终 AttributionRun 和 ReportReviewRun 里现成的字段，代码直接读，定稿阶段不再发起任何模型调用。

核心性有两个用处，不止终态一个：终态计算读最终 revision 的记录，修订决策读当前 revision 的记录（§4）。两处读的是同两个字段，规则完全一致，因此不会出现「修的时候算核心、定稿时算非核心」这种互相打架的情形。

去向核对只接收上一版的核心归因失败，因此判出的原地降级天然仍是核心问题。整体审阅的阻断项同样按核心处理。

判定使用对应 revision 的 AttributionRun 与 ReportReviewRun。终态计算只看最终 revision，中间 revision 的记录不参与。

### 5.3 `failed`

`failed` 有两个层次，必须分开看，否则「报告不合格」和「任务没跑完」会共用一个词，用户和排查的人都读不出发生了什么。

#### 报告核验结论 `failed`

写在 `report_runs_v2.verification_status`，任一情况成立：

- 报告没有实质回应 Brief；
- 主要推理或结论依赖无支持事实，即修订结束后仍有核心检索片段为 `failed`（核心的定义见 §5.2）；
- 报告遗漏会改变读者理解的重大冲突；
- 整体审阅留下任何其他阻断项。

**这类报告仍然交付。** 它已经是模型在给定材料和额度内能拿出的最好结果。扣住不发，用户拿到的是一片空白加一句错误信息——既看不到写出来的内容，也看不到问题出在哪；交付它，用户至少能读到正文，能从审计视图看出哪几句没站住。系统该做的是把结论如实记下来，不是替用户决定这份报告不值得看。

正文不加整篇级别或逐句的核验标注（§3.9）。失败原因留在审计视图中，供读者按需查看；正文保持为 Writer 产出的原文。

#### 任务失败 `jobs.status = failed`

报告根本没能产生，任一情况成立：

- Research Verifier 确认的 major gap 在预算耗尽后仍存在；
- synthesis、attribution、review 或其他模型输出合同重试后仍然失败；
- 确定性解析、持久化或渲染失败；
- 运行环境本身出错。

用户主动取消记为 `cancelled`，与失败分开。

只要走到渲染，Job 就是 `completed`，`outcome` 为 `report_rendered`，核验结论另行记录，不会因为报告不合格而把任务标成失败。网络中断这类外部原因也不写成任务失败，而是留成可恢复的中断，让 Job 重跑时从最深的已完成阶段继续（§7.2）。

成功定稿后的终态 phase 使用 `report_rendered`，不再用 `draft_rendered` 表示已经完成核验的最终报告。旧词在收尾判定里保留为兼容取值，只为让改名之前写下的 checkpoint 还能正常收尾。

---

## 6. 数据合同

### 6.1 保留的合同

- `ResearchBrief`；
- `Plan` 和 `ResearchTask`；
- `Document` 和 `DocumentView`；
- `Excerpt`；
- `Assertion`；
- `VerifierDecision`、`ConflictResolution` 和 `AssertionDisposition`；
- Research Worker 的 `search / save / finish` 动作合同。

### 6.2 新增或重定义的合同

#### ResearchSynthesisRun

- `synthesis_run_id`；
- `job_id`；
- `version`；
- 初稿 prompt 与完整原始输出；
- 独立检查 prompt、完整原始输出与缺陷列表；是否采用初稿由代码根据缺陷计算；
- `decision = ready | needs_research`；
- `synthesis`；
- 持久化的 `assertion_ids[]`（模型线协议为 `assertion_refs[]`）；
- 持久化的 `material_conflict_keys[]`（模型线协议为 `material_conflict_refs[]`）；
- `reason` 和 `evidence_needed`，仅用于 `needs_research`；
- 原始模型输出、运行状态和合同错误。

`synthesis` 是单个连续文本字段，不拆成 `conclusions[]`。

#### ReportRevision

- `report_id`；
- `revision`；
- `synthesis_run_id`；
- `full_prompt`；
- `raw_output`；
- `markdown`；
- `markdown_hash`；
- `parsed_blocks`；
- `status`。

`synthesis_run_id` 记录 Writer 当时看到的分析版本，不表示正文必须服从该版本。

`parsed_blocks` 的每个 block 除 `block_id` 外还记录 `source_start` / `source_end`：该块在原始 Markdown 字符串中的起止位置，供 §3.9 的角标定位使用。解析不出位置时记 `-1`，该块不参与角标插入。

#### Claim

Claim 继续表示 Report Attribution 对正文的落库验证记录，锚点改为：

- `block_id`；
- `start_offset`；
- `end_offset`；
- `text_hash`；
- `text`；
- `markers[]`。

`markers[]` 记录该 span 命中了 §3.7.2 的哪些标记，每一项带族别（`retrieval`、`candidate` 或 `advisory`）。`retrieval` 必须属于事实锚点；`candidate` 是否形成事实锚点由归因语义判断；只含 `advisory` 或为空的分析片段不产生事实失败。ClaimEvidence、blocking finding 与 ClaimPremise 共同说明该 Claim 是已核对事实、失败事实还是分析依赖。

Claim 不再带 `grounding` 或 `claim_type`——归因阶段不再区分事实 Claim 和判断 Claim。Claim 也不再要求 Writer 提供 statement id。

#### AttributionRun

- revision；
- block assessments；
- Claim 提取结果；
- ClaimEvidence 和 ClaimPremise；
- 失败记录（`blocking_findings`），每条带 `reason`；
- 核心失败的去向核对结果（仅修订版本），含提示词内短编号与上一版 finding 的绑定；
- 不检索片段的 audit notes；
- 本次使用的标记词表版本；
- 原始模型输出和合同错误。

#### ReportReviewRun

- revision；
- `synthesis_run_id`；
- 阻断项（`blocking_findings`）；
- `key_block_ids[]`：承载主要认识和推理的 block，供 §5.2 判定核心片段；
- audit notes；
- 原始模型输出和合同错误。

#### 报告运行记录

`report_runs_v2` 另有一列 `verification_status`，取值 `verified | partial | failed`，由 §5 的规则算出，在渲染完成的同一个事务里写入。它是核验结论的唯一权威来源：Job 的 `status` 和 `outcome` 只说明任务有没有跑完，API 的报告详情从这一列读结论。

### 6.3 删除的现行合同

完整实施时删除，不保留运行时兼容分支：

- Writer `ReportStatement.kind` 自报；
- Writer `statement_id`；
- Writer `candidate_excerpt_ids`；
- Writer `premise_statement_ids`；
- `ReportStreamAssembler` JSONL 成文协议；
- `patch_statement`；
- `patch_paragraph`；
- 基于旧 statement id 的 dirty propagation；
- 基于旧 statement id 的 citation map；
- 逐句 `overreach / miscalibrated` 驱动 Writer 修订的合同；
- 事实 Claim 与判断 Claim 的二分，以及 Claim 的 `grounding` 和 `claim_type` 字段；
- ClaimEvidence 的 `support / partial / contradict / none` 四值 `relation`；
- “多条 Excerpt 必须联合覆盖整句才算 support”的逐句覆盖度规则；
- 候选答案、答案选择和 AnswerContract 合同。

ClaimEvidence 与 ClaimPremise 保留，但生产者改为 Claim Attribution。ClaimEvidence 不再带 `relation`：只有通过归因的片段才生成 ClaimEvidence，失败的片段进入 AttributionRun 的 `blocking_findings`。

---

## 7. 持久化与恢复

### 7.1 Append-only 记录

以下对象均版本化或 append-only：

- ResearchSynthesisRun；
- Markdown revision；
- AttributionRun；
- ReportReviewRun；
- ClaimEvidence、ClaimPremise 和 verdict。

不得覆盖旧 revision 的正文、归因或审阅结果。

### 7.2 Checkpoint 恢复

恢复时根据数据库中已完成的最深阶段继续：

- Research Verifier 已通过、无合法 ResearchSynthesisRun：从⑤继续；
- Synthesis 请求补研究、尚无对应 verifier decision：从④继续；
- 已有可进入成文的 ResearchSynthesisRun、无 Markdown：从⑥继续；
- 已有完整 Markdown revision、无 AttributionRun：从⑦继续；
- AttributionRun 完成、无 ReportReviewRun：从⑧继续；
- review 通过、尚未 render：从⑨继续。

模型调用开始前必须先落 `running/prompted` 记录。只有完整、通过输出格式和锚点校验的结果才能标记完成。未完成输出不得作为下一阶段输入。

### 7.3 旧数据边界

旧结构化 report revision 无法无损转换成“Writer 原始 Markdown + 后置 Claim span”，因为旧正文已经受 statement 切分和 Writer 自报依据影响。本方案不定义旧报告继续进入新修订循环的兼容路径。

实施迁移时：

- 旧对象存储中的已渲染报告可以作为不可变历史产物保留；
- 旧运行中的 Job 不允许跨合同 resume；
- 活跃数据库只使用新合同；
- 删除旧报告表或旧列前，必须明确告知本地历史 report、revision 和 claim 数据将不可恢复，并单独取得执行授权。

---

## 8. Prompt 职责边界

### 8.1 Research Synthesis prompt

只包含：

- Brief；
- 全部 usable Assertion 和对应 Excerpt；
- 来源信息、冲突和 gap；
- 连续分析输出合同；
- 真实性和证据边界要求。

材料按 ResearchTask 的研究问题组织，避免把全部 Assertion 变成一条无结构的清单。第一步不要求固定数量的结论，不得生成标题、章节、文章提纲、篇幅或文风建议。

Research Synthesis、Writer、Claim Attribution 和 Whole-report Review 的模型上下文统一使用
局部短引用：普通研究材料使用 `tN / aN / eN / xN`，Attribution 因需表达 Assertion 与
Excerpt 归属而使用 `aN / aNeN / xN`。数据库 UUID 与完整冲突哈希只存在于领域对象、确定性
校验和持久化层。

第二步使用只含 Brief、初稿、研究任务问题与数量信息、已确认冲突和非来源类 minor gap 的独立消息上下文，检查初稿是否实质回应 Brief、是否沿 ResearchTask 复述、是否退化为材料罗列或缩略报告、是否缺少取舍和重要关系解释、是否被证据边界喧宾夺主，以及是否失真或遗漏重大冲突。任务问题和数量只帮助判断是否照搬采集路线，不恢复 Assertion/Excerpt 覆盖核对。出现这些实质分析问题时触发 `revise`；不得按文风、段落结构或另一种同样合理的分析偏好改写。

### 8.2 Writer prompt

只包含：

- 写作任务；
- Brief；
- ResearchSynthesis；
- 全部 usable Assertion 和对应 Excerpt；
- 来源 caveat、冲突和 gap；
- 真实性边界；
- GFM Markdown 输出协议。

其中 ResearchSynthesis 只投影最终采用的 `decision / synthesis / reason / evidence_needed`。版本、引用 id、运行状态和两步原始审计记录不得进入 Writer prompt；Whole-report Review 使用同一投影。

不规定段落顺序、材料取舍、论点位置、篇幅、每段结构或是否必须使用表格。不得要求 Writer 按 Synthesis 的顺序或内容逐项展开。

`source_credibility` minor gap 以 `source_caveat` 附着到受影响的 finding，不以全局 gap 进入 prompt。Writer 在使用该 finding 的位置直接写准来源主体和范围；正文直接讨论 Brief 所问对象，不用“材料”“证据”“本报告”替代事实和分析，也不为展示覆盖面逐项展开输入。`conflicts` 和 `minor_gaps` 按需选取，用到时写在它所改变的判断处，不集中安置于文末。

修订反馈要求写明材料实际支持的范围时，Writer 必须改写范围本身（例如把“行业普遍支持”改成“五家平台中有三家支持”）。不得用“在本报告收集到的材料范围内”“据现有资料”这类免责措辞代替范围改写——这不改变命题，只把过度概括藏进免责声明。

修订提示还必须说明：反馈只包含核心问题，通常只需改准相关内容并保留其余正文。输出合同仍是完整 Markdown，不恢复 patch。

修订也不得以降低具体性的方式过关。一个检索片段失败时，合格的改法只有三种：改准、换成有出处的说法、连同建立在它之上的推理一起删掉。把“42%”改成“大幅上升”、把“OpenAI 称”改成“有厂商称”、把“五家里三家”改成“部分平台”，都不是修复。这条由 §3.7.5 的去向核对执行，提示词里同时写明，避免 Writer 在不知情的情况下踩中。

#### 检查规则不进 Writer 提示词

阻断项分类、标记词表、`key_block_ids`、核心性判定规则，一律不写进 Writer 提示词。修订反馈只以具体问题的形式给出——这句话的哪个部分、材料里是什么、差在哪——不给出规则清单，也不给出示范措辞。

这是硬规定，因为它保护的正是本次重构的前提：检查全部位于 Writer 下游，所以增加检查不会收窄写作。规则一旦渗进提示词，这个解耦就消失了，提示词会重新长成一份写作守则，而写作守则正是上一代 Writer 写不出文章的原因。

**不给示范措辞**这半句尤其要守。现行提示词给过“在本报告收集到的材料范围内”这类修改示例，模型把它原样抄进了正文，同一句话在一篇报告里出现五次。反馈要说清问题，不要给出可以照抄的句子。

这里的分界是正例与反例：说明什么不算修复（“把 42% 改成大幅上升不是修复”）可以写，因为它不提供任何可以填进正文的措辞；给出一句“可以这样写”的模板不能写，因为模型会照抄。

这条规则约束的不只是提示词正文。修订反馈是拼进同一段上下文的 JSON，把整份归因记录或审阅记录序列化进去，阻断项分类就会从字段里原样漏出去，效果和写在提示词里一模一样。因此反馈只携带当前 block 的 id 与原文，以及说明材料边界、具体差异和核心影响的 `reason`。运行时必须展开 block 原文，不能让 Writer 猜内部编号。

### 8.3 Claim Attribution prompt

归因分成选择、核验和（如有修订）去向汇总三次模型调用，均不得一次吞下全文、全部候选和全部 Excerpt。第一阶段只给本批候选和 Assertion 文本目录；第二阶段只展开该批选中的 Assertion/Excerpt。只在代码给出的候选内提取最小事实锚点并作 `verified / failed` 判定；相邻分析使用 `analysis` 记录实际依据，不接受句级事实审批。专名只是候选标记，不自动使整句进入出处检索；数字、日期、引语和明确范围必须由事实锚点覆盖。每个候选都必须得到处理。不得重写正文、建议文风或使用输入外知识补证据。

每条 `failed` 必须写出可操作的 `reason`：正文写了什么、材料实际支持什么、差在哪。只说“找不到出处”不算。范围量词类失败必须写明材料实际支持的范围；不得建议用免责措辞替代范围改写。

### 8.4 Whole-report Review prompt

只判断这份深度研究报告作为整体是否成立：是否实质回应 Brief、遵守用户限制、遗漏会改变核心认识的反例或边界，以及主要结论能否从已核对事实、冲突或缺口形成可识别的推理链。内部阻断项只保留 `brief_response`、`user_constraint`、`material_omission` 和 `conclusion_integrity`。

另外必须输出 `key_block_ids[]`，指出承载主要认识和推理的 block。这是位置指认，不是观点批准：不评价这些块里的观点对不对，只说明报告的主要认识写在哪几块。

usable Assertion 只用于识别足以改变核心回答的遗漏，不是覆盖清单。不得重复逐项匹配 Excerpt，不得要求主要认识与事实锚点位于同一 block，不得把复杂结构、事实密度或个人文风写成阻断项，也不得因为 Writer 没有复述 Synthesis 而失败。每条阻断项必须引用当前 block 并说明它为什么影响核心回答。

---

## 9. 确定性代码职责

LLM 不负责：

- Markdown AST 解析；
- block id 和 span offset 分配；
- 表面标记扫描与标记词表匹配；
- 按连续 Markdown block 和序列化字符预算划分归因批次；
- 检索 span 至少覆盖一个检索标记位置的校验；
- text hash 校验；
- Assertion 和 Excerpt ID 白名单校验；
- ClaimEvidence 和 ClaimPremise 外键绑定；
- 引用编号、角标和来源列表；
- block 在原始 Markdown 中的起止位置记录，以及按绝对偏移的角标插入；
- 表格单元格内角标插入；
- revision、循环和预算计数；
- 核心 / 非核心检索片段的判定，以及据此作出的修订决策与终态决策；
- `verified / partial / failed` 状态计算；
- 核验结论的落库与对外暴露，及其与 Job 任务状态的分离；
- ResearchSynthesisRun 与 ReportRevision 的版本关联；
- checkpoint 恢复路由。

最终状态必须由代码根据已完成的 AttributionRun 和 ReportReviewRun 计算，不能让 Writer、Synthesis、Attribution 或 Reviewer 自己宣布。

---

## 10. 实现顺序

这是一次合同替换，按架构依赖顺序实施：

1. **schemas**：ResearchSynthesisRun、Markdown revision、带三类 `markers[]` 的 Claim span、事实锚点两值归因、分析依赖、带稳定 finding id 的归因失败，以及收敛为四类 blocker 的 Whole-review findings；
2. **store / migrations**：持久化新版本对象和 Claim 锚点，新增带族别的 `markers[]`、标记词表版本、`key_block_ids[]` 与失败记录的 `reason`，删除 Claim 的 `grounding` 与 `claim_type`、ClaimEvidence 的四值 `relation`，以及旧 statement 流、答案选择和 patch 合同；
3. **agents**：Research Synthesis、Markdown Writer、Claim Attribution（含失败片段去向核对）、Whole-report Reviewer；
4. **deterministic**：GFM AST、标记扫描器与词表、span 校验、核心性判定与状态计算、引用和审计组装；
5. **flow**：接入九阶段、研究循环和成文循环；
6. **API / CLI / events**：展示 synthesis、attribution、review 和新终态；
7. **web**：渲染原生 Markdown、表格、事实失败标记和可展开审计视图；
8. **docs / eval / tests**：同步替换 `docs/design.md`、`docs/implementations/m1.md`、AGENTS 领域词说明和验收用例。

第 1–7 步已完成。第 8 步只完成了本文自身的同步：`docs/design.md`、`docs/implementations/m1.md` 和 AGENTS 领域词说明尚未按新链路改写。

不得让新 Writer 输出 Markdown 后，再把它转换回旧 `ReportStatement` 复用旧 Verifier。这会同时保留旧约束和新增解析复杂度，违背本次重构目标。

---

## 11. 全链路验收

### 11.1 合同测试

- ResearchSynthesis 只接受 `ready` 和 `needs_research` 两种互斥输出；
- `synthesis` 必须是单个连续文本字段，不存在 `direct_answer`、`conclusions[]` 或候选答案；
- Research Verifier 与 Research Synthesis 的模型 JSON Schema 不包含 UUID；Verifier 两段共享同一
  `tN / aN / eN` 命名空间，Synthesis 另使用 `xN` 指向已确认冲突；
- Synthesis 引用只能指向 usable Assertion；
- Synthesis 冲突键只能指向当前 Job 的真实 ConflictResolution；
- `needs_research` 必须同时包含有限分析、`reason` 和具体 `evidence_needed`；
- 每次 Synthesis 固定保存初稿与独立检查；检查输出缺陷列表，有缺陷时必须带完整的修订结果；是否采用初稿由代码计算；
- Writer prompt 不出现 statement id、kind、Excerpt 绑定、premise 申报或按 Synthesis 逐项成文要求；
- Writer 输出只接受可解析 GFM Markdown，拒绝模型自生成引用脚注和 raw HTML；
- 每个 Markdown 可见文本块都有 block assessment；
- Claim span offset 与 hash 必须匹配冻结 revision；
- Attribution 只能绑定 usable Assertion 和对应 Excerpt；
- 事实锚点的归因结果只接受 `verified` 和 `failed` 两值；分析片段使用 `analysis` 记录依据，不产生事实失败；
- 只有 `verified` 生成引用；
- `verified` 必须绑定至少一个属于所选 Assertion 的 Excerpt；
- 每条 `failed` 记录必须带可操作的 `reason` 文本，说清材料原文、正文写法和差在哪；
- 每条 Claim 必须带 `markers[]`，每项含族别；不含 `retrieval` 标记的片段不得产生失败记录；
- 标记扫描必须由确定性代码完成；日期不得同时再生成普通数字标记；候选按连续 Markdown block 和序列化字符预算分批，批次边界不能由模型决定；每个候选都必须得到处理，每个强制检索标记都必须由 `verified` 或 `failed` 的事实锚点覆盖；
- 归因第一阶段只给 Assertion 文本目录，第二阶段只展开该批实际选择的 Excerpt；已完成批次可恢复，合同错误必须先落库再以 `attribution_contract_error` 失败；
- 专名只生成候选标记，不自动让相邻分析进入出处检索；单独的“行业”不是范围量词；
- 程度与意义词不得单独触发检索；只含提示标记的片段不得出现在 `blocking_findings` 中；
- Whole-report Review 必须输出 `key_block_ids[]`；
- 核心片段必须由 ClaimPremise 引用关系与 `key_block_ids[]` 计算得出，定稿阶段不得为此发起模型调用；
- 初稿不运行去向核对；修订版本必须用提示词内短编号对上一版每条核心归因失败给出去向判定；
- 去向判定为“原地降级”时必须进入 `blocking_findings` 并带 `reason`；改准、换出处、整体删除三种去向不得产生失败记录；
- Writer prompt 不得出现阻断项分类、标记词表、`key_block_ids`、核心性判定规则或可照抄的示范措辞；
- 不检索片段的 audit note 不进入句级修订；
- 每次 Markdown 改动后 Attribution 和 Whole-report Review 全量重跑；
- 专名白名单只收结构线索能证明边界的名字；材料里的普通行文（“分析师认为”“根据”“降至”）不得进入白名单；
- ClaimPremise 的依赖关系必须由模型自己的短编号解析得出，未知、自指或重复关系属于输出合同错误；
- 角标按 block 的绝对起止位置插入；正文存在重复文本块时，角标必须落在各自的块上；
- 交给 Writer 的修订反馈含当前 `block_id`、展开后的 block 原文和 `reason`，不含阻断项分类代号；
- 整体审阅收到事实锚点、分析依赖和失败摘要，不收到完整 AttributionRun 或 Excerpt；
- 单条畸形 Claim 可以丢弃并记入审计，但丢弃后必须继续满足候选完整覆盖；
- 只有核心归因失败或 Whole-report blocker 触发 Writer 修订；非核心失败直接收口为 `partial`；
- 无论核验结论是 `verified`、`partial` 还是 `failed`，报告都进入渲染，Job 记为 `completed`，`outcome` 为 `report_rendered`；
- 核验结论写入 `report_runs_v2.verification_status`，并能从 Job 详情读出。

### 11.2 行为测试

至少覆盖：

1. 研究问题有明确答案时，Synthesis 在连续分析中直接表达，不需要 `direct_answer` 字段；
2. 研究涉及多个方面时，Synthesis 自然连接多项认识，不输出编号结论；
3. 材料无法定论时，Synthesis 明确说明不能判断什么及其证据原因；
4. Synthesis 提出重大缺口时，必须先经 Research Verifier 确认再进入 Planner；
5. 初稿主要罗列材料、没有解释重要关系或把证据边界写成主线时，独立检查必须返回相应缺陷和完整修订结果；仅有文风差异时缺陷列表为空；
6. Research Verifier 不确认 Synthesis gap 时，不进入 Planner，Writer 使用有限分析继续成文；
7. Writer 不按 Synthesis 顺序组织文章，只要报告回应 Brief 且不失实，Whole-report Review 不得阻断；
8. 同一句里检索片段和不检索片段能分别处理，通过的片段照常生成角标；
9. 表格中每个事实单元格独立生成引用；
10. 厂商声明被写成无归属事实时判 `failed`；只有它属于核心片段时才触发修订；
11. 数字正确但范围或统计口径错误时判 `failed`，非核心时直接以 `partial` 收口；
12. 分析片段只记录依据；带范围量词的过度概括进入事实核对，反馈写明真实范围，不接受添加免责措辞的改法；
13. 报告没有实质回应 Brief 时，由 Whole-report Review 阻断；
14. Writer、修订 Writer 和 Whole-report Review 的 prompt 不包含 Synthesis 初稿、检查 prompt、检查输出或运行元数据；`source_credibility` minor gap 只附着到相关 finding，不作为全局免责声明输入；报告把研究材料、来源能力或证据边界写成主要叙述对象并挤占 Brief 解释时，以 `brief_response` 阻断；
14. 报告遗漏足以改变结论的冲突时，由 Whole-report Review 阻断；
15. 修订结束后只有非核心事实失败时判 `partial`，报告照常交付；
16. 主要推理依赖无支持事实时判 `failed`，报告仍然交付，正文不加整篇级别标注；
17. verifier、synthesis、attribution 或 review 输出合同重试后仍失败时 Job 失败，不修改正文；
18. 只含程度词的片段（如“这个变化显著改变了决策链条”）不进检索、不产生失败记录，同片段内没有数字时生成 audit note；专名只形成候选，不自动核验相邻分析；
19. 同一片段内既有数字又有程度词时，数字进检索、程度词不进检索，两者互不影响；
20. 剩余失败片段被 ClaimPremise 引用或落在 `key_block_ids[]` 内时判 `failed`；两者都不满足时才判 `partial`；
21. 上一版核心失败在新版被改准、换出处或整体删除时，去向核对放行；每条使用短编号且必须恰好交代一次；
22. 上一版核心失败在新版被改写成同义的笼统表述时，去向核对定位新版文字并判为原地降级；
23. 主要结论找不到从已核对事实、冲突或缺口形成的依据链时，由 `conclusion_integrity` 阻断；依据可以跨 block，不要求局部堆放事实；
24. 任何轮次都只有核心问题触发修订；纯非核心问题直接收口为 `partial`；
25. 空 `claims`、遗漏候选、强制标记无事实锚点或 `verified` 无对应 Excerpt 时按归因输出合同错误处理；
26. 模型一次格式错误后重试成功时 Job 不失败；单条畸形 Claim 在候选仍完整覆盖时不影响同批其余片段；
27. 重放同一个渲染 checkpoint 时不重复渲染，也不重复发出交付事件。

### 11.3 D12 回归

- `web_search` 结果不能直接成为 ClaimEvidence；
- 未经 `web_fetch` 冻结的内容不能进入 Excerpt；
- Writer、Research Synthesis 和 Claim Attribution 上下文都不能包含 Document 全文；
- 所有最终事实角标必须落到具体 Document version 和 Excerpt。

### 11.4 质量评测

使用冻结研究快照做旧链和新链对照，但不把固定写法设为断言。观察：

- 报告是否实质回应核心问题；
- 是否形成有信息量、能够由材料说明的研究认识；
- 是否解释事实之间的关系，而不是只按时间或来源罗列；
- 检索片段的通过率和口径错误率；
- 检索片段数量随修订轮次的变化——第二稿比第一稿少了一批而篇幅未缩短，就是在往笼统漂；
- 是否仍大量复制提示词或修订模板措辞；
- 是否能自然生成多段、列表或表格；
- 读者能否从审计视图追到事实和判断依据。

评测不能要求每篇报告必须有单一答案、固定结论数量、表格、固定章节数或固定篇幅。这些属于研究问题和 Writer 的写作选择。

---

## 12. 方案边界

本方案保留 Prospector 最有价值的部分：首次抓取即冻结 Document、精确 Excerpt、Assertion 血缘、来源冲突、代码生成引用和可恢复运行。

它删除的是：

- Writer 在写作时逐句记账；
- 用固定候选和答案选择代替真实综合；
- 把模型判断固化成 Writer 必须服从的答案；
- 用逐句审批不断削弱判断；
- 用逐句覆盖度检查惩罚跨材料综合；
- 把程度与意义词当成待检索事实，给判断加一道词汇税；
- 把“非核心”留成一句无人执行、也无法由代码执行的判据；
- 让“把具体表述改成笼统表述”成为一条无人拦截的合法修订路径；
- 把检查规则写进 Writer 提示词，让每增加一道检查就收窄一分写作；
- 让失败引用看起来像已验证引用；
- 把一次模型格式错误变成整个 Job 的死因；
- 因为报告不合格就扣住不发，让用户既看不到内容也看不到问题；
- 用一句盖在整篇上的免责声明，代替逐句可定位的标记。

最终职责收敛为五句话：

1. Research Worker 取得并冻结材料；
2. Research Verifier 决定材料是否有证据资格；
3. Research Synthesis 把零散材料整理成连续、可追溯的分析；
4. Writer 自由地把分析和材料写成完整报告；
5. Claim Attribution、Whole-report Review 和确定性代码让事实可核对、判断可追溯、最终状态可信。

这套系统仍不能保证每篇报告都优秀。它能保证的是：系统不会因为自身合同，把模型推向最安全、最空泛、最像流水账的写法。
