# ruff: noqa: E501
"""Let Research Synthesis route its evidence request through the Research Verifier.

A synthesis gap is confirmed by the same Research Verifier that owns major-gap
admission, so that round is a real verifier run at the plan version that already
passed.  The old UNIQUE(job_id, evaluated_plan_version) made the two rounds collide,
which is why the trigger joins the idempotency key.

research_synthesis_runs gains the verifier run it was built from: without it, replay
after new research reuses the stale synthesis run and the graph loops.  The column is
nullable because rows written before this migration have no such link.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260828_0024_synth_gap_verify"
down_revision: str | None = "20260828_0023_post_attr_report"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.verifier_runs DROP CONSTRAINT IF EXISTS verifier_runs_trigger_check"
    )
    op.execute(
        """
        ALTER TABLE app.verifier_runs
          ADD CONSTRAINT verifier_runs_trigger_check
          CHECK (trigger IN ('planner_finish','budget_exhausted','synthesis_gap'))
        """
    )
    op.execute(
        "ALTER TABLE app.verifier_runs "
        "DROP CONSTRAINT IF EXISTS verifier_runs_job_id_evaluated_plan_version_key"
    )
    op.execute(
        """
        ALTER TABLE app.verifier_runs
          ADD CONSTRAINT verifier_runs_job_plan_trigger_key
          UNIQUE (job_id, evaluated_plan_version, trigger)
        """
    )
    op.execute(
        """
        ALTER TABLE app.research_synthesis_runs
          ADD COLUMN verifier_run_id UUID REFERENCES app.verifier_runs(id) ON DELETE CASCADE
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX research_synthesis_runs_verifier_run_key "
        "ON app.research_synthesis_runs (job_id, verifier_run_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.research_synthesis_runs_verifier_run_key")
    op.execute("ALTER TABLE app.research_synthesis_runs DROP COLUMN verifier_run_id")
    op.execute("DELETE FROM app.verifier_runs WHERE trigger='synthesis_gap'")
    op.execute(
        "ALTER TABLE app.verifier_runs DROP CONSTRAINT IF EXISTS verifier_runs_job_plan_trigger_key"
    )
    op.execute(
        """
        ALTER TABLE app.verifier_runs
          ADD CONSTRAINT verifier_runs_job_id_evaluated_plan_version_key
          UNIQUE (job_id, evaluated_plan_version)
        """
    )
    op.execute(
        "ALTER TABLE app.verifier_runs DROP CONSTRAINT IF EXISTS verifier_runs_trigger_check"
    )
    op.execute(
        """
        ALTER TABLE app.verifier_runs
          ADD CONSTRAINT verifier_runs_trigger_check
          CHECK (trigger IN ('planner_finish','budget_exhausted'))
        """
    )
