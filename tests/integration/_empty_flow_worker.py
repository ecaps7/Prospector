"""Subprocess worker for checkpoint kill/resume integration test."""

from __future__ import annotations

import os
import sys
from uuid import UUID

# Ensure src is importable when launched as a script.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from prospector.config import clear_settings_cache, get_settings  # noqa: E402
from prospector.flow.research_graph import build_empty_flow_graph, thread_config  # noqa: E402
from prospector.flow.state import EmptyFlowState  # noqa: E402
from prospector.obs.logging import bind_job_id, setup_logging  # noqa: E402
from prospector.obs.tracing import setup_tracing  # noqa: E402
from prospector.store.checkpoint import checkpointer_session, close_pool  # noqa: E402
from prospector.store.jobs import JobStatus, create_job, update_job_status  # noqa: E402


def main() -> None:
    job_id = UUID(sys.argv[1])
    clear_settings_cache()
    setup_logging()
    setup_tracing()
    get_settings()
    bind_job_id(str(job_id))
    create_job(job_id=job_id)
    try:
        with checkpointer_session() as checkpointer:
            graph = build_empty_flow_graph(checkpointer)
            initial: EmptyFlowState = {
                "job_id": str(job_id),
                "step": 0,
                "notes": [],
            }
            graph.invoke(initial, thread_config(str(job_id)))
        update_job_status(job_id, JobStatus.COMPLETED)
    finally:
        bind_job_id(None)
        close_pool()


if __name__ == "__main__":
    main()
