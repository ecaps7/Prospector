# Prospector

Deep research agent (v2). See `docs/design.md` and `docs/implementations/`.

## M0 quick start

```bash
cp .env.example .env
docker compose up -d
uv sync
uv run alembic upgrade head
uv run prospector-local setup
uv run prospector-local run
```

Kill / resume demo:

```bash
# terminal 1 — long step_b window
PROSPECTOR_STEP_B_SLEEP_SECONDS=30 uv run prospector-local run --job-id <uuid>

# terminal 2 — after step_a logs appear
kill -9 <pid>
uv run prospector-local resume <uuid>
```

## Tests

```bash
# unit (no Docker)
uv run pytest tests/unit -q

# integration (requires compose)
docker compose up -d
uv run alembic upgrade head
uv run prospector-local setup
uv run pytest tests/integration -q -m integration

# quality gates (same as CI)
uv run ruff check .
uv run ruff format --check .
uv run basedpyright
uv run pytest tests/unit -q
uv run pytest tests/integration -q -m integration
```
