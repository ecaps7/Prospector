"""Hold user-stated Brief limits as fields instead of prose."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260809_0013_brief_constraints"
down_revision: str | None = "20260718_0012_ce_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.briefs
          ADD COLUMN IF NOT EXISTS user_constraints JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.briefs DROP COLUMN IF EXISTS user_constraints")
