"""HTTP request and response contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from prospector.schemas.brief import EffortLevel, ResearchBrief


class ScopeRequest(BaseModel):
    question: str = Field(..., min_length=1)
    effort: EffortLevel = "standard"
    language: str = Field(default="zh", min_length=1)
    clarification_question: str | None = None
    clarification_answer: str | None = None

    @field_validator("question", "language")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("clarification_question", "clarification_answer")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def clarification_is_complete(self) -> ScopeRequest:
        if (self.clarification_question is None) != (self.clarification_answer is None):
            raise ValueError(
                "clarification_question and clarification_answer must be provided together"
            )
        return self


class ScopeReviseRequest(BaseModel):
    question: str = Field(..., min_length=1)
    previous_brief: ResearchBrief
    revision_note: str = Field(..., min_length=1)
    effort: EffortLevel = "standard"
    language: str = Field(default="zh", min_length=1)

    @field_validator("question", "revision_note", "language")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ScopeReviseResponse(BaseModel):
    brief: ResearchBrief


class JobCreateRequest(BaseModel):
    brief: ResearchBrief


class JobCreateResponse(BaseModel):
    job_id: UUID
    brief_id: UUID
    status: Literal["running", "queued"]
    queue_position: int | None = Field(default=None, ge=1)


class JobCancelResponse(BaseModel):
    job_id: UUID
    status: Literal["cancelling", "cancelled"]


class JobListItem(BaseModel):
    job_id: UUID
    question: str | None
    effort: EffortLevel | None
    status: Literal[
        "queued",
        "running",
        "cancelling",
        "cancelled",
        "completed",
        "failed",
    ]
    phase: str
    outcome: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class JobTaskView(BaseModel):
    task_id: UUID
    question: str
    subjects: list[str]
    research_stage: str
    research_mode: str
    status: str
    stop_reason: str | None
    budget: dict[str, Any]
    tool_calls_used: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class UsageView(BaseModel):
    component: str
    input_tokens: int
    output_tokens: int
    tool_calls: int


class ReportView(BaseModel):
    report_id: UUID
    status: str
    verification_status: str | None
    markdown_ref: str | None
    json_ref: str | None


class JobDetail(JobListItem):
    brief_id: UUID | None
    language: str | None
    plan_version: int
    tasks: list[JobTaskView]
    usage: list[UsageView]
    report: ReportView | None


class ErrorResponse(BaseModel):
    error_code: str
    message: str


class HealthResponse(BaseModel):
    status: Literal["ok"]
