# Prospector 开工确认清单（Preflight）

- **版本**：1.1
- **日期**：2026-07-12
- **状态**：已确认（实现前锁定；v1.1 修订搜索与抓取选型，见 §4.2 与确认记录第 11 轮）
- **依据**：[路线图](./roadmap.md)、[设计文档](../design.md)、[CLI 文档](../cli.md)、[评测文档](../eval.md)
- **用途**：记录实现开始前已拍板的工程与供应商选择。设计 ADR（D1–D10）与里程碑顺序不在此重复；本文件只补「设计写了品类、实现必须钉死实例」的部分。

变更本文件中的锁定项时，应同步评估对 M0 基座与既有 schema 的影响。

---

## 1. 工程基座

| 项 | 确认 |
|----|------|
| Python | **3.13**（`requires-python = ">=3.13"`） |
| 包管理 | **uv**（锁文件纳入版本控制） |
| 布局 | **`src/prospector/`** 单仓库单包；目录树以路线图 §3 为准 |
| 包边界 | API / Orchestrator / Worker / CLI **同包**；M4 只拆进程入口，不拆包（触发条件见路线图 §3.3） |
| 评测代码 | `eval/` 置于 `src/` 之外（裁判与选手分离） |
| 迁移 | **Alembic**；目录 `migrations/` |
| DB schema 隔离 | 同库分 schema：业务 **`app`**，LangGraph checkpointer **`langgraph`** |
| 领域合同 | **Pydantic v2**；全部落在 `src/prospector/schemas/`，零业务逻辑 |
| 主键 | **UUID** |
| 内容哈希 | **SHA-256**（Document / Excerpt 去重） |
| 时间 | 库内与 API 一律 **UTC**，序列化为 ISO-8601 |

与路线图 §3 一致：`schemas/` → `store/` → `agents/` / `deterministic/` / `tools/` → `flow/` → `runtime/`；`deterministic/` 禁止 LLM 调用；`flow/` 不导入 `runtime/`。

---

## 2. 代码质量与 CI

| 项 | 确认 |
|----|------|
| Formatter / Linter | **ruff**（format + lint） |
| 类型检查 | **basedpyright** |
| 测试 | **pytest**（含 asyncio 支持按需配置） |
| CI | **GitHub Actions** |
| CI 门禁 | 强制：`ruff check` + `ruff format --check` + basedpyright + **unit** + **compose integration** |
| Integration 范围 | 真依赖：PostgreSQL、MinIO、LangGraph PG checkpointer。**M0**：无外部 LLM/搜索 API 调用点。**M1 起**：里程碑验收集成测试**允许**真实 LLM/搜索 API（`@pytest.mark.live`）；默认 CI 跑 `integration and not live`，缺密钥时 live **skip** 而非 fail（详见 [m1.md](./m1.md) §11） |
| Unit vs Integration | Unit：纯逻辑（schema、哈希、预算水位、权威链检查等）；Integration：跨模块行为与里程碑验收（如 checkpoint 杀进程续跑；M1+ 含 live E2E） |

---

## 3. 本地依赖与观测（按里程碑裁剪）

### 3.1 M0 `docker compose` 实际启动

| 服务 | M0 | 说明 |
|------|----|------|
| PostgreSQL | ✅ | 事实库 + checkpointer |
| MinIO（S3 兼容） | ✅ | Document 快照等对象存储 |
| OTel Collector | 可选 | 默认可只用进程内 **console exporter** |
| Tempo / Loki / Grafana | ❌ | 生产目标架构（设计 §10）；非 M0 必需。Tempo↔Loki 跳转等在 **M4** 验收 |
| Redis | ❌ | **M4** 再装（SSE Stream / 预算计数 / debug flag） |
| RabbitMQ | ❌ | **M4** 再装（三队列） |

### 3.2 观测硬约束（能力必须，后端可后接）

- 应用侧 M0 起接入：**OpenTelemetry SDK** + **structlog** JSON 日志，自动注入 `trace_id` / `span_id` / `job_id` 等。
- M0 验收：日志可关联 trace/span；不要求完整 Grafana 看板。
- 观测故障不得阻断业务提交、checkpoint 或（日后）MQ ACK；成本权威在 PG usage 表。
- LangSmith：可选，仅经 Collector 复制 OTLP，应用内不引入第二套 tracing SDK。

---

## 4. 外部 API 与模型

### 4.1 LLM

| 项 | 确认 |
|----|------|
| 协议 | **OpenAI-compatible** HTTP API |
| 客户端 | **OpenAI 官方 Python SDK**，`base_url` 指向兼容 endpoint |
| 最强档 | **`qwen3.7-max`**（Planner / Verifier 放行 / Narrative Composer 等） |
| 中档 | **`qwen3.7-plus`**（Scope / Worker / Claim 起草与验证等） |
| 配置 | **仅环境变量**（见 §7） |

档位与阶段的对应关系见设计文档 §3.3；引用渲染与 FigureSpec 填数走确定性代码，不调用 LLM。

### 4.2 搜索与网页

| 项 | 确认 |
|----|------|
| 搜索与内容检索 | **Exa**（search 发现 URL；`/contents` 取整页 `text`）。原为 Tavily + 自建 HTTP + trafilatura，见 [m1.md](./m1.md) v1.2 |
| 正文 | **必须**取整页文本并写入 Document 快照（权威链需要完整原文，不只靠搜索 snippet）；由 `/contents` 的 `text` 承担 |
| 摘录 | Worker 对快照**按段号选取**（`find_segments` / `read_segments` / `select_excerpts`）；不再用 Exa highlights 字符串锚定（[m1.md](./m1.md) v1.3） |
| 快照形态 | 抽取后的 **纯文本 / Markdown**（便于切段、哈希与 `char_span`） |
| 无头浏览器 | **不实现**；JS 动态页拉不到正文时按「工具受阻」停止条件处理 |
| 配置 | 密钥仅环境变量 |

### 4.3 PageIndex（本地文档，M2 正式；可提前 spike）

| 项 | 确认 |
|----|------|
| 形态 | **自托管**本地仓库（开发机参考路径 `/Users/ecaps7/PageIndex`），**不移植**进 Prospector 主仓 |
| 接入 | 适配既有 MCP 三原语：`list_documents` / `get_document_structure` / `get_document_content` → 本系统 `kb_list` / `kb_structure` / `kb_read` |
| 权威链 | 树产物同步落本系统对象存储；引用链不依赖 PageIndex 会话状态；快照权威在本系统 |
| 环境变量 | **`PAGEINDEX_ROOT`**；**M2 起强制**（M0–M1 无 kb 测试可不设） |
| 纪律 | 工具内不再套一层 LLM 检索；树导航留在 Research Worker（设计 D10） |

该本机仓库已含 `PageIndexEvidenceBackend` 与 line→page 映射，与设计 locator（PDF 至少含 page）对齐；接入时注意将外部 `doc_id` 映射到本系统 Document version。

---

## 5. 鉴权、租户与 CLI

| 项 | 确认 |
|----|------|
| 鉴权（M0–M3） | **固定 API token**（环境变量校验） |
| 租户 | **假租户**：统一 `workspace_id` / `user_id`；真多租户隔离留到 **M4** |
| CLI 配置目录 | **`~/.prospector/`**（`config.toml` + `credentials`，credentials `0600`） |
| 环境变量优先 | `PROSPECTOR_API_URL` / `PROSPECTOR_API_TOKEN` 覆盖配置文件（与 CLI 文档一致） |
| 报告默认语言 | Brief `language` 默认 **`zh`**；CLI 界面文案首版中文 |

---

## 6. 明确不做 / 后移（避免基座膨胀）

| 项 | 立场 |
|----|------|
| Redis / RabbitMQ | M4 再引入；M0–M3 单进程薄壳 |
| Tempo + Loki + Grafana 完整栈 | 非 M0 阻塞；生产目标保留 |
| 无头浏览器 | 不做 |
| 向量私有库 / Docling 私库主路径 | 不做（D10） |
| 端到端 RL | 不做（D6） |
| 拆多包 workspace | 暂不；真需独立分发 CLI 时再拆 |
| M1–M2 昂贵评测（题库跑批 / LLM-as-judge） | 不做；定量门槛自 M3 起 |
| CLI `--web` 打开服务端渲染页 | 开放问题，待 M3 评估看板后定 |

---

## 7. 环境变量清单（首版）

名称可在实现时微调，但语义锁定如下：

| 变量 | 用途 |
|------|------|
| `PROSPECTOR_API_URL` | CLI / 客户端 API 基址 |
| `PROSPECTOR_API_TOKEN` | 固定鉴权 token（服务端校验同一值） |
| `PROSPECTOR_LLM_BASE_URL` | OpenAI-compatible endpoint |
| `PROSPECTOR_LLM_API_KEY` | LLM 密钥 |
| `PROSPECTOR_LLM_MODEL_STRONG` | 默认 `qwen3.7-max` |
| `PROSPECTOR_LLM_MODEL_MID` | 默认 `qwen3.7-plus` |
| `EXA_API_KEY` | 搜索与网页内容检索（v1.1 起；原 `TAVILY_API_KEY`） |
| `DATABASE_URL` | PostgreSQL（含或另配 search_path / schema） |
| `S3_ENDPOINT` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` / `S3_BUCKET` | MinIO / S3 兼容存储 |
| `PAGEINDEX_ROOT` | 自托管 PageIndex 根目录（M2 起强制） |

本地 compose 应将上述非密钥默认值写入 `.env.example`（密钥不入库）。

---

## 8. M0 验收对齐（防范围膨胀）

完整实现设计见 [m0.md](./m0.md)。在已确认栈下，M0 只证明：

1. 空流程 job 状态迁移落 **PG checkpointer**，kill 后恢复续跑  
2. structlog JSON 日志自动关联 trace/span（console exporter 即可）  
3. Repo 骨架 + Alembic + MinIO 接入 + GitHub Actions（unit + compose integration；M0 无外部 API 调用点）

不把 Redis、RabbitMQ、完整 Grafana 栈、PageIndex、真实 LLM/搜索 API 调用纳入 M0 必达。M1 起 live 验收策略见 [m1.md](./m1.md) §11。

---

## 9. 确认记录

| 轮次 | 主题 | 结论 |
|------|------|------|
| 1 | 语言与布局 | 3.13 + uv + src + 同包 |
| 2 | 质量工具链 | ruff + basedpyright + pytest；CI 强制 unit + compose integration（默认不含 live）；M1+ live 验收允许真实 LLM/Exa |
| 3 | Compose / 观测 | PG + MinIO；观测 console（可选 Collector）；Redis/MQ → M4 |
| 4 | LLM | OpenAI-compatible；max/plus；官方 SDK；环境变量 |
| 5 | 搜索与抓取 | Tavily；必须正文快照（文本/MD）；无浏览器（已被第 11 轮取代） |
| 6 | 鉴权 | 固定 token + 假租户至 M4 + `~/.prospector/` |
| 7 | ID / schema | UUID + SHA-256 + Pydantic v2 + UTC；Alembic 分 `app` / `langgraph` |
| 8 | PageIndex | 自托管本地仓 + MCP 三原语适配；`PAGEINDEX_ROOT`；M2 强制 |
| 9 | CI | GitHub Actions；compose；默认 `not live`；M1+ live 可选/本地；`PAGEINDEX_ROOT` M2 起强制 |
| 10 | 语言与快照 | 默认 `zh`；网页快照为抽取文本/Markdown |
| 11 | 搜索与抓取修订 | Exa（search + `/contents`）取代第 5 轮的 Tavily + 自建 HTTP/trafilatura；`EXA_API_KEY` 取代 `TAVILY_API_KEY`。依据 [m1.md](./m1.md) v1.2 / 设计文档 v3.10 |
| 12 | 摘录与预算 | 摘录改为快照分段按引用选取（废止 highlights 锚定）；Task 预算取 Brief；Worker 执行 `allowed_tools`。依据 [m1.md](./m1.md) v1.3–v1.4 |
