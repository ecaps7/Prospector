"""FIFO single-consumer execution of the existing synchronous research graph."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any
from uuid import UUID

from opentelemetry import trace

from prospector.flow.cancellation import JobCancelledError
from prospector.flow.research_graph import VerifierMajorGapError
from prospector.obs.logging import get_logger
from prospector.schemas.brief import ResearchBrief
from prospector.store.repositories.jobs import CancelRequestSource, JobRepository

RunJob = Callable[[UUID, UUID], Mapping[str, Any]]

log = get_logger("prospector.api.scheduler")
tracer = trace.get_tracer("prospector.api.scheduler")


class JobScheduler:
    def __init__(
        self,
        repository: JobRepository,
        run_job: RunJob,
        *,
        recover_on_start: bool = False,
    ) -> None:
        self.repository = repository
        self.run_job = run_job
        self.recover_on_start = recover_on_start
        self._queue: asyncio.Queue[UUID] = asyncio.Queue()
        self._state_lock = asyncio.Lock()
        self._active_job_id: UUID | None = None
        self._worker: asyncio.Task[None] | None = None
        # Every Job this process is on the hook for: queued, or the one running now.
        # A Job that is 'running' in the database but missing here is stranded -- an
        # earlier process died holding it -- and only this scheduler can tell them apart.
        self._held: set[UUID] = set()

    async def start(self) -> None:
        if self._worker is not None:
            return
        # A 'cancelling' row is a request no one is left to honour: the worker that would
        # have reached a safe boundary died with its process.  Sweeping them is unrelated
        # to whether this scheduler resumes anything, so it does not hang off that switch.
        await asyncio.to_thread(self.repository.finalize_pending_cancellations)
        recovered = (
            await asyncio.to_thread(self.repository.recoverable_jobs)
            if self.recover_on_start
            else []
        )
        if recovered:
            first = recovered[0]
            first_id = UUID(str(first["job_id"]))
            if first["status"] == "queued":
                await asyncio.to_thread(self.repository.mark_running, first_id)
            self._active_job_id = first_id
            self._held.add(first_id)
            await self._queue.put(first_id)
            for row in recovered[1:]:
                job_id = UUID(str(row["job_id"]))
                if row["status"] == "running":
                    await asyncio.to_thread(self.repository.mark_queued, job_id)
                self._held.add(job_id)
                await self._queue.put(job_id)
        self._worker = asyncio.create_task(self._consume(), name="prospector-job-scheduler")

    async def stop(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker
        self._worker = None

    async def submit(self, brief: ResearchBrief) -> dict[str, Any]:
        async with self._state_lock:
            if self._worker is None or self._worker.done():
                raise RuntimeError("Job scheduler is not running")
            start_immediately = self._active_job_id is None and self._queue.empty()
            created = await asyncio.to_thread(
                self.repository.create_with_brief,
                brief,
                start_immediately=start_immediately,
            )
            job_id = UUID(str(created["job_id"]))
            if start_immediately:
                self._active_job_id = job_id
            self._held.add(job_id)
            await self._queue.put(job_id)
            return created

    async def cancel(self, job_id: UUID, *, requested_via: CancelRequestSource) -> str | None:
        async with self._state_lock:
            held = job_id in self._held
            status = await asyncio.to_thread(
                self.repository.request_cancel,
                job_id,
                requested_via=requested_via,
            )
            if status != "cancelling" or held:
                return status
            # Nothing in this process is executing the Job, so no safe boundary will ever
            # come.  Leaving it 'cancelling' strands it for good: the row is neither
            # stoppable nor removable from the Jobs list.
            await asyncio.to_thread(self.repository.finalize_cancelled, job_id)
            return "cancelled"

    async def _consume(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                if await asyncio.to_thread(self.repository.cancel_requested, job_id):
                    await asyncio.to_thread(self.repository.finalize_cancelled, job_id)
                    continue
                async with self._state_lock:
                    if self._active_job_id != job_id:
                        await asyncio.to_thread(self.repository.mark_running, job_id)
                        self._active_job_id = job_id
                runtime = await asyncio.to_thread(self.repository.runtime_input, job_id)
                try:
                    with tracer.start_as_current_span(
                        "job.execute",
                        attributes={"prospector.job_id": str(job_id)},
                    ):
                        result = await asyncio.to_thread(
                            self.run_job,
                            runtime["job_id"],
                            runtime["brief_id"],
                        )
                except JobCancelledError:
                    await asyncio.to_thread(self.repository.finalize_cancelled, job_id)
                except VerifierMajorGapError:
                    await asyncio.to_thread(
                        self.repository.finalize_failure,
                        job_id,
                        fallback_error_code="verifier_major_gap",
                    )
                except Exception as exc:
                    if await asyncio.to_thread(self.repository.cancel_requested, job_id):
                        await asyncio.to_thread(self.repository.finalize_cancelled, job_id)
                    else:
                        # An escaped exception interrupts the attempt; it is not a terminal
                        # state. Finalizing here would write job.stopped and strand the
                        # checkpoint: the row stays 'running' (or 'failed' when the graph
                        # already recorded a contract outcome), so a scheduler restart
                        # recovers it and an explicit resume can still finalize success.
                        log.exception(
                            "job.execute_interrupted", job_id=str(job_id), message=str(exc)
                        )
                else:
                    if await asyncio.to_thread(self.repository.cancel_requested, job_id):
                        await asyncio.to_thread(self.repository.finalize_cancelled, job_id)
                    else:
                        await asyncio.to_thread(self.repository.finalize_success, job_id, result)
            except Exception as exc:
                log.exception(
                    "job.scheduler_iteration_failed",
                    job_id=str(job_id),
                    message=str(exc),
                )
            finally:
                async with self._state_lock:
                    if self._active_job_id == job_id:
                        self._active_job_id = None
                    self._held.discard(job_id)
                self._queue.task_done()
