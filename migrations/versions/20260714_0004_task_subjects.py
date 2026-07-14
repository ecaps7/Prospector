"""Add declared research subjects to worker tasks."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260714_0004_subjects"
down_revision: str | None = "20260714_0003_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.tasks
          ADD COLUMN subjects JSONB NOT NULL DEFAULT '[]'::jsonb
        """
    )
    # Pre-existing tasks never declared subjects; backfill a single placeholder so
    # they still satisfy the one-subject minimum when replayed.
    op.execute("""UPDATE app.tasks SET subjects = '["unspecified"]'::jsonb""")


def downgrade() -> None:
    op.execute("ALTER TABLE app.tasks DROP COLUMN subjects")
