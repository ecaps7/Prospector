"""Let a stopped Job leave the history without leaving the evidence store.

The Jobs list is the only place a Job is addressed by browsing rather than by id, so
'delete' here means 'stop listing it'.  A hard delete would have to answer for
app.documents, which is deduplicated workspace-wide (`find_document` matches on
workspace + url + content_hash) yet carries the job_id of whichever Job fetched the
snapshot first: dropping that Job would take a snapshot a later Job's Excerpts still
cite.  Hiding the row leaves every citation, snapshot and checkpoint intact.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260830_0028_job_deleted"
down_revision: str | None = "20260829_0027_granularity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.jobs ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_jobs_listed
          ON app.jobs (created_at DESC, id DESC) WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.ix_jobs_listed")
    op.execute("ALTER TABLE app.jobs DROP COLUMN IF EXISTS deleted_at")
