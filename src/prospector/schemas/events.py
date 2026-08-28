"""Append-only business event vocabulary for the research timeline."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel, TypeAdapter

from prospector.schemas.brief import EffortLevel
from prospector.schemas.plan import TaskBudget


class EventType(StrEnum):
    JOB_PHASE_CHANGED = "job.phase_changed"
    JOB_STOPPED = "job.stopped"
    BRIEF_CONFIRMED = "brief.confirmed"
    PLANNER_STARTED = "planner.started"
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


class _EventPayload(BaseModel):
    model_config = ConfigDict(extra="allow")


class JobPhaseChangedPayload(_EventPayload):
    phase: str
    outcome: str | None = None
    error_code: str | None = None
    plan_version: int | None = None
    trigger: str | None = None
    revision: int | None = None
    requested_via: Literal["web_monitor", "cli"] | None = None


class JobStoppedPayload(_EventPayload):
    status: Literal["completed", "failed", "cancelled"]
    phase: str
    outcome: str | None = None
    error_code: str | None = None
    report_markdown_ref: str | None = None
    report_json_ref: str | None = None


class BriefConfirmedPayload(_EventPayload):
    brief_id: UUID
    effort: EffortLevel
    confirm_mode: Literal["c", "e", "i"]


class PlannerDecidedPayload(_EventPayload):
    decision_round: int
    decision: Literal["dispatch", "finish"]
    research_decisions_used: int | None = None
    plan_version: int | None = None
    task_ids: list[UUID] | None = None
    reason: str | None = None


class PlannerStartedPayload(_EventPayload):
    decision_round: int


class PlannerRejectedPayload(_EventPayload):
    decision_round: int
    reason_code: str
    research_decisions_used: int | None = None


class TaskStartedPayload(_EventPayload):
    task_id: UUID
    question: str
    budget: TaskBudget


class TaskRoundAdvancedPayload(_EventPayload):
    task_id: UUID
    rounds_used: int
    rounds_limit: int


class TaskToolUsedPayload(_EventPayload):
    task_id: UUID
    tool: str
    tool_call_id: str
    query: str | None = None
    url: str | None = None
    result_count: int | None = None
    doc_id: UUID | None = None
    error: str | None = None


class TaskEvidenceSavedPayload(_EventPayload):
    task_id: UUID
    assertion_ids: list[UUID]
    excerpt_count: int


class TaskFinishedPayload(_EventPayload):
    task_id: UUID
    stop_reason: str
    tool_calls_used: int
    rounds_used: int
    rounds_limit: int
    assertion_count: int
    finish_reason: str | None = None
    error: str | None = None


class VerifierGapSummary(_EventPayload):
    severity: str
    kind: str
    description: str
    evidence_needed: str = ""


class VerifierConflictSummary(_EventPayload):
    decision: str
    disputed_point: str


class VerifierUnusableSummary(_EventPayload):
    assertion_id: UUID
    reason: str


class VerifierCompletedPayload(_EventPayload):
    verifier_run_id: UUID
    plan_version: int
    release_decision: Literal["pass", "needs_research"]
    decision_reason: str
    major_gap_count: int
    minor_gap_count: int
    conflict_resolution_count: int
    unusable_assertion_count: int
    research_decisions_used: int | None = None
    gap_summaries: list[VerifierGapSummary] = Field(default_factory=list)
    conflict_summaries: list[VerifierConflictSummary] = Field(default_factory=list)
    unusable_summaries: list[VerifierUnusableSummary] = Field(default_factory=list)


class ReplanTriggeredPayload(_EventPayload):
    verifier_run_id: UUID
    plan_version: int
    decision_round: int | None = None


class ReportDraftRenderedPayload(_EventPayload):
    report_id: UUID
    verification_status: str
    markdown_ref: str | None = None
    json_ref: str | None = None
    revision: int | None = None
    # Deterministic structure metrics (see deterministic/report_structure.py). Recorded
    # for observation only: nothing gates on them until their distribution is known.
    structure: dict[str, Any] = Field(default_factory=dict)


class SseEventBase(BaseModel):
    """JSON object written to each SSE `data:` line."""

    model_config = ConfigDict(extra="allow")

    task_id: UUID | None = None
    decision_round: int | None = None
    created_at: datetime | None = None


class JobPhaseChangedEvent(SseEventBase):
    event_type: Literal["job.phase_changed"]
    payload: JobPhaseChangedPayload


class JobStoppedEvent(SseEventBase):
    event_type: Literal["job.stopped"]
    payload: JobStoppedPayload


class BriefConfirmedEvent(SseEventBase):
    event_type: Literal["brief.confirmed"]
    payload: BriefConfirmedPayload


class PlannerDecidedEvent(SseEventBase):
    event_type: Literal["planner.decided"]
    payload: PlannerDecidedPayload


class PlannerStartedEvent(SseEventBase):
    event_type: Literal["planner.started"]
    payload: PlannerStartedPayload


class PlannerRejectedEvent(SseEventBase):
    event_type: Literal["planner.rejected"]
    payload: PlannerRejectedPayload


class TaskStartedEvent(SseEventBase):
    event_type: Literal["task.started"]
    payload: TaskStartedPayload


class TaskRoundAdvancedEvent(SseEventBase):
    event_type: Literal["task.round_advanced"]
    payload: TaskRoundAdvancedPayload


class TaskToolUsedEvent(SseEventBase):
    event_type: Literal["task.tool_used"]
    payload: TaskToolUsedPayload


class TaskEvidenceSavedEvent(SseEventBase):
    event_type: Literal["task.evidence_saved"]
    payload: TaskEvidenceSavedPayload


class TaskFinishedEvent(SseEventBase):
    event_type: Literal["task.finished"]
    payload: TaskFinishedPayload


class VerifierCompletedEvent(SseEventBase):
    event_type: Literal["verifier.completed"]
    payload: VerifierCompletedPayload


class ReplanTriggeredEvent(SseEventBase):
    event_type: Literal["replan.triggered"]
    payload: ReplanTriggeredPayload


class ReportDraftRenderedEvent(SseEventBase):
    event_type: Literal["report.draft_rendered"]
    payload: ReportDraftRenderedPayload


SseEventVariant = Annotated[
    JobPhaseChangedEvent
    | JobStoppedEvent
    | BriefConfirmedEvent
    | PlannerStartedEvent
    | PlannerDecidedEvent
    | PlannerRejectedEvent
    | TaskStartedEvent
    | TaskRoundAdvancedEvent
    | TaskToolUsedEvent
    | TaskEvidenceSavedEvent
    | TaskFinishedEvent
    | VerifierCompletedEvent
    | ReplanTriggeredEvent
    | ReportDraftRenderedEvent,
    Field(discriminator="event_type"),
]


class SseEvent(RootModel[SseEventVariant]):
    """JSON object in each SSE `data:` line of `GET /api/jobs/{job_id}/events`."""


sse_event_adapter = TypeAdapter(SseEvent)
