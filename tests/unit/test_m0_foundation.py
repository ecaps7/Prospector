"""Unit tests for M0 foundation (no Docker)."""

from __future__ import annotations

from uuid import UUID

import pytest
from opentelemetry import trace

from prospector.config import (
    DEFAULT_USER_ID,
    DEFAULT_WORKSPACE_ID,
    Settings,
    clear_settings_cache,
)
from prospector.flow.state import EmptyFlowState, empty_flow_state_roundtrip
from prospector.obs.logging import bind_job_id, setup_logging
from prospector.obs.tracing import current_trace_context, setup_tracing
from prospector.store.object_store import workspace_key


def test_settings_require_database_and_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_settings_cache()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("S3_ENDPOINT", raising=False)
    monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_SECRET_KEY", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    with pytest.raises(Exception):  # noqa: B017
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_settings_cache()
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    monkeypatch.setenv("S3_ENDPOINT", "http://localhost:9000")
    monkeypatch.setenv("S3_ACCESS_KEY", "key")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("S3_BUCKET", "bucket")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.database_url.endswith("/db")
    assert settings.s3_bucket == "bucket"
    assert settings.workspace_id == DEFAULT_WORKSPACE_ID
    assert settings.user_id == DEFAULT_USER_ID


def test_empty_flow_state_json_roundtrip() -> None:
    state: EmptyFlowState = {
        "job_id": "11111111-1111-4111-8111-111111111111",
        "step": 2,
        "notes": ["step_a", "step_b"],
    }
    assert empty_flow_state_roundtrip(state) == state


def test_workspace_key_prefix() -> None:
    ws = UUID("00000000-0000-4000-8000-000000000001")
    assert workspace_key(ws, "m0", "smoke", "x") == (
        "00000000-0000-4000-8000-000000000001/m0/smoke/x"
    )


def test_structlog_injects_trace_and_job_id() -> None:
    setup_tracing()
    setup_logging(json_logs=True)
    bind_job_id("job-abc")
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("unit_span"):
        trace_id, span_id = current_trace_context()
        assert trace_id
        assert span_id
        from prospector.obs.logging import _inject_context

        payload = _inject_context(None, "info", {"event": "hello"})
        assert payload["trace_id"] == trace_id
        assert payload["span_id"] == span_id
        assert payload["job_id"] == "job-abc"
        assert payload["service"] == "prospector"
    bind_job_id(None)
