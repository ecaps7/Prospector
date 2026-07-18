"""Add report verifier runs, claim tables, and unlock report revision statuses."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260718_0011_report_claims"
down_revision: str | None = "20260718_0010_kb_read_view_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.reports DROP CONSTRAINT IF EXISTS reports_status_check
        """
    )
    op.execute(
        """
        ALTER TABLE app.reports ADD CONSTRAINT reports_status_check
          CHECK (status IN (
            'writing','verifying','revising','verified','draft_rendered','failed_gap'
          ))
        """
    )

    op.execute(
        """
        CREATE TABLE app.report_verifier_runs (
          id UUID PRIMARY KEY,
          report_id UUID NOT NULL REFERENCES app.reports(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL CHECK (revision >= 1),
          round INTEGER NOT NULL CHECK (round >= 1),
          status TEXT NOT NULL CHECK (status IN ('running','completed')),
          dirty_statement_ids JSONB NOT NULL,
          findings JSONB,
          statement_checks JSONB,
          created_at TIMESTAMPTZ NOT NULL,
          completed_at TIMESTAMPTZ,
          UNIQUE (report_id, revision, round),
          FOREIGN KEY (report_id, revision)
            REFERENCES app.report_revisions(report_id, revision) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE app.claims (
          id UUID PRIMARY KEY,
          report_id UUID NOT NULL REFERENCES app.reports(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL CHECK (revision >= 1),
          statement_id TEXT NOT NULL,
          text TEXT NOT NULL,
          claim_type TEXT NOT NULL
            CHECK (claim_type IN ('fact','number','causal','opinion_attributed')),
          grounding TEXT NOT NULL CHECK (grounding IN ('evidence','derived')),
          report_section TEXT,
          produced_by TEXT NOT NULL DEFAULT 'report_verifier'
            CHECK (produced_by = 'report_verifier'),
          created_at TIMESTAMPTZ NOT NULL,
          UNIQUE (report_id, revision, statement_id),
          FOREIGN KEY (report_id, revision)
            REFERENCES app.report_revisions(report_id, revision) ON DELETE CASCADE
        )
        """
    )

    op.execute(
        """
        CREATE TABLE app.claim_evidence (
          claim_id UUID NOT NULL REFERENCES app.claims(id) ON DELETE CASCADE,
          excerpt_id UUID NOT NULL REFERENCES app.excerpts(id) ON DELETE CASCADE,
          relation TEXT NOT NULL CHECK (relation IN ('support','contradict','partial')),
          verifier_run_id UUID NOT NULL
            REFERENCES app.report_verifier_runs(id) ON DELETE CASCADE,
          created_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (claim_id, excerpt_id, verifier_run_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE app.claim_premises (
          claim_id UUID NOT NULL REFERENCES app.claims(id) ON DELETE CASCADE,
          premise_claim_ids JSONB NOT NULL,
          inference_note TEXT,
          depth INTEGER NOT NULL CHECK (depth >= 0 AND depth <= 2),
          verifier_run_id UUID NOT NULL
            REFERENCES app.report_verifier_runs(id) ON DELETE CASCADE,
          created_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (claim_id, verifier_run_id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE app.claim_verdicts (
          claim_id UUID NOT NULL REFERENCES app.claims(id) ON DELETE CASCADE,
          verifier_run_id UUID NOT NULL
            REFERENCES app.report_verifier_runs(id) ON DELETE CASCADE,
          status TEXT NOT NULL CHECK (status IN (
            'pass','unsupported','conflicted','overreach','miscalibrated'
          )),
          reason TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (claim_id, verifier_run_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.claim_verdicts")
    op.execute("DROP TABLE IF EXISTS app.claim_premises")
    op.execute("DROP TABLE IF EXISTS app.claim_evidence")
    op.execute("DROP TABLE IF EXISTS app.claims")
    op.execute("DROP TABLE IF EXISTS app.report_verifier_runs")
    op.execute("ALTER TABLE app.reports DROP CONSTRAINT IF EXISTS reports_status_check")
    op.execute(
        """
        ALTER TABLE app.reports ADD CONSTRAINT reports_status_check
          CHECK (status IN ('writing','draft_rendered'))
        """
    )
