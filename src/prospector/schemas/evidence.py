"""Immutable document snapshots, exact excerpts, and assertion projections."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


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
    para_ids: list[int] = Field(..., min_length=1)
    statement: str = Field(..., min_length=1)
    topic_tags: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("para_ids")
    @classmethod
    def _valid_para_ids(cls, values: list[int]) -> list[int]:
        if any(value < 1 for value in values):
            raise ValueError("paragraph ids are one-based positive integers")
        return sorted(set(values))

    @field_validator("statement")
    @classmethod
    def _strip_statement(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("statement must not be blank")
        return text
