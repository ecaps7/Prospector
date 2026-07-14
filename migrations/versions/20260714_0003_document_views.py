"""Persist task-scoped document views used by workers."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260714_0003_views"
down_revision: str | None = "20260714_0002_pw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.document_views (
          id UUID PRIMARY KEY,
          job_id UUID NOT NULL REFERENCES app.jobs(id) ON DELETE CASCADE,
          task_id UUID NOT NULL REFERENCES app.tasks(id) ON DELETE CASCADE,
          doc_id UUID NOT NULL REFERENCES app.documents(id),
          doc_version INTEGER NOT NULL CHECK (doc_version >= 1),
          view_kind TEXT NOT NULL
            CHECK (view_kind = 'exa_highlights'),
          items JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_document_views_task ON app.document_views(task_id, id)")


def downgrade() -> None:
    op.execute("DROP TABLE app.document_views")
