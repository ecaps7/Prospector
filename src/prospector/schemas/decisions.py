"""Planner's forced dispatch / reflect / finish decision contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from prospector.schemas.plan import ResearchTaskDraft


class DispatchDecision(BaseModel):
    tasks: list[ResearchTaskDraft] = Field(..., min_length=1)
    reason: str = Field(
        ...,
        min_length=1,
        max_length=1200,
        description="一两句极短中文：本轮为何继续、为何派这些任务",
    )

    # Single-stage batches are enforced at the runtime gate, not here: mixing stages is a
    # planning error that must consume a decision round and come back as a readable
    # rejection, whereas a schema error here would be charged to the format-retry budget.


class ReflectDecision(BaseModel):
    note: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="一两句极短中文：策略调整理由",
    )


class FinishDecision(BaseModel):
    reason: str = Field(
        ...,
        min_length=1,
        max_length=1200,
        description="一两句极短中文：为何可以结束研究并交给 Verifier",
    )


class PlannerDecision(BaseModel):
    """Exactly one payload must match the decision discriminator."""

    decision: Literal["dispatch", "reflect", "finish"]
    dispatch: DispatchDecision | None = None
    reflect: ReflectDecision | None = None
    finish: FinishDecision | None = None

    @model_validator(mode="after")
    def _exactly_one_payload(self) -> PlannerDecision:
        payloads = {
            "dispatch": self.dispatch,
            "reflect": self.reflect,
            "finish": self.finish,
        }
        if payloads[self.decision] is None:
            raise ValueError(f"{self.decision} payload is required")
        if any(value is not None for key, value in payloads.items() if key != self.decision):
            raise ValueError("only the payload matching decision may be present")
        return self


class DecisionLog(BaseModel):
    decision_round: int = Field(..., ge=1)
    full_prompt: list[dict[str, object]]
    decision: PlannerDecision | None = None
    raw_output: object | None = None
    feedback: str | None = None
