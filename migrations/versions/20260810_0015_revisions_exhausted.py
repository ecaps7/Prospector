"""Separate a fully verified report from one that ran out of revision rounds."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Keep under alembic_version.version_num's varchar(32).
revision: str = "20260810_0015_rev_exhausted"
down_revision: str | None = "20260810_0014_vrun_failed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.reports DROP CONSTRAINT IF EXISTS reports_status_check")
    op.execute(
        """
        ALTER TABLE app.reports ADD CONSTRAINT reports_status_check
          CHECK (status IN (
            'writing','verifying','revising','verified','revisions_exhausted',
            'draft_rendered','failed_gap'
          ))
        """
    )


def downgrade() -> None:
    op.execute("UPDATE app.reports SET status='verified' WHERE status='revisions_exhausted'")
    op.execute("ALTER TABLE app.reports DROP CONSTRAINT IF EXISTS reports_status_check")
    op.execute(
        """
        ALTER TABLE app.reports ADD CONSTRAINT reports_status_check
          CHECK (status IN (
            'writing','verifying','revising','verified','draft_rendered','failed_gap'
          ))
        """
    )
