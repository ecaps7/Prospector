"""Fetch full text into an immutable snapshot and return only a paragraph-linked view."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Protocol
from uuid import uuid4

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, field_validator

from prospector.agents.llm import get_async_openai_client, mid_model
from prospector.deterministic.segment import Paragraph, segment_text
from prospector.schemas.evidence import SourceRef
from prospector.store.object_store import ObjectStore, workspace_key
from prospector.store.repositories import ResearchRepository
from prospector.tools.base import ToolContext
from prospector.tools.web_search import ExaClient


class CompressedPoint(BaseModel):
    text: str = Field(..., min_length=1)
    para_ids: list[int] = Field(..., min_length=1)

    @field_validator("para_ids")
    @classmethod
    def _positive_ids(cls, values: list[int]) -> list[int]:
        if any(value < 1 for value in values):
            raise ValueError("para_ids must be positive")
        return sorted(set(values))


class CompressedView(BaseModel):
    points: list[CompressedPoint]


class PageCompressor(Protocol):
    async def compress(self, task_question: str, paragraphs: list[Paragraph]) -> CompressedView: ...


class LlmPageCompressor:
    def __init__(self, client: AsyncOpenAI | None = None, model: str | None = None) -> None:
        self.client = client or get_async_openai_client()
        self.model = model or mid_model()

    async def compress(self, task_question: str, paragraphs: list[Paragraph]) -> CompressedView:
        numbered = "\n\n".join(f"[段 {p.para_id}]\n{p.text}" for p in paragraphs)
        prompt = f"""你是深度研究工具内部的网页压缩器。
围绕当前任务，把网页压缩成约原文 25% 到 30% 的任务相关视图。

当前任务：
{task_question}

严格规则：
- 只使用下方网页原文，不得用模型知识补齐。
- 保留会影响判断的数字、日期、单位、统计口径、限定条件、反例与相反信号。
- 每个要点必须列出支持它的原文段号；不要输出没有段号支撑的句子。
- 不要把要点写成引文，Worker 之后会凭段号从快照原文落证。

网页原文：
{numbered}

只输出 JSON：{{"points":[{{"text":"...","para_ids":[1,2]}}]}}。"""
        response = await self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("page compressor returned empty content")
        view = CompressedView.model_validate_json(content)
        valid_ids = {paragraph.para_id for paragraph in paragraphs}
        cited_ids = {para_id for point in view.points for para_id in point.para_ids}
        invalid = sorted(cited_ids - valid_ids)
        if invalid:
            raise ValueError(f"compressor cited unknown paragraph ids: {invalid}")
        return view


class WebFetchTool:
    name = "web_fetch"

    def __init__(
        self,
        repository: ResearchRepository,
        object_store: ObjectStore | None = None,
        exa: ExaClient | None = None,
        compressor: PageCompressor | None = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store or ObjectStore(repository.settings)
        self.exa = exa or ExaClient(repository.settings)
        self.compressor = compressor or LlmPageCompressor()

    async def __call__(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        url = str(arguments.get("url", "")).strip()
        if not url:
            raise ValueError("web_fetch.url must not be blank")
        raw = await self.exa.contents(url)
        results = raw.get("results") or []
        if not results:
            statuses = raw.get("statuses") or []
            raise RuntimeError(f"Exa returned no content for {url}: {statuses}")
        item = results[0]
        full_text = str(item.get("text") or "")
        if not full_text.strip():
            raise RuntimeError(f"Exa returned empty text for {url}")
        paragraphs = segment_text(full_text)
        if not paragraphs:
            raise RuntimeError(f"unable to segment fetched text for {url}")

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
                media_type="html",
                storage_ref=ref.as_uri(),
                source_meta={
                    "title": item.get("title"),
                    "publisher": item.get("author"),
                    "published_at": item.get("publishedDate"),
                },
                tool_call_id=context.tool_call_id,
            )
        else:
            document = existing
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

        view = await self.compressor.compress(context.task_question, paragraphs)
        return {
            "doc_id": str(document.doc_id),
            "version": document.version,
            "paragraph_count": len(paragraphs),
            "view": view.model_dump(),
        }


WEB_FETCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": "Snapshot one URL and return a task-focused view with stable paragraph ids.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}
