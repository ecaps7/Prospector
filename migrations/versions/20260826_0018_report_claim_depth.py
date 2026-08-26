"""Retain the actual depth of Report Verifier premise chains."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260826_0018_claim_depth"
down_revision: str | None = "20260826_0017_task_cancel"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.claim_premises DROP CONSTRAINT IF EXISTS claim_premises_depth_check"
    )
    op.execute(
        "ALTER TABLE app.claim_premises ADD CONSTRAINT claim_premises_depth_check "
        "CHECK (depth >= 0)"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM app.claim_premises WHERE depth > 2) THEN
            RAISE EXCEPTION 'cannot restore the depth<=2 constraint while deeper claims exist';
          END IF;
        END
        $$
        """
    )
    op.execute("ALTER TABLE app.claim_premises DROP CONSTRAINT claim_premises_depth_check")
    op.execute(
        "ALTER TABLE app.claim_premises ADD CONSTRAINT claim_premises_depth_check "
        "CHECK (depth >= 0 AND depth <= 2)"
    )
