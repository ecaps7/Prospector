"""Versioned research plan and self-contained worker task schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

TaskStatus = Literal["pending", "running", "done", "failed", "skipped", "cancelled"]
AllowedTool = Literal["web_search", "web_fetch", "save_findings"]


class TaskBudget(BaseModel):
    """Worker rounds are the single authoritative budget; tool calls are uncapped in
    total and bounded only per-round by the runtime's parallel-call limit."""

    max_worker_rounds: int = Field(..., ge=1)


class ResearchTaskDraft(BaseModel):
    """Planner-authored task content. Runtime owns execution shape and budget."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(
        ...,
        min_length=1,
        description="Self-contained research question handed to one Worker",
    )
    expected_evidence: str = Field(
        ...,
        min_length=1,
        description=(
            "Observable evidence state that would answer the task question; material counts "
            "or category counts alone are not completion"
        ),
    )

    @field_validator("question", "expected_evidence")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text


class ResearchTask(ResearchTaskDraft):
    task_id: UUID
    depends_on: list[UUID] = Field(default_factory=list)
    allowed_tools: list[AllowedTool] = Field(
        default_factory=lambda: ["web_search", "web_fetch", "save_findings"]
    )
    budget: TaskBudget
    status: TaskStatus = "pending"


class Plan(BaseModel):
    plan_id: UUID
    job_id: UUID
    version: int = Field(..., ge=1)
    decision_round: int = Field(..., ge=1)
    trigger_verifier_run: UUID | None = None
    task_ids: list[UUID] = Field(..., min_length=1)
    created_at: datetime | None = None
