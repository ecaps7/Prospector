# Prospector CLI 设计

- **版本**：v2.0（推倒 v1.3 重来）
- **日期**：2026-07-19
- **状态**：已实施
- **关联文档**：[系统设计](./design.md)、[M1 实现设计](./implementations/m1.md)

---

## 1. 定位与约束

产品 CLI 名为 `prospector`，是**单进程 API 服务端的瘦客户端**：只负责提交问题、Brief 本地交互、SSE 事件展示与报告下载，不在客户端实现 Planner、Worker 或质量门。研究图跑在服务端进程内。

设计约束（已确认的决策）：

| 决策点 | 结论 |
|--------|------|
| 架构姿态 | 先建服务端（FastAPI），CLI 为瘦客户端；一份设计覆盖两端 |
| 部署假设 | 本机单用户：服务端与 docker compose 跑在同一台机器，无认证、无多租户 |
| 通信方案 | SSE 推送事件 + REST 式 HITL（Brief 确认完全在客户端完成） |
| 进度展示 | Rich Live TUI；`--plain` 与非 TTY 降级为逐行时间线 |
| prospector-local | 保留，定位为不起服务端的调试与集成测试入口，行为不变 |

职责边界沿用系统设计：Scope 具体化问题并展开研究空间；用户确认的是研究输入快照；Planner 收敛实际范围并形成版本化 Plan；CLI 展示服务端事实，不自行推断状态、预算或结果。

---

## 2. 命令面

```text
prospector
├── [--effort quick|standard|deep] [--language zh|en|...] [--plain]
│                                  # 打开持久交互界面，循环输入研究问题
├── serve                        # 前台启动 API 服务端（uvicorn 单进程）
│     [--port 7620] [--init]    # --init: 首次运行时建 checkpointer 表 + MinIO bucket
├── job
│   ├── list                     # 各 job 的状态 / 阶段 / 开始时间
│   ├── status <job_id>          # 单个 job 快照：阶段、Plan 版本、任务表、用量
│   ├── cancel <job_id>          # 取消排队或运行中的 job
│   └── attach <job_id>          # 重新接上 TUI/时间线（SSE + Last-Event-ID 回放）
└── report
    ├── show <job_id>            # 终端渲染报告 Markdown
    └── export <job_id> [--format md|json] [-o <path>]
```

有意不做的（YAGNI）：

- **`login` / `config`**：本机单用户无认证；服务端地址用环境变量 `PROSPECTOR_SERVER`（默认 `http://127.0.0.1:7620`）。
- **`usage` 命令**：用量直接显示在 TUI 底栏与 `job status` 中。
- **`job events`**：与 `attach` 是同一条事件流的两种渲染，合并为 `attach`（TTY 下 TUI，`--plain` 下逐行）。
- **`report export --format html`**：渲染器只产 md/json，不承诺不存在的能力。

根交互界面必须运行在交互终端（Brief 确认不可跳过）；非 TTY 启动时报用法错误。

---

## 3. API 契约（服务端）

FastAPI 单进程。代码位于 `src/prospector/runtime/entrypoints/server.py` + 新增 `src/prospector/api/` 模块（路由、请求/响应 schema）。研究图在服务端后台任务中执行（asyncio 任务经线程池包装现有同步 `graph.invoke`）。**一次只允许一个 job 处于运行态**——单用户本机串行足够；第二个提交排队并在响应中说明。

```text
POST /api/scope                      # Scope 展开问题（同步，数十秒）
  body: { question, effort, language, clarification_question?, clarification_answer? }
  resp: { kind: "clarify", clarification_question }
      | { kind: "brief_pending", brief: {...} }

POST /api/scope/revise               # 对应 Brief 卡片的 [i]：一条修订指令改写一轮
  body: { question, previous_brief, revision_note, effort, language }
  resp: { brief }

POST /api/jobs                       # 冻结 Brief 并启动研究
  body: { brief }                # brief 由客户端提交；[e] 本地编辑后的结果也走这里
  resp: { job_id, brief_id }     # schema 校验失败返回 422

POST /api/jobs/{id}/cancel           # queued 立即取消；running 请求协作式停止
  body: { requested_via: "web_monitor" | "cli" }

GET  /api/jobs                       # job list
GET  /api/jobs/{id}                  # job status 快照
GET  /api/jobs/{id}/events           # SSE；支持 Last-Event-ID 从 PG 事件表回放
GET  /api/jobs/{id}/report?format=md|json   # 服务端从对象存储代理返回报告字节流
GET  /api/healthz                    # serve 启动自检 + CLI 连接探测
```

业务路由统一挂在 `/api` 下，不保留根路径别名。浏览器客户端应与 API 同源（开发时由前端代理 `/api` 到 `127.0.0.1:7620`）；服务端不开放 CORS。

关键决定：

- **HITL 完全在客户端**：服务端没有"等待确认中"的 job 状态。`/api/scope` 与 `/api/scope/revise` 是进程内直接调用 `run_scope` / `write_research_brief` 的纯函数式端点；c/e/i/q 循环全部发生在 CLI 本地，确认完才 `POST /api/jobs`。服务端因此不需要 interrupt/resume 的 HTTP 化——job 一旦存在就必然在跑或已停。
- **SSE 事件即 PG 事件表的行**：`id` 为事件表自增 ID，`data` 为结构化 JSON（事件类型 + 载荷）。渲染语义留给客户端（现有 `ResearchTimelineRenderer` 的逻辑移植到 CLI 侧复用）。job 停止后服务端发终结事件 `job.stopped`（含 outcome / phase / report refs）并关流，CLI 以此判断退出。
- **取消同样持久化**：queued Job 直接进入 `cancelled`；running Job 先进入 `cancelling`，事件记录 `requested_via`，在当前模型或工具调用结束后的安全边界停止；未完成 Task 一并进入 `cancelled`（`stop_reason=job_cancelled`），随后写入唯一 `job.stopped`，服务重启不会恢复。
- **报告下载走服务端代理**：CLI 不直连 MinIO；下载结果落地 `~/.prospector/reports/<job_id>/`。服务端产物是权威源，本地目录只是缓存。
- **错误契约**：LLM 未配置、Verifier 重大缺口等既有异常映射为结构化错误体 `{ error_code, message }`。请求校验失败额外带稳定的 `details: [{ path, reason }]`（字段路径 + 原因），供表单定位；`path` 为空表示请求级错误。CLI 按 `error_code` 决定退出码，不解析 message 文本。

---

## 4. 根命令交互流程

```text
prospector
  → 显示服务连接状态与当前 effort/language
  → prompt 用户输入研究问题
  → POST /api/scope（spinner："Scope 正在展开问题…"）
  → 若 clarify：打印澄清问题，prompt 用户回答一次，带答案重新 /api/scope
  → Brief 卡片（CLI 本地渲染）：
      [c] 确认      → POST /api/jobs
      [e] 编辑      → $EDITOR 打开 Brief YAML；schema 校验失败打印字段级错误并
                      重开编辑器（临时文件保留，不丢弃编辑内容），通过后回到卡片
      [i] 指令修订  → prompt 一条修订意向 → POST /api/scope/revise → 新 Brief 直接 POST /api/jobs
      [q] 放弃      → 退出，不创建 job
  → JOB_CREATED: <job_id>（始终打印，attach 中断也能找回）
  → 进入 attach
  → 完成、失败、放弃或离开 attach 后返回问题输入，继续下一次研究
```

`c/e/i/q` 语义沿用现有 `confirm_brief` 实现，仅将 revise 从进程内调用换为 HTTP。Brief 冻结后不可修改，需求变化必须创建新任务。

---

## 5. TUI

Rich Live 实现；`attach` 与根交互研究流程共用同一渲染组件。

```text
╭─ Prospector ─────────────────────────────────────────────────────────────────╮
│  ◉ 研究中   a1b2c3d4   评估 2025–2026 年亚太区竞品经营态势        deep · zh  │
╰──────────────────────────────────────────────────────────────────────────────╯

  Brief ─── 规划 ─── 搜集 ─── 验证 ─── 成文 ─── 句级验证 ─── 渲染
   ✔        ✔       ◉

╭─ Plan v2 ────────────────────────────╮ ╭─ 限额与用量 ──────────────────────╮
│ Replan：商业化证据缺口                │ │ planner   ▰▰▰▱▱▱▱▱▱▱▱▱  3/12     │
│                                      │ │ 并发      ▰▰▱             2/3     │
│ t_01 区域收入与客户证据    factual ✔ │ │ tokens    0.41M                   │
│ t_02 技术路线与产品差异 comparison ◉ │ │ 工具调用  38                      │
│      └ worker rounds ▰▰▰▱▱▱  7/49    │ │ 已运行    12:41                   │
│ t_03 商业化数据反方证据  counter…  ○ │ ╰───────────────────────────────────╯
╰──────────────────────────────────────╯

╭─ 时间线 ─────────────────────────────────────────────────────────────────────╮
│ 11:42:03  t_01  保存 2 excerpts · 3 assertions                               │
│ 11:43:05  verifier  ⚠ 检出重大缺口 → 请求 Planner 决策                       │
│ 11:43:40  plan  Plan v2 派发 t_03                                            │
│ 11:44:12  t_02  web_search "APAC revenue breakdown …"                        │
╰──────────────────────────────────────────────────────────────────────────────╯
  Ctrl-C 离开（任务继续）   x 终止 Job                       prospector · v0.1.0
```

视觉规范：

- **顶部状态胶囊**：状态点 + job 短 ID + 问题 + effort/语言；状态点带 spinner 呼吸动画——研究中青色 ◉、完成绿 ✔、失败红 ✘、SSE 重连中黄色。
- **阶段轨道**：带连接线的站点图，当前站高亮挂 spinner；站点对应主图真实 phase（含成文与句级验证）。
- **双栏中段**：左侧 Plan 面板用缩进列表，当前运行任务内嵌 worker rounds 迷你进度条；右侧限额面板统一 `▰▱` 条形图。窄终端（<100 列）双栏降为上下堆叠。
- **时间线语义着色**：evidence 常规色、verifier 缺口黄、replan 品红、工具调用暗灰（视觉退后）、时间戳暗色；滚动保留最后 8 条。
- **底部状态栏**：左侧展示离开与终止快捷键、取消请求状态，右侧展示版本号。
- 配色只用 Rich 默认 256 色安全集（cyan/green/yellow/magenta/dim），不依赖真彩终端。

架构要求：**TUI 是事件流的纯投影**。CLI 内部维护由事件折叠出的 `JobView` 状态（阶段、Plan、任务表、计数），Live 面板只渲染该结构；`--plain` 模式消费同一条流逐行打印。CLI 不根据事件文本重新计算状态。

**Ctrl-C 语义**：attach 期间 Ctrl-C 只断开展示，研究在服务端继续，打印
`已离开，任务继续运行：prospector job attach <id>`。这与 prospector-local（Ctrl-C 即杀研究）是刻意的行为差异。

**x 语义**：attach 期间按 `x` 请求服务端取消 Job。排队中的 Job 立即取消；运行中的 Job
在当前模型或工具调用结束后的安全边界停止。TUI 不立即退出，而是继续消费 SSE，直到收到
`job.stopped(status=cancelled)`。

**终态**：收到 `job.stopped` 后退出 Live，打印终态摘要卡片；成功时自动下载一次报告并打印本地路径，不自动倾倒全文：

```text
╭─ ✔ 研究完成 · 22 分 14 秒 ───────────────────────────────╮
│  报告    ~/.prospector/reports/a1b2c3d4/report.md        │
│  引用    96 条    信息局限 3 处（已在报告中披露）         │
│  查看    prospector report show a1b2c3d4                 │
╰──────────────────────────────────────────────────────────╯
```

---

## 6. 错误处理与退出码

| 码 | 含义 |
|----|------|
| 0 | 成功完成；或用户主动离开 attach（任务仍在跑）；或 `q` 放弃 Brief |
| 1 | 平台错误：服务端连不上、LLM 未配置、服务端 5xx |
| 2 | 用法错误：参数非法、非 TTY 启动交互界面、job_id 不存在 |
| 3 | 研究质量失败：Verifier 重大缺口等；partial 产物已保存 |
| 130 | Ctrl-C 在 Brief 冻结前中断（未创建 job） |

关键错误场景：

- **服务端未启动**：所有命令先探测 `/api/healthz`，失败时给可执行提示（`服务端未运行，先执行: prospector serve`），不吐 connection traceback。
- **SSE 断线**：attach 自动重连（指数退避，上限 30s），带 `Last-Event-ID` 续传；TUI 状态点变黄提示重连中。重连期间研究不受影响（事实源在 PG）。
- **Worker 轮数**：每轮模型决策完成后写入 `task.round_advanced`，TUI 直接投影 `rounds_used/rounds_limit`，不使用工具调用数推算。
- **交互中断恢复**：job_id 创建时立刻打印；任何后续失败（含 CLI 崩溃）都可 `job attach` 找回。

---

## 7. 测试策略

- **单测**：Brief 卡片状态机（c/e/i/q）、事件折叠 `JobView` 的逻辑、SSE 客户端重连/续传（mock 流）、退出码映射；TUI 渲染用 Rich console capture 做快照测试。
- **集成**：FastAPI `TestClient` 覆盖 `/api/scope → /api/jobs → /api/jobs/{id}/events` 全链路（LLM mock）；一条 `live` 标记的端到端用真实 LLM 走通根交互流程与 `attach`。
- 现有 `prospector-local` 测试不动，保证开发入口回归安全。

---

## 8. 实施分期

各期均已交付：

1. [x] `api/` 模块 + `serve`：/api/scope、/api/jobs、SSE、报告代理。
2. [x] `prospector` 持久交互 CLI + `--plain` 全流程。
3. [x] Rich TUI（`JobView` 投影 + Live 面板）。
4. [x] `job list/status`、`report show/export`、错误与真实 usage 闭环。
