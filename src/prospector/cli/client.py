"""Typed HTTP client for the local Prospector API."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import httpx

from prospector.api.schemas import (
    JobCancelResponse,
    JobCreateResponse,
    JobDetail,
    JobListItem,
    ScopeReviseResponse,
)
from prospector.schemas.brief import EffortLevel, ResearchBrief, ScopeOutcome

DEFAULT_SERVER = "http://127.0.0.1:7620"
REQUEST_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
STREAM_TIMEOUT = httpx.Timeout(None, connect=5.0)


class CliApiError(Exception):
    """A structured error returned by the Prospector API."""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


class CliConnectionError(Exception):
    """The Prospector service could not be reached."""


class CliProtocolError(Exception):
    """The service returned a response outside the published contract."""


class CliLocalError(Exception):
    """The CLI could not complete a local operation such as writing a report."""


def server_url() -> str:
    return (os.environ.get("PROSPECTOR_SERVER") or DEFAULT_SERVER).rstrip("/")


class ProspectorClient:
    """Synchronous client used by Typer commands and the SSE follower."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or server_url()).rstrip("/")
        # Local CLI must not inherit macOS/system HTTP proxies: when nothing
        # listens on the Prospector port, those proxies often return an empty
        # HTTP 502 instead of a connect error, which hides "serve not running".
        self.http = httpx.Client(
            base_url=self.base_url,
            timeout=REQUEST_TIMEOUT,
            transport=transport,
            trust_env=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self.http.close()

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        error_code = "service_unavailable"
        message = f"Prospector service returned HTTP {response.status_code}"
        try:
            body = response.json()
            if isinstance(body, dict):
                error_code = str(body.get("error_code") or error_code)
                message = str(body.get("message") or message)
        except ValueError:
            pass
        raise CliApiError(response.status_code, error_code, message)

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise CliConnectionError(str(exc)) from exc
        self._raise_for_error(response)
        return response

    def health(self) -> None:
        self._request("GET", "/api/healthz")

    def scope(
        self,
        question: str,
        *,
        effort: EffortLevel,
        language: str,
        clarification_question: str | None = None,
        clarification_answer: str | None = None,
    ) -> ScopeOutcome:
        payload: dict[str, Any] = {
            "question": question,
            "effort": effort,
            "language": language,
        }
        if clarification_question is not None:
            payload["clarification_question"] = clarification_question
            payload["clarification_answer"] = clarification_answer
        response = self._request("POST", "/api/scope", json=payload)
        try:
            return ScopeOutcome.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise CliProtocolError("Invalid /api/scope response") from exc

    def revise_scope(
        self,
        question: str,
        previous_brief: ResearchBrief,
        revision_note: str,
        *,
        effort: EffortLevel,
        language: str,
    ) -> ResearchBrief:
        response = self._request(
            "POST",
            "/api/scope/revise",
            json={
                "question": question,
                "previous_brief": previous_brief.model_dump(mode="json"),
                "revision_note": revision_note,
                "effort": effort,
                "language": language,
            },
        )
        try:
            return ScopeReviseResponse.model_validate(response.json()).brief
        except (ValueError, TypeError) as exc:
            raise CliProtocolError("Invalid /api/scope/revise response") from exc

    def create_job(self, brief: ResearchBrief) -> JobCreateResponse:
        response = self._request(
            "POST",
            "/api/jobs",
            json={"brief": brief.model_dump(mode="json")},
        )
        try:
            return JobCreateResponse.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise CliProtocolError("Invalid /api/jobs response") from exc

    def get_job(self, job_id: UUID) -> JobDetail:
        response = self._request("GET", f"/api/jobs/{job_id}")
        try:
            return JobDetail.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise CliProtocolError("Invalid job response") from exc

    def cancel_job(self, job_id: UUID) -> JobCancelResponse:
        response = self._request("POST", f"/api/jobs/{job_id}/cancel")
        try:
            return JobCancelResponse.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise CliProtocolError("Invalid job cancellation response") from exc

    def list_jobs(self) -> list[JobListItem]:
        response = self._request("GET", "/api/jobs")
        try:
            body = response.json()
            if not isinstance(body, list):
                raise TypeError
            return [JobListItem.model_validate(item) for item in body]
        except (ValueError, TypeError) as exc:
            raise CliProtocolError("Invalid job list response") from exc

    def download_report(self, job_id: UUID, report_format: str = "md") -> bytes:
        return self._request(
            "GET",
            f"/api/jobs/{job_id}/report",
            params={"format": report_format},
        ).content

    def stream_events(
        self,
        job_id: UUID,
        *,
        last_event_id: int | None,
        on_open: Callable[[], None] | None = None,
    ) -> Iterator[str]:
        headers = {}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        try:
            with self.http.stream(
                "GET",
                f"/api/jobs/{job_id}/events",
                headers=headers,
                timeout=STREAM_TIMEOUT,
            ) as response:
                self._raise_for_error(response)
                if on_open is not None:
                    on_open()
                yield from response.iter_lines()
        except CliApiError:
            raise
        except httpx.RequestError as exc:
            raise CliConnectionError(str(exc)) from exc
