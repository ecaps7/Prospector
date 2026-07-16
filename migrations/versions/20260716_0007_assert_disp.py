"""Add assertion evidence-usability dispositions."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260716_0007_assert_disp"
down_revision: str | None = "20260716_0006_research_verifier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.assertion_dispositions (
          assertion_id UUID NOT NULL REFERENCES app.assertions(id) ON DELETE CASCADE,
          verifier_run_id UUID NOT NULL REFERENCES app.verifier_runs(id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK (status IN ('unusable','restored')),
          reason TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (assertion_id, verifier_run_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_assertion_dispositions_run
          ON app.assertion_dispositions (verifier_run_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.assertion_dispositions")
