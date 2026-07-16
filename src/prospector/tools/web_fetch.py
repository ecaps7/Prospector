"""Fetch full text into an immutable snapshot and persist Exa highlights as its task view."""

from __future__ import annotations

import asyncio
import hashlib
import urllib.request
from typing import Any, Literal, Protocol
from uuid import uuid4

from prospector.schemas.evidence import SourceRef, SourceViewItem
from prospector.store.object_store import ObjectStore, workspace_key
from prospector.store.repositories import ResearchRepository
from prospector.tools._retry import retry_async
from prospector.tools.base import ToolContext
from prospector.tools.web_search import ExaClient

MediaType = Literal["html", "pdf"]


class SourceMediaProbe(Protocol):
    async def detect(self, url: str) -> MediaType: ...


class HttpSourceMediaProbe:
    @staticmethod
    def _detect(url: str) -> MediaType:
        request = urllib.request.Request(
            url,
            headers={
                "Accept-Encoding": "identity",
                "Range": "bytes=0-1023",
                "User-Agent": "Prospector/1.0",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=15.0) as response:  # noqa: S310
            prefix = response.read(1024)
        return "pdf" if b"%PDF-" in prefix else "html"

    async def detect(self, url: str) -> MediaType:
        return await retry_async(
            asyncio.to_thread,
            self._detect,
            url,
            label=f"media_probe({url!r})",
        )


class WebFetchTool:
    name = "web_fetch"

    def __init__(
        self,
        repository: ResearchRepository,
        object_store: ObjectStore | None = None,
        exa: ExaClient | None = None,
        media_probe: SourceMediaProbe | None = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store or ObjectStore(repository.settings)
        self.exa = exa or ExaClient(repository.settings)
        self.media_probe = media_probe or HttpSourceMediaProbe()

    async def __call__(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        url = str(arguments.get("url", "")).strip()
        if not url:
            raise ValueError("web_fetch.url must not be blank")
        media_type = await self.media_probe.detect(url)
        raw = await self.exa.contents(url, context.task_question)
        results = raw.get("results") or []
        if not results:
            statuses = raw.get("statuses") or []
            raise RuntimeError(f"Exa returned no content for {url}: {statuses}")
        item = results[0]
        full_text = str(item.get("text") or "")
        if not full_text.strip():
            raise RuntimeError(f"Exa returned empty text for {url}")

        content_hash = "sha256:" + hashlib.sha256(full_text.encode("utf-8")).hexdigest()
        existing = await asyncio.to_thread(self.repository.find_document, url, content_hash)
        if existing is None:
            doc_id = uuid4()
            version = await asyncio.to_thread(self.repository.next_document_version, url)
            key = workspace_key(
                self.repository.settings.workspace_id,
                "docs",
                str(doc_id),
                f"v{version}.txt",
            )
            ref = await asyncio.to_thread(
                self.object_store.put_bytes,
                key,
                full_text.encode("utf-8"),
                content_type="text/plain; charset=utf-8",
            )
            document = await asyncio.to_thread(
                self.repository.save_document,
                job_id=context.job_id,
                task_id=context.task_id,
                doc_id=doc_id,
                source_ref=SourceRef(kind="url", uri=url),
                content_hash=content_hash,
                version=version,
                media_type=media_type,
                storage_ref=ref.as_uri(),
                source_meta={
                    "title": item.get("title"),
                    "author": item.get("author"),
                    "published_at": item.get("publishedDate"),
                },
                tool_call_id=context.tool_call_id,
            )
        else:
            document = existing
            if document.media_type != media_type:
                document = await asyncio.to_thread(
                    self.repository.update_document_media_type,
                    document.doc_id,
                    media_type,
                )
            await asyncio.to_thread(
                self.repository.record_tool_used,
                context.job_id,
                context.task_id,
                {
                    "tool": self.name,
                    "tool_call_id": context.tool_call_id,
                    "url": url,
                    "doc_id": str(document.doc_id),
                },
            )

        highlights = [
            str(value).strip() for value in item.get("highlights") or [] if str(value).strip()
        ]
        if not highlights:
            raise RuntimeError("Exa returned no highlights for source")
        items = [
            SourceViewItem(item_id=f"highlight_{index}", text=text, source_ids=[f"h{index}"])
            for index, text in enumerate(highlights, start=1)
        ]

        view = await asyncio.to_thread(
            self.repository.save_document_view,
            job_id=context.job_id,
            task_id=context.task_id,
            document=document,
            view_kind="exa_highlights",
            items=items,
        )
        return {
            "doc_id": str(document.doc_id),
            "version": document.version,
            "media_type": document.media_type,
            "view_id": str(view.view_id),
            "view_kind": view.view_kind,
            "items": [value.model_dump() for value in view.items],
        }


WEB_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "Snapshot one URL and return a persisted task-focused source view.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}
