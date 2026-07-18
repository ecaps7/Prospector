"""Read original text from an indexed document snapshot via PageIndex."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from prospector.schemas.evidence import SourceViewItem
from prospector.store.repositories import ResearchRepository
from prospector.tools.base import ToolContext


class PageIndexReadResult(BaseModel):
    """Text fragment returned by PageIndex for a single locator query."""

    text: str = Field(..., min_length=1)
    page: int | None = None
    line_span: list[int] | None = None


class PageIndexClient(Protocol):
    """External PageIndex integration boundary.

    Implementations navigate the document tree built at ingest time and
    return the original text for the requested locator without any LLM call.
    """

    async def read(
        self,
        *,
        doc_id: UUID,
        index_ref: str,
        storage_ref: str,
        locator: dict[str, Any],
    ) -> PageIndexReadResult: ...


class KbReadTool:
    """Read a text fragment from an already-indexed document via PageIndex.

    The returned text is persisted as a task-scoped DocumentView so that
    the worker can later reference it through ``save_findings`` using the
    exposed ``source_ids``.
    """

    name = "kb_read"

    def __init__(
        self,
        repository: ResearchRepository,
        page_index: PageIndexClient,
    ) -> None:
        self.repository = repository
        self.page_index = page_index

    async def __call__(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        raw_doc_id = str(arguments.get("doc_id", "")).strip()
        if not raw_doc_id:
            raise ValueError("kb_read.doc_id must not be blank")
        doc_id = UUID(raw_doc_id)

        locator = arguments.get("locator")
        if not isinstance(locator, dict) or not locator:
            raise ValueError("kb_read.locator must be a non-empty object")

        document = await asyncio.to_thread(self.repository.get_document, doc_id)
        if document.index_ref is None:
            raise RuntimeError(
                f"document {doc_id} has no PageIndex tree; kb_read requires an indexed document"
            )

        result = await self.page_index.read(
            doc_id=doc_id,
            index_ref=document.index_ref,
            storage_ref=document.storage_ref,
            locator=locator,
        )

        locator_with_meta: dict[str, Any] = {
            "kind": "page_index",
            **({"page": result.page} if result.page is not None else {}),
            **(
                {"line_span": result.line_span}
                if result.line_span is not None
                else {}
            ),
            "doc_id": str(doc_id),
            "doc_version": document.version,
        }

        items = [
            SourceViewItem(
                item_id="kb_fragment_1",
                text=result.text,
                source_ids=["h1"],
            ),
        ]

        view = await asyncio.to_thread(
            self.repository.save_document_view,
            job_id=context.job_id,
            task_id=context.task_id,
            document=document,
            view_kind="kb_read",
            items=items,
        )

        await asyncio.to_thread(
            self.repository.record_tool_used,
            context.job_id,
            context.task_id,
            {
                "tool": self.name,
                "tool_call_id": context.tool_call_id,
                "doc_id": str(doc_id),
                "locator": locator,
            },
        )

        return {
            "doc_id": str(document.doc_id),
            "version": document.version,
            "media_type": document.media_type,
            "view_id": str(view.view_id),
            "view_kind": view.view_kind,
            "locator": locator_with_meta,
            "items": [value.model_dump() for value in view.items],
        }


KB_READ_SCHEMA = {
    "type": "function",
    "function": {
        "name": "kb_read",
        "description": (
            "Read original text from an already-indexed private document. "
            "Provide the doc_id and a locator (e.g. page number or line range)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "UUID of the indexed document.",
                },
                "locator": {
                    "type": "object",
                    "description": (
                        "Position to read from the document. "
                        "Examples: {\"page\": 14}, {\"line_span\": [35, 78]}."
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["doc_id", "locator"],
            "additionalProperties": False,
        },
    },
}
