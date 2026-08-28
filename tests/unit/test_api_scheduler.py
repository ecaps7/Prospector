from __future__ import annotations

import asyncio
import threading
from typing import Any
from uuid import UUID, uuid4

import pytest

from prospector.api.scheduler import JobScheduler
from prospector.flow.research_graph import VerifierMajorGapError
from prospector.schemas.brief import ResearchBrief


class FakeJobRepository:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.recovered: list[dict[str, Any]] = []
        self.running: list[UUID] = []
        self.queued: list[UUID] = []
        self.completed: list[UUID] = []
        self.failed: list[tuple[UUID, str]] = []
        self.cancelled: list[UUID] = []
        self.cancel_requests: dict[UUID, str] = {}
        self.cancel_finalization_attempted = threading.Event()
        self.fail_cancel_finalization_once = False

    def finalize_pending_cancellations(self) -> None:
        pass

    def cancel_requested(self, job_id: UUID) -> bool:
        return self.cancel_requests.get(job_id) in {"cancelling", "cancelled"}

    def request_cancel(self, job_id: UUID, *, requested_via: str) -> str | None:
        assert requested_via in {"web_monitor", "cli"}
        if not any(item["job_id"] == job_id for item in self.created):
            return None
        self.cancel_requests[job_id] = "cancelling"
        return "cancelling"

    def finalize_cancelled(self, job_id: UUID) -> None:
        self.cancel_finalization_attempted.set()
        if self.fail_cancel_finalization_once:
            self.fail_cancel_finalization_once = False
            raise RuntimeError("cancel finalization failed")
        self.cancel_requests[job_id] = "cancelled"
        if job_id not in self.cancelled:
            self.cancelled.append(job_id)

    def recoverable_jobs(self) -> list[dict[str, Any]]:
        return self.recovered

    def create_with_brief(
        self, _brief: ResearchBrief, *, start_immediately: bool
    ) -> dict[str, Any]:
        job_id = uuid4()
        item = {
            "job_id": job_id,
            "brief_id": uuid4(),
            "status": "running" if start_immediately else "queued",
            "queue_position": None if start_immediately else len(self.created),
        }
        self.created.append(item)
        return item

    def mark_running(self, job_id: UUID) -> None:
        self.running.append(job_id)

    def mark_queued(self, job_id: UUID) -> None:
        self.queued.append(job_id)

    def runtime_input(self, job_id: UUID) -> dict[str, UUID]:
        item = next(item for item in self.created if item["job_id"] == job_id)
        return {"job_id": job_id, "brief_id": item["brief_id"]}

    def finalize_success(self, job_id: UUID, _result: dict[str, Any]) -> None:
        self.completed.append(job_id)

    def finalize_failure(self, job_id: UUID, *, fallback_error_code: str) -> None:
        self.failed.append((job_id, fallback_error_code))


async def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_scheduler_reports_first_running_then_fifo_queued() -> None:
    repository = FakeJobRepository()
    first_started = threading.Event()
    release_first = threading.Event()
    run_order: list[UUID] = []

    def run_job(job_id: UUID, _brief_id: UUID) -> dict[str, Any]:
        run_order.append(job_id)
        if len(run_order) == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        return {"phase": "draft_rendered", "outcome": "draft_rendered"}

    scheduler = JobScheduler(repository, run_job)  # type: ignore[arg-type]
    await scheduler.start()
    try:
        brief = ResearchBrief(question="Q", brief_text="Brief")
        first = await scheduler.submit(brief)
        assert first["status"] == "running"
        await asyncio.to_thread(first_started.wait, 2)

        second = await scheduler.submit(brief)
        assert second["status"] == "queued"
        assert second["queue_position"] == 1

        release_first.set()
        await _wait_until(lambda: len(repository.completed) == 2)
        assert run_order == [first["job_id"], second["job_id"]]
        assert repository.running == [second["job_id"]]
    finally:
        release_first.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_leaves_an_interrupted_job_recoverable() -> None:
    """An unhandled graph error interrupts the attempt without a terminal write.

    Finalizing failure here would write job.stopped and veto the completed write of a
    later resume; the row stays recoverable instead, and the loop moves on.
    """
    repository = FakeJobRepository()
    interrupted: list[UUID] = []

    def run_job(job_id: UUID, _brief_id: UUID) -> dict[str, Any]:
        if not interrupted:
            interrupted.append(job_id)
            raise RuntimeError("transient execution error")
        return {"phase": "draft_rendered", "outcome": "draft_rendered"}

    scheduler = JobScheduler(repository, run_job)  # type: ignore[arg-type]
    await scheduler.start()
    try:
        first = await scheduler.submit(ResearchBrief(question="Q", brief_text="Brief"))
        second = await scheduler.submit(ResearchBrief(question="Q2", brief_text="Brief2"))
        await _wait_until(lambda: second["job_id"] in repository.completed)
        assert interrupted == [first["job_id"]]
        assert repository.failed == []
        assert first["job_id"] not in repository.completed
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_finalizes_verifier_major_gap_as_terminal_failure() -> None:
    """The decision budget is exhausted, so a resume would only hit the same wall."""
    repository = FakeJobRepository()

    def run_job(_job_id: UUID, _brief_id: UUID) -> dict[str, Any]:
        raise VerifierMajorGapError("major gap, budget exhausted")

    scheduler = JobScheduler(repository, run_job)  # type: ignore[arg-type]
    await scheduler.start()
    try:
        created = await scheduler.submit(ResearchBrief(question="Q", brief_text="Brief"))
        await _wait_until(lambda: bool(repository.failed))
        assert repository.failed == [(created["job_id"], "verifier_major_gap")]
    finally:
        await scheduler.stop()


@pytest.mark.asyncio
async def test_startup_recovery_normalizes_extra_running_jobs_to_fifo_queue() -> None:
    repository = FakeJobRepository()
    first_id = uuid4()
    second_id = uuid4()
    repository.created = [
        {"job_id": first_id, "brief_id": uuid4()},
        {"job_id": second_id, "brief_id": uuid4()},
    ]
    repository.recovered = [
        {"job_id": first_id, "status": "running"},
        {"job_id": second_id, "status": "running"},
    ]
    first_started = threading.Event()
    release_first = threading.Event()

    def run_job(job_id: UUID, _brief_id: UUID) -> dict[str, Any]:
        if job_id == first_id:
            first_started.set()
            assert release_first.wait(timeout=2)
        return {"phase": "draft_rendered", "outcome": "draft_rendered"}

    scheduler = JobScheduler(repository, run_job)  # type: ignore[arg-type]
    await scheduler.start()
    try:
        await asyncio.to_thread(first_started.wait, 2)
        assert repository.queued == [second_id]
        release_first.set()
        await _wait_until(lambda: len(repository.completed) == 2)
        assert repository.running == [second_id]
    finally:
        release_first.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_scheduler_cancels_running_job_instead_of_completing_it() -> None:
    repository = FakeJobRepository()
    started = threading.Event()
    release = threading.Event()

    def run_job(_job_id: UUID, _brief_id: UUID) -> dict[str, Any]:
        started.set()
        assert release.wait(timeout=2)
        return {"phase": "draft_rendered", "outcome": "draft_rendered"}

    scheduler = JobScheduler(repository, run_job)  # type: ignore[arg-type]
    await scheduler.start()
    try:
        created = await scheduler.submit(ResearchBrief(question="Q", brief_text="Brief"))
        await asyncio.to_thread(started.wait, 2)
        assert await scheduler.cancel(created["job_id"], requested_via="cli") == "cancelling"
        release.set()
        await _wait_until(lambda: repository.cancelled == [created["job_id"]])
        assert repository.completed == []
    finally:
        release.set()
        await scheduler.stop()


@pytest.mark.asyncio
async def test_one_finalization_failure_does_not_kill_later_jobs() -> None:
    repository = FakeJobRepository()
    repository.fail_cancel_finalization_once = True
    first_started = threading.Event()
    release_first = threading.Event()
    run_order: list[UUID] = []

    def run_job(job_id: UUID, _brief_id: UUID) -> dict[str, Any]:
        run_order.append(job_id)
        if len(run_order) == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        return {"phase": "draft_rendered", "outcome": "draft_rendered"}

    scheduler = JobScheduler(repository, run_job)  # type: ignore[arg-type]
    await scheduler.start()
    try:
        brief = ResearchBrief(question="Q", brief_text="Brief")
        first = await scheduler.submit(brief)
        await asyncio.to_thread(first_started.wait, 2)
        assert await scheduler.cancel(first["job_id"], requested_via="cli") == "cancelling"
        release_first.set()
        await asyncio.to_thread(repository.cancel_finalization_attempted.wait, 2)

        second = await scheduler.submit(brief)
        await _wait_until(lambda: second["job_id"] in repository.completed)
        assert run_order == [first["job_id"], second["job_id"]]
    finally:
        release_first.set()
        await scheduler.stop()
