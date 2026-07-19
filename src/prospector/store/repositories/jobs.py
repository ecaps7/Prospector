"""Persistence used by the single-process API job scheduler."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from prospector.config import Settings, get_settings
from prospector.schemas.brief import ResearchBrief
from prospector.schemas.events import EventType
from prospector.store.database import get_engine

JobRuntimeStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "cancelled",
    "completed",
    "failed",
]


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class JobRepository:
    """Own API-facing job lifecycle facts without changing the research graph."""

    def __init__(
        self,
        engine: Engine | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.engine = engine or get_engine()
        self.settings = settings or get_settings()

    @staticmethod
    def _append_event(
        conn: Connection,
        *,
        job_id: UUID,
        event_type: EventType,
        payload: Mapping[str, Any],
    ) -> int:
        return int(
            conn.execute(
                text(
                    """
                    INSERT INTO app.events
                      (job_id, event_type, task_id, decision_round, payload, created_at)
                    VALUES
                      (:job_id, :event_type, NULL, NULL, CAST(:payload AS JSONB), :created_at)
                    RETURNING id
                    """
                ),
                {
                    "job_id": job_id,
                    "event_type": event_type.value,
                    "payload": _json(dict(payload)),
                    "created_at": datetime.now(UTC),
                },
            ).scalar_one()
        )

    def create_with_brief(
        self,
        brief: ResearchBrief,
        *,
        start_immediately: bool,
    ) -> dict[str, Any]:
        """Atomically create the Job, frozen Brief, and initial events."""
        job_id = uuid4()
        brief_id = uuid4()
        now = datetime.now(UTC)
        status: JobRuntimeStatus = "running" if start_immediately else "queued"
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO app.jobs
                      (id, workspace_id, user_id, status, created_at, updated_at,
                       effort, brief_id, outcome, error_code, thread_id)
                    VALUES
                      (:id, :workspace_id, :user_id, :status, :now, :now,
                       :effort, NULL, NULL, NULL, :thread_id)
                    """
                ),
                {
                    "id": job_id,
                    "workspace_id": self.settings.workspace_id,
                    "user_id": self.settings.user_id,
                    "status": status,
                    "now": now,
                    "effort": brief.effort,
                    "thread_id": str(job_id),
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO app.briefs
                      (id, job_id, question, brief_text, output_format, language, effort, frozen_at)
                    VALUES
                      (:id, :job_id, :question, :brief_text, :output_format,
                       :language, :effort, :frozen_at)
                    """
                ),
                {
                    "id": brief_id,
                    "job_id": job_id,
                    "question": brief.question,
                    "brief_text": brief.brief_text,
                    "output_format": brief.output_format,
                    "language": brief.language,
                    "effort": brief.effort,
                    "frozen_at": now,
                },
            )
            conn.execute(
                text("UPDATE app.jobs SET brief_id=:brief_id WHERE id=:job_id"),
                {"brief_id": brief_id, "job_id": job_id},
            )
            self._append_event(
                conn,
                job_id=job_id,
                event_type=EventType.BRIEF_CONFIRMED,
                payload={
                    "brief_id": str(brief_id),
                    "effort": brief.effort,
                    "confirm_mode": "c",
                },
            )
            self._append_event(
                conn,
                job_id=job_id,
                event_type=EventType.JOB_PHASE_CHANGED,
                payload={"phase": status, "outcome": None, "error_code": None},
            )
            queue_position = None
            if status == "queued":
                queue_position = int(
                    conn.execute(
                        text("SELECT COUNT(*) FROM app.jobs WHERE status='queued'")
                    ).scalar_one()
                )
        return {
            "job_id": job_id,
            "brief_id": brief_id,
            "status": status,
            "queue_position": queue_position,
        }

    def recoverable_jobs(self) -> list[dict[str, Any]]:
        """Return interrupted running work first, then queued work in FIFO order."""
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT id AS job_id, brief_id, status, created_at
                        FROM app.jobs j
                        WHERE status IN ('running','queued')
                          AND brief_id IS NOT NULL
                          AND NOT EXISTS (
                            SELECT 1 FROM app.events e
                            WHERE e.job_id=j.id AND e.event_type='job.stopped'
                          )
                        ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at, id
                        """
                    )
                ).mappings()
            ]

    def mark_running(self, job_id: UUID) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            updated = conn.execute(
                text(
                    """
                    UPDATE app.jobs SET status='running', updated_at=:now
                    WHERE id=:job_id AND status='queued'
                    """
                ),
                {"job_id": job_id, "now": now},
            ).rowcount
            if updated:
                self._append_event(
                    conn,
                    job_id=job_id,
                    event_type=EventType.JOB_PHASE_CHANGED,
                    payload={"phase": "running", "outcome": None, "error_code": None},
                )

    def mark_queued(self, job_id: UUID) -> None:
        """Normalize an extra interrupted runner before FIFO recovery."""
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            updated = conn.execute(
                text(
                    """
                    UPDATE app.jobs SET status='queued', updated_at=:now
                    WHERE id=:job_id AND status='running'
                    """
                ),
                {"job_id": job_id, "now": now},
            ).rowcount
            if updated:
                self._append_event(
                    conn,
                    job_id=job_id,
                    event_type=EventType.JOB_PHASE_CHANGED,
                    payload={"phase": "queued", "outcome": None, "error_code": None},
                )

    def runtime_input(self, job_id: UUID) -> dict[str, UUID]:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT id AS job_id, brief_id FROM app.jobs WHERE id=:job_id"),
                    {"job_id": job_id},
                )
                .mappings()
                .one()
            )
        return {"job_id": UUID(str(row["job_id"])), "brief_id": UUID(str(row["brief_id"]))}

    def request_cancel(self, job_id: UUID) -> JobRuntimeStatus | None:
        cancel_immediately = False
        with self.engine.begin() as conn:
            current = conn.execute(
                text("SELECT status FROM app.jobs WHERE id=:job_id FOR UPDATE"),
                {"job_id": job_id},
            ).scalar_one_or_none()
            if current is None:
                return None
            status = str(current)
            if status in {"completed", "failed", "cancelled"}:
                return status  # type: ignore[return-value]
            if status == "cancelling":
                return "cancelling"
            cancel_immediately = status == "queued"
            conn.execute(
                text(
                    """
                    UPDATE app.jobs SET status='cancelling', updated_at=:now
                    WHERE id=:job_id
                    """
                ),
                {"job_id": job_id, "now": datetime.now(UTC)},
            )
            self._append_event(
                conn,
                job_id=job_id,
                event_type=EventType.JOB_PHASE_CHANGED,
                payload={"phase": "cancelling", "outcome": None, "error_code": None},
            )
        if cancel_immediately:
            self.finalize_cancelled(job_id)
            return "cancelled"
        return "cancelling"

    def cancel_requested(self, job_id: UUID) -> bool:
        with self.engine.connect() as conn:
            status = conn.execute(
                text("SELECT status FROM app.jobs WHERE id=:job_id"),
                {"job_id": job_id},
            ).scalar_one_or_none()
        return status in {"cancelling", "cancelled"}

    def finalize_pending_cancellations(self) -> None:
        with self.engine.connect() as conn:
            job_ids = [
                UUID(str(value))
                for value in conn.execute(
                    text("SELECT id FROM app.jobs WHERE status='cancelling'")
                ).scalars()
            ]
        for job_id in job_ids:
            self.finalize_cancelled(job_id)

    @staticmethod
    def _report_refs(conn: Connection, job_id: UUID) -> tuple[str | None, str | None]:
        row = (
            conn.execute(
                text("SELECT markdown_ref, json_ref FROM app.reports WHERE job_id=:job_id"),
                {"job_id": job_id},
            )
            .mappings()
            .first()
        )
        if row is None:
            return None, None
        return row["markdown_ref"], row["json_ref"]

    @staticmethod
    def _latest_phase(conn: Connection, job_id: UUID) -> str | None:
        value = conn.execute(
            text(
                """
                SELECT payload->>'phase' FROM app.events
                WHERE job_id=:job_id AND event_type='job.phase_changed'
                ORDER BY id DESC LIMIT 1
                """
            ),
            {"job_id": job_id},
        ).scalar_one_or_none()
        return None if value is None else str(value)

    def finalize_success(self, job_id: UUID, result: Mapping[str, Any]) -> None:
        phase = str(result.get("phase") or "draft_rendered")
        outcome = str(result.get("outcome") or "draft_rendered")
        if phase != "draft_rendered" or outcome != "draft_rendered":
            self.finalize_failure(job_id, fallback_error_code="job_execution_error")
            return
        self._finalize(
            job_id,
            status="completed",
            phase=phase,
            outcome=outcome,
            error_code=None,
        )

    def finalize_failure(self, job_id: UUID, *, fallback_error_code: str) -> None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT outcome, error_code FROM app.jobs WHERE id=:job_id"),
                    {"job_id": job_id},
                )
                .mappings()
                .one()
            )
        error_code = (
            str(row["error_code"])
            if row["outcome"] == "failed" and row["error_code"]
            else fallback_error_code
        )
        self._finalize(
            job_id,
            status="failed",
            phase="failed",
            outcome="failed",
            error_code=error_code,
        )

    def finalize_cancelled(self, job_id: UUID) -> None:
        self._finalize(
            job_id,
            status="cancelled",
            phase="cancelled",
            outcome="cancelled",
            error_code=None,
        )

    def _finalize(
        self,
        job_id: UUID,
        *,
        status: Literal["completed", "failed", "cancelled"],
        phase: str,
        outcome: str,
        error_code: str | None,
    ) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            current_status = conn.execute(
                text("SELECT status FROM app.jobs WHERE id=:job_id FOR UPDATE"),
                {"job_id": job_id},
            ).scalar_one()
            stopped = conn.execute(
                text(
                    """
                    SELECT 1 FROM app.events
                    WHERE job_id=:job_id AND event_type='job.stopped' LIMIT 1
                    """
                ),
                {"job_id": job_id},
            ).scalar_one_or_none()
            if stopped:
                return
            if current_status == "cancelling" and status != "cancelled":
                status = "cancelled"
                phase = "cancelled"
                outcome = "cancelled"
                error_code = None
            conn.execute(
                text(
                    """
                    UPDATE app.jobs
                    SET status=:status, outcome=:outcome, error_code=:error_code, updated_at=:now
                    WHERE id=:job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "status": status,
                    "outcome": outcome,
                    "error_code": error_code,
                    "now": now,
                },
            )
            if status == "cancelled":
                conn.execute(
                    text(
                        """
                        UPDATE app.tasks
                        SET status='cancelled', stop_reason='job_cancelled',
                            finished_at=COALESCE(finished_at, :now)
                        WHERE job_id=:job_id AND status IN ('pending','running')
                        """
                    ),
                    {"job_id": job_id, "now": now},
                )
            if self._latest_phase(conn, job_id) != phase:
                self._append_event(
                    conn,
                    job_id=job_id,
                    event_type=EventType.JOB_PHASE_CHANGED,
                    payload={"phase": phase, "outcome": outcome, "error_code": error_code},
                )
            markdown_ref, json_ref = self._report_refs(conn, job_id)
            self._append_event(
                conn,
                job_id=job_id,
                event_type=EventType.JOB_STOPPED,
                payload={
                    "status": status,
                    "phase": phase,
                    "outcome": outcome,
                    "error_code": error_code,
                    "report_markdown_ref": markdown_ref,
                    "report_json_ref": json_ref,
                },
            )

    def job_exists(self, job_id: UUID) -> bool:
        with self.engine.connect() as conn:
            return (
                conn.execute(
                    text("SELECT 1 FROM app.jobs WHERE id=:job_id"), {"job_id": job_id}
                ).scalar_one_or_none()
                is not None
            )

    def list_jobs(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT j.id AS job_id, b.question, b.effort, j.status,
                           COALESCE(
                             (SELECT e.payload->>'phase' FROM app.events e
                              WHERE e.job_id=j.id AND e.event_type='job.phase_changed'
                              ORDER BY e.id DESC LIMIT 1),
                             j.status
                           ) AS phase,
                           j.outcome, j.error_code, j.created_at, j.updated_at
                    FROM app.jobs j
                    LEFT JOIN app.briefs b ON b.id=j.brief_id
                    ORDER BY j.created_at DESC, j.id DESC
                    """
                )
            ).mappings()
            return [dict(row) for row in rows]

    def get_job(self, job_id: UUID) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            base = (
                conn.execute(
                    text(
                        """
                        SELECT j.id AS job_id, j.brief_id, b.question, b.effort, b.language,
                               j.status,
                               COALESCE(
                                 (SELECT e.payload->>'phase' FROM app.events e
                                  WHERE e.job_id=j.id AND e.event_type='job.phase_changed'
                                  ORDER BY e.id DESC LIMIT 1), j.status
                               ) AS phase,
                               j.outcome, j.error_code, j.created_at, j.updated_at,
                               COALESCE((SELECT MAX(version) FROM app.plans p
                                         WHERE p.job_id=j.id), 0) AS plan_version
                        FROM app.jobs j
                        LEFT JOIN app.briefs b ON b.id=j.brief_id
                        WHERE j.id=:job_id
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
            if base is None:
                return None
            tasks = [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT id AS task_id, question, subjects, research_stage,
                               research_mode, status, stop_reason, budget,
                               tool_calls_used, created_at, started_at, finished_at
                        FROM app.tasks WHERE job_id=:job_id ORDER BY created_at, id
                        """
                    ),
                    {"job_id": job_id},
                ).mappings()
            ]
            usage = [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT component,
                               COALESCE(SUM(input_tokens), 0) AS input_tokens,
                               COALESCE(SUM(output_tokens), 0) AS output_tokens,
                               COALESCE(SUM(tool_calls), 0) AS tool_calls
                        FROM app.usage WHERE job_id=:job_id
                        GROUP BY component ORDER BY component
                        """
                    ),
                    {"job_id": job_id},
                ).mappings()
            ]
            report = (
                conn.execute(
                    text(
                        """
                        SELECT r.id AS report_id, r.status, r.markdown_ref, r.json_ref,
                               (SELECT e.payload->>'verification_status' FROM app.events e
                                WHERE e.job_id=r.job_id
                                  AND e.event_type='report.draft_rendered'
                                ORDER BY e.id DESC LIMIT 1) AS verification_status
                        FROM app.reports r WHERE r.job_id=:job_id
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
        result = dict(base)
        result["tasks"] = tasks
        result["usage"] = usage
        result["report"] = None if report is None else dict(report)
        return result

    def list_events_after(self, job_id: UUID, after_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT id, event_type, task_id, decision_round, payload, created_at
                        FROM app.events
                        WHERE job_id=:job_id AND id>:after_id ORDER BY id
                        """
                    ),
                    {"job_id": job_id, "after_id": after_id},
                ).mappings()
            ]

    def stopped_event_id(self, job_id: UUID) -> int | None:
        with self.engine.connect() as conn:
            value = conn.execute(
                text(
                    """
                    SELECT id FROM app.events
                    WHERE job_id=:job_id AND event_type='job.stopped'
                    ORDER BY id DESC LIMIT 1
                    """
                ),
                {"job_id": job_id},
            ).scalar_one_or_none()
        return None if value is None else int(value)

    def report_ref(self, job_id: UUID, report_format: Literal["md", "json"]) -> str | None:
        column = "markdown_ref" if report_format == "md" else "json_ref"
        with self.engine.connect() as conn:
            value = conn.execute(
                text(f"SELECT {column} FROM app.reports WHERE job_id=:job_id"),
                {"job_id": job_id},
            ).scalar_one_or_none()
        return None if value is None else str(value)

    def health_check(self) -> None:
        required = (
            "app.jobs",
            "app.briefs",
            "app.events",
            "app.reports",
            "langgraph.checkpoints",
        )
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1")).scalar_one()
            missing = [
                table
                for table in required
                if conn.execute(text("SELECT to_regclass(:table)"), {"table": table}).scalar_one()
                is None
            ]
        if missing:
            raise RuntimeError("missing required tables: " + ", ".join(missing))
