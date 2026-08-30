from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from prospector.api.app import ApiServices, create_app
from prospector.api.scheduler import JobScheduler
from prospector.config import clear_settings_cache
from prospector.schemas.brief import ResearchBrief, ScopeOutcome
from prospector.schemas.report import ResearchSynthesisRun
from prospector.store.checkpoint import close_pool, setup_checkpointer
from prospector.store.database import clear_engine_cache
from prospector.store.object_store import ObjectStore
from prospector.store.repositories.jobs import JobRepository
from prospector.store.repositories.research import ResearchRepository

pytestmark = pytest.mark.integration
REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _environment() -> Iterator[None]:
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())
    os.environ.setdefault(
        "DATABASE_URL", "postgresql://prospector:prospector@localhost:5432/prospector"
    )
    os.environ.setdefault("S3_ENDPOINT", "http://localhost:9000")
    os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
    os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
    os.environ.setdefault("S3_BUCKET", "prospector")
    clear_settings_cache()
    clear_engine_cache()
    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
    setup_checkpointer()
    ObjectStore().ensure_bucket()
    yield
    close_pool()
    clear_engine_cache()
    clear_settings_cache()


def test_atomic_fifo_lifecycle_and_stopped_event_idempotency() -> None:
    repository = JobRepository()
    brief = ResearchBrief(
        question="API integration",
        brief_text="Verify atomic API job persistence and its terminal event.",
    )
    created_ids: list[UUID] = []
    try:
        first = repository.create_with_brief(brief, start_immediately=True)
        second = repository.create_with_brief(brief, start_immediately=False)
        created_ids = [first["job_id"], second["job_id"]]

        assert first["status"] == "running"
        assert first["queue_position"] is None
        assert second["status"] == "queued"
        assert int(second["queue_position"]) >= 1

        recovered = repository.recoverable_jobs()
        recovered_ids = [row["job_id"] for row in recovered]
        assert first["job_id"] in recovered_ids
        assert second["job_id"] in recovered_ids
        assert recovered_ids.index(first["job_id"]) < recovered_ids.index(second["job_id"])

        repository.mark_running(second["job_id"])
        repository.finalize_success(
            first["job_id"],
            {"phase": "draft_rendered", "outcome": "draft_rendered"},
        )
        repository.finalize_success(
            first["job_id"],
            {"phase": "draft_rendered", "outcome": "draft_rendered"},
        )
        repository.finalize_failure(second["job_id"], fallback_error_code="job_execution_error")

        first_view = repository.get_job(first["job_id"])
        second_view = repository.get_job(second["job_id"])
        assert first_view is not None and first_view["status"] == "completed"
        assert first_view["phase"] == "draft_rendered"
        assert second_view is not None and second_view["status"] == "failed"
        assert second_view["error_code"] == "job_execution_error"

        first_events = repository.list_events_after(first["job_id"], 0)
        stopped = [event for event in first_events if event["event_type"] == "job.stopped"]
        assert len(stopped) == 1
        assert stopped[0]["payload"]["status"] == "completed"
        assert repository.stopped_event_id(first["job_id"]) == stopped[0]["id"]
        repository.health_check()
    finally:
        if created_ids:
            with repository.engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM app.jobs WHERE id = ANY(:job_ids)"),
                    {"job_ids": created_ids},
                )


def test_interrupted_job_without_stopped_event_finalizes_success_after_resume() -> None:
    """The resume contract: an interruption never writes job.stopped.

    A contract error leaves the row 'failed' via set_research_outcome but keeps the
    checkpoint resumable; when the resumed run later renders the report, finalize_success
    must win over the stale 'failed' row and write the one and only job.stopped.
    """
    jobs = JobRepository()
    research = ResearchRepository(engine=jobs.engine)
    brief = ResearchBrief(
        question="Resume semantics",
        brief_text="Interrupt a job, then finalize the resumed run as completed.",
    )
    created = jobs.create_with_brief(brief, start_immediately=True)
    job_id = created["job_id"]
    try:
        research.set_research_outcome(
            job_id,
            outcome="failed",
            error_code="writer_contract_error",
            phase="failed",
        )
        interrupted_view = jobs.get_job(job_id)
        assert interrupted_view is not None
        assert interrupted_view["status"] == "failed"
        # A failed row needs a fix before re-entry, so it waits for an explicit
        # `job resume` instead of the scheduler's automatic crash recovery.
        assert job_id not in {row["job_id"] for row in jobs.recoverable_jobs()}

        jobs.finalize_success(job_id, {"phase": "draft_rendered", "outcome": "draft_rendered"})

        view = jobs.get_job(job_id)
        assert view is not None
        assert view["status"] == "completed"
        assert view["outcome"] == "draft_rendered"
        assert view["error_code"] is None
        stopped = [
            event
            for event in jobs.list_events_after(job_id, 0)
            if event["event_type"] == "job.stopped"
        ]
        assert len(stopped) == 1
        assert stopped[0]["payload"]["status"] == "completed"
    finally:
        with jobs.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_scope_job_and_sse_http_flow_uses_persisted_events() -> None:
    repository = JobRepository()
    brief = ResearchBrief(
        question="API HTTP integration",
        brief_text="Run the HTTP contract through the real PostgreSQL event ledger.",
    )

    def run_job(_job_id: UUID, _brief_id: UUID) -> dict[str, str]:
        return {"phase": "draft_rendered", "outcome": "draft_rendered"}

    scheduler = JobScheduler(repository, run_job, recover_on_start=False)
    services = ApiServices(
        repository=repository,
        object_store=ObjectStore(),
        scheduler=scheduler,
        scope=lambda *_args, **_kwargs: ScopeOutcome(kind="brief_pending", brief=brief),
        revise=lambda *_args, **_kwargs: brief,
        require_llm=lambda: None,
    )
    job_id: UUID | None = None
    try:
        with TestClient(create_app(services)) as client:
            scope_response = client.post("/api/scope", json={"question": "Research this"})
            assert scope_response.status_code == 200
            assert scope_response.json()["brief"]["question"] == brief.question

            created = client.post("/api/jobs", json={"brief": brief.model_dump(mode="json")})
            assert created.status_code == 201
            job_id = UUID(created.json()["job_id"])
            assert created.json()["status"] == "running"

            events = client.get(f"/api/jobs/{job_id}/events")
            assert events.status_code == 200
            assert "event: brief.confirmed" in events.text
            assert "event: job.stopped" in events.text

            detail = client.get(f"/api/jobs/{job_id}")
            assert detail.status_code == 200
            assert detail.json()["status"] == "completed"
            assert detail.json()["phase"] == "draft_rendered"
    finally:
        if job_id is not None:
            with repository.engine.begin() as conn:
                conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_job_detail_aggregates_persisted_model_and_tool_usage() -> None:
    jobs = JobRepository()
    research = ResearchRepository(engine=jobs.engine)
    created = jobs.create_with_brief(
        ResearchBrief(question="Usage integration", brief_text="Verify persisted usage."),
        start_immediately=True,
    )
    job_id = created["job_id"]
    task_id = UUID("00000000-0000-0000-0000-000000000123")
    try:
        research.record_usage(
            job_id,
            component="planner",
            model="strong-model",
            input_tokens=120,
            output_tokens=30,
        )
        research.record_tool_used(
            job_id,
            task_id,
            {
                "tool": "web_search",
                "tool_call_id": "usage-test-call",
                "result_count": 1,
            },
        )

        detail = jobs.get_job(job_id)
        assert detail is not None
        assert detail["latest_event_id"] == jobs.list_events_after(job_id, 0)[-1]["id"]
        by_component = {row["component"]: row for row in detail["usage"]}
        assert by_component["planner"]["input_tokens"] == 120
        assert by_component["planner"]["output_tokens"] == 30
        assert by_component["research_worker_tools"]["tool_calls"] == 1
    finally:
        with jobs.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_job_detail_orders_tasks_by_first_plan_appearance() -> None:
    jobs = JobRepository()
    created = jobs.create_with_brief(
        ResearchBrief(question="Task order integration", brief_text="Verify plan task order."),
        start_immediately=True,
    )
    job_id = created["job_id"]
    task_ids = [
        UUID("00000000-0000-0000-0000-000000000301"),
        UUID("00000000-0000-0000-0000-000000000302"),
        UUID("00000000-0000-0000-0000-000000000303"),
    ]
    plan_order = [task_ids[2], task_ids[0], task_ids[1]]
    try:
        with jobs.engine.begin() as conn:
            for task_id in task_ids:
                conn.execute(
                    text(
                        """
                        INSERT INTO app.tasks
                          (id, job_id, question, allowed_tools,
                           expected_evidence, depends_on, budget, status, created_at)
                        VALUES
                          (:id, :job_id, :question, '["web_search"]'::jsonb,
                           'One exact source excerpt',
                           '[]'::jsonb, '{"max_worker_rounds": 2}'::jsonb, 'pending',
                           '2026-08-26T12:00:00Z'::timestamptz)
                        """
                    ),
                    {
                        "id": task_id,
                        "job_id": job_id,
                        "question": f"Task order question {task_id}",
                    },
                )
            conn.execute(
                text(
                    """
                    INSERT INTO app.plans
                      (id, job_id, version, decision_round, task_ids, created_at)
                    VALUES
                      (:id, :job_id, 1, 1, CAST(:task_ids AS JSONB),
                       '2026-08-26T12:00:00Z'::timestamptz)
                    """
                ),
                {
                    "id": UUID("00000000-0000-0000-0000-000000000399"),
                    "job_id": job_id,
                    "task_ids": str([str(task_id) for task_id in plan_order]).replace("'", '"'),
                },
            )

        detail = jobs.get_job(job_id)
        assert detail is not None
        assert [task["task_id"] for task in detail["tasks"]] == plan_order
    finally:
        with jobs.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_queued_and_running_jobs_reach_idempotent_cancelled_terminal_state() -> None:
    repository = JobRepository()
    brief = ResearchBrief(question="Cancel integration", brief_text="Cancel this Job.")
    queued = repository.create_with_brief(brief, start_immediately=False)
    running = repository.create_with_brief(brief, start_immediately=True)
    job_ids = [queued["job_id"], running["job_id"]]
    pending_task_id = UUID("00000000-0000-0000-0000-000000000201")
    running_task_id = UUID("00000000-0000-0000-0000-000000000202")
    try:
        with repository.engine.begin() as conn:
            for task_id, status in (
                (pending_task_id, "pending"),
                (running_task_id, "running"),
            ):
                conn.execute(
                    text(
                        """
                        INSERT INTO app.tasks
                          (id, job_id, question, allowed_tools,
                           expected_evidence, depends_on, budget, status, created_at, started_at)
                        VALUES
                          (:id, :job_id, :question, '["web_search"]'::jsonb,
                           :expected_evidence,
                           '[]'::jsonb, '{"max_worker_rounds": 2}'::jsonb,
                           :status, NOW(), CASE WHEN :status='running' THEN NOW() ELSE NULL END)
                        """
                    ),
                    {
                        "id": task_id,
                        "job_id": running["job_id"],
                        "question": f"Cancellation integration task {status}",
                        "expected_evidence": "One exact source excerpt",
                        "status": status,
                    },
                )

        assert repository.request_cancel(queued["job_id"], requested_via="cli") == "cancelled"
        assert (
            repository.request_cancel(running["job_id"], requested_via="web_monitor")
            == "cancelling"
        )
        repository.finalize_cancelled(running["job_id"])
        repository.finalize_cancelled(running["job_id"])

        for job_id in job_ids:
            detail = repository.get_job(job_id)
            assert detail is not None
            assert detail["status"] == "cancelled"
            assert detail["phase"] == "cancelled"
            stopped = [
                event
                for event in repository.list_events_after(job_id, 0)
                if event["event_type"] == "job.stopped"
            ]
            assert len(stopped) == 1
            assert stopped[0]["payload"]["status"] == "cancelled"
        cancelling = [
            event
            for event in repository.list_events_after(running["job_id"], 0)
            if event["event_type"] == "job.phase_changed"
            and event["payload"].get("phase") == "cancelling"
        ]
        assert cancelling[0]["payload"]["requested_via"] == "web_monitor"
        running_detail = repository.get_job(running["job_id"])
        assert running_detail is not None
        assert {task["status"] for task in running_detail["tasks"]} == {"cancelled"}
        assert {task["stop_reason"] for task in running_detail["tasks"]} == {"job_cancelled"}
        assert all(task["finished_at"] is not None for task in running_detail["tasks"])
        assert not any(row["job_id"] in job_ids for row in repository.recoverable_jobs())
    finally:
        with repository.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM app.jobs WHERE id = ANY(:job_ids)"),
                {"job_ids": job_ids},
            )


def test_unaccepted_verifier_run_refreezes_its_prompt_instead_of_stranding_the_job() -> None:
    """Editing the Verifier prompt must not permanently strand Jobs that stopped mid-run.

    The guard only ever sees runs that never completed -- the graph short-circuits on a
    completed one -- so there is no accepted answer whose provenance it could protect here.
    """
    jobs = JobRepository()
    research = ResearchRepository()
    brief = ResearchBrief(
        question="Verifier replay integration",
        brief_text="Re-enter a stopped Verifier run after its prompt changed.",
    )
    created = jobs.create_with_brief(brief, start_immediately=True)
    job_id = created["job_id"]
    try:
        old_prompt = [{"role": "system", "content": "旧版 Verifier 提示词"}]
        new_prompt = [{"role": "system", "content": "新版 Verifier 提示词"}]
        run_id = research.begin_verifier_run(
            job_id,
            evaluated_plan_version=1,
            decision_round=1,
            trigger="planner_finish",
            full_prompt=old_prompt,
        )
        research.fail_verifier_run(
            job_id, run_id, raw_output={"content": "被拒的原话"}, error="invalid decision"
        )

        replayed = research.begin_verifier_run(
            job_id,
            evaluated_plan_version=1,
            decision_round=1,
            trigger="planner_finish",
            full_prompt=new_prompt,
        )

        assert replayed == run_id
        with research.engine.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT full_prompt, raw_output FROM app.verifier_runs WHERE id=:id"),
                    {"id": run_id},
                )
                .mappings()
                .one()
            )
        assert row["full_prompt"] == new_prompt
        assert row["raw_output"] == {"content": "被拒的原话"}
    finally:
        with jobs.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_rendered_report_finalizes_as_completed_and_surfaces_its_verdict() -> None:
    """The v2 pipeline's terminal phase must read as success, verdict and all.

    Before this was wired, ``finalize_success`` only recognised the pre-refactor phase
    name, so every successful run of the current pipeline was written to the Job row as
    a failure.
    """
    jobs = JobRepository()
    research = ResearchRepository(engine=jobs.engine)
    created = jobs.create_with_brief(
        ResearchBrief(question="Render integration", brief_text="Verify rendered finalization."),
        start_immediately=True,
    )
    job_id = created["job_id"]
    try:
        verifier_run_id = research.begin_verifier_run(
            job_id,
            evaluated_plan_version=1,
            decision_round=1,
            trigger="planner_finish",
            full_prompt=[{"role": "system", "content": "verify"}],
        )
        synthesis_run_id, synthesis_version = research.begin_synthesis_run(
            job_id, [{"role": "system", "content": "synthesize"}], verifier_run_id
        )
        synthesis_raw = {
            "draft": '{"decision":"ready"}',
            "review_prompt": [{"role": "system", "content": "review"}],
            "review": '{"decision":"accept"}',
        }
        research.complete_synthesis_run(
            synthesis_run_id,
            ResearchSynthesisRun(
                synthesis_run_id=synthesis_run_id,
                job_id=job_id,
                version=synthesis_version,
                verifier_run_id=verifier_run_id,
                decision="ready",
                synthesis="材料已经能够回应问题。",
            ),
            synthesis_raw,
        )
        persisted_synthesis = research.get_latest_synthesis_run(job_id)
        assert persisted_synthesis is not None
        assert persisted_synthesis.raw_output == synthesis_raw
        synthesis_events = [
            event
            for event in jobs.list_events_after(job_id, 0)
            if event["event_type"] == "synthesis.completed"
        ]
        assert len(synthesis_events) == 1
        assert synthesis_events[0]["payload"] == {
            "synthesis_run_id": str(synthesis_run_id),
            "decision": "ready",
            "synthesis": "材料已经能够回应问题。",
        }
        report_id, revision = research.begin_markdown_revision(
            job_id,
            verifier_run_id,
            synthesis_run_id,
            [{"role": "system", "content": "write"}],
        )
        research.set_v2_report_status(report_id, "partial")
        research.complete_v2_report_render(
            job_id,
            report_id,
            revision=revision,
            markdown_ref="s3://reports/report.md",
            markdown_hash="sha256:" + "0" * 64,
            json_ref="s3://reports/report.json",
            json_hash="sha256:" + "1" * 64,
            verification_status="partial",
        )

        jobs.finalize_success(job_id, {"phase": "report_rendered", "outcome": "report_rendered"})
        detail = jobs.get_job(job_id)
        assert detail is not None
        assert detail["status"] == "completed"
        assert detail["outcome"] == "report_rendered"
        assert detail["error_code"] is None
        # An unqualified report is still delivered; the verdict rides on the report row.
        assert detail["report"]["report_id"] == report_id
        assert detail["report"]["verification_status"] == "partial"
        assert detail["report"]["markdown_ref"] == "s3://reports/report.md"
        # 'outcome' only says a report was delivered, so the verdict is lifted to the top
        # level as well: the Jobs list has no report object to read it from.
        assert detail["verification_status"] == "partial"
        listed = next(row for row in jobs.list_jobs() if row["job_id"] == job_id)
        assert listed["verification_status"] == "partial"
        # The refs are what GET /jobs/{id}/report resolves.  They used to be read from
        # the pre-refactor table alone, so every Job the current pipeline delivered
        # answered 409 report_not_ready with its report sitting in object storage.
        assert jobs.report_ref(job_id, "md") == "s3://reports/report.md"
        assert jobs.report_ref(job_id, "json") == "s3://reports/report.json"
        delivery = [
            event
            for event in jobs.list_events_after(job_id, 0)
            if event["event_type"] == "report.draft_rendered"
        ]
        assert delivery and delivery[-1]["payload"]["verification_status"] == "partial"
    finally:
        with jobs.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})


def test_attribution_batches_remain_after_a_later_batch_fails() -> None:
    jobs = JobRepository()
    research = ResearchRepository(engine=jobs.engine)
    created = jobs.create_with_brief(
        ResearchBrief(question="Attribution batches", brief_text="Persist batch recovery."),
        start_immediately=True,
    )
    job_id = created["job_id"]
    try:
        verifier_run_id = research.begin_verifier_run(
            job_id,
            evaluated_plan_version=1,
            decision_round=1,
            trigger="planner_finish",
            full_prompt=[{"role": "system", "content": "verify"}],
        )
        synthesis_run_id, synthesis_version = research.begin_synthesis_run(
            job_id, [{"role": "system", "content": "synthesize"}], verifier_run_id
        )
        research.complete_synthesis_run(
            synthesis_run_id,
            ResearchSynthesisRun(
                synthesis_run_id=synthesis_run_id,
                job_id=job_id,
                version=synthesis_version,
                verifier_run_id=verifier_run_id,
                decision="ready",
                synthesis="材料已经能够回应问题。",
            ),
            {"draft": "{}", "review": "{}"},
        )
        report_id, revision = research.begin_markdown_revision(
            job_id,
            verifier_run_id,
            synthesis_run_id,
            [{"role": "system", "content": "write"}],
        )
        run_id = research.begin_attribution_run(
            report_id, revision, {"batches": [{"batch_index": 0}, {"batch_index": 1}]}
        )
        research.begin_attribution_batch(
            run_id,
            0,
            block_ids=["b_0001"],
            candidate_refs=["k1"],
            selection_prompt=[{"role": "user", "content": "select-0"}],
        )
        research.save_attribution_batch_selection(
            run_id,
            0,
            raw_output="selected-0",
            result={"assertion_ids": []},
            verify_prompt=[{"role": "user", "content": "verify-0"}],
        )
        research.complete_attribution_batch(
            run_id, 0, raw_output="verified-0", result={"claims": []}
        )
        research.begin_attribution_batch(
            run_id,
            1,
            block_ids=["b_0002"],
            candidate_refs=["k2"],
            selection_prompt=[{"role": "user", "content": "select-1"}],
        )
        research.fail_attribution_batch(
            run_id, 1, raw_output="bad-1", error="omitted candidates: ['k2']"
        )
        research.fail_attribution_run(
            run_id, raw_output="bad-1", error="omitted candidates: ['k2']"
        )

        attempt = research.get_attribution_attempt(report_id, revision)
        assert attempt is not None
        assert attempt["status"] == "failed"
        assert "omitted candidates" in str(attempt["contract_error"])
        batches = research.list_attribution_batches(run_id)
        assert [row["status"] for row in batches] == ["completed", "failed"]
        assert research.get_attribution_run(report_id, revision) is None

        resumed = research.begin_attribution_run(report_id, revision, {"batches": []})
        assert resumed == run_id
        resumed_attempt = research.get_attribution_attempt(report_id, revision)
        assert resumed_attempt is not None
        assert resumed_attempt["status"] == "prompted"
        assert [row["status"] for row in research.list_attribution_batches(run_id)] == [
            "completed",
            "failed",
        ]
    finally:
        with jobs.engine.begin() as conn:
            conn.execute(text("DELETE FROM app.jobs WHERE id=:job_id"), {"job_id": job_id})
