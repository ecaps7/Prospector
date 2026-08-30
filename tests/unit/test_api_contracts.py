from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from prospector.api.app import ApiServices, create_app, parse_storage_uri
from prospector.api.errors import validation_error_details
from prospector.api.scheduler import JobScheduler
from prospector.api.sse import encode_event, heartbeat
from prospector.schemas.brief import ResearchBrief, ScopeOutcome
from prospector.schemas.events import sse_event_adapter


class FakeObjectStore:
    bucket = "reports"

    def check_bucket(self) -> None:
        pass

    def get_bytes(self, key: str) -> bytes:
        if key == "workspace/report.md":
            return b"# report"
        if key == "workspace/report.json":
            return b'{"title":"ok"}'
        raise AssertionError(key)


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
        self.excerpts: list[dict[str, Any]] = []

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
        if not self.ready_report:
            return None
        if report_format == "md":
            return "s3://reports/workspace/report.md"
        return "s3://reports/workspace/report.json"

    def list_jobs(self) -> list[dict[str, Any]]:
        return []

    def get_job(self, job_id: UUID) -> dict[str, Any] | None:
        return None

    def list_excerpts(self, job_id: UUID, excerpt_ids: list[UUID]) -> list[dict[str, Any]] | None:
        if job_id != self.job_id:
            return None
        found = [item for item in self.excerpts if item["excerpt_id"] in excerpt_ids]
        if len(found) != len(list(dict.fromkeys(excerpt_ids))):
            return None
        by_id = {item["excerpt_id"]: item for item in found}
        return [by_id[excerpt_id] for excerpt_id in excerpt_ids]


class FakeScheduler:
    def __init__(self) -> None:
        self.cancel_source: str | None = None

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

    async def cancel(self, job_id: UUID, *, requested_via: str) -> str | None:
        self.cancel_source = requested_via
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


def _sse_data(frame: str) -> str:
    for line in frame.split("\n"):
        if line.startswith("data: "):
            return line[6:]
    raise AssertionError(f"SSE frame is missing data: {frame!r}")


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
    sse_event_adapter.validate_json(_sse_data(frame))
    assert heartbeat() == ": heartbeat\n\n"


def test_encoded_job_stopped_matches_published_event_schema() -> None:
    frame = encode_event(
        {
            "id": 2,
            "event_type": "job.stopped",
            "payload": {
                "status": "completed",
                "phase": "draft_rendered",
                "outcome": "draft_rendered",
                "error_code": None,
                "report_markdown_ref": "s3://reports/workspace/report.md",
                "report_json_ref": None,
            },
            "task_id": None,
            "decision_round": None,
            "created_at": datetime(2026, 7, 19, tzinfo=UTC),
        }
    )
    sse_event_adapter.validate_json(_sse_data(frame))


def test_encoded_planner_started_matches_published_event_schema() -> None:
    frame = encode_event(
        {
            "id": 3,
            "event_type": "planner.started",
            "payload": {"decision_round": 1},
            "task_id": None,
            "decision_round": 1,
            "created_at": datetime(2026, 7, 19, tzinfo=UTC),
        }
    )
    event = sse_event_adapter.validate_json(_sse_data(frame)).root
    assert event.event_type == "planner.started"
    assert event.payload.decision_round == 1


def test_encoded_synthesis_completed_matches_published_event_schema() -> None:
    synthesis_run_id = uuid4()
    frame = encode_event(
        {
            "id": 4,
            "event_type": "synthesis.completed",
            "payload": {
                "synthesis_run_id": str(synthesis_run_id),
                "decision": "ready",
                "synthesis": "材料已经能够回应问题。",
            },
            "task_id": None,
            "decision_round": None,
            "created_at": datetime(2026, 7, 19, tzinfo=UTC),
        }
    )

    event = sse_event_adapter.validate_json(_sse_data(frame)).root
    assert event.event_type == "synthesis.completed"
    assert event.payload.synthesis_run_id == synthesis_run_id
    assert event.payload.synthesis == "材料已经能够回应问题。"


def test_sse_openapi_documents_event_envelope_and_reconnect() -> None:
    repository = FakeRepository()
    app = create_app(_services(repository), validate_startup=False)
    spec = app.openapi()
    operation = spec["paths"]["/api/jobs/{job_id}/events"]["get"]

    header = next(item for item in operation["parameters"] if item["name"] == "Last-Event-ID")
    assert "non-negative integer" in header["description"]
    assert "job.stopped" in header["description"]

    response_200 = operation["responses"]["200"]
    assert "text/event-stream" in response_200["content"]
    assert "application/json" not in response_200["content"]
    assert response_200["content"]["text/event-stream"]["schema"] == {
        "$ref": "#/components/schemas/SseEvent"
    }
    assert "job.stopped" in response_200["description"]
    assert operation["responses"]["400"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }

    schemas = spec["components"]["schemas"]
    error = schemas["ErrorResponse"]
    assert set(error["required"]) == {"error_code", "message"}
    assert "details" in error["properties"]
    detail = schemas["ValidationErrorDetail"]
    assert set(detail["required"]) == {"path", "reason"}
    assert "HTTPValidationError" not in schemas
    scope_422 = spec["paths"]["/api/scope"]["post"]["responses"]["422"]
    assert scope_422["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    assert schemas["SseEvent"]["discriminator"]["propertyName"] == "event_type"
    assert "job.stopped" in schemas["SseEvent"]["discriminator"]["mapping"]
    stopped = schemas["JobStoppedEvent"]
    for field in ("event_type", "payload", "task_id", "decision_round", "created_at"):
        assert field in stopped["properties"]
    payload = schemas["JobStoppedPayload"]
    assert set(payload["required"]) == {"status", "phase"}
    assert set(payload["properties"]["status"]["enum"]) == {
        "completed",
        "failed",
        "cancelled",
    }
    assert "report_markdown_ref" in payload["properties"]
    assert "report_json_ref" in payload["properties"]


def test_validation_error_details_use_field_paths() -> None:
    assert validation_error_details(
        [
            {
                "loc": ("body", "question"),
                "msg": "Value error, must not be blank",
                "ctx": {"error": ValueError("must not be blank")},
            },
            {
                "loc": ("body", "brief", "user_constraints", "regions"),
                "msg": "List should have at most 12 items after validation, not 13",
            },
            {
                "loc": ("body",),
                "msg": (
                    "Value error, clarification_question and clarification_answer "
                    "must be provided together"
                ),
            },
        ]
    ) == [
        {"path": "question", "reason": "must not be blank"},
        {
            "path": "brief.user_constraints.regions",
            "reason": "List should have at most 12 items after validation, not 13",
        },
        {
            "path": "",
            "reason": "clarification_question and clarification_answer must be provided together",
        },
    ]


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
            "details": [
                {
                    "path": "",
                    "reason": (
                        "clarification_question and clarification_answer must be provided together"
                    ),
                }
            ],
        }

        response = client.post("/api/scope", json={"question": "   "})
        assert response.status_code == 422
        assert response.json() == {
            "error_code": "validation_error",
            "message": "request validation failed",
            "details": [{"path": "question", "reason": "must not be blank"}],
        }

        response = client.post("/api/scope", json={"question": "Question", "language": "  "})
        assert response.status_code == 422
        assert response.json()["details"] == [{"path": "language", "reason": "must not be blank"}]

        response = client.post(
            "/api/jobs",
            json={"brief": {"question": "  ", "brief_text": "Detailed research space"}},
        )
        assert response.status_code == 422
        assert response.json()["details"] == [
            {"path": "brief.question", "reason": "must not be blank"}
        ]

        response = client.post(
            "/api/jobs",
            json={
                "brief": {
                    "question": "Question",
                    "brief_text": "Detailed research space",
                    "user_constraints": {"regions": [str(index) for index in range(13)]},
                }
            },
        )
        assert response.status_code == 422
        assert response.json()["details"] == [
            {
                "path": "brief.user_constraints.regions",
                "reason": "List should have at most 12 items after validation, not 13",
            }
        ]

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

        response = client.get(f"/api/jobs/{repository.job_id}/report?format=json")
        assert response.status_code == 200
        assert response.content == b'{"title":"ok"}'
        assert response.headers["content-disposition"] == 'inline; filename="report.json"'


def test_cancel_endpoint_returns_structured_terminal_status() -> None:
    repository = FakeRepository()
    services = _services(repository)
    scheduler = cast(FakeScheduler, services.scheduler)
    with TestClient(create_app(services, validate_startup=False)) as client:
        response = client.post(
            f"/api/jobs/{repository.job_id}/cancel",
            json={"requested_via": "web_monitor"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "job_id": str(repository.job_id),
            "status": "cancelled",
        }
        assert scheduler.cancel_source == "web_monitor"

        missing_source = client.post(f"/api/jobs/{repository.job_id}/cancel")
        assert missing_source.status_code == 422


def test_unprefixed_routes_are_not_kept_as_aliases() -> None:
    repository = FakeRepository()
    with TestClient(
        create_app(_services(repository), validate_startup=False, web_dist=None)
    ) as client:
        assert client.get("/healthz").status_code == 404
        assert client.post("/scope", json={"question": "Question"}).status_code == 404
        assert client.get("/jobs").status_code == 404
        health = client.get("/api/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}


def test_excerpts_are_scoped_to_the_job() -> None:
    repository = FakeRepository()
    excerpt_id = uuid4()
    foreign_id = uuid4()
    repository.excerpts = [
        {
            "excerpt_id": excerpt_id,
            "text": "archived quote",
            "doc_version": 1,
            "locator": {"h": "h1"},
            "source_uri": "https://example.com",
            "title": "Example",
            "author": None,
            "published_at": None,
        }
    ]
    with TestClient(create_app(_services(repository), validate_startup=False)) as client:
        response = client.get(
            f"/api/jobs/{repository.job_id}/excerpts",
            params=[("ids", str(excerpt_id))],
        )
        assert response.status_code == 200
        body = response.json()
        assert body[0]["text"] == "archived quote"
        assert body[0]["source_uri"] == "https://example.com"

        missing = client.get(
            f"/api/jobs/{repository.job_id}/excerpts",
            params=[("ids", str(foreign_id))],
        )
        assert missing.status_code == 404
        assert missing.json()["error_code"] == "excerpt_not_found"

        unknown_job = client.get(f"/api/jobs/{uuid4()}/excerpts", params=[("ids", str(excerpt_id))])
        assert unknown_job.status_code == 404
        assert unknown_job.json()["error_code"] == "job_not_found"


def test_spa_fallback_serves_index_and_keeps_api_json(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Prospector</title>", encoding="utf-8")
    (dist / "asset.txt").write_text("ok", encoding="utf-8")
    repository = FakeRepository()
    with TestClient(
        create_app(_services(repository), validate_startup=False, web_dist=dist)
    ) as client:
        html = client.get("/", headers={"sec-fetch-dest": "document"})
        assert html.status_code == 200
        assert "Prospector" in html.text

        document_nav = client.get(
            f"/jobs/{repository.job_id}/report",
            headers={"sec-fetch-dest": "document"},
        )
        assert document_nav.status_code == 200
        assert document_nav.headers["content-type"].startswith("text/html")

        unknown = client.get("/settings")
        assert unknown.status_code == 200
        assert "Prospector" in unknown.text

        asset = client.get("/asset.txt")
        assert asset.status_code == 200
        assert asset.text == "ok"

        jobs = client.get("/jobs")
        assert jobs.status_code == 200
        assert "Prospector" in jobs.text

        api_jobs = client.get("/api/jobs")
        assert api_jobs.status_code == 200
        assert api_jobs.headers["content-type"].startswith("application/json")

        jobs_document = client.get("/jobs", headers={"sec-fetch-dest": "document"})
        assert jobs_document.status_code == 200
        assert jobs_document.headers["content-type"].startswith("text/html")
