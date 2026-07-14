"""Versioned research plan and self-contained worker task schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

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
    """Worker rounds are the single authoritative budget; tool calls are uncapped in
    total and bounded only per-round by the runtime's parallel-call limit."""

    max_worker_rounds: int = Field(..., ge=1)


class ResearchTaskDraft(BaseModel):
    """Planner-authored task fields. Runtime-owned fields are intentionally absent."""

    question: str = Field(
        ...,
        min_length=20,
        description="A self-contained paragraph describing the research subproblem",
    )
    subjects: list[str] = Field(
        ...,
        min_length=1,
        max_length=6,
        description=(
            "Declared research subjects; scout may list one bounded candidate set, "
            "deep_dive/verify must declare exactly one subject"
        ),
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

    @field_validator("subjects")
    @classmethod
    def _clean_subjects(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if not cleaned:
            raise ValueError("subjects must contain at least one non-blank subject")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def _stage_bounds_subjects(self) -> ResearchTaskDraft:
        if self.research_stage != "scout" and len(self.subjects) != 1:
            raise ValueError(
                f"{self.research_stage} task must declare exactly one subject; "
                f"got {len(self.subjects)} — split into one task per subject"
            )
        return self


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
