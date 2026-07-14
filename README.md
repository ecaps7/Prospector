# Prospector

Prospector 是一个可控、可审计、可恢复的深度研究智能体。系统设计见 [docs/design.md](docs/design.md)，当前 M1 实现合同见 [docs/implementations/m1.md](docs/implementations/m1.md)。

## 当前实现状态

- M0 工程基座已完成：PostgreSQL checkpointer、Alembic、MinIO、日志、trace 与 CI。
- M1 已完成 Scope、Research Brief schema 和 interactive HITL。
- 当前实现边界是 Planner-Worker；完整研究主图仍在实施中。

## 本地运行

```bash
cp .env.example .env
docker compose up -d
uv sync
uv run --env-file .env alembic upgrade head
uv run --env-file .env prospector-local setup
uv run --env-file .env prospector-local ask "研究一个需要多侧面检索的问题" --effort standard --language zh
```

`ask` 当前执行：问题输入 → 最多一轮澄清 → Brief 生成 → `c/e/i/q` 确认。Planner-Worker 完成后，同一命令会从冻结 Brief 继续进入完整研究主图。

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
