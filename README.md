# Prospector

Prospector 是一个可控、可审计、可恢复的深度研究智能体。系统设计见 [docs/design.md](docs/design.md)，当前 M1 实现合同见 [docs/implementations/m1.md](docs/implementations/m1.md)。

## 当前实现状态

- M0 工程基座已完成：PostgreSQL checkpointer、Alembic、MinIO、日志、trace 与 CI。
- M1 已完成 Brief 生成、interactive HITL 和 Planner-Worker 研究链。
- 当前主图从冻结 Brief 运行到 `verifier_pending`；Verifier/Replan 与成文链仍在实施中。

## 本地运行

```bash
cp .env.example .env
docker compose up -d
uv sync
uv run --env-file .env alembic upgrade head
uv run --env-file .env prospector-local setup
uv run --env-file .env prospector-local ask "研究一个需要多侧面检索的问题" --effort standard --language zh
```

`ask` 当前执行：问题输入 → 最多一轮澄清 → Brief 生成 → `c/e/i/q` 确认 → Planner-Worker 研究。确认后，同一终端会按 PostgreSQL 事件账本实时显示 Planner 派发、Worker 搜索/抓取/落证与收工状态；当前研究终点是 `verifier_pending`。

已创建的 Job 可随时回放或继续跟随同一条人类可读时间线：

```bash
uv run --env-file .env prospector-local job events <job-id> --follow
```

## 测试

```bash
# unit（不需要 Docker）
uv run pytest tests/unit -q

# integration（需要 PostgreSQL + MinIO）
docker compose up -d
uv run --env-file .env alembic upgrade head
uv run --env-file .env prospector-local setup
uv run --env-file .env pytest tests/integration -q -m "integration and not live"

# 与 CI 一致的质量门
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run pytest tests/unit -q
uv run pytest tests/integration -q -m "integration and not live"
```

M0 的 checkpoint kill/resume 能力由集成测试验证，不作为空流程产品命令暴露。
