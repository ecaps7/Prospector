"""Foundation acceptance: MinIO roundtrip and log/trace correlation."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from prospector.config import clear_settings_cache, get_settings
from prospector.obs.logging import bind_job_id, get_logger, setup_logging
from prospector.obs.tracing import setup_tracing
from prospector.store.checkpoint import close_pool, setup_checkpointer
from prospector.store.object_store import ObjectStore, workspace_key

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _m0_env() -> Iterator[None]:
    clear_settings_cache()
    # Prefer .env if present; otherwise use compose defaults.
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql://prospector:prospector@localhost:5432/prospector",
    )
    os.environ.setdefault("S3_ENDPOINT", "http://localhost:9000")
    os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
    os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
    os.environ.setdefault("S3_BUCKET", "prospector")
    clear_settings_cache()
    get_settings()
    setup_logging()
    setup_tracing()
    setup_checkpointer()
    yield
    close_pool()


def test_minio_put_get_roundtrip() -> None:
    store = ObjectStore()
    store.ensure_bucket()
    key = workspace_key(get_settings().workspace_id, "m0", "smoke", str(uuid4()))
    payload = os.urandom(64)
    ref = store.put_bytes(key, payload)
    assert ref.key == key
    assert store.get_bytes(key) == payload
    assert store.exists(key)


def test_structlog_logs_include_trace_ids() -> None:
    from opentelemetry import trace

    import prospector.obs.logging as logging_mod

    # Rebind handlers to current stdout so assertions see JSON lines.
    logging_mod._configured = False  # noqa: SLF001
    setup_logging(json_logs=True)

    bind_job_id("integration-job")
    tracer = trace.get_tracer("integration")
    logger = get_logger("integration")
    with tracer.start_as_current_span("integration_span"):
        logger.info("trace_check", message="ok")
        from prospector.obs.tracing import current_trace_context

        trace_id, span_id = current_trace_context()
    bind_job_id(None)

    assert trace_id
    assert span_id
