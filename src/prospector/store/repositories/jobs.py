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
CancelRequestSource = Literal["web_monitor", "cli"]


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
                      (id, job_id, question, brief_text, user_constraints,
                       output_format, language, effort, frozen_at)
                    VALUES
                      (:id, :job_id, :question, :brief_text, CAST(:user_constraints AS JSONB),
                       :output_format, :language, :effort, :frozen_at)
                    """
                ),
                {
                    "id": brief_id,
                    "job_id": job_id,
                    "question": brief.question,
                    "brief_text": brief.brief_text,
                    "user_constraints": _json(brief.user_constraints.model_dump(mode="json")),
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

    def request_cancel(
        self,
        job_id: UUID,
        *,
        requested_via: CancelRequestSource,
    ) -> JobRuntimeStatus | None:
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
                payload={
                    "phase": "cancelling",
                    "outcome": None,
                    "error_code": None,
                    "requested_via": requested_via,
                },
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

    def delete_job(self, job_id: UUID) -> JobRuntimeStatus | None:
        """Drop a stopped Job from the history list, keeping its evidence intact.

        Returns the Job's status so the caller can refuse a Job that has not stopped:
        the scheduler still holds a queued or running job_id, and the status is read
        under a row lock so it cannot start between the check and the update.  Nothing
        is erased -- the Excerpts, Document snapshots and checkpoint stay addressable
        by id, because a Document snapshot is shared with every later Job that cited
        the same page.
        """
        with self.engine.begin() as conn:
            status = conn.execute(
                text("SELECT status FROM app.jobs WHERE id=:job_id FOR UPDATE"),
                {"job_id": job_id},
            ).scalar_one_or_none()
            if status is None:
                return None
            if str(status) not in {"completed", "failed", "cancelled"}:
                return str(status)  # type: ignore[return-value]
            conn.execute(
                text(
                    """
                    UPDATE app.jobs SET deleted_at=:now
                    WHERE id=:job_id AND deleted_at IS NULL
                    """
                ),
                {"job_id": job_id, "now": datetime.now(UTC)},
            )
        return str(status)  # type: ignore[return-value]

    # Both report tables are read, newest first.  The current pipeline stores its refs on
    # report_runs_v2; app.reports is no longer written but still holds every report
    # delivered before the refactor, and those Jobs must stay downloadable.
    @staticmethod
    def _stored_ref(conn: Connection, job_id: UUID, column: str) -> str | None:
        for table in ("app.report_runs_v2", "app.reports"):
            value = conn.execute(
                text(f"SELECT {column} FROM {table} WHERE job_id=:job_id"),
                {"job_id": job_id},
            ).scalar_one_or_none()
            if value is not None:
                return str(value)
        return None

    @classmethod
    def _report_refs(cls, conn: Connection, job_id: UUID) -> tuple[str | None, str | None]:
        return (
            cls._stored_ref(conn, job_id, "markdown_ref"),
            cls._stored_ref(conn, job_id, "json_ref"),
        )

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

    # Terminal phases that mean the graph delivered a report.  'draft_rendered' is the
    # pre-refactor name and stays accepted so a checkpoint written before the rename can
    # still finalize; 'report_rendered' is what the current pipeline emits.
    RENDERED_PHASES = frozenset({"draft_rendered", "report_rendered"})

    def finalize_success(self, job_id: UUID, result: Mapping[str, Any]) -> None:
        phase = str(result.get("phase") or "draft_rendered")
        outcome = str(result.get("outcome") or "draft_rendered")
        if phase not in self.RENDERED_PHASES or outcome not in self.RENDERED_PHASES:
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
                           j.outcome, j.error_code,
                           (SELECT r.verification_status FROM app.report_runs_v2 r
                            WHERE r.job_id=j.id) AS verification_status,
                           j.created_at, j.updated_at
                    FROM app.jobs j
                    LEFT JOIN app.briefs b ON b.id=j.brief_id
                    WHERE j.deleted_at IS NULL
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
                                         WHERE p.job_id=j.id), 0) AS plan_version,
                               COALESCE((SELECT MAX(e.id) FROM app.events e
                                         WHERE e.job_id=j.id), 0) AS latest_event_id
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
                        WITH first_plan_task AS (
                            SELECT DISTINCT ON (task_ref.task_id)
                                   task_ref.task_id::uuid AS task_id,
                                   p.version AS plan_version,
                                   task_ref.task_position
                            FROM app.plans p
                            CROSS JOIN LATERAL jsonb_array_elements_text(p.task_ids)
                                WITH ORDINALITY AS task_ref(task_id, task_position)
                            WHERE p.job_id=:job_id
                            ORDER BY task_ref.task_id, p.version, task_ref.task_position
                        )
                        SELECT id AS task_id, question, status, stop_reason, budget,
                               tool_calls_used, created_at, started_at, finished_at
                        FROM app.tasks t
                        LEFT JOIN first_plan_task p ON p.task_id=t.id
                        WHERE t.job_id=:job_id
                        ORDER BY p.plan_version NULLS LAST, p.task_position NULLS LAST,
                                 t.created_at, t.id
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
            # The current pipeline writes report_runs_v2 and keeps its verdict on the
            # row; the legacy table is still read for Jobs that predate the refactor,
            # where the verdict only ever existed in the delivery event.
            report = (
                conn.execute(
                    text(
                        """
                        SELECT r.id AS report_id, r.status, r.verification_status,
                               r.markdown_ref, r.json_ref
                        FROM app.report_runs_v2 r WHERE r.job_id=:job_id
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
            if report is None:
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
        result["verification_status"] = None if report is None else report["verification_status"]
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

    def list_excerpts(self, job_id: UUID, excerpt_ids: list[UUID]) -> list[dict[str, Any]] | None:
        """Return excerpts that belong to *job_id*. Missing or foreign ids yield None."""
        unique_ids = list(dict.fromkeys(excerpt_ids))
        if not unique_ids:
            return []
        if not self.job_exists(job_id):
            return None
        placeholders = ", ".join(f":id_{index}" for index in range(len(unique_ids)))
        params: dict[str, Any] = {"job_id": job_id}
        for index, excerpt_id in enumerate(unique_ids):
            params[f"id_{index}"] = excerpt_id
        with self.engine.connect() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    text(
                        f"""
                        SELECT e.id AS excerpt_id, e.text, e.doc_version, e.locator,
                               d.source_uri,
                               d.source_meta->>'title' AS title,
                               d.source_meta->>'author' AS author,
                               d.source_meta->>'published_at' AS published_at
                        FROM app.excerpts e
                        JOIN app.documents d ON d.id = e.doc_id
                        WHERE e.job_id=:job_id AND e.id IN ({placeholders})
                        """
                    ),
                    params,
                ).mappings()
            ]
        found = {UUID(str(row["excerpt_id"])) for row in rows}
        if any(excerpt_id not in found for excerpt_id in unique_ids):
            return None
        by_id = {UUID(str(row["excerpt_id"])): row for row in rows}
        return [by_id[excerpt_id] for excerpt_id in unique_ids]

    def report_ref(self, job_id: UUID, report_format: Literal["md", "json"]) -> str | None:
        column = "markdown_ref" if report_format == "md" else "json_ref"
        with self.engine.connect() as conn:
            return self._stored_ref(conn, job_id, column)

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
