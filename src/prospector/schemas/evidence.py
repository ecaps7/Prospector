"""Immutable document snapshots, exact excerpts, and assertion projections."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    source_ids: list[str] = Field(..., min_length=1)
    statement: str = Field(..., min_length=1)
    topic_tags: list[str] = Field(..., max_length=12)

    @field_validator("source_ids")
    @classmethod
    def _valid_source_ids(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(
            len(value) < 2 or value[0] != "h" or not value[1:].isdigit() or int(value[1:]) < 1
            for value in cleaned
        ):
            raise ValueError("source ids must use positive hN identifiers")
        return list(dict.fromkeys(cleaned))

    @field_validator("statement")
    @classmethod
    def _strip_statement(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("statement must not be blank")
        return text
