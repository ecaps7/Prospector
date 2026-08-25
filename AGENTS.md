# AGENTS.md

Prospector — 自动深度调研智能体（Python 3.13 + LangGraph + PostgreSQL + MinIO）。
产品是一个 CLI：`prospector`（瘦客户端 + FastAPI 服务端）与 `prospector-local`（不起服务端的本地/调试入口）。
面向人类开发者的完整说明见 `README.md`、`docs/design.md`、`docs/cli.md`。

## Cursor Cloud specific instructions

启动时的更新脚本已经跑过 `uv sync --frozen`（依赖装在 `.venv`，Python 3.13 由 uv 自带）。
无需重复安装依赖；下面只记录非显而易见的运行/测试注意事项。

### 服务（都在本机单进程运行）

- **PostgreSQL + MinIO**：集成测试、迁移、服务端预检都依赖这两个容器。用 `docker compose up -d --wait postgres minio minio-init` 启动。Docker 已随 VM 装好，但 **daemon 不会自动起**；若 `docker info` 失败，先运行 `sudo dockerd > /tmp/dockerd.log 2>&1 &`（daemon 用 `fuse-overlayfs` 存储驱动 + legacy iptables，已在 `/etc/docker/daemon.json` 配好）。compose 命令需要 `sudo`。
- **`.env`**：`.env` 被 gitignore，不会随仓库带来。启动会话若没有 `.env`，先 `cp .env.example .env`。里面是本机默认值（DB/S3），LLM 与 Exa 的 key 是空占位符。
- **迁移 + checkpointer**：容器起来后跑 `uv run --env-file .env alembic upgrade head` 和 `uv run --env-file .env prospector-local setup`（建 checkpointer 表 + MinIO bucket）。二者幂等，可重复跑。
- **API 服务端**：`uv run --env-file .env prospector serve --init`，默认监听 `127.0.0.1:7620`。`--init` 会顺带建表/建 bucket。`prospector` 瘦客户端命令（`job list`、`job status` 等）走这个端口。

### 关键 gotcha：`TERM=dumb` 会让 Rich 退回 80 列

Cursor 的 shell 默认 `TERM=dumb`。Rich 在 dumb 终端下强制 80 列（`Console(width=...)` 显式宽度也会被忽略），于是 `prospector job list` / `job status` 的表格会**丢列**，`tests/unit/test_cli_commands.py::test_job_list_and_status_render_authoritative_snapshots` 也会因此假失败。
**跑测试和 CLI 命令时请设一个正常的 TERM**，例如：

```bash
TERM=xterm uv run pytest tests/unit -q -m "not live"
```

同类问题：`prospector job status` 重定向到文件时 Rich 退回 80 列，需要 `COLUMNS=200`（`examples/collect_run.py` 已代为设置）。CI（非 TTY，TERM 非 dumb）不受影响，所以这是本环境特有的坑。

### 质量门与测试（与 `.github/workflows/ci.yml` 一致）

```bash
uv run ruff check . && uv run ruff format --check . && uv run basedpyright
TERM=xterm uv run pytest tests/unit -q -m "not live"
TERM=xterm uv run --env-file .env pytest tests/integration -q -m "integration and not live"
```

- 标了 `live` 的测试需要真实 LLM / Exa 凭据，未配置时**自动跳过**（正常现象，不是失败）。
- 集成测试用 mock LLM 跑通完整研究图，因此**不需要**任何外部 API key。

### 真实研究运行需要外部凭据

`prospector ask` 与 API 的 `/scope`、`/jobs` 会预检 LLM 配置；没有 `PROSPECTOR_LLM_BASE_URL` / `PROSPECTOR_LLM_API_KEY`（以及检索用的 `EXA_API_KEY`）时返回结构化错误 `llm_not_configured`，属预期行为。要跑出真实报告，需在 `.env` 里填这些 key。离线可验证核心「引用血缘」保证：`uv run python examples/verify_lineage.py examples/verified-neijuan-semantic-shift/`（仅用标准库，无需 DB / key）。
