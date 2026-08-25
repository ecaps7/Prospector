from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
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
            "/scope",
            json={"question": "Question", "clarification_question": "Which?"},
        )
        assert response.status_code == 422
        assert response.json() == {
            "error_code": "validation_error",
            "message": "request validation failed",
        }

        response = client.post("/scope", json={"question": "   "})
        assert response.status_code == 422
        assert response.json()["error_code"] == "validation_error"

        response = client.post("/scope", json={"question": "Question"})
        assert response.status_code == 200
        assert response.json()["kind"] == "brief_pending"


def test_sse_replays_stopped_event_and_closed_cursor_returns_empty_stream() -> None:
    repository = FakeRepository()
    with TestClient(create_app(_services(repository), validate_startup=False)) as client:
        response = client.get(f"/jobs/{repository.job_id}/events")
        assert response.status_code == 200
        assert "id: 1" in response.text
        assert "id: 2\nevent: job.stopped" in response.text

        response = client.get(f"/jobs/{repository.job_id}/events", headers={"Last-Event-ID": "2"})
        assert response.status_code == 200
        assert response.text == ""


def test_invalid_sse_cursor_and_report_states_use_structured_errors() -> None:
    repository = FakeRepository()
    with TestClient(create_app(_services(repository), validate_startup=False)) as client:
        response = client.get(f"/jobs/{repository.job_id}/events", headers={"Last-Event-ID": "bad"})
        assert response.status_code == 400
        assert response.json()["error_code"] == "invalid_last_event_id"

        response = client.get(f"/jobs/{repository.job_id}/report?format=md")
        assert response.status_code == 409
        assert response.json()["error_code"] == "report_not_ready"

        repository.ready_report = True
        response = client.get(f"/jobs/{repository.job_id}/report?format=md")
        assert response.status_code == 200
        assert response.content == b"# report"
        assert response.headers["content-disposition"] == 'attachment; filename="report.md"'

        response = client.get(f"/jobs/{repository.job_id}/report?format=json")
        assert response.status_code == 200
        assert response.content == b'{"title":"ok"}'
        assert response.headers["content-disposition"] == 'inline; filename="report.json"'


def test_cancel_endpoint_returns_structured_terminal_status() -> None:
    repository = FakeRepository()
    with TestClient(create_app(_services(repository), validate_startup=False)) as client:
        response = client.post(f"/jobs/{repository.job_id}/cancel")
        assert response.status_code == 200
        assert response.json() == {
            "job_id": str(repository.job_id),
            "status": "cancelled",
        }


def test_api_prefix_aliases_existing_health_and_job_routes() -> None:
    repository = FakeRepository()
    with TestClient(create_app(_services(repository), validate_startup=False)) as client:
        assert client.get("/api/jobs").status_code == 200
        response = client.get(f"/api/jobs/{repository.job_id}/events")
        assert response.status_code == 200
        assert "event: job.stopped" in response.text


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
        assert jobs.headers["content-type"].startswith("application/json")

        jobs_document = client.get("/jobs", headers={"sec-fetch-dest": "document"})
        assert jobs_document.status_code == 200
        assert jobs_document.headers["content-type"].startswith("text/html")
