"""Remove redundant Research Verifier rationale columns."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260827_0020_verifier_contract"
down_revision: str | None = "20260827_0019_task_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.verifier_runs
          DROP COLUMN coverage_rationale,
          DROP COLUMN brief_alignment,
          DROP COLUMN brief_alignment_rationale,
          DROP COLUMN credibility_rationale
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM app.verifier_runs) THEN
            RAISE EXCEPTION
              'cannot restore removed Research Verifier rationales after runs have existed';
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE app.verifier_runs
          ADD COLUMN coverage_rationale TEXT,
          ADD COLUMN brief_alignment TEXT
            CHECK (brief_alignment IN ('aligned','misaligned')),
          ADD COLUMN brief_alignment_rationale TEXT,
          ADD COLUMN credibility_rationale TEXT
        """
    )
