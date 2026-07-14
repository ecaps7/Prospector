# Prospector CLI 设计文档

- **版本**：v1.1（草案）
- **日期**：2026-07-14
- **状态**：待评审
- **关联文档**：`docs/design.md`（系统设计 v4.9，下文引用其章节号）

### v1.1 变更

- Brief 确认交互改为 `c` 原样确认 / `e` 直接编辑 / `i` 指令修订（一轮定稿）/ `q` 放弃；废除 `r` 重新收敛多轮环。
- 确认卡片按 design v4.8+：展示 `question` + `brief_text` + 轻量元数据，不再使用 `must_cover`；确认语义为输入快照而非执行合同。

---

## 1. 定位与原则

`prospector` 是系统的命令行客户端。四条原则：

1. **纯瘦客户端**。CLI 不包含任何编排逻辑，所有状态在服务端（design §13）；CLI 只调用 API 服务的 HTTP 接口与 SSE 进度流。开发期通过 `docker compose up` 启动本地服务，CLI 指向 `localhost`，**不提供** `--local` 内嵌模式。
2. **任务生命周期与 CLI 进程解耦**（design §13.1）。提交后 CLI 退出、断网、换机器都不影响任务；`job attach` 凭 Last-Event-ID 回放后续传实时流。CLI 的角色是"提交 + 附加"，类似 `docker logs -f`，而非前台阻塞进程。
3. **Brief 确认不可跳过**（design D4 / FR-2）。CLI 不提供 `--yes`、`--brief-file` 等任何绕过交互确认的途径；`ask` 必须在交互终端（TTY）中运行。这是产品立场而非服务端能力边界：服务端存在 brief-direct 提交模式（design §5.1，调用方直接提交完整 Brief、即时冻结，无特权语义），供离线评测（eval §4.3）与程序化集成使用；CLI 面向人类用户，刻意只暴露 interactive 模式——Scope 收敛环节对人有真实价值。
4. **进度默认 live TUI，`--plain` 降级**。stdout 非 TTY（管道、重定向、CI）时自动降级为 plain 模式。

## 2. 配置与认证

```text
~/.prospector/
├── config.toml          # api_url、默认 effort、默认知识库等
├── credentials          # API token（0600 权限）
└── reports/<job_id>/    # 完成任务的本地产物（见 §7）
```

- `prospector login` 交互式输入 API 地址与 token，写入上述文件。
- 环境变量 `PROSPECTOR_API_URL` / `PROSPECTOR_API_TOKEN` 优先于配置文件（便于多环境切换）。
- 所有请求携带 token；服务端据此完成多租户隔离与 per-user 计量（design FR-12）。

## 3. 命令总览

```text
prospector
├── ask <question> [flags]        # 主入口：提交 → 澄清 → Brief 确认 → attach
├── job
│   ├── list [--all|--running|--failed]
│   ├── status <job_id>
│   ├── attach <job_id>
│   ├── pause <job_id>
│   ├── resume <job_id>
│   ├── cancel <job_id>
│   └── events <job_id> [--since <event_id>] [--follow]
├── report
│   ├── show <job_id>
│   ├── export <job_id> --format md|json|html [-o <path>]
│   └── gaps <job_id>
├── followup <job_id> <question> [flags]
├── kb
│   ├── list
│   ├── add <files...> --kb <name>
│   ├── docs <kb-name>
│   └── ingest-status [--kb <name>]
├── usage [--job <id> | --month <yyyy-mm>]
├── debug on <job_id> --ttl <duration> | debug off <job_id>
├── login
└── config [get|set <key> [value]]
```

不设短别名，命令全称 `prospector`。

## 4. 核心流程：`prospector ask`

### 4.1 参数

| 参数 | 说明 |
|------|------|
| `<question>` | 自然语言研究问题（必填） |
| `--file <path>` | 可重复；附带 PDF/表格，提交时上传入库（FR-1，快照 + PageIndex 建树） |
| `--kb <name>` | 指定本次研究可访问的私有知识库 |
| `--seed <doc_id>` | 可重复；`seed_document_refs`，仅优先提示，不缩小检索范围（design §3.4） |
| `--effort <quick|standard|deep>` | 研究档位（默认 `standard`）。决定每 worker 工具次数、Plan 最大 task 数、最大 replan 轮数（quick/standard=1，deep=2）。不设 Job 总工具/时长上限；token 只计量。选 `deep` 时须提示可能跑数小时（m2 §6.1） |
| `--detach` | Brief 确认完成后立即退出，不进入进度视图 |
| `--plain` | 强制 plain 进度输出 |

### 4.2 流程

```text
提交问题（含附件上传）
→ Scope Agent 澄清（最多 1 轮反问，design §5.1；终端内问答）
→ Brief 卡片呈现
→ 用户原样确认 / 直接编辑 / 单轮指令修订 / 放弃
→ 定稿后 Brief 作为输入快照，任务开始
→ 默认自动 attach 进度流（--detach 则打印 job_id 后退出）
```

`ask` 在非 TTY 环境直接报错退出（exit 2），提示 Brief 确认需要交互终端。

### 4.3 Brief 确认卡片

```text
┌─ Research Brief（确认后作为本次任务输入快照）─────────────────┐
│ question       评估竞品 2025–2026 年在亚太区的经营态势          │
│ effort         standard                                         │
│ language       zh                                               │
│ output_format  report_with_citations                            │
│                                                                 │
│ brief_text:                                                     │
│   为区域投入决策提供依据，具体研究……竞争解释与反例路径……      │
└─────────────────────────────────────────────────────────────────┘
  [c] 确认  [e] 直接编辑  [i] 指令修订（一轮定稿）  [q] 放弃
```

若档位为 `deep`，卡片下方须追加一行预期提示（非阻断）：「深度档最坏可能运行数小时；单次模型/抓取调用仍有超时上限。」依据 m2 §6.1 最坏时长上界。

- **`c` 确认**：接受当前 Brief 并定稿（design §5.1），任务进入规划阶段。
- **`e` 直接编辑**：将 Brief 以 YAML 形式打开 `$EDITOR`；保存退出后经 schema 校验，校验失败显示错误并重新打开编辑器；通过后回到卡片，可再次确认、再编辑或改走 `i`。
- **`i` 指令修订**：输入一条修改意向，交回 Scope Agent **改写恰好一轮**；改完即定稿，**不再二次确认**，也不允许多轮模型修订（不占用澄清反问轮次）。
- **`q` 放弃**：任务终止，不产生任何计费之外的痕迹。

中途改需求 = 新任务（design §5.1）；确认后 CLI 不再提供修改 Brief 的入口，只能 `cancel` 后重新 `ask` 或用 `followup` 续研。

## 5. 进度界面

### 5.1 Live TUI（默认）

数据源为 SSE 业务事件流（design §9.1"业务事件"通道）。界面自上而下四个区块：

```text
 job_20260712_042  评估 2024–2026 年国内 AI Agent 中间件…        [运行中]
 阶段  澄清 ✔ → 规划 ✔ → 搜集 ● → 验证 ○ → 声明 ○ → 成文 ○
 计划  Plan v2（6 任务，v1→v2：Verifier 补搜）
 ┌────┬────────────────────────────┬──────────────┬─────────┐
 │ ID │ 任务                        │ mode         │ 状态    │
 ├────┼────────────────────────────┼──────────────┼─────────┤
 │t_01│ 主要厂商技术路线梳理        │ comparison   │ ✔ done  │
 │t_02│ 竞品融资与商业化进展        │ factual      │ ✔ done  │
 │t_03│ 私有库行业报告精读          │ factual      │ ● 运行中│
 │t_04│ 技术路线反方观点            │ counterargu… │ ○ 等待  │
 │t_05│ 市场规模数据核算（沙箱计算）│ —(data)      │ ○ 等待  │
 │t_06│ 补搜：竞品 B/C 商业化数据   │ factual      │ ● 运行中│
 └────┴────────────────────────────┴──────────────┴─────────┘
 限额  task tools ≤10/worker · plan ≤4 tasks · replan ≤1
       tokens 0.41M · tool_calls 38（仅计量）
 事件  11:42:03  t_03 kb_read doc_889 p.14 → 落证 2 条 Excerpt
       11:43:05  Verifier 检出缺口：商业化进展覆盖不足 → Plan v2
       11:43:40  t_06 开始执行
─────────────────────────────────────────────────────────────────
 [q] 离开（任务继续）  [p] 暂停/恢复  [c] 取消  [o] 完成后打开报告
```

区块与 design.md 实体的对应关系：

| 区块 | 对应 |
|------|------|
| 阶段流 | §3.3 的六阶段流水线 |
| 计划与任务表 | Research Plan 版本 + ResearchTask（含 `research_mode`）；Plan 版本号变化让 replan 显式可见（§5.3） |
| 限额条 | 展示三项硬闸；token / 累计工具次数只展示，不驱动打断 |
| 事件尾 | 业务事件的人类可读渲染，保留最近 N 条 |

声明与成文阶段的任务表切换为 claim 计数视图（起草 / 验证通过 / 驳回回炉），并展示 no-new-facts 审计的检出与补录（§5.4）。

按键语义：

- `q`：离开视图，任务继续（打印重连命令提示）。
- `p`：暂停/恢复切换（等价 `job pause` / `job resume`）。
- `c`：取消，需二次确认（destructive）。
- `o`：任务完成后打开本地报告文件。

### 5.2 Plain 模式（`--plain` 或非 TTY 自动降级）

每条业务事件一行，`HH:MM:SS  <event>  <人类可读描述>`：

```text
11:40:12  job.started        job_20260712_042 Plan v1（5 任务）
11:42:03  evidence.persisted t_03 kb_read doc_889 p.14 → 2 excerpts
11:43:05  plan.replanned     v1→v2 缺口：商业化进展覆盖不足（+t_06）
11:55:47  plan.replanned     replan_round 达上限 → 进入质量门
12:02:31  job.completed      1.61M tokens · $4.87 · 报告已写入本地
```

需要机器可解析格式的场景使用 `job events --follow`（输出服务端原始事件的 NDJSON），plain 模式只保证人类可读。

### 5.3 终态输出

attach 视图（TUI 与 plain 一致）在任务到达终态时打印摘要并以对应退出码退出：

```text
✔ 完成（22 分 14 秒 · 1.61M tokens · $4.87）
  报告：~/.prospector/reports/job_20260712_042/report.md（96 条引用，3 处标注信息局限）
```

失败出口（§7）：

```text
✘ 失败：补搜轮次用尽后仍存在 required 缺口（商业化进展：3 家竞品无独立第二来源）
  已保存 partial report 与 gap artifact → ~/.prospector/reports/job_20260712_042/
  续研：prospector followup job_20260712_042 "<补充方向>"
```

## 6. 任务管理：`prospector job`

| 命令 | 行为 |
|------|------|
| `list` | 当前用户任务列表：job_id、问题摘要、状态、阶段、开始时间、成本。默认显示近 20 条，`--running` / `--failed` / `--all` 过滤 |
| `status <id>` | 单次快照：阶段、Plan 版本、任务表、effort / 用量计数、最近事件（即 TUI 的静态一帧） |
| `attach <id>` | 连接进度流。凭本地记录的 Last-Event-ID 先回放缺失事件（Redis Stream 内直接回放，超窗从 PG 事件表补，design §13.4），再转实时。已终态的 job 直接打印终态摘要 |
| `pause <id>` | 暂停：服务端停止派发新任务，在跑的 execution attempt 完成或落 checkpoint 后挂起（FR-9）。TUI 中阶段行显示 `[已暂停]` |
| `resume <id>` | 从 checkpoint 恢复续跑，已完成子任务不重跑（NFR-3） |
| `cancel <id>` | 取消（不可逆，需确认；`--force` 跳过确认供脚本使用）。已产生的 Evidence Store 保留，可被 followup 复用 |
| `events <id>` | 原始业务事件（NDJSON），`--since` 指定起始 event_id，`--follow` 持续跟踪。供调试与脚本消费 |

暂停/恢复是服务端语义的直接映射，CLI 不做本地状态推断；暂停中的任务在 `list` 中标记为 `paused`，不占用 per-user 并发额度（design §13.4 准入控制只 count running）。

## 7. 报告与产物

### 7.1 本地产物目录

任务到达终态时，**处于 attach 状态的 CLI 自动拉取产物**写入本地：

```text
~/.prospector/reports/<job_id>/
├── report.md        # 主报告（引用角标 + 文末来源列表，确定性渲染产物）
├── assets/          # 图表 SVG（FigureSpec 确定性渲染产物，design §5.5）
├── report.json      # 结构化输出（FR-7：claim 集、引用链、FigureSpec 与解析后数据）
├── meta.json        # brief、最终 plan 版本、预算消耗、终态与出口原因
└── gaps.json        # 仅失败/降级时存在：结构化 gap artifact（§5.3 / §7）
```

终态时无 CLI 在线（`--detach` 或断线）的情况下，下次对该 job 执行 `attach` / `report show` / `report export` 时自动补拉。服务端永远是产物权威源，本地目录是缓存；`report show` 发现本地缺失或哈希不符时重新拉取。

### 7.2 命令

| 命令 | 行为 |
|------|------|
| `show <id>` | 终端渲染 report.md（角标、表格）；`--pager` 控制分页 |
| `export <id> --format md\|json\|html [-o]` | 导出到指定路径；html 为服务端渲染的自包含单文件 |
| `gaps <id>` | 渲染 gap artifact：每条 gap 的覆盖项优先级、缺什么、试过什么、为什么没拿到（§5.3），并提示 followup 用法 |

## 8. 续研：`prospector followup`

```text
prospector followup <job_id> "<新问题或补充方向>" [--file ...] [--effort ...]
```

FR-11 的入口：以指定 job 的 Evidence Store 与 gap artifact 为起点创建**新任务**（新 job_id）。流程与 `ask` 完全一致——同样经过 Scope 澄清与 Brief 确认（Brief 卡片额外显示"续研自 job_xxx，复用其证据库"）。原 job 及其产物不被修改。

## 9. 知识库管理：`prospector kb`

对应 design §3.4 的控制面入库。

| 命令 | 行为 |
|------|------|
| `list` | 当前用户可访问的知识库：名称、文档数、总页数 |
| `add <files...> --kb <name>` | 上传文档：写不可变 Document 快照 → 构建 PageIndex 树。命令在上传完成后即返回，建树异步进行；输出各文件的 doc_id。重复上传同内容（content_hash 相同）幂等提示"已存在" |
| `docs <kb>` | 列出库内 Document：doc_id、标题、description、版本、页数、建树状态 |
| `ingest-status` | 未完成建树的文档队列与进度。`ask --kb` 引用了含未建树文档的库时给出警告（该文档暂不可被 kb_read 检索） |

## 10. 用量与诊断

- `prospector usage`：按月汇总当前用户的 token / 工具调用 / 成本（读服务端 usage 表，权威计量，design §9.1）；`--job <id>` 显示单任务分阶段消耗。
- `prospector debug on <job_id> --ttl 30m`：设置 design §9.1.5 的限时 debug 开关（Redis TTL key）；`--ttl` 必填，上限由服务端约束。`debug off` 提前关闭。输出提示：完整负载写入对象存储诊断前缀，经 API 鉴权访问。

## 11. 退出码

`ask`（不带 `--detach`）与 `job attach` 跟踪到终态时，退出码反映任务出口（与 design §7 出口表对齐）：

| 退出码 | 含义 |
|--------|------|
| 0 | 任务完成（含"仅 optional gap、显式声明信息局限"的完成） |
| 1 | CLI 自身错误（网络、服务端 5xx、认证失败） |
| 2 | 用法错误（参数非法、非 TTY 下运行 ask） |
| 3 | 任务失败：required gap（partial report 与 gap artifact 已保存） |
| 4 | 任务被用户取消 |
| 5 | 任务失败：存在未通过 Claim 验证的事实声明 |
| 130 | 用户在 Brief 确认前中断（Ctrl-C），任务未创建 |

attach 中按 `q` 或 Ctrl-C 离开运行中的任务：退出码 0（离开不是失败），并打印重连提示。

## 12. 与服务端接口的对应

CLI 是 API 的忠实映射，不引入服务端不存在的语义：

| CLI | 服务端 |
|-----|--------|
| `ask` 提交 | `POST /jobs`（写 task 行即返回，design §13.4 outbox） |
| 澄清问答 / Brief 确认与编辑 | Scope 交互与 Brief 确认接口（HITL 状态机） |
| `job attach` | `GET /jobs/{id}/events`（SSE，带 Last-Event-ID） |
| `job pause/resume/cancel` | 对应生命周期接口 |
| `report *` | 产物下载接口 |
| `kb add` | 上传 + 入库接口（快照 + 建树） |
| `usage` | usage 表查询接口 |
| `debug on/off` | debug flag 接口 |

任意 API 副本可服务任意任务的进度流（design §13.1），CLI 不感知副本。

## 13. 实现说明与开放问题

- **技术栈建议**：Python + Typer（命令解析）+ Rich/Textual（TUI）——与服务端同语言，SSE 客户端与 schema 可复用服务端定义。
- **TUI 的事件驱动渲染**：所有区块状态由业务事件流推导，CLI 不轮询 status 接口；重连回放天然重建界面状态。
- **开放问题 1**：`report show` 的终端 markdown 渲染对复杂表格/图表的保真度有限，html 导出是保真兜底；是否需要 `--web` 直接打开服务端渲染页，待前端（M3 评估看板/可视化）确定后决定。
- **开放问题 2**：多语言输出（Brief 的 `language` 字段）不影响 CLI 结构，界面文案首版仅中文。
