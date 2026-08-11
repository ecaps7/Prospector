# 运行实例

两次真实运行的完整产物，未经任何人工编辑。

## 三分钟看什么

1. `verified-neijuan-semantic-shift/report.md` —— 看 `[^N]` 角标和「来源」节。这些不是模型写
   的，是 [citation_render.py](../src/prospector/deterministic/citation_render.py) 按源首次出
   现顺序渲染的。
2. `partial-social-media-teen-mental-health/report.json` 的 `failed_statement_ids` —— 七条未
   通过逐句验证的句子。回到同目录 `report.md` 里找它们：**原文保留，但不带引用角标**。系统
   能精确说出哪一句它扛不住。
3. 跑一下审计脚本，别信上面两条：

```bash
python3 examples/verify_lineage.py examples/verified-neijuan-semantic-shift/
```

## 两次运行

| 目录 | 问题 | 终态 |
|------|------|------|
| `verified-neijuan-semantic-shift/` | "内卷"从人类学术语到网络流行语，含义发生了哪些关键转变？ | `verified` |
| `partial-social-media-teen-mental-health/` | 社交媒体导致青少年心理健康恶化的因果证据到底有多强？ | `partial` |

两次都是 `standard` 档、中文、无用户约束。

| | verified（内卷） | partial（社交媒体） |
|---|---|---|
| 规划 | Plan v4，11 个任务 | Plan v10，11 轮决策，26 个任务 |
| 证据 | 39 条断言，32 个来源 | 182 条断言，54 个来源 |
| 报告 | 69 句 | 123 句 |
| 逐句验证失败数 | 17 → 3 → **0** | 29 → 17 → **7** |
| 输入 token | 581,060 | 3,519,039 |
| 工具调用 | 119 | 848 |

**对照着看这两行才有意思。** 两次的失败残留都集中在 `derived`（推理层）——事实层的
`evidence` 句在两轮修订内都被清零了。区别在于第一次能收敛到零，第二次剩下 7 条。

差别不在系统，在题目。"某个词的含义怎么变的"有可考的文献与用例；"社交媒体是否导致心理健康
恶化"存在学界真实分歧，推理层注定有扛不住的地方。同一套机制，在有答案的题上收敛，在没答案
的题上诚实止步——后者被标成 `partial`，那 7 句保留原文但不附已验证引用。

成本也随之分化：58 万 vs 352 万输入 token，相差 6 倍。Planner 在证据快速收敛时会早停，不会
把预算跑满。

各自的细节见两个目录里的 `question.md`。

## 每个文件

| 文件 | 生成命令 | 独自证明什么 |
|------|----------|--------------|
| `question.md` | 手写 | 原始问题、档位、冻结的 Brief 全文、这次跑出了什么 |
| `report.md` | `prospector report export --format md` | 引用角标与来源节由确定性代码渲染 |
| `report.json` | `prospector report export --format json` | `verification_status`、`failed_statement_ids`、`citation_excerpt_ids`（statement → Excerpt UUID）、`sources[].excerpt_ids` + `document_version` |
| `timeline.txt` | `prospector-local job events` | 过程：Planner 决策与驳回、Replan、任务并行、工具调用、Verifier 完成 |
| `status.txt` | `prospector job status` | 每组件 token 用量、每任务工具调用数、Plan 版本 |

## 审计脚本

`verify_lineage.py` 只读 `report.json` 和 `report.md`，检查七条不变量：

1. **引用编号闭合** —— 正文每个编号都能在 `sources` 找到条目；
2. **Excerpt 归属唯一** —— 每个被引 Excerpt 恰好归属一个来源；
3. **源快照可定位** —— 每个来源都带 `source_uri` + `document_version`，不是裸 URL；
4. **已验证引用不超出候选** —— 渲染出的引用是 Writer 当初提出的候选的子集；
5. **未通过句不带引用** —— `failed_statement_ids` 里的句子引用列表必须为空，且
   `verification_status` 与之自洽；
6. **分型与前提深度** —— `evidence` 挂 Excerpt、`derived` 挂前提且前提承载证据、推理链深度
   不超过 2；
7. **Markdown 与 JSON 一致** —— 正文角标集合与 `statement_citations` 完全相等。

两份产物都是七条全过。第 7 条同时是它们未经修饰的自证：任何手工润色 Markdown 的行为都会被
它抓出来。

脚本**不 import prospector 的任何模块**，只用标准库——审计不应依赖被审计的代码。裸 `python3`
就能跑，不需要装依赖、数据库或 API key。

这七条对应 [docs/future/eval.md](../docs/future/eval.md) §3.1 的「A 层确定性不变量」。评测设计
的 B/C/D 层仍是草案，A 层已经是可执行代码。

## 已知局限

**`partial` 那次不是一次跑通的。** 它的 `timeline.txt` 里看得到中断痕迹。研究阶段结束后，它
先后撞上三个契约 bug 才走到成文：Research Verifier 把同一 Excerpt 内的年份矛盾误判成来源冲
突、Report Writer 修订时把根据挂到了后文的句子上、Report Verifier 的逐句契约违约没有重试通
道。三者形状相同——增量校验有重试、整体校验没有——修复分别见 `research_verifier.py`、
`report_writer.py`、`report_verifier.py` 的对应提交。产物本身是修复后正常流程的输出，没有任
何人工干预内容。`verified` 那次是在修复之后跑的，一次到底。

**澄清问答和 HITL 确认过程没有留痕。** 系统只冻结最终 Brief，不持久化到达它的过程。所以
`question.md` 里那两节只能写"没有记录"。对一个主打可审计的系统来说，人在环里做了什么恰恰
是该留档的。

**七条未通过句里，我认为至少有一条验证器判错了。** `s_099` 说三种函数形态（倒U型、线性、阈
值）并不完全互斥——若拐点出现在较高使用量处，多数样本观察到的就是近似线性的区间。这是一个
**逻辑观察**，不是经验主张，本不需要"直接证据"。验证器以"缺乏直接证据支持这种整合"判它
`overreach`，等于对纯推理施加了经验证据的要求。这暴露出当前 Report Verifier 对
`derived` 句缺少"分析性 vs 经验性"的区分。

其余六条我认为判得对，其中 `s_112` 尤其准：它在一条 `derived` 句里写入了"下降约 0.09 个标准
差"这个具体数字，而带数字的事实陈述应该是 `evidence` 并挂 excerpt，写成推理句就丢了出处。

**`verified` 那次的时间线里有一条 `web_fetch 失败：UniqueViolation`。** 那不是抓取失败——网页
取到了，但两个 Worker 并发抓同一个 URL，第二次入库撞上唯一约束，于是一份已经拿到的文档被当
成失败丢掉了。落库现在是幂等的，这条记录留着是缺陷存在时的真实痕迹。

**两次都是中文、`standard` 档、无用户约束。** 还没有验证过跨语言（中文问题 + 英文证据时
Excerpt 是否保持原文入库）、其他档位，以及用户施加范围约束时 Planner 的表现。

## 如何复现

需要 `.env`（数据库、对象存储、LLM、Exa）与 `docker compose up -d`。先起服务：

```bash
uv run --env-file .env prospector serve
```

另开一个终端进入交互控制台，输入问题，走完澄清与 Brief 确认：

```bash
uv run --env-file .env prospector --effort standard --language zh
```

记下终端打印的 `job_id`，一条命令收齐四份产物：

```bash
python3 examples/collect_run.py <job-id> examples/<run-directory>/
```

`collect_run.py` 只是那些 CLI 命令的包装——产物内容与手工执行完全一致，它负责落盘位置、覆盖
保护（已有产物需 `--force`）、渲染宽度，以及收尾自动跑一遍 `verify_lineage.py`。等价于：

```text
prospector job status <job-id>                      → status.txt
prospector-local job events <job-id>                → timeline.txt
prospector report export <job-id> --format md       → report.md
prospector report export <job-id> --format json     → report.json
```

其中 `job status` 需要 `COLUMNS=200`：它用的是默认 `Console()`，重定向到文件时 Rich 会退回
80 列并截断任务表格。脚本已代为设置。

同一个问题重跑不会得到同一份报告——检索结果和模型输出都不确定。这里的产物是某一次具体运行
的记录，不是可复现的基准。
