"""Rename the Worker stop observation to its decision-log meaning."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260714_0005_finish_reason"
down_revision: str | None = "20260714_0004_subjects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.tasks RENAME COLUMN gap_note TO finish_reason")


def downgrade() -> None:
    op.execute("ALTER TABLE app.tasks RENAME COLUMN finish_reason TO gap_note")
