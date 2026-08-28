# AGENTS.md

Prospector 是查证型深度调研系统，不是通用 Agent 框架。人读 README；机制与验收以 `docs/design.md` 与 `docs/implementations/m1.md` 为准。M0/M1 已实现；`docs/future/` 是未实现草案，不要当代码合同。

## Commands

```bash
cp .env.example .env          # 填 LLM / Exa 密钥；不要提交 .env
docker compose up -d          # PostgreSQL 18 + MinIO
uv sync
uv run --env-file .env alembic upgrade head
uv run --env-file .env prospector-local setup

uv run ruff check . && uv run ruff format --check . && uv run basedpyright
uv run pytest tests/unit -q -m "not live"
uv run --env-file .env pytest tests/integration -q -m "integration and not live"

uv run pytest tests/unit/test_foo.py -q          # 单文件
uv run pytest tests/unit/test_foo.py::test_bar -q
uv run --env-file .env python eval/run_report_verifier.py
```

服务端口是 **7620**（`prospector serve` / CLI 默认），不是 `PROSPECTOR_API_URL` 占位里的 8000。前端：`cd web && npm ci && npm run dev`（Vite 把 `/api` 代理到 7620）。改完 UI 后 `npm run build`，产物在 `web/dist/`，由 `prospector serve` 托管。

`@pytest.mark.live` 需要真实 LLM/Exa，密钥未配置时跳过；不要为了让 live 变绿去改断言口径。

## Layout and imports

```
schemas  ← 零业务逻辑（Pydantic 合同）
  ↑
store / tools / deterministic / reporting / obs
  ↑
agents
  ↑
flow
  ↑
api / runtime / cli
```

- `deterministic/` 与 `reporting/` 禁止调用 LLM。引用角标、编号、表格/图表绑定、预算注入、质量门、血缘检查、成文渲染只走代码。
- `flow/` 禁止导入 `runtime/`。图状态必须可序列化（无连接、锁、协程对象）。
- 目录按架构边界分，不按里程碑拆包。不要新增「单 Worker 捷径图」或「跳过 Verifier 的成文路径」。

## Domain (do not invent synonyms)

| 词 | 含义 |
|---|---|
| Brief | 已确认的研究输入快照。展开问题，**不是**覆盖合同。 |
| Plan | 版本化执行合同。Verifier 对照 Plan 履约，用 Brief 查偏题。 |
| Excerpt | 快照里的精确原文片段。禁止用模型摘要冒充 Excerpt。 |
| Assertion | 绑在 Excerpt 上的结构化断言。 |
| Claim | Report Verifier 对某句正文的落库验证记录。 |
| statement id | Writer 给句子的稳定 id；模型只声明依据哪些 Excerpt，不写角标数字。 |

废止且禁止实现：分层 Brief、前哨校准、`must_cover` 硬清单（D11）。

## Invariants

1. **D12 联网路径**：`web_search` 只给元数据；`web_fetch` 把全文写入 Document 快照、把 Exa highlights 写入任务级 DocumentView；Worker 只选 `source_ref`，由 `save_findings` 解析后原子写入 Excerpt + Assertion。全文不进任何 Prospector LLM 上下文。不要做「搜索即摘要」或让 Worker 读整页。
2. **证据链**：首次抓取就必须留 Document 快照与精确 Excerpt。核对不过的句子最多修订两轮；触顶后保留原文、标记 `partial`，**不得**给失败句生成已验证引用角标。
3. **预算**：硬闸是 Planner 决策轮，以及按 effort 注入的批次并发与 Worker 决策轮；运行时把可用动作、工具和具体上限直接反馈给模型。工具调用总数 / Job 墙钟不是硬停条件。停止研究不能绕过 Research Verifier、Claim 验证或成文审计。
4. **Worker 动作**：每轮唯一 `search` / `save` / `finish`（严格 JSON Schema），不要改成供应商 Function Calling 参数。
5. **Planner 线程准入封闭**：只追加决策对象、断言投影摘要 + 收工声明、拒绝/格式错误、verifier gap、预算余额。禁止塞 worker 原始轨迹、Document 正文、客户端句柄。
6. **不要提前做 M2–M4**：Data Worker / `kb_*` / FigureSpec / Redis / 多用户调度均未实现。Spike 可以，不要当验收范围，也不要为它们加运行时兼容旧合同的分支。

改领域语义时同步 `docs/design.md` 与对应实现设计，不要靠代码里留旧分支「兼容」文档。

## Style

- Python 3.13，`src/` layout；行宽 100；Ruff (`E,F,I,UP,B,SIM`) + basedpyright `standard`。
- 新合同先改 `schemas/`，再改 store / agents / flow。测试钉合同，不钉模型措辞。
- 日志默认禁止密钥、连接串和研究正文。OTel exporter 失败不得阻断 checkpoint。
- 不要改 `examples/` 里已落盘的运行产物，除非任务就是更新示例。

前端约定见 `web/AGENTS.md`。
