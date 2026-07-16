"""Read-only live replay of Research Verifier against one persisted research Job."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from prospector.agents.llm import LlmNotConfiguredError, require_llm_settings
from prospector.agents.research_verifier import OpenAIResearchVerifier
from prospector.schemas.verifier import validate_verifier_references
from prospector.store.repositories import ResearchRepository

pytestmark = [pytest.mark.integration, pytest.mark.live]

JOB_ID = UUID("483bf0f5-d516-4ce0-b03d-7a7eb7101751")
DECISION_ROUND = 6
DECISION_ROUND_LIMIT = 12


def _persistence_fingerprint(repository: ResearchRepository) -> dict[str, Any]:
    """Capture every Verifier-owned persistent surface this replay must not mutate."""
    with repository.engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT j.status, j.outcome, j.error_code,
                           (SELECT COUNT(*) FROM app.events e
                            WHERE e.job_id=j.id) AS event_count,
                           (SELECT COUNT(*) FROM app.verifier_runs vr
                            WHERE vr.job_id=j.id) AS verifier_run_count,
                           (SELECT COUNT(*)
                            FROM app.conflict_resolutions cr
                            JOIN app.verifier_runs vr ON vr.id=cr.verifier_run_id
                            WHERE vr.job_id=j.id) AS conflict_resolution_count
                    FROM app.jobs j WHERE j.id=:job_id
                    """
                ),
                {"job_id": JOB_ID},
            )
            .mappings()
            .first()
        )
    if row is None:
        pytest.skip(f"persisted replay Job is unavailable: {JOB_ID}")
    return dict(row)


def test_replay_persisted_job_through_research_verifier_without_writes() -> None:
    """Reuse frozen evidence, call the real Verifier, and leave the Job untouched."""
    try:
        require_llm_settings()
    except LlmNotConfiguredError as exc:
        pytest.skip(str(exc))

    repository = ResearchRepository()
    before = _persistence_fingerprint(repository)
    snapshot = repository.build_verifier_snapshot(
        JOB_ID,
        trigger="planner_finish",
        decision_round=DECISION_ROUND,
        decision_round_limit=DECISION_ROUND_LIMIT,
    )

    assert snapshot["plans"][-1]["version"] == 5
    assert snapshot["planner_exit"]["decision_rounds_remaining"] == 6
    assert len(snapshot["assertions"]) == 132
    assert len(snapshot["excerpts"]) == 60
    assert all("publisher" not in excerpt and "author" in excerpt for excerpt in snapshot["excerpts"])

    result = OpenAIResearchVerifier().verify(snapshot)
    decision = result.decision
    validate_verifier_references(
        decision,
        task_ids={UUID(str(row["task_id"])) for row in snapshot["tasks"]},
        assertion_ids={UUID(str(row["assertion_id"])) for row in snapshot["assertions"]},
        excerpt_ids={UUID(str(row["excerpt_id"])) for row in snapshot["excerpts"]},
    )

    print(decision.model_dump_json(indent=2))
    assert _persistence_fingerprint(repository) == before
