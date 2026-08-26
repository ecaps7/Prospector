"""Allow research tasks to record terminal cancellation explicitly."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260826_0017_task_cancel"
down_revision: str | None = "20260810_0016_rv_failed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT IF EXISTS tasks_status_check")
    op.execute(
        """
        ALTER TABLE app.tasks ADD CONSTRAINT tasks_status_check
          CHECK (status IN ('pending','running','done','failed','skipped','cancelled'))
        """
    )


def downgrade() -> None:
    op.execute("UPDATE app.tasks SET status='skipped' WHERE status='cancelled'")
    op.execute("ALTER TABLE app.tasks DROP CONSTRAINT IF EXISTS tasks_status_check")
    op.execute(
        """
        ALTER TABLE app.tasks ADD CONSTRAINT tasks_status_check
          CHECK (status IN ('pending','running','done','failed','skipped'))
        """
    )
