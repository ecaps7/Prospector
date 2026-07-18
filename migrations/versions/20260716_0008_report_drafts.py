"""Add structured Report Writer drafts and rendered preview artifacts."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260716_0008_report_drafts"
down_revision: str | None = "20260716_0007_assert_disp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.reports (
          id UUID PRIMARY KEY,
          job_id UUID NOT NULL UNIQUE REFERENCES app.jobs(id) ON DELETE CASCADE,
          verifier_run_id UUID NOT NULL REFERENCES app.verifier_runs(id),
          current_revision INTEGER NOT NULL DEFAULT 1 CHECK (current_revision >= 1),
          status TEXT NOT NULL CHECK (status IN ('writing','draft_rendered')),
          markdown_ref TEXT,
          markdown_hash TEXT,
          json_ref TEXT,
          json_hash TEXT,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.report_revisions (
          report_id UUID NOT NULL REFERENCES app.reports(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL CHECK (revision >= 1),
          full_prompt JSONB NOT NULL,
          raw_output JSONB,
          draft JSONB,
          body_char_count INTEGER CHECK (body_char_count >= 0),
          status TEXT NOT NULL CHECK (status IN ('prompted','generated','rendered')),
          created_at TIMESTAMPTZ NOT NULL,
          completed_at TIMESTAMPTZ,
          PRIMARY KEY (report_id, revision)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.report_statements (
          report_id UUID NOT NULL,
          revision INTEGER NOT NULL,
          statement_id TEXT NOT NULL,
          section_id TEXT NOT NULL,
          paragraph_id TEXT NOT NULL,
          statement_order INTEGER NOT NULL CHECK (statement_order >= 1),
          kind TEXT NOT NULL CHECK (kind IN ('evidence','derived','transition','limitation')),
          text TEXT NOT NULL,
          candidate_excerpt_ids JSONB NOT NULL,
          premise_statement_ids JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (report_id, revision, statement_id),
          UNIQUE (report_id, revision, statement_order),
          FOREIGN KEY (report_id, revision)
            REFERENCES app.report_revisions(report_id, revision) ON DELETE CASCADE
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE app.report_statements")
    op.execute("DROP TABLE app.report_revisions")
    op.execute("DROP TABLE app.reports")
