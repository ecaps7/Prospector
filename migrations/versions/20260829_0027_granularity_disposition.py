"""Let the Verifier record a granularity problem without destroying the evidence.

A merged Assertion -- several separately checkable facts in one row -- was disqualified
like a fabricated one. Across this project's Jobs that accounted for 122 of 187
disqualifications, all of them statements the bound Excerpt supported; one Job lost 47 of
its 51 disqualified Assertions to it and wrote its report on 41 rows instead of 88.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260829_0027_granularity"
down_revision: str | None = "20260829_0026_readthrough"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.assertion_dispositions "
        "DROP CONSTRAINT IF EXISTS assertion_dispositions_status_check"
    )
    op.execute(
        """
        ALTER TABLE app.assertion_dispositions
          ADD CONSTRAINT assertion_dispositions_status_check
          CHECK (status IN ('unusable','granularity','restored'))
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM app.assertion_dispositions WHERE status='granularity'")
    op.execute(
        "ALTER TABLE app.assertion_dispositions "
        "DROP CONSTRAINT IF EXISTS assertion_dispositions_status_check"
    )
    op.execute(
        """
        ALTER TABLE app.assertion_dispositions
          ADD CONSTRAINT assertion_dispositions_status_check
          CHECK (status IN ('unusable','restored'))
        """
    )
