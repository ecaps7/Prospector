"""Store the final read-through and the revision patch that produced each revision.

Coherence never had a check of its own: attribution judges spans, review judges the
argument, and the assumption that a full rewrite preserved the prose was never tested.
The read-through is that check, and it is stored beside the review it follows.

The patch column is the other half of the same change.  Revision replaces named block
ranges instead of re-emitting the document, so the diff between two revisions is an
object rather than something a reader has to reconstruct from two full texts.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0026_readthrough"
down_revision: str | None = "20260829_0025_attr_batches"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.report_review_runs_v2
          ADD COLUMN IF NOT EXISTS readthrough_prompt JSONB,
          ADD COLUMN IF NOT EXISTS readthrough_raw JSONB,
          ADD COLUMN IF NOT EXISTS readthrough JSONB
        """
    )
    op.execute(
        """
        ALTER TABLE app.report_revisions_v2
          ADD COLUMN IF NOT EXISTS patch JSONB
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.report_review_runs_v2
          DROP COLUMN IF EXISTS readthrough_prompt,
          DROP COLUMN IF EXISTS readthrough_raw,
          DROP COLUMN IF EXISTS readthrough
        """
    )
    op.execute("ALTER TABLE app.report_revisions_v2 DROP COLUMN IF EXISTS patch")
