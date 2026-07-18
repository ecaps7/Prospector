"""Add kb_read to the document_views view_kind CHECK constraint."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260718_0010_kb_read_view_kind"
down_revision: str | None = "20260717_0009_kind_elaboration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.document_views DROP CONSTRAINT document_views_view_kind_check"
    )
    op.execute(
        "ALTER TABLE app.document_views ADD CONSTRAINT document_views_view_kind_check "
        "CHECK (view_kind IN ('exa_highlights','kb_read'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE app.document_views DROP CONSTRAINT document_views_view_kind_check"
    )
    op.execute(
        "ALTER TABLE app.document_views ADD CONSTRAINT document_views_view_kind_check "
        "CHECK (view_kind = 'exa_highlights')"
    )
