"""Metadata-only web search contract."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from prospector.tools.base import ToolContext
from prospector.tools.web_search import WebSearchTool


class _Exa:
    async def search(self, query: str, num_results: int) -> dict[str, Any]:
        assert query == "test query"
        assert num_results == 3
        return {
            "results": [
                {
                    "title": "Source",
                    "url": "https://example.com/source",
                    "publishedDate": "2026-08-27",
                    "author": "Author",
                    "summary": "Search-engine generated text must not reach the Worker.",
                }
            ]
        }


class _Repository:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def record_tool_used(
        self,
        job_id: object,
        task_id: object,
        payload: dict[str, object],
    ) -> None:
        del job_id, task_id
        self.events.append(payload)


@pytest.mark.asyncio
async def test_web_search_never_returns_provider_summary() -> None:
    repository = _Repository()
    tool = WebSearchTool(repository, _Exa())  # type: ignore[arg-type]
    context = ToolContext(
        job_id=uuid4(),
        task_id=uuid4(),
        worker_id="worker-1",
        task_question="question",
        tool_call_id="call-1",
    )

    result = await tool({"query": "test query", "num_results": 3}, context)

    assert result == {
        "results": [
            {
                "title": "Source",
                "url": "https://example.com/source",
                "published_date": "2026-08-27",
                "author": "Author",
            }
        ]
    }
    assert repository.events == [
        {
            "tool": "web_search",
            "tool_call_id": "call-1",
            "query": "test query",
            "result_count": 1,
        }
    ]
