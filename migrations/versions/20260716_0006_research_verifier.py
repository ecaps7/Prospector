"""Add Research Verifier runs and conflict resolutions."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260716_0006_research_verifier"
down_revision: str | None = "20260714_0005_finish_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.verifier_runs (
          id UUID PRIMARY KEY,
          job_id UUID NOT NULL REFERENCES app.jobs(id) ON DELETE CASCADE,
          evaluated_plan_version INTEGER NOT NULL CHECK (evaluated_plan_version >= 1),
          decision_round INTEGER NOT NULL CHECK (decision_round >= 1),
          trigger TEXT NOT NULL CHECK (trigger IN ('planner_finish','budget_exhausted')),
          full_prompt JSONB NOT NULL,
          raw_output JSONB,
          decision_reason TEXT,
          coverage_rationale TEXT,
          brief_alignment TEXT CHECK (brief_alignment IN ('aligned','misaligned')),
          brief_alignment_rationale TEXT,
          credibility_rationale TEXT,
          release_decision TEXT CHECK (release_decision IN ('pass','needs_research')),
          gaps JSONB,
          status TEXT NOT NULL CHECK (status IN ('prompted','completed')),
          created_at TIMESTAMPTZ NOT NULL,
          completed_at TIMESTAMPTZ,
          UNIQUE(job_id, evaluated_plan_version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.conflict_resolutions (
          conflict_key TEXT NOT NULL,
          verifier_run_id UUID NOT NULL REFERENCES app.verifier_runs(id) ON DELETE CASCADE,
          disputed_point TEXT NOT NULL,
          excerpt_ids JSONB NOT NULL,
          decision TEXT NOT NULL CHECK (decision IN ('present_both','adjudicated')),
          winning_excerpt_ids JSONB NOT NULL,
          rationale TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(conflict_key, verifier_run_id)
        )
        """
    )
    op.execute(
        """
        ALTER TABLE app.plans
          ADD CONSTRAINT fk_plans_trigger_verifier_run
          FOREIGN KEY (trigger_verifier_run) REFERENCES app.verifier_runs(id)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.plans DROP CONSTRAINT fk_plans_trigger_verifier_run")
    op.execute("DROP TABLE app.conflict_resolutions")
    op.execute("DROP TABLE app.verifier_runs")
