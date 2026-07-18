"""Transactional persistence for the Planner-Worker research loop."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from prospector.config import Settings, get_settings
from prospector.schemas.brief import EffortLevel, ResearchBrief
from prospector.schemas.decisions import PlannerDecision
from prospector.schemas.events import EventType
from prospector.schemas.evidence import (
    Assertion,
    Document,
    DocumentView,
    FindingInput,
    SourceRef,
    SourceViewItem,
)
from prospector.schemas.plan import Plan, ResearchTask
from prospector.schemas.report import ReportDraft, WriterSnapshot
from prospector.schemas.verifier import (
    AssertionDisposition,
    ConflictResolution,
    VerifierDecision,
    VerifierTrigger,
    conflict_key,
    effective_unusable_assertion_ids,
    validate_verifier_references,
)
from prospector.store.database import get_engine


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _hash(text_value: str) -> str:
    return "sha256:" + hashlib.sha256(text_value.encode("utf-8")).hexdigest()


class ResearchRepository:
    def __init__(
        self,
        engine: Engine | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.engine = engine or get_engine()
        self.settings = settings or get_settings()

    @staticmethod
    def _event(
        conn: Connection,
        *,
        job_id: UUID,
        event_type: EventType,
        payload: dict[str, Any],
        task_id: UUID | None = None,
        decision_round: int | None = None,
    ) -> int:
        return int(
            conn.execute(
                text(
                    """
                    INSERT INTO app.events
                      (job_id, event_type, task_id, decision_round, payload, created_at)
                    VALUES
                      (:job_id, :event_type, :task_id, :decision_round,
                       CAST(:payload AS JSONB), :created_at)
                    RETURNING id
                    """
                ),
                {
                    "job_id": job_id,
                    "event_type": event_type.value,
                    "task_id": task_id,
                    "decision_round": decision_round,
                    "payload": _json(payload),
                    "created_at": datetime.now(UTC),
                },
            ).scalar_one()
        )

    def freeze_brief(self, job_id: UUID, brief: ResearchBrief, confirm_mode: str = "c") -> UUID:
        brief_id = uuid4()
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
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
                text(
                    """
                    UPDATE app.jobs
                    SET brief_id=:brief_id, effort=:effort, thread_id=:thread_id, updated_at=:now
                    WHERE id=:job_id
                    """
                ),
                {
                    "brief_id": brief_id,
                    "effort": brief.effort,
                    "thread_id": str(job_id),
                    "now": now,
                    "job_id": job_id,
                },
            )
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.BRIEF_CONFIRMED,
                payload={
                    "brief_id": str(brief_id),
                    "effort": brief.effort,
                    "confirm_mode": confirm_mode,
                },
            )
        return brief_id

    def get_brief(self, brief_id: UUID) -> ResearchBrief:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    SELECT question, brief_text, output_format, language, effort
                    FROM app.briefs WHERE id=:id
                    """
                    ),
                    {"id": brief_id},
                )
                .mappings()
                .one()
            )
        return ResearchBrief.model_validate(dict(row))

    def get_job_effort(self, job_id: UUID) -> EffortLevel:
        with self.engine.connect() as conn:
            effort = conn.execute(
                text("SELECT effort FROM app.jobs WHERE id=:job_id"),
                {"job_id": job_id},
            ).scalar_one()
        if effort not in {"quick", "standard", "deep"}:
            raise ValueError(f"job has invalid effort: {effort!r}")
        return cast(EffortLevel, effort)

    def begin_decision(
        self,
        job_id: UUID,
        decision_round: int,
        prompt: list[dict[str, Any]],
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO app.decision_log
                      (job_id, decision_round, full_prompt, status, created_at)
                    VALUES (:job_id, :decision_round, CAST(:prompt AS JSONB), 'prompted', :now)
                    ON CONFLICT (job_id, decision_round) DO NOTHING
                    """
                ),
                {
                    "job_id": job_id,
                    "decision_round": decision_round,
                    "prompt": _json(prompt),
                    "now": datetime.now(UTC),
                },
            )

    def get_completed_decision(
        self,
        job_id: UUID,
        decision_round: int,
        prompt: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    SELECT full_prompt, raw_output, decision_payload, feedback, status
                    FROM app.decision_log
                    WHERE job_id=:job_id AND decision_round=:decision_round
                    """
                    ),
                    {"job_id": job_id, "decision_round": decision_round},
                )
                .mappings()
                .first()
            )
        if row is None or row["status"] == "prompted":
            return None
        if row["full_prompt"] != prompt:
            raise RuntimeError(
                f"decision round {decision_round} replayed with a different Planner prompt"
            )
        return dict(row)

    def complete_decision(
        self,
        job_id: UUID,
        decision_round: int,
        *,
        decision: PlannerDecision | None,
        raw_output: object,
        feedback: str | None = None,
        status: str = "accepted",
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE app.decision_log
                    SET raw_output=CAST(:raw AS JSONB), decision_type=:decision_type,
                        decision_payload=CAST(:payload AS JSONB), feedback=:feedback,
                        status=:status, completed_at=:now
                    WHERE job_id=:job_id AND decision_round=:decision_round
                    """
                ),
                {
                    "raw": _json(raw_output),
                    "decision_type": decision.decision if decision else None,
                    "payload": _json(decision.model_dump(mode="json")) if decision else None,
                    "feedback": feedback,
                    "status": status,
                    "now": datetime.now(UTC),
                    "job_id": job_id,
                    "decision_round": decision_round,
                },
            )

    def record_planner_event(
        self,
        job_id: UUID,
        decision_round: int,
        decision: PlannerDecision,
        payload: dict[str, Any],
    ) -> None:
        with self.engine.begin() as conn:
            exists = conn.execute(
                text(
                    """
                    SELECT 1 FROM app.events
                    WHERE job_id=:job_id AND event_type=:event_type
                      AND decision_round=:decision_round
                    LIMIT 1
                    """
                ),
                {
                    "job_id": job_id,
                    "event_type": EventType.PLANNER_DECIDED.value,
                    "decision_round": decision_round,
                },
            ).scalar_one_or_none()
            if exists:
                return
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.PLANNER_DECIDED,
                decision_round=decision_round,
                payload={
                    "decision_round": decision_round,
                    "decision": decision.decision,
                    **payload,
                },
            )

    def record_planner_rejection(
        self,
        job_id: UUID,
        decision_round: int,
        reason_code: str,
    ) -> None:
        with self.engine.begin() as conn:
            exists = conn.execute(
                text(
                    """
                    SELECT 1 FROM app.events
                    WHERE job_id=:job_id AND event_type=:event_type
                      AND decision_round=:decision_round
                    LIMIT 1
                    """
                ),
                {
                    "job_id": job_id,
                    "event_type": EventType.PLANNER_REJECTED.value,
                    "decision_round": decision_round,
                },
            ).scalar_one_or_none()
            if exists:
                return
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.PLANNER_REJECTED,
                decision_round=decision_round,
                payload={"decision_round": decision_round, "reason_code": reason_code},
            )

    def create_plan(
        self,
        job_id: UUID,
        decision_round: int,
        tasks: Sequence[ResearchTask],
        *,
        reason: str,
        trigger_verifier_run: UUID | None = None,
    ) -> Plan:
        now = datetime.now(UTC)
        plan_id = uuid4()
        with self.engine.begin() as conn:
            existing = (
                conn.execute(
                    text(
                        """
                    SELECT id, version, trigger_verifier_run, task_ids, created_at
                    FROM app.plans
                    WHERE job_id=:job_id AND decision_round=:decision_round
                    """
                    ),
                    {"job_id": job_id, "decision_round": decision_round},
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return Plan(
                    plan_id=existing["id"],
                    job_id=job_id,
                    version=existing["version"],
                    decision_round=decision_round,
                    trigger_verifier_run=existing["trigger_verifier_run"],
                    task_ids=[UUID(value) for value in existing["task_ids"]],
                    created_at=existing["created_at"],
                )
            version = int(
                conn.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(version), 0) + 1
                        FROM app.plans WHERE job_id=:job_id
                        """
                    ),
                    {"job_id": job_id},
                ).scalar_one()
            )
            for task in tasks:
                payload = task.model_dump(mode="json")
                conn.execute(
                    text(
                        """
                        INSERT INTO app.tasks
                          (id, job_id, question, subjects, research_stage, research_mode,
                           source_policy, allowed_tools, expected_evidence, depends_on, budget,
                           status, created_at)
                        VALUES
                          (:id, :job_id, :question, CAST(:subjects AS JSONB),
                           :research_stage, :research_mode,
                           CAST(:source_policy AS JSONB), CAST(:allowed_tools AS JSONB),
                           :expected_evidence, CAST(:depends_on AS JSONB),
                           CAST(:budget AS JSONB), :status, :created_at)
                        """
                    ),
                    {
                        "id": task.task_id,
                        "job_id": job_id,
                        "question": task.question,
                        "subjects": _json(payload["subjects"]),
                        "research_stage": task.research_stage,
                        "research_mode": task.research_mode,
                        "source_policy": _json(payload["source_policy"]),
                        "allowed_tools": _json(payload["allowed_tools"]),
                        "expected_evidence": task.expected_evidence,
                        "depends_on": _json(payload["depends_on"]),
                        "budget": _json(payload["budget"]),
                        "status": task.status,
                        "created_at": now,
                    },
                )
            task_ids = [str(task.task_id) for task in tasks]
            conn.execute(
                text(
                    """
                    INSERT INTO app.plans
                      (id, job_id, version, decision_round, trigger_verifier_run,
                       task_ids, created_at)
                    VALUES
                      (:id, :job_id, :version, :decision_round, :trigger_verifier_run,
                       CAST(:task_ids AS JSONB), :created_at)
                    """
                ),
                {
                    "id": plan_id,
                    "job_id": job_id,
                    "version": version,
                    "decision_round": decision_round,
                    "trigger_verifier_run": trigger_verifier_run,
                    "task_ids": _json(task_ids),
                    "created_at": now,
                },
            )
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.PLANNER_DECIDED,
                decision_round=decision_round,
                payload={
                    "decision_round": decision_round,
                    "decision": "dispatch",
                    "plan_version": version,
                    "task_ids": task_ids,
                    "reason": reason.splitlines()[0],
                },
            )
            if trigger_verifier_run is not None:
                self._event(
                    conn,
                    job_id=job_id,
                    event_type=EventType.REPLAN_TRIGGERED,
                    decision_round=decision_round,
                    payload={
                        "verifier_run_id": str(trigger_verifier_run),
                        "plan_version": version,
                        "decision_round": decision_round,
                    },
                )
        return Plan(
            plan_id=plan_id,
            job_id=job_id,
            version=version,
            decision_round=decision_round,
            trigger_verifier_run=trigger_verifier_run,
            task_ids=[task.task_id for task in tasks],
            created_at=now,
        )

    def get_task(self, task_id: UUID) -> ResearchTask:
        with self.engine.connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM app.tasks WHERE id=:id"), {"id": task_id})
                .mappings()
                .one()
            )
        return ResearchTask.model_validate(
            {
                "task_id": row["id"],
                "question": row["question"],
                "subjects": row["subjects"],
                "research_stage": row["research_stage"],
                "research_mode": row["research_mode"],
                "source_policy": row["source_policy"],
                "allowed_tools": row["allowed_tools"],
                "expected_evidence": row["expected_evidence"],
                "depends_on": row["depends_on"],
                "budget": row["budget"],
                "status": row["status"],
            }
        )

    def start_task(self, job_id: UUID, task: ResearchTask) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE app.tasks SET status='running', started_at=:now WHERE id=:id"),
                {"now": now, "id": task.task_id},
            )
            self._event(
                conn,
                job_id=job_id,
                task_id=task.task_id,
                event_type=EventType.TASK_STARTED,
                payload={
                    "task_id": str(task.task_id),
                    "research_stage": task.research_stage,
                    "research_mode": task.research_mode,
                    "subjects": task.subjects,
                    "source_policy": task.source_policy.model_dump(),
                    "question": task.question.splitlines()[0],
                    "budget": task.budget.model_dump(),
                },
            )

    def finish_task(
        self,
        job_id: UUID,
        task_id: UUID,
        *,
        stop_reason: str,
        finish_reason: str,
        summary: object,
        tool_calls_used: int,
        worker_rounds_used: int,
        worker_rounds_limit: int,
        error: str | None = None,
    ) -> None:
        status = "failed" if error else "done"
        assertion_count = len(self.list_assertions(task_id))
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE app.tasks
                    SET status=:status, stop_reason=:stop_reason,
                        finish_reason=:finish_reason,
                        worker_summary=CAST(:summary AS JSONB), tool_calls_used=:tool_calls_used,
                        error=:error, finished_at=:now
                    WHERE id=:task_id
                    """
                ),
                {
                    "status": status,
                    "stop_reason": stop_reason,
                    "finish_reason": finish_reason,
                    "summary": _json(summary),
                    "tool_calls_used": tool_calls_used,
                    "error": error,
                    "now": datetime.now(UTC),
                    "task_id": task_id,
                },
            )
            self._event(
                conn,
                job_id=job_id,
                task_id=task_id,
                event_type=EventType.TASK_FINISHED,
                payload={
                    "task_id": str(task_id),
                    "stop_reason": stop_reason,
                    "tool_calls_used": tool_calls_used,
                    "rounds_used": worker_rounds_used,
                    "rounds_limit": worker_rounds_limit,
                    "assertion_count": assertion_count,
                    "finish_reason": (
                        finish_reason.splitlines()[0].strip() if finish_reason else ""
                    ),
                },
            )

    def get_task_feedback(self, task_id: UUID) -> dict[str, Any]:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    SELECT status, stop_reason, finish_reason,
                           worker_summary, tool_calls_used, error
                    FROM app.tasks WHERE id=:task_id
                    """
                    ),
                    {"task_id": task_id},
                )
                .mappings()
                .one()
            )
        return dict(row)

    def record_tool_used(
        self,
        job_id: UUID,
        task_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        with self.engine.begin() as conn:
            self._event(
                conn,
                job_id=job_id,
                task_id=task_id,
                event_type=EventType.TASK_TOOL_USED,
                payload={"task_id": str(task_id), **payload},
            )

    def count_task_tool_events(self, task_id: UUID) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        """
                        SELECT COUNT(DISTINCT payload->>'tool_call_id') FROM app.events
                        WHERE task_id=:task_id AND event_type=:event_type
                        """
                    ),
                    {
                        "task_id": task_id,
                        "event_type": EventType.TASK_TOOL_USED.value,
                    },
                ).scalar_one()
            )

    def has_task_tool_error_event(self, task_id: UUID, tool_call_id: str) -> bool:
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        """
                        SELECT EXISTS(
                          SELECT 1 FROM app.events
                          WHERE task_id=:task_id AND event_type=:event_type
                            AND payload->>'tool_call_id'=:tool_call_id
                            AND payload ? 'error'
                        )
                        """
                    ),
                    {
                        "task_id": task_id,
                        "event_type": EventType.TASK_TOOL_USED.value,
                        "tool_call_id": tool_call_id,
                    },
                ).scalar_one()
            )

    def next_document_version(self, source_uri: str) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        """
                        SELECT COALESCE(MAX(version), 0) + 1 FROM app.documents
                        WHERE workspace_id=:workspace_id AND source_uri=:source_uri
                        """
                    ),
                    {"workspace_id": self.settings.workspace_id, "source_uri": source_uri},
                ).scalar_one()
            )

    def find_document(self, source_uri: str, content_hash: str) -> Document | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    SELECT * FROM app.documents
                    WHERE workspace_id=:workspace_id AND source_uri=:source_uri
                      AND content_hash=:content_hash
                    """
                    ),
                    {
                        "workspace_id": self.settings.workspace_id,
                        "source_uri": source_uri,
                        "content_hash": content_hash,
                    },
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return self._document_from_row(row)

    @staticmethod
    def _document_from_row(row: Any) -> Document:
        return Document.model_validate(
            {
                "doc_id": row["id"],
                "source_ref": row["source_ref"],
                "content_hash": row["content_hash"],
                "version": row["version"],
                "retrieved_at": row["retrieved_at"],
                "media_type": row["media_type"],
                "storage_ref": row["storage_ref"],
                "index_ref": row["index_ref"],
                "source_meta": row["source_meta"],
            }
        )

    def save_document(
        self,
        *,
        job_id: UUID,
        task_id: UUID,
        doc_id: UUID,
        source_ref: SourceRef,
        content_hash: str,
        version: int,
        media_type: str,
        storage_ref: str,
        source_meta: dict[str, Any],
        tool_call_id: str,
    ) -> Document:
        retrieved_at = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO app.documents
                      (id, workspace_id, source_ref, source_uri, content_hash, version,
                       retrieved_at, media_type, storage_ref, index_ref, source_meta)
                    VALUES
                      (:id, :workspace_id, CAST(:source_ref AS JSONB), :source_uri,
                       :content_hash, :version, :retrieved_at, :media_type, :storage_ref,
                       NULL, CAST(:source_meta AS JSONB))
                    """
                ),
                {
                    "id": doc_id,
                    "workspace_id": self.settings.workspace_id,
                    "source_ref": _json(source_ref.model_dump()),
                    "source_uri": source_ref.uri,
                    "content_hash": content_hash,
                    "version": version,
                    "retrieved_at": retrieved_at,
                    "media_type": media_type,
                    "storage_ref": storage_ref,
                    "source_meta": _json(source_meta),
                },
            )
            self._event(
                conn,
                job_id=job_id,
                task_id=task_id,
                event_type=EventType.TASK_TOOL_USED,
                payload={
                    "task_id": str(task_id),
                    "tool": "web_fetch",
                    "tool_call_id": tool_call_id,
                    "url": source_ref.uri,
                    "doc_id": str(doc_id),
                },
            )
        return Document(
            doc_id=doc_id,
            source_ref=source_ref,
            content_hash=content_hash,
            version=version,
            retrieved_at=retrieved_at,
            media_type=media_type,
            storage_ref=storage_ref,
            source_meta=source_meta,
        )

    def get_document(self, doc_id: UUID) -> Document:
        with self.engine.connect() as conn:
            row = (
                conn.execute(text("SELECT * FROM app.documents WHERE id=:id"), {"id": doc_id})
                .mappings()
                .one()
            )
        return self._document_from_row(row)

    def update_document_media_type(self, doc_id: UUID, media_type: str) -> Document:
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE app.documents SET media_type=:media_type WHERE id=:id"),
                {"id": doc_id, "media_type": media_type},
            )
        return self.get_document(doc_id)

    def save_document_view(
        self,
        *,
        job_id: UUID,
        task_id: UUID,
        document: Document,
        view_kind: Literal["exa_highlights", "kb_read"],
        items: list[SourceViewItem],
    ) -> DocumentView:
        view_id = uuid4()
        created_at = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO app.document_views
                      (id, job_id, task_id, doc_id, doc_version, view_kind, items, created_at)
                    VALUES
                      (:id, :job_id, :task_id, :doc_id, :doc_version, :view_kind,
                       CAST(:items AS JSONB), :created_at)
                    """
                ),
                {
                    "id": view_id,
                    "job_id": job_id,
                    "task_id": task_id,
                    "doc_id": document.doc_id,
                    "doc_version": document.version,
                    "view_kind": view_kind,
                    "items": _json([item.model_dump() for item in items]),
                    "created_at": created_at,
                },
            )
        return DocumentView(
            view_id=view_id,
            job_id=job_id,
            task_id=task_id,
            doc_id=document.doc_id,
            doc_version=document.version,
            view_kind=view_kind,
            items=items,
            created_at=created_at,
        )

    def get_document_view(self, view_id: UUID) -> DocumentView:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT * FROM app.document_views WHERE id=:id"),
                    {"id": view_id},
                )
                .mappings()
                .one()
            )
        return DocumentView.model_validate(
            {
                "view_id": row["id"],
                "job_id": row["job_id"],
                "task_id": row["task_id"],
                "doc_id": row["doc_id"],
                "doc_version": row["doc_version"],
                "view_kind": row["view_kind"],
                "items": row["items"],
                "created_at": row["created_at"],
            }
        )

    def save_findings(
        self,
        *,
        job_id: UUID,
        task_id: UUID,
        document: Document,
        findings: Sequence[tuple[FindingInput, str, dict[str, object]]],
        worker_id: str,
        tool_call_id: str,
    ) -> tuple[list[Assertion], int]:
        assertions: list[Assertion] = []
        inserted_rows = 0
        inserted_excerpts = 0
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            for finding, excerpt_text, locator in findings:
                excerpt_hash = _hash(excerpt_text)
                excerpt_row = conn.execute(
                    text(
                        """
                        INSERT INTO app.excerpts
                          (id, job_id, doc_id, doc_version, text, locator, excerpt_hash,
                           extracted_by, created_at)
                        VALUES
                          (:id, :job_id, :doc_id, :doc_version, :text, CAST(:locator AS JSONB),
                           :excerpt_hash, CAST(:extracted_by AS JSONB), :created_at)
                        ON CONFLICT (job_id, doc_id, excerpt_hash) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "id": uuid4(),
                        "job_id": job_id,
                        "doc_id": document.doc_id,
                        "doc_version": document.version,
                        "text": excerpt_text,
                        "locator": _json(locator),
                        "excerpt_hash": excerpt_hash,
                        "extracted_by": _json(
                            {
                                "task_id": str(task_id),
                                "worker": worker_id,
                                "tool_call_id": tool_call_id,
                            }
                        ),
                        "created_at": now,
                    },
                ).scalar_one_or_none()
                if excerpt_row is None:
                    excerpt_id = conn.execute(
                        text(
                            """
                            SELECT id FROM app.excerpts
                            WHERE job_id=:job_id AND doc_id=:doc_id
                              AND excerpt_hash=:excerpt_hash
                            """
                        ),
                        {
                            "job_id": job_id,
                            "doc_id": document.doc_id,
                            "excerpt_hash": excerpt_hash,
                        },
                    ).scalar_one()
                else:
                    excerpt_id = excerpt_row
                    inserted_rows += 1
                    inserted_excerpts += 1
                statement_hash = _hash(finding.statement)
                assertion_id = uuid4()
                row = conn.execute(
                    text(
                        """
                        INSERT INTO app.assertions
                          (id, job_id, task_id, statement, statement_hash, excerpt_ids,
                           topic_tags, produced_by, created_at)
                        VALUES
                          (:id, :job_id, :task_id, :statement, :statement_hash,
                           CAST(:excerpt_ids AS JSONB), CAST(:topic_tags AS JSONB),
                           CAST(:produced_by AS JSONB), :created_at)
                        ON CONFLICT (job_id, task_id, statement_hash) DO NOTHING
                        RETURNING id
                        """
                    ),
                    {
                        "id": assertion_id,
                        "job_id": job_id,
                        "task_id": task_id,
                        "statement": finding.statement,
                        "statement_hash": statement_hash,
                        "excerpt_ids": _json([str(excerpt_id)]),
                        "topic_tags": _json(finding.topic_tags),
                        "produced_by": _json({"task_id": str(task_id), "worker": worker_id}),
                        "created_at": now,
                    },
                ).scalar_one_or_none()
                if row is None:
                    existing = (
                        conn.execute(
                            text(
                                """
                            SELECT id, excerpt_ids, topic_tags, produced_by
                            FROM app.assertions
                            WHERE job_id=:job_id AND task_id=:task_id
                              AND statement_hash=:statement_hash
                            """
                            ),
                            {
                                "job_id": job_id,
                                "task_id": task_id,
                                "statement_hash": statement_hash,
                            },
                        )
                        .mappings()
                        .one()
                    )
                    row = existing["id"]
                    excerpt_ids = list(existing["excerpt_ids"])
                    if str(excerpt_id) not in excerpt_ids:
                        excerpt_ids.append(str(excerpt_id))
                        conn.execute(
                            text(
                                """
                                UPDATE app.assertions
                                SET excerpt_ids=CAST(:excerpt_ids AS JSONB)
                                WHERE id=:id
                                """
                            ),
                            {"excerpt_ids": _json(excerpt_ids), "id": row},
                        )
                else:
                    inserted_rows += 1
                    excerpt_ids = [str(excerpt_id)]
                assertions.append(
                    Assertion(
                        assertion_id=row,
                        statement=finding.statement,
                        excerpt_ids=[UUID(value) for value in excerpt_ids],
                        topic_tags=finding.topic_tags,
                        produced_by={"task_id": str(task_id), "worker": worker_id},
                    )
                )
            self._event(
                conn,
                job_id=job_id,
                task_id=task_id,
                event_type=EventType.TASK_TOOL_USED,
                payload={
                    "task_id": str(task_id),
                    "tool": "save_findings",
                    "tool_call_id": tool_call_id,
                    "doc_id": str(document.doc_id),
                    "result_count": inserted_rows,
                },
            )
            unique_assertions = list(
                {assertion.assertion_id: assertion for assertion in assertions}.values()
            )
            if inserted_rows:
                self._event(
                    conn,
                    job_id=job_id,
                    task_id=task_id,
                    event_type=EventType.TASK_EVIDENCE_SAVED,
                    payload={
                        "task_id": str(task_id),
                        "assertion_ids": [str(item.assertion_id) for item in unique_assertions],
                        "excerpt_count": inserted_excerpts,
                    },
                )
        return unique_assertions, inserted_rows

    def list_assertions(self, task_id: UUID, *, usable_only: bool = False) -> list[Assertion]:
        with self.engine.connect() as conn:
            rows = (
                conn.execute(
                    text(
                        """
                        SELECT * FROM app.assertions
                        WHERE task_id=:task_id
                        ORDER BY created_at, id
                        """
                    ),
                    {"task_id": task_id},
                )
                .mappings()
                .all()
            )
            job_id = None
            if usable_only and rows:
                job_id = UUID(str(rows[0]["job_id"]))
        assertions = [
            Assertion.model_validate(
                {
                    "assertion_id": row["id"],
                    "statement": row["statement"],
                    "excerpt_ids": row["excerpt_ids"],
                    "topic_tags": row["topic_tags"],
                    "produced_by": row["produced_by"],
                }
            )
            for row in rows
        ]
        if not usable_only or not assertions or job_id is None:
            return assertions
        unusable = self.list_effective_unusable_assertion_ids(job_id)
        return [item for item in assertions if item.assertion_id not in unusable]

    def list_effective_unusable_assertion_ids(self, job_id: UUID) -> set[UUID]:
        """Return assertion IDs whose latest disposition status is unusable."""
        with self.engine.connect() as conn:
            return self._effective_unusable_assertion_ids(conn, job_id)

    @staticmethod
    def _load_dispositions_by_plan_version(
        conn: Connection, job_id: UUID
    ) -> list[tuple[int, list[AssertionDisposition]]]:
        rows = conn.execute(
            text(
                """
                SELECT vr.evaluated_plan_version AS plan_version,
                       d.assertion_id, d.status, d.reason
                FROM app.assertion_dispositions d
                JOIN app.verifier_runs vr ON vr.id = d.verifier_run_id
                WHERE vr.job_id=:job_id AND vr.status='completed'
                ORDER BY vr.evaluated_plan_version, d.assertion_id
                """
            ),
            {"job_id": job_id},
        ).mappings()
        by_version: dict[int, list[AssertionDisposition]] = {}
        for row in rows:
            plan_version = int(row["plan_version"])
            by_version.setdefault(plan_version, []).append(
                AssertionDisposition(
                    assertion_id=UUID(str(row["assertion_id"])),
                    status=row["status"],
                    reason=row["reason"],
                )
            )
        return sorted(by_version.items(), key=lambda item: item[0])

    @classmethod
    def _effective_unusable_assertion_ids(cls, conn: Connection, job_id: UUID) -> set[UUID]:
        return effective_unusable_assertion_ids(
            cls._load_dispositions_by_plan_version(conn, job_id)
        )

    def count_excerpts(self, job_id: UUID) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM app.excerpts WHERE job_id=:job_id"),
                    {"job_id": job_id},
                ).scalar_one()
            )

    def build_verifier_snapshot(
        self,
        job_id: UUID,
        *,
        trigger: VerifierTrigger,
        decision_round: int,
        decision_round_limit: int,
    ) -> dict[str, Any]:
        """Reconstruct the Verifier's authority view from persisted business facts."""
        with self.engine.connect() as conn:
            brief = (
                conn.execute(
                    text(
                        """
                        SELECT b.question, b.brief_text, b.output_format, b.language, b.effort
                        FROM app.briefs b JOIN app.jobs j ON j.brief_id=b.id
                        WHERE j.id=:job_id
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .one()
            )
            plans = [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT id AS plan_id, version, decision_round,
                               trigger_verifier_run, task_ids, created_at
                        FROM app.plans WHERE job_id=:job_id ORDER BY version
                        """
                    ),
                    {"job_id": job_id},
                ).mappings()
            ]
            tasks = [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT id AS task_id, question, subjects, research_stage, research_mode,
                               expected_evidence, status, stop_reason, finish_reason
                        FROM app.tasks WHERE job_id=:job_id ORDER BY created_at, id
                        """
                    ),
                    {"job_id": job_id},
                ).mappings()
            ]
            assertions = [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT id AS assertion_id, task_id, statement, excerpt_ids, topic_tags
                        FROM app.assertions WHERE job_id=:job_id ORDER BY created_at, id
                        """
                    ),
                    {"job_id": job_id},
                ).mappings()
            ]
            excerpts = [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT e.id AS excerpt_id, e.doc_id, e.doc_version, e.text, e.locator,
                               d.source_uri AS url,
                               d.source_meta->>'title' AS title,
                               d.source_meta->>'author' AS author,
                               d.source_meta->>'published_at' AS published_at
                        FROM app.excerpts e JOIN app.documents d ON d.id=e.doc_id
                        WHERE e.job_id=:job_id ORDER BY e.created_at, e.id
                        """
                    ),
                    {"job_id": job_id},
                ).mappings()
            ]
            if trigger == "planner_finish":
                planner_finish_reason = conn.execute(
                    text(
                        """
                        SELECT decision_payload->'finish'->>'reason'
                        FROM app.decision_log
                        WHERE job_id=:job_id AND decision_type='finish' AND status='accepted'
                        ORDER BY decision_round DESC LIMIT 1
                        """
                    ),
                    {"job_id": job_id},
                ).scalar_one_or_none()
            else:
                planner_finish_reason = None

            prior_by_version = self._load_dispositions_by_plan_version(conn, job_id)
            prior_assertion_dispositions = [
                {
                    "plan_version": plan_version,
                    "assertion_id": str(item.assertion_id),
                    "status": item.status,
                    "reason": item.reason,
                }
                for plan_version, dispositions in prior_by_version
                for item in dispositions
            ]
            effective_unusable = sorted(
                str(value) for value in effective_unusable_assertion_ids(prior_by_version)
            )

        if not plans:
            raise RuntimeError("Verifier requires at least one persisted Plan")
        return {
            "brief": dict(brief),
            "plans": plans,
            "tasks": tasks,
            "planner_exit": {
                "trigger": trigger,
                "finish_reason": planner_finish_reason,
                "decision_round": decision_round,
                "decision_round_limit": decision_round_limit,
                "decision_rounds_remaining": max(0, decision_round_limit - decision_round),
            },
            "assertions": assertions,
            "excerpts": excerpts,
            "prior_assertion_dispositions": prior_assertion_dispositions,
            "effective_unusable_assertion_ids": effective_unusable,
        }

    def get_completed_verifier_run(
        self, job_id: UUID, evaluated_plan_version: int
    ) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        SELECT * FROM app.verifier_runs
                        WHERE job_id=:job_id AND evaluated_plan_version=:plan_version
                        """
                    ),
                    {"job_id": job_id, "plan_version": evaluated_plan_version},
                )
                .mappings()
                .first()
            )
            if row is None or row["status"] != "completed":
                return None
            resolutions = [
                dict(item)
                for item in conn.execute(
                    text(
                        """
                        SELECT disputed_point, excerpt_ids, decision, winning_excerpt_ids, rationale
                        FROM app.conflict_resolutions
                        WHERE verifier_run_id=:run_id ORDER BY conflict_key
                        """
                    ),
                    {"run_id": row["id"]},
                ).mappings()
            ]
            dispositions = [
                dict(item)
                for item in conn.execute(
                    text(
                        """
                        SELECT assertion_id, status, reason
                        FROM app.assertion_dispositions
                        WHERE verifier_run_id=:run_id ORDER BY assertion_id
                        """
                    ),
                    {"run_id": row["id"]},
                ).mappings()
            ]
        decision = VerifierDecision.model_validate(
            {
                "release_decision": row["release_decision"],
                "decision_reason": row["decision_reason"],
                "brief_alignment": row["brief_alignment"],
                "coverage_rationale": row["coverage_rationale"],
                "brief_alignment_rationale": row["brief_alignment_rationale"],
                "credibility_rationale": row["credibility_rationale"],
                "gaps": row["gaps"],
                "conflict_resolutions": resolutions,
                "assertion_dispositions": dispositions,
            }
        )
        return {"run_id": row["id"], "decision": decision}

    def begin_verifier_run(
        self,
        job_id: UUID,
        *,
        evaluated_plan_version: int,
        decision_round: int,
        trigger: VerifierTrigger,
        full_prompt: list[dict[str, str]],
    ) -> UUID:
        run_id = uuid4()
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO app.verifier_runs
                      (id, job_id, evaluated_plan_version, decision_round, trigger,
                       full_prompt, status, created_at)
                    VALUES
                      (:id, :job_id, :plan_version, :decision_round, :trigger,
                       CAST(:full_prompt AS JSONB), 'prompted', :now)
                    ON CONFLICT (job_id, evaluated_plan_version) DO NOTHING
                    """
                ),
                {
                    "id": run_id,
                    "job_id": job_id,
                    "plan_version": evaluated_plan_version,
                    "decision_round": decision_round,
                    "trigger": trigger,
                    "full_prompt": _json(full_prompt),
                    "now": datetime.now(UTC),
                },
            )
            row = (
                conn.execute(
                    text(
                        """
                        SELECT id, full_prompt FROM app.verifier_runs
                        WHERE job_id=:job_id AND evaluated_plan_version=:plan_version
                        """
                    ),
                    {"job_id": job_id, "plan_version": evaluated_plan_version},
                )
                .mappings()
                .one()
            )
            if row["full_prompt"] != full_prompt:
                raise RuntimeError("Verifier replayed with a different persisted prompt")
            return UUID(str(row["id"]))

    def complete_verifier_run(
        self,
        job_id: UUID,
        run_id: UUID,
        *,
        decision: VerifierDecision,
        raw_output: object,
        evaluated_plan_version: int,
        decision_round: int,
    ) -> None:
        with self.engine.begin() as conn:
            task_ids = {
                UUID(str(value))
                for value in conn.execute(
                    text("SELECT id FROM app.tasks WHERE job_id=:job_id"),
                    {"job_id": job_id},
                ).scalars()
            }
            assertion_ids = {
                UUID(str(value))
                for value in conn.execute(
                    text("SELECT id FROM app.assertions WHERE job_id=:job_id"),
                    {"job_id": job_id},
                ).scalars()
            }
            excerpt_ids = {
                UUID(str(value))
                for value in conn.execute(
                    text("SELECT id FROM app.excerpts WHERE job_id=:job_id"),
                    {"job_id": job_id},
                ).scalars()
            }
            validate_verifier_references(
                decision,
                task_ids=task_ids,
                assertion_ids=assertion_ids,
                excerpt_ids=excerpt_ids,
            )
            now = datetime.now(UTC)
            updated = conn.execute(
                text(
                    """
                    UPDATE app.verifier_runs
                    SET raw_output=CAST(:raw_output AS JSONB),
                        decision_reason=:decision_reason,
                        coverage_rationale=:coverage_rationale,
                        brief_alignment=:brief_alignment,
                        brief_alignment_rationale=:brief_alignment_rationale,
                        credibility_rationale=:credibility_rationale,
                        release_decision=:release_decision,
                        gaps=CAST(:gaps AS JSONB), status='completed', completed_at=:now
                    WHERE id=:run_id AND job_id=:job_id AND status='prompted'
                    """
                ),
                {
                    "raw_output": _json(raw_output),
                    "decision_reason": decision.decision_reason,
                    "coverage_rationale": decision.coverage_rationale,
                    "brief_alignment": decision.brief_alignment,
                    "brief_alignment_rationale": decision.brief_alignment_rationale,
                    "credibility_rationale": decision.credibility_rationale,
                    "release_decision": decision.release_decision,
                    "gaps": _json([gap.model_dump(mode="json") for gap in decision.gaps]),
                    "now": now,
                    "run_id": run_id,
                    "job_id": job_id,
                },
            ).rowcount
            if updated != 1:
                raise RuntimeError("Verifier run is missing or already completed")
            for resolution in decision.conflict_resolutions:
                self._insert_conflict_resolution(conn, run_id, resolution, now)
            for disposition in decision.assertion_dispositions:
                self._insert_assertion_disposition(conn, run_id, disposition, now)
            major_count = sum(gap.severity == "major" for gap in decision.gaps)
            minor_count = len(decision.gaps) - major_count
            unusable_dispositions = [
                item for item in decision.assertion_dispositions if item.status == "unusable"
            ]
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.VERIFIER_COMPLETED,
                decision_round=decision_round,
                payload={
                    "verifier_run_id": str(run_id),
                    "plan_version": evaluated_plan_version,
                    "release_decision": decision.release_decision,
                    "decision_reason": decision.decision_reason.splitlines()[0],
                    "major_gap_count": major_count,
                    "minor_gap_count": minor_count,
                    "conflict_resolution_count": len(decision.conflict_resolutions),
                    "unusable_assertion_count": len(unusable_dispositions),
                    "gap_summaries": [
                        {
                            "severity": gap.severity,
                            "kind": gap.kind,
                            "description": gap.description.splitlines()[0],
                            "recommended_research": (
                                gap.recommended_research.splitlines()[0]
                                if gap.recommended_research
                                else ""
                            ),
                        }
                        for gap in decision.gaps
                        if gap.severity == "major"
                    ],
                    "conflict_summaries": [
                        {
                            "decision": resolution.decision,
                            "disputed_point": resolution.disputed_point.splitlines()[0],
                        }
                        for resolution in decision.conflict_resolutions
                    ],
                    "unusable_summaries": [
                        {
                            "assertion_id": str(item.assertion_id),
                            "reason": item.reason.splitlines()[0],
                        }
                        for item in unusable_dispositions[:8]
                    ],
                },
            )

    @staticmethod
    def _insert_conflict_resolution(
        conn: Connection,
        run_id: UUID,
        resolution: ConflictResolution,
        created_at: datetime,
    ) -> None:
        conn.execute(
            text(
                """
                INSERT INTO app.conflict_resolutions
                  (conflict_key, verifier_run_id, disputed_point, excerpt_ids,
                   decision, winning_excerpt_ids, rationale, created_at)
                VALUES
                  (:conflict_key, :run_id, :disputed_point, CAST(:excerpt_ids AS JSONB),
                   :decision, CAST(:winners AS JSONB), :rationale, :created_at)
                """
            ),
            {
                "conflict_key": conflict_key(resolution.excerpt_ids),
                "run_id": run_id,
                "disputed_point": resolution.disputed_point,
                "excerpt_ids": _json([str(value) for value in resolution.excerpt_ids]),
                "decision": resolution.decision,
                "winners": _json([str(value) for value in resolution.winning_excerpt_ids]),
                "rationale": resolution.rationale,
                "created_at": created_at,
            },
        )

    @staticmethod
    def _insert_assertion_disposition(
        conn: Connection,
        run_id: UUID,
        disposition: AssertionDisposition,
        created_at: datetime,
    ) -> None:
        conn.execute(
            text(
                """
                INSERT INTO app.assertion_dispositions
                  (assertion_id, verifier_run_id, status, reason, created_at)
                VALUES
                  (:assertion_id, :run_id, :status, :reason, :created_at)
                """
            ),
            {
                "assertion_id": disposition.assertion_id,
                "run_id": run_id,
                "status": disposition.status,
                "reason": disposition.reason,
                "created_at": created_at,
            },
        )

    def list_unusable_assertion_details(
        self, job_id: UUID, dispositions: list[AssertionDisposition]
    ) -> list[dict[str, str]]:
        """Resolve statements for unusable dispositions in the given decision."""
        unusable = [item for item in dispositions if item.status == "unusable"]
        if not unusable:
            return []
        reason_by_id = {item.assertion_id: item.reason for item in unusable}
        details: list[dict[str, str]] = []
        with self.engine.connect() as conn:
            for disposition in unusable:
                row = (
                    conn.execute(
                        text(
                            """
                            SELECT id, statement FROM app.assertions
                            WHERE job_id=:job_id AND id=:assertion_id
                            """
                        ),
                        {
                            "job_id": job_id,
                            "assertion_id": disposition.assertion_id,
                        },
                    )
                    .mappings()
                    .first()
                )
                if row is None:
                    continue
                details.append(
                    {
                        "assertion_id": str(row["id"]),
                        "statement": row["statement"],
                        "reason": reason_by_id[UUID(str(row["id"]))].splitlines()[0],
                    }
                )
        return details

    def set_research_outcome(
        self,
        job_id: UUID,
        *,
        outcome: str,
        error_code: str | None,
        phase: str,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE app.jobs SET outcome=:outcome, error_code=:error_code, updated_at=:now
                        , status=CASE WHEN :outcome='failed' THEN 'failed' ELSE status END
                    WHERE id=:job_id
                    """
                ),
                {
                    "outcome": outcome,
                    "error_code": error_code,
                    "now": datetime.now(UTC),
                    "job_id": job_id,
                },
            )
            exists = conn.execute(
                text(
                    """
                    SELECT 1 FROM app.events
                    WHERE job_id=:job_id AND event_type=:event_type
                      AND payload->>'phase'=:phase
                      AND payload->>'outcome'=:outcome
                      AND COALESCE(payload->>'error_code', '')=COALESCE(:error_code, '')
                    LIMIT 1
                    """
                ),
                {
                    "job_id": job_id,
                    "event_type": EventType.JOB_PHASE_CHANGED.value,
                    "phase": phase,
                    "outcome": outcome,
                    "error_code": error_code,
                },
            ).scalar_one_or_none()
            if exists:
                return
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.JOB_PHASE_CHANGED,
                payload={"phase": phase, "outcome": outcome, "error_code": error_code},
            )

    def build_writer_snapshot(self, job_id: UUID, verifier_run_id: UUID) -> WriterSnapshot:
        """Build the Writer's compact view without Excerpt or Document body text."""
        with self.engine.connect() as conn:
            brief = (
                conn.execute(
                    text(
                        """
                        SELECT b.question, b.brief_text, b.output_format, b.language, b.effort
                        FROM app.briefs b JOIN app.jobs j ON j.brief_id=b.id
                        WHERE j.id=:job_id
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .one()
            )
            verifier = (
                conn.execute(
                    text(
                        """
                        SELECT gaps FROM app.verifier_runs
                        WHERE id=:run_id AND job_id=:job_id AND status='completed'
                          AND release_decision='pass'
                        """
                    ),
                    {"run_id": verifier_run_id, "job_id": job_id},
                )
                .mappings()
                .one()
            )
            plans = [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT version, decision_round, task_ids
                        FROM app.plans WHERE job_id=:job_id ORDER BY version
                        """
                    ),
                    {"job_id": job_id},
                ).mappings()
            ]
            tasks = {
                str(row["id"]): dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT id, question, research_stage, research_mode, expected_evidence,
                               status, stop_reason, finish_reason
                        FROM app.tasks WHERE job_id=:job_id ORDER BY created_at, id
                        """
                    ),
                    {"job_id": job_id},
                ).mappings()
            }
            unusable = self._effective_unusable_assertion_ids(conn, job_id)
            assertion_rows = [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT id, statement, excerpt_ids
                        FROM app.assertions WHERE job_id=:job_id ORDER BY created_at, id
                        """
                    ),
                    {"job_id": job_id},
                ).mappings()
                if UUID(str(row["id"])) not in unusable
            ]
            excerpt_rows = {
                str(row["excerpt_id"]): dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT e.id AS excerpt_id, d.source_uri, d.version AS document_version,
                               d.source_meta->>'title' AS title,
                               d.source_meta->>'author' AS author,
                               d.source_meta->>'published_at' AS published_at
                        FROM app.excerpts e JOIN app.documents d ON d.id=e.doc_id
                        WHERE e.job_id=:job_id
                        """
                    ),
                    {"job_id": job_id},
                ).mappings()
            }
            conflict_rows = [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT conflict_key, disputed_point, excerpt_ids, decision,
                               winning_excerpt_ids, rationale
                        FROM app.conflict_resolutions
                        WHERE verifier_run_id=:run_id ORDER BY conflict_key
                        """
                    ),
                    {"run_id": verifier_run_id},
                ).mappings()
            ]

        plan_summary = []
        for plan in plans:
            plan_summary.append(
                {
                    "version": int(plan["version"]),
                    "decision_round": int(plan["decision_round"]),
                    "tasks": [tasks[str(task_id)] for task_id in plan["task_ids"]],
                }
            )

        cards = []
        for assertion in assertion_rows:
            assertion_excerpt_ids = {str(value) for value in assertion["excerpt_ids"]}
            excerpts = [excerpt_rows[value] for value in assertion_excerpt_ids]
            cards.append(
                {
                    "assertion_id": assertion["id"],
                    "assertion_statement": assertion["statement"],
                    "excerpts": [
                        {
                            "excerpt_id": item["excerpt_id"],
                            "source": {
                                "title": item["title"],
                                "author": item["author"],
                                "published_at": item["published_at"],
                                "source_uri": item["source_uri"],
                                "document_version": item["document_version"],
                            },
                        }
                        for item in excerpts
                    ],
                }
            )
        if not cards:
            raise RuntimeError("Report Writer requires at least one usable Assertion")

        minor_gaps = [gap for gap in (verifier["gaps"] or []) if gap.get("severity") == "minor"]
        return WriterSnapshot.model_validate(
            {
                "job_id": job_id,
                "brief": dict(brief),
                "final_plan_summary": plan_summary,
                "evidence_cards": cards,
                "conflicts": conflict_rows,
                "minor_gaps": minor_gaps,
            }
        )

    def get_report_revision(self, job_id: UUID) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        SELECT r.id AS report_id, r.verifier_run_id, r.status AS report_status,
                               rr.revision, rr.full_prompt, rr.raw_output, rr.draft,
                               rr.status AS revision_status, r.markdown_ref, r.json_ref
                        FROM app.reports r
                        JOIN app.report_revisions rr
                          ON rr.report_id=r.id AND rr.revision=r.current_revision
                        WHERE r.job_id=:job_id
                        """
                    ),
                    {"job_id": job_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        result = dict(row)
        if result["draft"] is not None:
            result["draft"] = ReportDraft.model_validate(result["draft"])
        return result

    def begin_report_revision(
        self,
        job_id: UUID,
        verifier_run_id: UUID,
        full_prompt: list[dict[str, str]],
    ) -> UUID:
        report_id = uuid4()
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO app.reports
                      (id, job_id, verifier_run_id, current_revision, status,
                       created_at, updated_at)
                    VALUES (:id, :job_id, :run_id, 1, 'writing', :now, :now)
                    ON CONFLICT (job_id) DO NOTHING
                    """
                ),
                {"id": report_id, "job_id": job_id, "run_id": verifier_run_id, "now": now},
            )
            report = (
                conn.execute(
                    text("SELECT id, verifier_run_id FROM app.reports WHERE job_id=:job_id"),
                    {"job_id": job_id},
                )
                .mappings()
                .one()
            )
            if UUID(str(report["verifier_run_id"])) != verifier_run_id:
                raise RuntimeError("Report replayed against a different Verifier run")
            report_id = UUID(str(report["id"]))
            conn.execute(
                text(
                    """
                    INSERT INTO app.report_revisions
                      (report_id, revision, full_prompt, status, created_at)
                    VALUES (:report_id, 1, CAST(:prompt AS JSONB), 'prompted', :now)
                    ON CONFLICT (report_id, revision) DO NOTHING
                    """
                ),
                {"report_id": report_id, "prompt": _json(full_prompt), "now": now},
            )
            stored_prompt = conn.execute(
                text(
                    """
                    SELECT full_prompt FROM app.report_revisions
                    WHERE report_id=:report_id AND revision=1
                    """
                ),
                {"report_id": report_id},
            ).scalar_one()
            if stored_prompt != full_prompt:
                raise RuntimeError("Report Writer replayed with a different prompt")
        return report_id

    def complete_report_revision(
        self,
        report_id: UUID,
        draft: ReportDraft,
        raw_output: object,
    ) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            updated = conn.execute(
                text(
                    """
                    UPDATE app.report_revisions
                    SET raw_output=CAST(:raw AS JSONB), draft=CAST(:draft AS JSONB),
                        body_char_count=:chars, status='generated', completed_at=:now
                    WHERE report_id=:report_id AND revision=1 AND status='prompted'
                    """
                ),
                {
                    "raw": _json(raw_output),
                    "draft": _json(draft.model_dump(mode="json")),
                    "chars": draft.body_char_count(),
                    "now": now,
                    "report_id": report_id,
                },
            ).rowcount
            if updated != 1:
                raise RuntimeError("Report revision is missing or already generated")
            statement_order = 0
            paragraph_groups = [
                (section.section_id, section.paragraphs) for section in draft.sections
            ] + [("sec_conclusion", draft.conclusion)]
            for section_id, paragraphs in paragraph_groups:
                for paragraph in paragraphs:
                    for statement in paragraph.statements:
                        statement_order += 1
                        conn.execute(
                            text(
                                """
                                INSERT INTO app.report_statements
                                  (report_id, revision, statement_id, section_id, paragraph_id,
                                   statement_order, kind, text, candidate_excerpt_ids,
                                   premise_statement_ids, created_at)
                                VALUES
                                  (:report_id, 1, :statement_id, :section_id, :paragraph_id,
                                   :statement_order, :kind, :text, CAST(:excerpt_ids AS JSONB),
                                   CAST(:premise_ids AS JSONB), :now)
                                """
                            ),
                            {
                                "report_id": report_id,
                                "statement_id": statement.statement_id,
                                "section_id": section_id,
                                "paragraph_id": paragraph.paragraph_id,
                                "statement_order": statement_order,
                                "kind": statement.kind,
                                "text": statement.text,
                                "excerpt_ids": _json(
                                    [str(value) for value in statement.candidate_excerpt_ids]
                                ),
                                "premise_ids": _json(statement.premise_statement_ids),
                                "now": now,
                            },
                        )

    def complete_report_render(
        self,
        job_id: UUID,
        report_id: UUID,
        *,
        markdown_ref: str,
        markdown_hash: str,
        json_ref: str,
        json_hash: str,
    ) -> None:
        now = datetime.now(UTC)
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE app.report_revisions SET status='rendered'
                    WHERE report_id=:report_id AND revision=1 AND status='generated'
                    """
                ),
                {"report_id": report_id},
            )
            updated = conn.execute(
                text(
                    """
                    UPDATE app.reports
                    SET status='draft_rendered', markdown_ref=:markdown_ref,
                        markdown_hash=:markdown_hash, json_ref=:json_ref,
                        json_hash=:json_hash, updated_at=:now
                    WHERE id=:report_id AND job_id=:job_id AND status='writing'
                    """
                ),
                {
                    "report_id": report_id,
                    "job_id": job_id,
                    "markdown_ref": markdown_ref,
                    "markdown_hash": markdown_hash,
                    "json_ref": json_ref,
                    "json_hash": json_hash,
                    "now": now,
                },
            ).rowcount
            if updated != 1:
                raise RuntimeError("Report is missing or already rendered")
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.REPORT_DRAFT_RENDERED,
                payload={
                    "report_id": str(report_id),
                    "revision": 1,
                    "verification_status": "pending",
                    "markdown_ref": markdown_ref,
                    "json_ref": json_ref,
                },
            )
            conn.execute(
                text(
                    """
                    UPDATE app.jobs SET outcome='draft_rendered', error_code=NULL, updated_at=:now
                    WHERE id=:job_id
                    """
                ),
                {"job_id": job_id, "now": now},
            )
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.JOB_PHASE_CHANGED,
                payload={
                    "phase": "draft_rendered",
                    "outcome": "draft_rendered",
                    "error_code": None,
                },
            )

    def record_phase_changed(
        self,
        job_id: UUID,
        phase: str,
        *,
        plan_version: int | None = None,
        trigger: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"phase": phase, "outcome": None, "error_code": None}
        if plan_version is not None:
            payload["plan_version"] = plan_version
        if trigger is not None:
            payload["trigger"] = trigger
        with self.engine.begin() as conn:
            if plan_version is None:
                exists = conn.execute(
                    text(
                        """
                        SELECT 1 FROM app.events
                        WHERE job_id=:job_id AND event_type=:event_type
                          AND payload->>'phase'=:phase
                        LIMIT 1
                        """
                    ),
                    {
                        "job_id": job_id,
                        "event_type": EventType.JOB_PHASE_CHANGED.value,
                        "phase": phase,
                    },
                ).scalar_one_or_none()
            else:
                exists = conn.execute(
                    text(
                        """
                        SELECT 1 FROM app.events
                        WHERE job_id=:job_id AND event_type=:event_type
                          AND payload->>'phase'=:phase
                          AND payload->>'plan_version'=:plan_version
                        LIMIT 1
                        """
                    ),
                    {
                        "job_id": job_id,
                        "event_type": EventType.JOB_PHASE_CHANGED.value,
                        "phase": phase,
                        "plan_version": str(plan_version),
                    },
                ).scalar_one_or_none()
            if exists:
                return
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.JOB_PHASE_CHANGED,
                payload=payload,
            )

    def list_events(self, job_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text("SELECT * FROM app.events WHERE job_id=:job_id ORDER BY id"),
                    {"job_id": job_id},
                ).mappings()
            ]

    def latest_event_id(self, job_id: UUID) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COALESCE(MAX(id), 0) FROM app.events WHERE job_id=:job_id"),
                    {"job_id": job_id},
                ).scalar_one()
            )

    def list_events_after(self, job_id: UUID, after_id: int) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text(
                        """
                        SELECT * FROM app.events
                        WHERE job_id=:job_id AND id>:after_id
                        ORDER BY id
                        """
                    ),
                    {"job_id": job_id, "after_id": after_id},
                ).mappings()
            ]
