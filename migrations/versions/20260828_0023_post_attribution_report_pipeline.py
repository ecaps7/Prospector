# ruff: noqa: E501
"""Add append-only storage for the post-attribution report pipeline.

Legacy report tables intentionally remain untouched as immutable local history.  The
runtime switches exclusively to these v2 tables; deleting historical reports requires
separate user authorization because it is not reversible.

Every table here is per-Job data, so every foreign key cascades -- including the ones
pointing at sibling run tables.  Without that, deleting a Job fails on a half-cascaded
tree instead of removing it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260828_0023_post_attr_report"
down_revision: str | None = "20260827_0022_rv_retries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE app.research_synthesis_runs (
          id UUID PRIMARY KEY, job_id UUID NOT NULL REFERENCES app.jobs(id) ON DELETE CASCADE,
          version INTEGER NOT NULL CHECK (version >= 1), full_prompt JSONB NOT NULL,
          decision TEXT CHECK (decision IN ('ready','needs_research')), synthesis TEXT,
          falsification TEXT, assertion_ids JSONB, material_conflict_keys JSONB,
          reason TEXT, evidence_needed TEXT, raw_output JSONB, contract_error TEXT,
          status TEXT NOT NULL CHECK (status IN ('prompted','completed','failed')),
          created_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ,
          UNIQUE (job_id, version)
        )
    """)
    op.execute("""
        CREATE TABLE app.report_runs_v2 (
          id UUID PRIMARY KEY, job_id UUID NOT NULL UNIQUE REFERENCES app.jobs(id) ON DELETE CASCADE,
          verifier_run_id UUID NOT NULL REFERENCES app.verifier_runs(id) ON DELETE CASCADE, current_revision INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL CHECK (status IN ('writing','attributing','reviewing','revising','verified','partial','failed','report_rendered')),
          verification_status TEXT CHECK (verification_status IN ('verified','partial','failed')),
          markdown_ref TEXT, markdown_hash TEXT, json_ref TEXT, json_hash TEXT,
          created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE app.report_revisions_v2 (
          report_id UUID NOT NULL REFERENCES app.report_runs_v2(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL CHECK (revision >= 1),
          synthesis_run_id UUID NOT NULL REFERENCES app.research_synthesis_runs(id) ON DELETE CASCADE,
          full_prompt JSONB NOT NULL, raw_output JSONB, markdown TEXT, markdown_hash TEXT,
          parsed_blocks JSONB, status TEXT NOT NULL CHECK (status IN ('prompted','generated','attributed','reviewed','rendered','failed')),
          created_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ, PRIMARY KEY (report_id, revision)
        )
    """)
    for table, extra in (
        ("attribution_runs_v2", ""),
        (
            "report_review_runs_v2",
            ", synthesis_run_id UUID NOT NULL REFERENCES app.research_synthesis_runs(id) ON DELETE CASCADE",
        ),
    ):
        op.execute(f"""
            CREATE TABLE app.{table} (
              id UUID PRIMARY KEY, report_id UUID NOT NULL, revision INTEGER NOT NULL,
              full_prompt JSONB NOT NULL, result JSONB, contract_error TEXT,
              status TEXT NOT NULL CHECK (status IN ('prompted','completed','failed')),
              created_at TIMESTAMPTZ NOT NULL, completed_at TIMESTAMPTZ{extra},
              UNIQUE (report_id, revision), FOREIGN KEY (report_id, revision)
                REFERENCES app.report_revisions_v2(report_id, revision) ON DELETE CASCADE
            )
        """)


def downgrade() -> None:
    op.execute("DROP TABLE app.report_review_runs_v2")
    op.execute("DROP TABLE app.attribution_runs_v2")
    op.execute("DROP TABLE app.report_revisions_v2")
    op.execute("DROP TABLE app.report_runs_v2")
    op.execute("DROP TABLE app.research_synthesis_runs")
