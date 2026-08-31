"""Planner's forced dispatch / finish decision contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from prospector.schemas.plan import ResearchTaskDraft


class PlannerDecision(BaseModel):
    """One executable Planner decision with no redundant nested payload."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["dispatch", "finish"]
    reason: str = Field(..., min_length=1)
    tasks: list[ResearchTaskDraft] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _payload_matches_decision(self) -> PlannerDecision:
        if self.decision == "dispatch":
            if not self.tasks:
                raise ValueError("dispatch requires at least one task")
        elif self.tasks is not None:
            raise ValueError("finish must not include tasks")
        return self
