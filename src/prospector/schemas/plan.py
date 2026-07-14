"""Versioned research plan and self-contained worker task schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

ResearchMode = Literal["factual", "comparison", "counterargument", "risk_scan", "timeline"]
ResearchStage = Literal["scout", "deep_dive", "verify"]
TaskStatus = Literal["pending", "running", "done", "failed", "skipped"]
AllowedTool = Literal["web_search", "web_fetch", "save_findings"]


class SourcePolicy(BaseModel):
    """Optional source preferences; source type and research posture stay orthogonal."""

    preferred_tiers: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("preferred_tiers")
    @classmethod
    def _clean_tiers(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(cleaned))


class TaskBudget(BaseModel):
    max_tool_calls: int = Field(..., ge=1)


class ResearchTaskDraft(BaseModel):
    """Planner-authored task fields. Runtime-owned fields are intentionally absent."""

    question: str = Field(
        ...,
        min_length=20,
        description="A self-contained paragraph describing the research subproblem",
    )
    research_stage: ResearchStage
    research_mode: ResearchMode
    source_policy: SourcePolicy = Field(default_factory=SourcePolicy)
    expected_evidence: str = Field(..., min_length=8)

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
