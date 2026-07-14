"""Planner-Worker: frozen briefs, plans, tasks, evidence, decisions, events, usage."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260714_0002_pw"
down_revision: str | None = "20260714_0001_m0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.jobs
          ADD COLUMN IF NOT EXISTS effort TEXT,
          ADD COLUMN IF NOT EXISTS brief_id UUID,
          ADD COLUMN IF NOT EXISTS outcome TEXT,
          ADD COLUMN IF NOT EXISTS error_code TEXT,
          ADD COLUMN IF NOT EXISTS thread_id TEXT
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.briefs (
          id UUID PRIMARY KEY,
          job_id UUID NOT NULL UNIQUE REFERENCES app.jobs(id) ON DELETE CASCADE,
          question TEXT NOT NULL,
          brief_text TEXT NOT NULL,
          output_format TEXT NOT NULL,
          language TEXT NOT NULL,
          effort TEXT NOT NULL CHECK (effort IN ('quick', 'standard', 'deep')),
          frozen_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.tasks (
          id UUID PRIMARY KEY,
          job_id UUID NOT NULL REFERENCES app.jobs(id) ON DELETE CASCADE,
          question TEXT NOT NULL,
          research_stage TEXT NOT NULL CHECK (research_stage IN ('scout','deep_dive','verify')),
          research_mode TEXT NOT NULL,
          source_policy JSONB NOT NULL,
          allowed_tools JSONB NOT NULL,
          expected_evidence TEXT NOT NULL,
          depends_on JSONB NOT NULL,
          budget JSONB NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed','skipped')),
          stop_reason TEXT,
          gap_note TEXT,
          worker_summary JSONB,
          tool_calls_used INTEGER NOT NULL DEFAULT 0,
          error TEXT,
          created_at TIMESTAMPTZ NOT NULL,
          started_at TIMESTAMPTZ,
          finished_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_tasks_job_status ON app.tasks(job_id, status)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.plans (
          id UUID PRIMARY KEY,
          job_id UUID NOT NULL REFERENCES app.jobs(id) ON DELETE CASCADE,
          version INTEGER NOT NULL CHECK (version >= 1),
          decision_round INTEGER NOT NULL CHECK (decision_round >= 1),
          trigger_verifier_run UUID,
          task_ids JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          UNIQUE(job_id, version),
          UNIQUE(job_id, decision_round)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.documents (
          id UUID PRIMARY KEY,
          workspace_id UUID NOT NULL,
          source_ref JSONB NOT NULL,
          source_uri TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          version INTEGER NOT NULL CHECK (version >= 1),
          retrieved_at TIMESTAMPTZ NOT NULL,
          media_type TEXT NOT NULL,
          storage_ref TEXT NOT NULL,
          index_ref TEXT,
          source_meta JSONB NOT NULL,
          UNIQUE(workspace_id, source_uri, content_hash),
          UNIQUE(workspace_id, source_uri, version)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.excerpts (
          id UUID PRIMARY KEY,
          job_id UUID NOT NULL REFERENCES app.jobs(id) ON DELETE CASCADE,
          doc_id UUID NOT NULL REFERENCES app.documents(id),
          doc_version INTEGER NOT NULL,
          text TEXT NOT NULL,
          locator JSONB NOT NULL,
          excerpt_hash TEXT NOT NULL,
          extracted_by JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          UNIQUE(job_id, doc_id, excerpt_hash)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_excerpts_job ON app.excerpts(job_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.assertions (
          id UUID PRIMARY KEY,
          job_id UUID NOT NULL REFERENCES app.jobs(id) ON DELETE CASCADE,
          task_id UUID NOT NULL REFERENCES app.tasks(id) ON DELETE CASCADE,
          statement TEXT NOT NULL,
          statement_hash TEXT NOT NULL,
          excerpt_ids JSONB NOT NULL,
          topic_tags JSONB NOT NULL,
          produced_by JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          UNIQUE(job_id, task_id, statement_hash)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_assertions_task ON app.assertions(task_id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.decision_log (
          id BIGSERIAL PRIMARY KEY,
          job_id UUID NOT NULL REFERENCES app.jobs(id) ON DELETE CASCADE,
          decision_round INTEGER NOT NULL CHECK (decision_round >= 1),
          full_prompt JSONB NOT NULL,
          raw_output JSONB,
          decision_type TEXT,
          decision_payload JSONB,
          feedback TEXT,
          status TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          completed_at TIMESTAMPTZ,
          UNIQUE(job_id, decision_round)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.events (
          id BIGSERIAL PRIMARY KEY,
          job_id UUID NOT NULL REFERENCES app.jobs(id) ON DELETE CASCADE,
          event_type TEXT NOT NULL,
          task_id UUID,
          decision_round INTEGER,
          payload JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_events_job_id ON app.events(job_id, id)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app.usage (
          id BIGSERIAL PRIMARY KEY,
          job_id UUID NOT NULL REFERENCES app.jobs(id) ON DELETE CASCADE,
          task_id UUID,
          component TEXT NOT NULL,
          model TEXT,
          input_tokens INTEGER,
          output_tokens INTEGER,
          tool_calls INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          ALTER TABLE app.jobs
            ADD CONSTRAINT fk_jobs_brief FOREIGN KEY (brief_id) REFERENCES app.briefs(id);
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.jobs DROP CONSTRAINT IF EXISTS fk_jobs_brief")
    for table in (
        "usage",
        "events",
        "decision_log",
        "assertions",
        "excerpts",
        "documents",
        "plans",
        "tasks",
        "briefs",
    ):
        op.execute(f"DROP TABLE IF EXISTS app.{table}")
    op.execute(
        """
        ALTER TABLE app.jobs
          DROP COLUMN IF EXISTS effort,
          DROP COLUMN IF EXISTS brief_id,
          DROP COLUMN IF EXISTS outcome,
          DROP COLUMN IF EXISTS error_code,
          DROP COLUMN IF EXISTS thread_id
        """
    )
