"""Transactional persistence for the Planner-Worker research loop."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from prospector.config import Settings, get_settings
from prospector.schemas.brief import ResearchBrief
from prospector.schemas.decisions import PlannerDecision
from prospector.schemas.events import EventType
from prospector.schemas.evidence import Assertion, Document, FindingInput, SourceRef
from prospector.schemas.plan import Plan, ResearchTask
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
                          (id, job_id, question, research_stage, research_mode,
                           source_policy, allowed_tools, expected_evidence, depends_on, budget,
                           status, created_at)
                        VALUES
                          (:id, :job_id, :question, :research_stage, :research_mode,
                           CAST(:source_policy AS JSONB), CAST(:allowed_tools AS JSONB),
                           :expected_evidence, CAST(:depends_on AS JSONB),
                           CAST(:budget AS JSONB), :status, :created_at)
                        """
                    ),
                    {
                        "id": task.task_id,
                        "job_id": job_id,
                        "question": task.question,
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
        gap_note: str,
        summary: object,
        tool_calls_used: int,
        tool_calls_limit: int,
        error: str | None = None,
    ) -> None:
        status = "failed" if error else "done"
        assertion_count = len(self.list_assertions(task_id))
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE app.tasks
                    SET status=:status, stop_reason=:stop_reason, gap_note=:gap_note,
                        worker_summary=CAST(:summary AS JSONB), tool_calls_used=:tool_calls_used,
                        error=:error, finished_at=:now
                    WHERE id=:task_id
                    """
                ),
                {
                    "status": status,
                    "stop_reason": stop_reason,
                    "gap_note": gap_note,
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
                    "used": tool_calls_used,
                    "limit": tool_calls_limit,
                    "assertion_count": assertion_count,
                },
            )

    def get_task_feedback(self, task_id: UUID) -> dict[str, Any]:
        with self.engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        """
                    SELECT status, stop_reason, gap_note, worker_summary, tool_calls_used, error
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
                        SELECT COUNT(*) FROM app.events
                        WHERE task_id=:task_id AND event_type=:event_type
                        """
                    ),
                    {
                        "task_id": task_id,
                        "event_type": EventType.TASK_TOOL_USED.value,
                    },
                ).scalar_one()
            )

    def has_task_tool_event(self, task_id: UUID, tool_call_id: str) -> bool:
        with self.engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        """
                        SELECT EXISTS(
                          SELECT 1 FROM app.events
                          WHERE task_id=:task_id AND event_type=:event_type
                            AND payload->>'tool_call_id'=:tool_call_id
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

    def list_assertions(self, task_id: UUID) -> list[Assertion]:
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
        return [
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

    def count_excerpts(self, job_id: UUID) -> int:
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    text("SELECT COUNT(*) FROM app.excerpts WHERE job_id=:job_id"),
                    {"job_id": job_id},
                ).scalar_one()
            )

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
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.JOB_PHASE_CHANGED,
                payload={"phase": phase, "outcome": outcome, "error_code": error_code},
            )

    def record_phase_changed(self, job_id: UUID, phase: str) -> None:
        with self.engine.begin() as conn:
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
            if exists:
                return
            self._event(
                conn,
                job_id=job_id,
                event_type=EventType.JOB_PHASE_CHANGED,
                payload={"phase": phase, "outcome": None, "error_code": None},
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
