from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from prospector.api.app import ApiServices, create_app, parse_storage_uri
from prospector.api.scheduler import JobScheduler
from prospector.api.sse import encode_event, heartbeat
from prospector.schemas.brief import ResearchBrief, ScopeOutcome


class FakeObjectStore:
    bucket = "reports"

    def check_bucket(self) -> None:
        pass

    def get_bytes(self, key: str) -> bytes:
        assert key == "workspace/report.md"
        return b"# report"


class FakeRepository:
    def __init__(self) -> None:
        self.job_id = uuid4()
        self.events = [
            {
                "id": 1,
                "event_type": "brief.confirmed",
                "task_id": None,
                "decision_round": None,
                "payload": {"effort": "standard"},
                "created_at": datetime.now(UTC),
            },
            {
                "id": 2,
                "event_type": "job.stopped",
                "task_id": None,
                "decision_round": None,
                "payload": {
                    "status": "completed",
                    "phase": "draft_rendered",
                    "outcome": "draft_rendered",
                    "error_code": None,
                    "report_markdown_ref": "s3://reports/workspace/report.md",
                    "report_json_ref": None,
                },
                "created_at": datetime.now(UTC),
            },
        ]
        self.ready_report = False

    def health_check(self) -> None:
        pass

    def job_exists(self, job_id: UUID) -> bool:
        return job_id == self.job_id

    def list_events_after(self, job_id: UUID, after_id: int) -> list[dict[str, Any]]:
        assert job_id == self.job_id
        return [event for event in self.events if int(event["id"]) > after_id]

    def stopped_event_id(self, job_id: UUID) -> int | None:
        assert job_id == self.job_id
        return 2

    def report_ref(self, job_id: UUID, report_format: str) -> str | None:
        assert job_id == self.job_id
        if self.ready_report and report_format == "md":
            return "s3://reports/workspace/report.md"
        return None

    def list_jobs(self) -> list[dict[str, Any]]:
        return []

    def get_job(self, job_id: UUID) -> dict[str, Any] | None:
        return None


class FakeScheduler:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def submit(self, _brief: ResearchBrief) -> dict[str, Any]:
        return {
            "job_id": uuid4(),
            "brief_id": uuid4(),
            "status": "running",
            "queue_position": None,
        }

    async def cancel(self, job_id: UUID) -> str | None:
        return "cancelled" if job_id else None


def _services(repository: FakeRepository) -> ApiServices:
    brief = ResearchBrief(question="Q", brief_text="Detailed", effort="standard")
    return ApiServices(
        repository=cast(Any, repository),
        object_store=cast(Any, FakeObjectStore()),
        scheduler=cast(JobScheduler, FakeScheduler()),
        scope=lambda *_args, **_kwargs: ScopeOutcome(kind="brief_pending", brief=brief),
        revise=lambda *_args, **_kwargs: brief,
        require_llm=lambda: None,
    )


def test_storage_uri_must_match_configured_bucket() -> None:
    assert parse_storage_uri("s3://reports/a/report.md", "reports") == "a/report.md"
    for uri in ("https://reports/a", "s3://other/a", "s3://reports"):
        try:
            parse_storage_uri(uri, "reports")
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid storage URI: {uri}")


def test_sse_frame_contains_persisted_id_type_and_structured_data() -> None:
    frame = encode_event(
        {
            "id": 7,
            "event_type": "job.phase_changed",
            "payload": {"phase": "research"},
            "task_id": None,
            "decision_round": 1,
            "created_at": datetime(2026, 7, 19, tzinfo=UTC),
        }
    )
    assert frame.startswith("id: 7\nevent: job.phase_changed\ndata: ")
    assert '"event_type":"job.phase_changed"' in frame
    assert heartbeat() == ": heartbeat\n\n"


def test_scope_validation_and_error_body_are_stable() -> None:
    repository = FakeRepository()
    with TestClient(create_app(_services(repository), validate_startup=False)) as client:
        response = client.post(
            "/api/scope",
            json={"question": "Question", "clarification_question": "Which?"},
        )
        assert response.status_code == 422
        assert response.json() == {
            "error_code": "validation_error",
            "message": "request validation failed",
        }

        response = client.post("/api/scope", json={"question": "   "})
        assert response.status_code == 422
        assert response.json()["error_code"] == "validation_error"

        response = client.post("/api/scope", json={"question": "Question"})
        assert response.status_code == 200
        assert response.json()["kind"] == "brief_pending"


def test_sse_replays_stopped_event_and_closed_cursor_returns_empty_stream() -> None:
    repository = FakeRepository()
    with TestClient(create_app(_services(repository), validate_startup=False)) as client:
        response = client.get(f"/api/jobs/{repository.job_id}/events")
        assert response.status_code == 200
        assert "id: 1" in response.text
        assert "id: 2\nevent: job.stopped" in response.text

        response = client.get(
            f"/api/jobs/{repository.job_id}/events",
            headers={"Last-Event-ID": "2"},
        )
        assert response.status_code == 200
        assert response.text == ""


def test_invalid_sse_cursor_and_report_states_use_structured_errors() -> None:
    repository = FakeRepository()
    with TestClient(create_app(_services(repository), validate_startup=False)) as client:
        response = client.get(
            f"/api/jobs/{repository.job_id}/events",
            headers={"Last-Event-ID": "bad"},
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "invalid_last_event_id"

        response = client.get(f"/api/jobs/{repository.job_id}/report?format=md")
        assert response.status_code == 409
        assert response.json()["error_code"] == "report_not_ready"

        repository.ready_report = True
        response = client.get(f"/api/jobs/{repository.job_id}/report?format=md")
        assert response.status_code == 200
        assert response.content == b"# report"
        assert response.headers["content-disposition"] == 'attachment; filename="report.md"'


def test_cancel_endpoint_returns_structured_terminal_status() -> None:
    repository = FakeRepository()
    with TestClient(create_app(_services(repository), validate_startup=False)) as client:
        response = client.post(f"/api/jobs/{repository.job_id}/cancel")
        assert response.status_code == 200
        assert response.json() == {
            "job_id": str(repository.job_id),
            "status": "cancelled",
        }


def test_unprefixed_routes_are_not_kept_as_aliases() -> None:
    repository = FakeRepository()
    with TestClient(create_app(_services(repository), validate_startup=False)) as client:
        assert client.get("/healthz").status_code == 404
        assert client.post("/scope", json={"question": "Question"}).status_code == 404
        assert client.get("/jobs").status_code == 404
        health = client.get("/api/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
