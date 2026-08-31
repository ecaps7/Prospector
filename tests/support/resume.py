"""Fresh-process recovery probe. No external answers are available in this process."""

import sys
from uuid import UUID

from prospector.config import Settings
from prospector.flow.research_graph import build_research_graph, thread_config
from prospector.store.checkpoint import close_pool, get_checkpointer
from prospector.store.database import clear_engine_cache
from prospector.store.repositories.jobs import JobRepository
from tests.support.providers import Providers

if __name__ == "__main__":
    Settings.model_config["env_file"] = None
    providers = Providers()
    try:
        job_id = UUID(sys.argv[1])
        result = build_research_graph(get_checkpointer(), providers.services()).invoke(
            None,
            thread_config(str(job_id)),
        )
        jobs = JobRepository()
        jobs.finalize_success(job_id, result)
        jobs.finalize_success(job_id, result)
        assert not providers.requests, "Recovery repeated completed model work"
    finally:
        providers.close()
        close_pool()
        clear_engine_cache()
