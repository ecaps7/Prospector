"""FastAPI application for the local single-user Prospector service."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from prospector.agents.llm import LlmNotConfiguredError, require_llm_settings
from prospector.agents.scope import run_scope, write_research_brief
from prospector.api.errors import ApiError
from prospector.api.scheduler import JobScheduler
from prospector.api.schemas import (
    ErrorResponse,
    HealthResponse,
    JobCancelResponse,
    JobCreateRequest,
    JobCreateResponse,
    JobDetail,
    JobListItem,
    ScopeRequest,
    ScopeReviseRequest,
    ScopeReviseResponse,
)
from prospector.api.sse import encode_event, heartbeat
from prospector.flow.research_graph import build_research_graph, thread_config
from prospector.flow.state import initial_research_state
from prospector.obs.logging import get_logger
from prospector.schemas.brief import ResearchBrief, ScopeOutcome
from prospector.store.checkpoint import checkpointer_session
from prospector.store.object_store import ObjectStore
from prospector.store.repositories.jobs import JobRepository

POLL_INTERVAL_SECONDS = 0.2
HEARTBEAT_INTERVAL_SECONDS = 15.0
log = get_logger("prospector.api")

ScopeFn = Callable[..., ScopeOutcome]
ReviseFn = Callable[..., ResearchBrief]


def _default_run_job(job_id: UUID, brief_id: UUID) -> Mapping[str, Any]:
    with checkpointer_session() as checkpointer:
        graph = build_research_graph(checkpointer)
        return graph.invoke(
            initial_research_state(job_id=str(job_id), brief_id=str(brief_id)),
            thread_config(str(job_id)),
        )


@dataclass(slots=True)
class ApiServices:
    repository: JobRepository
    object_store: ObjectStore
    scheduler: JobScheduler
    scope: ScopeFn = run_scope
    revise: ReviseFn = write_research_brief
    require_llm: Callable[[], Any] = require_llm_settings


def default_services() -> ApiServices:
    repository = JobRepository()
    return ApiServices(
        repository=repository,
        object_store=ObjectStore(repository.settings),
        scheduler=JobScheduler(repository, _default_run_job),
    )


def parse_storage_uri(uri: str, expected_bucket: str) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or parsed.netloc != expected_bucket:
        raise ValueError("report reference does not belong to the configured bucket")
    key = parsed.path.lstrip("/")
    if not key:
        raise ValueError("report reference has no object key")
    return key


def create_app(
    services: ApiServices | None = None,
    *,
    validate_startup: bool = True,
) -> FastAPI:
    supplied_services = services

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = supplied_services or default_services()
        app.state.services = runtime
        if validate_startup:
            await asyncio.to_thread(runtime.repository.health_check)
            await asyncio.to_thread(runtime.object_store.check_bucket)
        await runtime.scheduler.start()
        try:
            yield
        finally:
            await runtime.scheduler.stop()

    app = FastAPI(title="Prospector API", version="0.1.0", lifespan=lifespan)
    router = APIRouter(prefix="/api")

    def runtime(request: Request) -> ApiServices:
        return request.app.state.services

    @app.exception_handler(ApiError)
    async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": exc.error_code, "message": exc.message},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error_code": "validation_error", "message": "request validation failed"},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("http.unhandled_error", message=str(exc))
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "service_unavailable",
                "message": "Service unavailable",
            },
        )

    async def preflight_llm(api: ApiServices) -> None:
        try:
            await run_in_threadpool(api.require_llm)
        except LlmNotConfiguredError as exc:
            raise ApiError(503, "llm_not_configured", str(exc)) from exc

    @router.post(
        "/scope",
        response_model=ScopeOutcome,
        responses={503: {"model": ErrorResponse}},
    )
    async def scope(payload: ScopeRequest, request: Request) -> ScopeOutcome:
        api = runtime(request)
        await preflight_llm(api)
        try:
            return await run_in_threadpool(
                api.scope,
                payload.question,
                clarification_question=payload.clarification_question,
                clarification_answer=payload.clarification_answer,
                language=payload.language,
                effort=payload.effort,
            )
        except LlmNotConfiguredError as exc:
            raise ApiError(503, "llm_not_configured", str(exc)) from exc
        except Exception as exc:
            raise ApiError(503, "service_unavailable", "Scope service unavailable") from exc

    @router.post(
        "/scope/revise",
        response_model=ScopeReviseResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def revise(payload: ScopeReviseRequest, request: Request) -> ScopeReviseResponse:
        api = runtime(request)
        await preflight_llm(api)
        try:
            brief = await run_in_threadpool(
                api.revise,
                payload.question,
                previous_brief=payload.previous_brief,
                revision_note=payload.revision_note,
                language=payload.language,
                effort=payload.effort,
            )
        except LlmNotConfiguredError as exc:
            raise ApiError(503, "llm_not_configured", str(exc)) from exc
        except Exception as exc:
            raise ApiError(503, "service_unavailable", "Scope revision unavailable") from exc
        return ScopeReviseResponse(brief=brief)

    @router.post(
        "/jobs",
        response_model=JobCreateResponse,
        status_code=201,
        responses={503: {"model": ErrorResponse}},
    )
    async def create_job(payload: JobCreateRequest, request: Request) -> dict[str, Any]:
        api = runtime(request)
        await preflight_llm(api)
        try:
            return await api.scheduler.submit(payload.brief)
        except Exception as exc:
            raise ApiError(503, "service_unavailable", "Job submission unavailable") from exc

    @router.get("/jobs", response_model=list[JobListItem])
    async def list_jobs(request: Request) -> list[dict[str, Any]]:
        return await run_in_threadpool(runtime(request).repository.list_jobs)

    @router.post(
        "/jobs/{job_id}/cancel",
        response_model=JobCancelResponse,
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def cancel_job(job_id: UUID, request: Request) -> JobCancelResponse:
        status = await runtime(request).scheduler.cancel(job_id)
        if status is None:
            raise ApiError(404, "job_not_found", "Job not found")
        if status in {"completed", "failed"}:
            raise ApiError(409, "job_not_cancellable", "Job has already stopped")
        if status == "cancelling":
            return JobCancelResponse(job_id=job_id, status="cancelling")
        if status == "cancelled":
            return JobCancelResponse(job_id=job_id, status="cancelled")
        raise ApiError(503, "service_unavailable", "Cancellation state is invalid")

    @router.get(
        "/jobs/{job_id}",
        response_model=JobDetail,
        responses={404: {"model": ErrorResponse}},
    )
    async def get_job(job_id: UUID, request: Request) -> dict[str, Any]:
        job = await run_in_threadpool(runtime(request).repository.get_job, job_id)
        if job is None:
            raise ApiError(404, "job_not_found", "Job not found")
        return job

    @router.get(
        "/jobs/{job_id}/events",
        responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    async def job_events(
        job_id: UUID,
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        repository = runtime(request).repository
        if not await run_in_threadpool(repository.job_exists, job_id):
            raise ApiError(404, "job_not_found", "Job not found")
        try:
            after_id = 0 if last_event_id is None else int(last_event_id)
            if after_id < 0:
                raise ValueError
        except ValueError as exc:
            raise ApiError(
                400,
                "invalid_last_event_id",
                "Last-Event-ID must be a non-negative integer",
            ) from exc

        async def stream():
            cursor = after_id
            heartbeat_at = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
            while True:
                if await request.is_disconnected():
                    return
                events = await asyncio.to_thread(repository.list_events_after, job_id, cursor)
                for event in events:
                    cursor = int(event["id"])
                    yield encode_event(event)
                    if event["event_type"] == "job.stopped":
                        return
                stopped_id = await asyncio.to_thread(repository.stopped_event_id, job_id)
                if stopped_id is not None and stopped_id <= cursor:
                    return
                now = time.monotonic()
                if now >= heartbeat_at:
                    yield heartbeat()
                    heartbeat_at = now + HEARTBEAT_INTERVAL_SECONDS
                await asyncio.sleep(POLL_INTERVAL_SECONDS)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get(
        "/jobs/{job_id}/report",
        responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    )
    async def report(
        job_id: UUID,
        request: Request,
        report_format: Literal["md", "json"] = Query(default="md", alias="format"),
    ) -> Response:
        api = runtime(request)
        if not await run_in_threadpool(api.repository.job_exists, job_id):
            raise ApiError(404, "job_not_found", "Job not found")
        ref = await run_in_threadpool(api.repository.report_ref, job_id, report_format)
        if ref is None:
            raise ApiError(409, "report_not_ready", "Report is not ready")
        try:
            key = parse_storage_uri(ref, api.object_store.bucket)
            content = await run_in_threadpool(api.object_store.get_bytes, key)
        except Exception as exc:
            raise ApiError(503, "service_unavailable", "Report object unavailable") from exc
        filename = "report.md" if report_format == "md" else "report.json"
        media_type = (
            "text/markdown; charset=utf-8"
            if report_format == "md"
            else "application/json; charset=utf-8"
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get(
        "/healthz",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
    )
    async def healthz(request: Request) -> HealthResponse:
        api = runtime(request)
        try:
            await run_in_threadpool(api.repository.health_check)
            await run_in_threadpool(api.object_store.check_bucket)
        except Exception as exc:
            raise ApiError(503, "service_unavailable", "Service dependencies unavailable") from exc
        return HealthResponse(status="ok")

    app.include_router(router)
    return app
