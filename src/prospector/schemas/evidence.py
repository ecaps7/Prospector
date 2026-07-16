"""Immutable document snapshots, exact excerpts, and assertion projections."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

HighlightSourceId = Annotated[str, StringConstraints(pattern=r"^h[1-9][0-9]*$")]


class SourceRef(BaseModel):
    kind: Literal["url", "upload", "private"]
    uri: str = Field(..., min_length=1)


class Document(BaseModel):
    doc_id: UUID
    source_ref: SourceRef
    content_hash: str
    version: int = Field(..., ge=1)
    retrieved_at: datetime
    media_type: str
    storage_ref: str
    index_ref: str | None = None
    source_meta: dict[str, Any] = Field(default_factory=dict)


class SourceViewItem(BaseModel):
    item_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    source_ids: list[str] = Field(..., min_length=1)


class DocumentView(BaseModel):
    view_id: UUID
    job_id: UUID
    task_id: UUID
    doc_id: UUID
    doc_version: int = Field(..., ge=1)
    view_kind: Literal["exa_highlights"]
    items: list[SourceViewItem] = Field(..., min_length=1)
    created_at: datetime


class Excerpt(BaseModel):
    excerpt_id: UUID
    doc_id: UUID
    doc_version: int = Field(..., ge=1)
    text: str = Field(..., min_length=1)
    locator: dict[str, Any]
    excerpt_hash: str
    extracted_by: dict[str, str]


class Assertion(BaseModel):
    assertion_id: UUID
    statement: str = Field(..., min_length=1)
    excerpt_ids: list[UUID] = Field(..., min_length=1)
    topic_tags: list[str] = Field(default_factory=list)
    produced_by: dict[str, str]


class FindingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ids: list[HighlightSourceId] = Field(..., min_length=1)
    statement: str = Field(..., min_length=1)
    topic_tags: list[str] = Field(..., max_length=12)

    @field_validator("source_ids")
    @classmethod
    def _deduplicate_source_ids(
        cls, values: list[HighlightSourceId]
    ) -> list[HighlightSourceId]:
        return list(dict.fromkeys(values))

    @field_validator("statement")
    @classmethod
    def _strip_statement(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("statement must not be blank")
        return text
