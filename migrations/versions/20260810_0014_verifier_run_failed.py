"""Let a verifier_run record that it was answered but the answer was rejected."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# Keep under alembic_version.version_num's varchar(32).
revision: str = "20260810_0014_vrun_failed"
down_revision: str | None = "20260809_0013_brief_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.verifier_runs DROP CONSTRAINT IF EXISTS verifier_runs_status_check")
    op.execute(
        """
        ALTER TABLE app.verifier_runs
          ADD CONSTRAINT verifier_runs_status_check
          CHECK (status IN ('prompted','completed','failed'))
        """
    )


def downgrade() -> None:
    op.execute("UPDATE app.verifier_runs SET status='prompted' WHERE status='failed'")
    op.execute("ALTER TABLE app.verifier_runs DROP CONSTRAINT IF EXISTS verifier_runs_status_check")
    op.execute(
        """
        ALTER TABLE app.verifier_runs
          ADD CONSTRAINT verifier_runs_status_check
          CHECK (status IN ('prompted','completed'))
        """
    )
