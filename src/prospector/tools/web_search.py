"""Exa metadata-only web search."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any

from pydantic import BaseModel

from prospector.config import Settings, get_settings
from prospector.store.repositories import ResearchRepository
from prospector.tools._retry import retry_async
from prospector.tools.base import ToolContext


class SearchResult(BaseModel):
    title: str
    url: str
    published_date: str | None = None
    author: str | None = None
    summary: str | None = None


class ExaClient:
    """Minimal client following Exa's POST /search and /contents contracts."""

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = settings or get_settings()
        if not cfg.exa_api_key.strip():
            raise RuntimeError("EXA_API_KEY is required")
        self.api_key = cfg.exa_api_key

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"https://api.exa.ai/{path.lstrip('/')}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30.0) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    async def search(self, query: str, num_results: int) -> dict[str, Any]:
        return await retry_async(
            asyncio.to_thread,
            self._post,
            "search",
            {"query": query, "numResults": num_results, "type": "auto"},
            label=f"exa.search({query!r})",
        )

    async def contents(
        self,
        url: str,
        task_question: str,
    ) -> dict[str, Any]:
        return await retry_async(
            asyncio.to_thread,
            self._post,
            "contents",
            {
                "urls": [url],
                "text": True,
                "highlights": {"query": task_question},
            },
            label=f"exa.contents({url!r})",
        )


class WebSearchTool:
    name = "web_search"

    def __init__(
        self,
        repository: ResearchRepository,
        exa: ExaClient | None = None,
    ) -> None:
        self.repository = repository
        self.exa = exa or ExaClient(repository.settings)

    async def __call__(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("web_search.query must not be blank")
        num_results = int(arguments.get("num_results", 8))
        if not 1 <= num_results <= 20:
            raise ValueError("web_search.num_results must be between 1 and 20")
        raw = await self.exa.search(query, num_results)
        results = [
            SearchResult(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or item.get("id") or ""),
                published_date=item.get("publishedDate"),
                author=item.get("author"),
                summary=item.get("summary"),
            )
            for item in raw.get("results", [])
            if item.get("url") or item.get("id")
        ]
        await asyncio.to_thread(
            self.repository.record_tool_used,
            context.job_id,
            context.task_id,
            {
                "tool": self.name,
                "tool_call_id": context.tool_call_id,
                "query": query,
                "result_count": len(results),
            },
        )
        return {"results": [result.model_dump() for result in results]}
