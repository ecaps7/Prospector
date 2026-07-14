"""Research Brief and Scope decision schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EffortLevel = Literal["quick", "standard", "deep"]


class ResearchBrief(BaseModel):
    """A concrete research question with an expanded candidate research space."""

    question: str = Field(..., min_length=1, description="Short title for lists / eval index")
    brief_text: str = Field(
        ...,
        min_length=1,
        description="Concrete research question and candidate directions for Planner selection",
    )
    output_format: str = Field(default="report_with_citations")
    language: str = Field(default="zh", min_length=1)
    effort: EffortLevel = Field(default="standard")

    @field_validator("question", "brief_text", "language", "output_format")
    @classmethod
    def _strip_nonempty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text


class ClarifyDecision(BaseModel):
    """Decide whether one clarification is required before expanding the question."""

    need_clarification: bool
    question: str = Field(
        default="",
        description="Clarifying question for the user when need_clarification is true",
    )

    @model_validator(mode="after")
    def _consistent_fields(self) -> ClarifyDecision:
        if self.need_clarification and not self.question.strip():
            raise ValueError("clarification question required when need_clarification")
        if not self.need_clarification and self.question.strip():
            raise ValueError("clarification question must be blank when not needed")
        return self


class ScopeOutcome(BaseModel):
    """Result of Scope: one clarification request or an expanded Brief for review."""

    kind: Literal["clarify", "brief_pending"]
    clarification_question: str | None = None
    brief: ResearchBrief | None = None

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> ScopeOutcome:
        if self.kind == "clarify":
            if not (self.clarification_question and self.clarification_question.strip()):
                raise ValueError("clarify outcome requires clarification_question")
            if self.brief is not None:
                raise ValueError("clarify outcome must not include brief")
        elif self.kind == "brief_pending":
            if self.brief is None:
                raise ValueError("brief_pending outcome requires brief")
            if self.clarification_question is not None:
                raise ValueError("brief_pending outcome must not include clarification_question")
        return self
