"""Script external responses, not Prospector's graph, validation or persistence."""

from __future__ import annotations

import asyncio
import io
import json
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any

import httpx
from openai import AsyncOpenAI, OpenAI

from prospector.agents.planner import OpenAIPlannerModel
from prospector.agents.report_attribution import OpenAIClaimAttribution
from prospector.agents.report_readthrough import OpenAIReadthrough
from prospector.agents.report_review import OpenAIReportReview
from prospector.agents.report_writer import OpenAIReportWriter
from prospector.agents.research_synthesis import OpenAIResearchSynthesis
from prospector.agents.research_verifier import OpenAIResearchVerifier
from prospector.agents.research_worker import OpenAIWorkerModel, ResearchWorker
from prospector.flow.research_graph import ResearchGraphServices
from prospector.store.object_store import ObjectStore
from prospector.store.repositories import ResearchRepository
from prospector.tools.save_findings import SaveFindingsTool
from prospector.tools.web_fetch import WebFetchTool
from prospector.tools.web_search import ExaClient, WebSearchTool

SOURCE_URL = "https://source.test/annual-report"
FACT = "Revenue fell by 12% in 2024."
DOCUMENT = FACT + "\nARCHIVE_ONLY_SENTINEL: unrelated full-page appendix."
MARKDOWN = "# Results\n\n" + FACT + "\n"
DISPATCH = {
    "decision": "dispatch",
    "reason": "Find the reported change.",
    "tasks": [{"question": "How did revenue change?", "expected_evidence": "Annual report."}],
}
FINISH = {"decision": "finish", "reason": "The reported change is established."}
QUALIFICATION = {"conflicts": [], "assertion_dispositions": [], "source_credibility_findings": []}
PASS = {
    "decision": "pass",
    "reason": "The annual report answers the question.",
    "answerability_checks": [
        {
            "requirement": "Reported revenue change",
            "status": "answered",
            "answer": FACT,
            "supporting_assertion_refs": ["a1"],
            "evidence_bridge": "The annual report states the change for the requested year.",
        }
    ],
    "gaps": [],
}
SYNTHESIS = {
    "decision": "ready",
    "synthesis": "Revenue contracted in the reporting period.",
    "assertion_refs": ["a1"],
    "material_conflict_refs": [],
}
REVIEW = {"blocking_findings": [], "key_block_ids": ["b_0002"]}


def payload(request: dict[str, Any]) -> dict[str, Any]:
    return json.loads(request["messages"][-1]["content"])


def verified_fact(request):
    """The fixture's explicit verdict; offsets are taken from the supplied candidates."""
    candidates = payload(request)["candidates"]
    assert candidates, "The numerical statement must reach Attribution"
    assert all(item["text"].rstrip(".") == FACT.rstrip(".") for item in candidates), candidates
    return {
        "claims": [
            {
                "claim_ref": f"c{index}",
                "block_id": item["block_id"],
                "start_offset": item["start_offset"],
                "end_offset": item["end_offset"],
                "candidate_refs": [item["candidate_ref"]],
                "status": "verified",
                "assertion_refs": ["a1"],
                "excerpt_refs": ["a1e1"],
            }
            for index, item in enumerate(candidates, 1)
        ]
    }


class Providers:
    def __init__(self):
        self.responses: dict[str, deque[Any]] = defaultdict(deque)
        self.requests: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.clients = ExitStack()
        self.async_clients: list[AsyncOpenAI] = []
        self.document = DOCUMENT
        self.highlights = [FACT]
        self.on_request: Callable[[str, dict[str, Any]], None] | None = None

    def script(self, stage: str, *responses):
        self.responses[stage].extend(responses)

    def happy_path(self):
        self.script("planner", DISPATCH, FINISH)
        self.worker_round()
        self.script("verifier", QUALIFICATION, PASS)
        self.script("synthesis", SYNTHESIS, {"defects": [], "reason": "Answers the question."})
        self.script("writer", MARKDOWN)
        self.script("attribution", {"assertion_refs": ["a1"]}, verified_fact)
        self.script("review", REVIEW)
        self.script("readthrough", {"findings": []})

    def worker_round(self):
        self.script(
            "worker",
            {"action": "search", "searches": [{"query": "annual revenue report"}]},
            {
                "action": "save",
                "save_batches": [
                    {
                        "findings": [
                            {"source_refs": ["s1:h1"], "statement": FACT},
                        ]
                    }
                ],
            },
            {"goal_met": True, "reason": "The requested annual figure is saved."},
            {"summary": FACT},
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        stage = body["model"].removeprefix("qwen-")
        if self.on_request is not None:
            self.on_request(stage, body)
        self.requests[stage].append(body)
        assert self.responses[stage], (
            f"Unexpected {stage} call: {body['messages'][-1]['content'][-1000:]}"
        )
        answer = self.responses[stage].popleft()
        if callable(answer):
            answer = answer(body)
        if isinstance(answer, Exception):
            raise answer
        content = answer if isinstance(answer, str) else json.dumps(answer)
        common = {"id": "test-response", "created": 0, "model": body["model"]}
        if body.get("stream"):
            chunk = {
                **common,
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {"content": content}, "finish_reason": None},
                ],
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n",
            )
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if body.get("tools"):
            assert isinstance(answer, dict)
            slots = body["tools"][0]["function"]["parameters"]["properties"]
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "summary",
                        "type": "function",
                        "function": {
                            "name": "submit_worker_summary",
                            "arguments": json.dumps({slot: answer["summary"] for slot in slots}),
                        },
                    }
                ],
            }
        return httpx.Response(
            200,
            json={
                **common,
                "object": "chat.completion",
                "choices": [
                    {"index": 0, "message": message, "finish_reason": "stop"},
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )

    def urlopen(self, request, timeout=None):
        if request.full_url == SOURCE_URL:
            return io.BytesIO(b"<html>Annual report</html>")
        assert request.full_url in {"https://api.exa.ai/search", "https://api.exa.ai/contents"}
        body = json.loads(request.data)
        item: dict[str, Any] = {"url": SOURCE_URL, "title": "Annual report"}
        if request.full_url.endswith("/contents"):
            assert body["urls"] == [SOURCE_URL]
            item.update(text=self.document, highlights=self.highlights)
        return io.BytesIO(json.dumps({"results": [item]}).encode())

    def services(self) -> ResearchGraphServices:
        repository, store, exa = ResearchRepository(), ObjectStore(), ExaClient()

        def client():
            return self.clients.enter_context(
                OpenAI(
                    api_key="test",
                    base_url="https://llm.test/v1",
                    max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(self.handle)),
                )
            )

        worker_client = AsyncOpenAI(
            api_key="test",
            base_url="https://llm.test/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(self.handle)),
        )
        self.async_clients.append(worker_client)
        return ResearchGraphServices(
            repository=repository,
            planner=OpenAIPlannerModel(client(), "qwen-planner", "qwen-planner"),
            worker=ResearchWorker(
                repository,
                [
                    WebSearchTool(repository, exa),
                    WebFetchTool(repository, store, exa),
                    SaveFindingsTool(repository),
                ],
                OpenAIWorkerModel(worker_client, "qwen-worker"),
            ),
            verifier=OpenAIResearchVerifier(client(), "qwen-verifier", "qwen-verifier"),
            synthesis=OpenAIResearchSynthesis(client(), "qwen-synthesis"),
            writer=OpenAIReportWriter(client(), "qwen-writer"),
            attribution=OpenAIClaimAttribution(client(), "qwen-attribution"),
            review=OpenAIReportReview(client(), "qwen-review"),
            readthrough=OpenAIReadthrough(client(), "qwen-readthrough"),
            object_store=store,
            cancel_requested=repository.job_cancel_requested,
        )

    def assert_consumed(self):
        assert not {stage: len(queue) for stage, queue in self.responses.items() if queue}

    def close(self):
        self.clients.close()
        for client in self.async_clients:
            asyncio.run(client.close())
