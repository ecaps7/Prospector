"""Cascade claim_evidence when parent excerpts are deleted."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260718_0012_ce_fk"
down_revision: str | None = "20260718_0011_report_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.claim_evidence DROP CONSTRAINT claim_evidence_excerpt_id_fkey")
    op.execute(
        """
        ALTER TABLE app.claim_evidence
          ADD CONSTRAINT claim_evidence_excerpt_id_fkey
          FOREIGN KEY (excerpt_id) REFERENCES app.excerpts(id) ON DELETE CASCADE
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.claim_evidence DROP CONSTRAINT claim_evidence_excerpt_id_fkey")
    op.execute(
        """
        ALTER TABLE app.claim_evidence
          ADD CONSTRAINT claim_evidence_excerpt_id_fkey
          FOREIGN KEY (excerpt_id) REFERENCES app.excerpts(id)
        """
    )
