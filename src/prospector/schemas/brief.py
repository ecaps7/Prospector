"""Research Brief and Scope decision schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

EffortLevel = Literal["quick", "standard", "deep"]


class UserConstraints(BaseModel):
    """Limits the user stated themselves, kept apart from anything Scope proposed.

    brief_text carries the open research space: suggestions the Planner may take or
    drop. These fields carry the opposite kind of information — things that are wrong
    to violate. Dissolving both into one paragraph forces every downstream agent to
    re-infer which is which, so the binding half is held as fields instead.

    Every field is empty unless the user actually said it; empty is the common case.
    """

    time_range: str = Field(default="", description="用户说的时间范围原话")
    regions: list[str] = Field(default_factory=list, max_length=12)
    comparison_targets: list[str] = Field(default_factory=list, max_length=12)
    source_rules: list[str] = Field(default_factory=list, max_length=12)
    exclusions: list[str] = Field(default_factory=list, max_length=12)
    deliverable_rules: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("time_range")
    @classmethod
    def _strip_optional(cls, value: str) -> str:
        return value.strip()

    @field_validator("regions", "comparison_targets", "source_rules", "exclusions")
    @classmethod
    def _clean_entries(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(cleaned))

    @field_validator("deliverable_rules")
    @classmethod
    def _clean_deliverable_rules(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        return list(dict.fromkeys(cleaned))

    def is_empty(self) -> bool:
        return not (
            self.time_range
            or self.regions
            or self.comparison_targets
            or self.source_rules
            or self.exclusions
            or self.deliverable_rules
        )


class ResearchBrief(BaseModel):
    """A concrete research question with an expanded candidate research space."""

    question: str = Field(..., min_length=1, description="Short title for lists / eval index")
    brief_text: str = Field(
        ...,
        min_length=1,
        description="Concrete research question and candidate directions for Planner selection",
    )
    user_constraints: UserConstraints = Field(
        default_factory=UserConstraints,
        description="Binding limits the user stated; never Scope's own suggestions",
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
    assessment: str = Field(
        default="",
        description=(
            "Why this question is answerable as-is, and which gaps the Brief should open "
            "rather than ask about; handed to the Brief writer instead of being discarded"
        ),
    )

    @model_validator(mode="after")
    def _consistent_fields(self) -> ClarifyDecision:
        if self.need_clarification and not self.question.strip():
            raise ValueError("clarification question required when need_clarification")
        if not self.need_clarification and self.question.strip():
            raise ValueError("clarification question must be blank when not needed")
        # assessment stays optional: losing a hint is a worse trade than failing Scope
        # outright, and Scope has no retry path.
        self.assessment = self.assessment.strip()
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
