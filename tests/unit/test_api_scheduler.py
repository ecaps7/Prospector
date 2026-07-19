from __future__ import annotations

import asyncio
import threading
from typing import Any
from uuid import UUID, uuid4

import pytest

from prospector.api.scheduler import JobScheduler
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

    def finalize_pending_cancellations(self) -> None:
        pass

    def cancel_requested(self, job_id: UUID) -> bool:
        return self.cancel_requests.get(job_id) in {"cancelling", "cancelled"}

    def request_cancel(self, job_id: UUID) -> str | None:
        if not any(item["job_id"] == job_id for item in self.created):
            return None
        self.cancel_requests[job_id] = "cancelling"
        return "cancelling"

    def finalize_cancelled(self, job_id: UUID) -> None:
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
async def test_scheduler_converts_unhandled_graph_error_to_stable_failure() -> None:
    repository = FakeJobRepository()

    def run_job(_job_id: UUID, _brief_id: UUID) -> dict[str, Any]:
        raise RuntimeError("private failure detail")

    scheduler = JobScheduler(repository, run_job)  # type: ignore[arg-type]
    await scheduler.start()
    try:
        created = await scheduler.submit(ResearchBrief(question="Q", brief_text="Brief"))
        await _wait_until(lambda: bool(repository.failed))
        assert repository.failed == [(created["job_id"], "job_execution_error")]
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
        assert await scheduler.cancel(created["job_id"]) == "cancelling"
        release.set()
        await _wait_until(lambda: repository.cancelled == [created["job_id"]])
        assert repository.completed == []
    finally:
        release.set()
        await scheduler.stop()
