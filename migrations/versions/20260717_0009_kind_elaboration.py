"""Replace transition with elaboration in report_statements kind CHECK constraint."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260717_0009_kind_elaboration"
down_revision: str | None = "20260716_0008_report_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.report_statements DROP CONSTRAINT report_statements_kind_check"
    )
    op.execute(
        "ALTER TABLE app.report_statements ADD CONSTRAINT report_statements_kind_check "
        "CHECK (kind IN ('evidence','derived','elaboration','limitation'))"
    )
    op.execute(
        "UPDATE app.report_statements SET kind = 'elaboration' WHERE kind = 'transition'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE app.report_statements SET kind = 'transition' WHERE kind = 'elaboration'"
    )
    op.execute(
        "ALTER TABLE app.report_statements DROP CONSTRAINT report_statements_kind_check"
    )
    op.execute(
        "ALTER TABLE app.report_statements ADD CONSTRAINT report_statements_kind_check "
        "CHECK (kind IN ('evidence','derived','transition','limitation'))"
    )
