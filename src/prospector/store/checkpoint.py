"""LangGraph PostgreSQL checkpointer assembly (langgraph schema)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from prospector.config import Settings, get_settings

_pool: ConnectionPool[Connection[dict[str, Any]]] | None = None


def open_pool(settings: Settings | None = None) -> ConnectionPool[Connection[dict[str, Any]]]:
    global _pool
    if _pool is not None and not _pool.closed:
        return _pool
    cfg = settings or get_settings()
    # Force checkpointer tables into the langgraph schema via search_path.
    _pool = ConnectionPool(
        conninfo=cfg.database_url,
        min_size=1,
        max_size=10,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": "-c search_path=langgraph,public",
        },
        open=True,
    )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None and not _pool.closed:
        _pool.close(timeout=1.0)
    _pool = None


def get_checkpointer(
    settings: Settings | None = None,
) -> PostgresSaver:
    pool = open_pool(settings)
    return PostgresSaver(pool)


def setup_checkpointer(settings: Settings | None = None) -> None:
    """Create langgraph checkpointer tables (idempotent)."""
    checkpointer = get_checkpointer(settings)
    checkpointer.setup()


@contextmanager
def checkpointer_session(
    settings: Settings | None = None,
) -> Iterator[PostgresSaver]:
    """Yield a checkpointer backed by the process pool."""
    yield get_checkpointer(settings)
