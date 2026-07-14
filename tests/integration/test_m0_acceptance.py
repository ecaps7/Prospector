"""M0 acceptance: checkpoint kill/resume, MinIO roundtrip, log trace correlation."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from prospector.config import clear_settings_cache, get_settings
from prospector.flow.research_graph import build_empty_flow_graph, thread_config
from prospector.obs.logging import bind_job_id, get_logger, setup_logging
from prospector.obs.tracing import setup_tracing
from prospector.store.checkpoint import (
    checkpointer_session,
    close_pool,
    setup_checkpointer,
)
from prospector.store.jobs import JobStatus, get_job_status, update_job_status
from prospector.store.object_store import ObjectStore, workspace_key

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER = Path(__file__).with_name("_empty_flow_worker.py")


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


def _pg_url() -> str:
    url = get_settings().database_url
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


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


def test_checkpoint_kill_and_resume(tmp_path: Path) -> None:
    """Kill during step_b wait; resume must not re-run step_a."""
    job_id = uuid4()
    wait_file = tmp_path / "continue"
    env = os.environ.copy()
    env["PROSPECTOR_STEP_B_WAIT_FILE"] = str(wait_file)
    env.pop("PROSPECTOR_STEP_B_SLEEP_SECONDS", None)

    proc = subprocess.Popen(
        [sys.executable, str(WORKER), str(job_id)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Wait until step_a is checkpointed (notes contain step_a, not yet step_b done).
    deadline = time.time() + 30
    saw_step_a = False
    while time.time() < deadline:
        with checkpointer_session() as checkpointer:
            graph = build_empty_flow_graph(checkpointer)
            snap = graph.get_state(thread_config(str(job_id)))
        values = snap.values if snap else None
        if values and "step_a" in (values.get("notes") or []):
            saw_step_a = True
            # Still waiting in step_b if continue file absent and process alive.
            if proc.poll() is None:
                break
        time.sleep(0.1)

    assert saw_step_a, (
        "step_a never checkpointed; "
        f"worker_exit={proc.poll()} output={proc.stdout.read() if proc.stdout else ''}"
    )
    assert proc.poll() is None, "worker exited before kill window"

    os.kill(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    # Resume in this process after removing the wait barrier.
    wait_file.write_text("go")
    update_job_status(job_id, JobStatus.RUNNING)
    with checkpointer_session() as checkpointer:
        graph = build_empty_flow_graph(checkpointer)
        result = graph.invoke(None, thread_config(str(job_id)))
    update_job_status(job_id, JobStatus.COMPLETED)

    notes = result["notes"]
    assert notes == ["step_a", "step_b", "step_c"], notes
    assert result["step"] == 3
    assert notes.count("step_a") == 1
    assert get_job_status(job_id) == JobStatus.COMPLETED.value

    # Checkpoint rows live in langgraph schema.
    eng = create_engine(_pg_url())
    with eng.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT COUNT(*) FROM langgraph.checkpoints
                WHERE thread_id = :thread_id
                """
            ),
            {"thread_id": str(job_id)},
        ).scalar_one()
    assert int(row) >= 1
