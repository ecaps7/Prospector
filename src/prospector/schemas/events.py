"""Append-only business event vocabulary for the research timeline."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class EventType(StrEnum):
    JOB_PHASE_CHANGED = "job.phase_changed"
    JOB_STOPPED = "job.stopped"
    BRIEF_CONFIRMED = "brief.confirmed"
    PLANNER_DECIDED = "planner.decided"
    PLANNER_REJECTED = "planner.rejected"
    TASK_STARTED = "task.started"
    TASK_ROUND_ADVANCED = "task.round_advanced"
    TASK_TOOL_USED = "task.tool_used"
    TASK_EVIDENCE_SAVED = "task.evidence_saved"
    TASK_FINISHED = "task.finished"
    VERIFIER_COMPLETED = "verifier.completed"
    REPLAN_TRIGGERED = "replan.triggered"
    REPORT_DRAFT_RENDERED = "report.draft_rendered"
    REPORT_GENERATED = "report.generated"
    GAP_ARTIFACT_WRITTEN = "gap_artifact.written"


class ResearchEvent(BaseModel):
    id: int | None = None
    job_id: UUID
    event_type: EventType
    payload: dict[str, Any]
    task_id: UUID | None = None
    decision_round: int | None = None
    created_at: datetime | None = None
