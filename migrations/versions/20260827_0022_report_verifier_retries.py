"""Preserve Report Verifier retries and represent irrelevant candidate evidence."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260827_0022_rv_retries"
down_revision: str | None = "20260827_0021_no_stage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.report_verifier_runs
          ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1)
        """
    )
    op.execute(
        """
        ALTER TABLE app.report_verifier_runs
          DROP CONSTRAINT report_verifier_runs_report_id_revision_round_key
        """
    )
    op.execute(
        """
        ALTER TABLE app.report_verifier_runs
          ADD CONSTRAINT report_verifier_runs_revision_attempt_key
          UNIQUE (report_id, revision, round, attempt)
        """
    )

    op.execute("ALTER TABLE app.claim_evidence DROP CONSTRAINT claim_evidence_relation_check")
    op.execute(
        """
        ALTER TABLE app.claim_evidence
          ADD CONSTRAINT claim_evidence_relation_check
          CHECK (relation IN ('support','contradict','partial','irrelevant'))
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM app.report_verifier_runs WHERE attempt > 1
          ) THEN
            RAISE EXCEPTION
              'cannot remove Report Verifier attempts after retries have been recorded';
          END IF;
          IF EXISTS (
            SELECT 1 FROM app.claim_evidence WHERE relation = 'irrelevant'
          ) THEN
            RAISE EXCEPTION
              'cannot remove irrelevant evidence relation while rows still use it';
          END IF;
        END
        $$
        """
    )

    op.execute("ALTER TABLE app.claim_evidence DROP CONSTRAINT claim_evidence_relation_check")
    op.execute(
        """
        ALTER TABLE app.claim_evidence
          ADD CONSTRAINT claim_evidence_relation_check
          CHECK (relation IN ('support','contradict','partial'))
        """
    )

    op.execute(
        """
        ALTER TABLE app.report_verifier_runs
          DROP CONSTRAINT report_verifier_runs_revision_attempt_key
        """
    )
    op.execute(
        """
        ALTER TABLE app.report_verifier_runs
          ADD CONSTRAINT report_verifier_runs_report_id_revision_round_key
          UNIQUE (report_id, revision, round)
        """
    )
    op.execute("ALTER TABLE app.report_verifier_runs DROP COLUMN attempt")
