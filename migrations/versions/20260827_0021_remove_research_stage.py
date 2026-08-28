"""Remove the Planner-authored research stage resource profile."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260827_0021_no_stage"
down_revision: str | None = "20260827_0020_verifier_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM app.jobs
            WHERE status IN ('queued','running','cancelling')
          ) THEN
            RAISE EXCEPTION
              'finish or cancel active jobs before removing research_stage';
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        UPDATE app.decision_log
        SET decision_payload = jsonb_set(
          decision_payload,
          '{dispatch,tasks}',
          (
            SELECT COALESCE(jsonb_agg(task_item - 'research_stage'), '[]'::jsonb)
            FROM jsonb_array_elements(decision_payload->'dispatch'->'tasks')
              AS items(task_item)
          ),
          false
        )
        WHERE jsonb_typeof(decision_payload->'dispatch'->'tasks') = 'array'
        """
    )
    op.execute(
        """
        UPDATE app.decision_log
        SET decision_payload = jsonb_set(
          decision_payload,
          '{dispatch}',
          (decision_payload->'dispatch') - 'research_stage',
          false
        )
        WHERE jsonb_typeof(decision_payload->'dispatch') = 'object'
        """
    )
    op.execute(
        """
        UPDATE app.decision_log
        SET decision_payload = jsonb_set(
          decision_payload,
          '{tasks}',
          (
            SELECT COALESCE(jsonb_agg(task_item - 'research_stage'), '[]'::jsonb)
            FROM jsonb_array_elements(decision_payload->'tasks') AS items(task_item)
          ),
          false
        )
        WHERE jsonb_typeof(decision_payload->'tasks') = 'array'
        """
    )
    op.execute(
        """
        UPDATE app.decision_log
        SET decision_payload = decision_payload - 'research_stage'
        WHERE decision_payload ? 'research_stage'
        """
    )
    op.execute("ALTER TABLE app.tasks DROP COLUMN research_stage")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM app.tasks)
             OR EXISTS (SELECT 1 FROM app.decision_log) THEN
            RAISE EXCEPTION
              'cannot restore research_stage after tasks or Planner decisions have existed';
          END IF;
        END
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE app.tasks
          ADD COLUMN research_stage TEXT NOT NULL
          CHECK (research_stage IN ('scout','deep_dive','verify'))
        """
    )
