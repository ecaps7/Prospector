"""Minimal app.jobs helpers for M0 start / resume demos."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from prospector.config import Settings, get_settings


class JobStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


def _engine(settings: Settings | None = None) -> Engine:
    cfg = settings or get_settings()
    url = cfg.database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url, pool_pre_ping=True)


def create_job(
    *,
    job_id: UUID | None = None,
    settings: Settings | None = None,
) -> UUID:
    cfg = settings or get_settings()
    jid = job_id or uuid4()
    now = datetime.now(UTC)
    eng = _engine(cfg)
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO app.jobs (id, workspace_id, user_id, status, created_at, updated_at)
                VALUES (:id, :workspace_id, :user_id, :status, :created_at, :updated_at)
                """
            ),
            {
                "id": jid,
                "workspace_id": cfg.workspace_id,
                "user_id": cfg.user_id,
                "status": JobStatus.RUNNING.value,
                "created_at": now,
                "updated_at": now,
            },
        )
    return jid


def update_job_status(
    job_id: UUID,
    status: JobStatus,
    *,
    settings: Settings | None = None,
) -> None:
    eng = _engine(settings)
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE app.jobs
                SET status = :status, updated_at = :updated_at
                WHERE id = :id
                """
            ),
            {
                "id": job_id,
                "status": status.value,
                "updated_at": datetime.now(UTC),
            },
        )


def get_job_status(job_id: UUID, *, settings: Settings | None = None) -> str | None:
    eng = _engine(settings)
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT status FROM app.jobs WHERE id = :id"),
            {"id": job_id},
        ).fetchone()
    return None if row is None else str(row[0])
