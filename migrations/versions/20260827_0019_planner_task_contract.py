"""Reduce Planner-authored task fields to executable research content."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260827_0019_task_contract"
down_revision: str | None = "20260826_0018_claim_depth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.tasks
          DROP COLUMN subjects,
          DROP COLUMN research_mode,
          DROP COLUMN source_policy
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM app.tasks) THEN
            RAISE EXCEPTION
              'cannot restore removed Planner task fields after task rows have existed';
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE app.tasks
          ADD COLUMN subjects JSONB NOT NULL,
          ADD COLUMN research_mode TEXT NOT NULL,
          ADD COLUMN source_policy JSONB NOT NULL
        """
    )
