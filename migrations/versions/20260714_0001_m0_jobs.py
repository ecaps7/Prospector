"""M0: create app.jobs and ensure schema isolation."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260714_0001_m0"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute("CREATE SCHEMA IF NOT EXISTS langgraph")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.jobs (
            id UUID PRIMARY KEY,
            workspace_id UUID NOT NULL,
            user_id UUID NOT NULL,
            status TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_jobs_status ON app.jobs (status)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.ix_jobs_status")
    op.execute("DROP TABLE IF EXISTS app.jobs")
