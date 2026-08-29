"""Persist recoverable Claim Attribution batches.

A completed AttributionRun is still the only object later stages may read.  The
attempt row and its batches exist so a later batch failure does not throw away
work that already passed, and so a contract error is stored before the Job fails.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0025_attr_batches"
down_revision: str | None = "20260828_0024_synth_gap_verify"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.attribution_runs_v2
          ADD COLUMN IF NOT EXISTS raw_output JSONB
        """
    )
    op.execute(
        """
        CREATE TABLE app.attribution_batches_v2 (
          id UUID PRIMARY KEY,
          attribution_run_id UUID NOT NULL REFERENCES app.attribution_runs_v2(id) ON DELETE CASCADE,
          batch_index INTEGER NOT NULL CHECK (batch_index >= 0),
          block_ids JSONB NOT NULL,
          candidate_refs JSONB NOT NULL,
          selection_prompt JSONB,
          selection_raw JSONB,
          selection_result JSONB,
          verify_prompt JSONB,
          verify_raw JSONB,
          verify_result JSONB,
          contract_error TEXT,
          status TEXT NOT NULL CHECK (status IN ('prompted','selected','completed','failed')),
          created_at TIMESTAMPTZ NOT NULL,
          completed_at TIMESTAMPTZ,
          UNIQUE (attribution_run_id, batch_index)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.attribution_batches_v2")
    op.execute("ALTER TABLE app.attribution_runs_v2 DROP COLUMN IF EXISTS raw_output")
