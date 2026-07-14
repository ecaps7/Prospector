"""Single-process local entrypoint: setup / run / resume empty flow."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid4

import typer
from opentelemetry import trace

from prospector.config import clear_settings_cache, get_settings
from prospector.flow.research_graph import build_empty_flow_graph, thread_config
from prospector.flow.state import EmptyFlowState
from prospector.obs.logging import bind_job_id, get_logger, setup_logging
from prospector.obs.tracing import setup_tracing
from prospector.store.checkpoint import (
    checkpointer_session,
    close_pool,
    setup_checkpointer,
)
from prospector.store.jobs import JobStatus, create_job, update_job_status
from prospector.store.object_store import ObjectStore

app = typer.Typer(add_completion=False, no_args_is_help=True)
log = get_logger("prospector.local")
tracer = trace.get_tracer("prospector.local")


def _bootstrap() -> None:
    clear_settings_cache()
    setup_logging()
    setup_tracing()
    get_settings()  # fail-fast on missing env


@app.command()
def setup() -> None:
    """Create checkpointer tables and ensure the MinIO bucket exists."""
    _bootstrap()
    setup_checkpointer()
    store = ObjectStore()
    store.ensure_bucket()
    log.info("setup_complete", message="checkpointer + bucket ready")
    close_pool()


@app.command("run")
def run_job(
    job_id: Annotated[
        UUID | None,
        typer.Option("--job-id", help="Reuse an existing job/thread id"),
    ] = None,
) -> None:
    """Start a new empty-flow job (or continue if checkpoint already exists)."""
    _bootstrap()
    jid = job_id or uuid4()
    create_job(job_id=jid)
    bind_job_id(str(jid))
    try:
        with tracer.start_as_current_span("empty_flow.run") as span:
            span.set_attribute("job_id", str(jid))
            with checkpointer_session() as checkpointer:
                graph = build_empty_flow_graph(checkpointer)
                initial: EmptyFlowState = {
                    "job_id": str(jid),
                    "step": 0,
                    "notes": [],
                }
                result = graph.invoke(initial, thread_config(str(jid)))
            update_job_status(jid, JobStatus.COMPLETED)
            log.info(
                "empty_flow_completed",
                message="empty flow finished",
                job_id=str(jid),
                step=result.get("step"),
                notes=result.get("notes"),
            )
            typer.echo(str(jid))
    except Exception:
        update_job_status(jid, JobStatus.FAILED)
        raise
    finally:
        bind_job_id(None)
        close_pool()


@app.command()
def resume(
    job_id: Annotated[UUID, typer.Argument(help="Job / thread id to resume")],
) -> None:
    """Resume an interrupted empty-flow job from PG checkpoint."""
    _bootstrap()
    bind_job_id(str(job_id))
    try:
        update_job_status(job_id, JobStatus.RUNNING)
        with tracer.start_as_current_span("empty_flow.resume") as span:
            span.set_attribute("job_id", str(job_id))
            with checkpointer_session() as checkpointer:
                graph = build_empty_flow_graph(checkpointer)
                # None input resumes from the latest checkpoint for this thread.
                result = graph.invoke(None, thread_config(str(job_id)))
            update_job_status(job_id, JobStatus.COMPLETED)
            log.info(
                "empty_flow_resumed",
                message="empty flow resumed to completion",
                job_id=str(job_id),
                step=result.get("step"),
                notes=result.get("notes"),
            )
            typer.echo(str(job_id))
    except Exception:
        update_job_status(job_id, JobStatus.FAILED)
        raise
    finally:
        bind_job_id(None)
        close_pool()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
