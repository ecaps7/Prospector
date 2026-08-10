"""Persist Report Verifier contract failures without leaving running state behind."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260810_0016_rv_failed"
down_revision: str | None = "20260810_0015_rev_exhausted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.report_verifier_runs "
        "DROP CONSTRAINT IF EXISTS report_verifier_runs_status_check"
    )
    op.execute(
        """
        ALTER TABLE app.report_verifier_runs
          ADD CONSTRAINT report_verifier_runs_status_check
          CHECK (status IN ('running','completed','failed'))
        """
    )
    op.execute("ALTER TABLE app.report_verifier_runs ADD COLUMN error JSONB")

    op.execute("ALTER TABLE app.reports DROP CONSTRAINT IF EXISTS reports_status_check")
    op.execute(
        """
        ALTER TABLE app.reports ADD CONSTRAINT reports_status_check
          CHECK (status IN (
            'writing','verifying','revising','verified','revisions_exhausted',
            'verification_failed','draft_rendered','failed_gap'
          ))
        """
    )


def downgrade() -> None:
    op.execute("UPDATE app.report_verifier_runs SET status='running' WHERE status='failed'")
    op.execute("ALTER TABLE app.report_verifier_runs DROP COLUMN error")
    op.execute(
        "ALTER TABLE app.report_verifier_runs "
        "DROP CONSTRAINT IF EXISTS report_verifier_runs_status_check"
    )
    op.execute(
        """
        ALTER TABLE app.report_verifier_runs
          ADD CONSTRAINT report_verifier_runs_status_check
          CHECK (status IN ('running','completed'))
        """
    )

    op.execute("UPDATE app.reports SET status='verifying' WHERE status='verification_failed'")
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
