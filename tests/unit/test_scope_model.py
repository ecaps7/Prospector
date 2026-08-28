"""Scope Brief transport: thinking-mode stream, JSON parse, one structural repair."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from prospector.agents.scope import decide_clarification, write_research_brief


def _brief_payload() -> dict[str, object]:
    return {
        "question": "钠离子电池发展到了什么程度？",
        "brief_text": "梳理钠离子电池的技术成熟度、产业化进展与主要瓶颈。",
        "user_constraints": {},
        "output_format": "report_with_citations",
        "language": "zh",
        "effort": "standard",
    }


def _chunks(text: str, *, reasoning: str = "先思考一下") -> list[object]:
    """Stream chunks: reasoning deltas must be ignored, content deltas aggregated."""
    deltas: list[object] = [
        SimpleNamespace(content=None, reasoning_content=reasoning),
    ]
    step = max(1, len(text) // 3)
    for start in range(0, len(text), step):
        deltas.append(SimpleNamespace(content=text[start : start + step]))
    chunks = [SimpleNamespace(choices=[SimpleNamespace(delta=delta)]) for delta in deltas]
    return [SimpleNamespace(choices=[]), *chunks]


class _FakeCompletions:
    def __init__(self, stream_text: str, repair_text: str | None = None) -> None:
        self.stream_text = stream_text
        self.repair_text = repair_text
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            return iter(_chunks(self.stream_text))
        assert self.repair_text is not None, "unexpected non-stream (repair) call"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.repair_text))]
        )


class _FakeClient:
    def __init__(self, stream_text: str, repair_text: str | None = None) -> None:
        self.completions = _FakeCompletions(stream_text, repair_text)
        self.chat = SimpleNamespace(completions=self.completions)


def test_write_brief_streams_with_thinking_and_no_json_mode() -> None:
    payload = _brief_payload()
    client = _FakeClient(json.dumps(payload, ensure_ascii=False))

    brief = write_research_brief(
        "钠离子电池发展到了什么程度？",
        client=client,  # type: ignore[arg-type]
        model="qwen3.7-max",
    )

    assert brief.question == payload["question"]
    (request,) = client.completions.requests
    assert request["stream"] is True
    assert request["extra_body"] == {"enable_thinking": True}
    assert "response_format" not in request
    assert "tools" not in request


def test_write_brief_uses_deepseek_thinking_parameter() -> None:
    client = _FakeClient(json.dumps(_brief_payload(), ensure_ascii=False))

    write_research_brief(
        "钠离子电池发展到了什么程度？",
        client=client,  # type: ignore[arg-type]
        model="deepseek-v4-flash",
    )

    (request,) = client.completions.requests
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert request["stream"] is True
    assert "response_format" not in request


def test_write_brief_repairs_broken_json_with_thinking_off() -> None:
    valid = json.dumps(_brief_payload(), ensure_ascii=False)
    client = _FakeClient("经过思考，Brief 如下：" + valid, repair_text=valid)

    brief = write_research_brief(
        "钠离子电池发展到了什么程度？",
        client=client,  # type: ignore[arg-type]
        model="qwen3.7-max",
    )

    assert brief.brief_text == _brief_payload()["brief_text"]
    stream_request, repair_request = client.completions.requests
    assert stream_request["stream"] is True
    assert stream_request["extra_body"] == {"enable_thinking": True}
    assert "response_format" not in stream_request
    assert repair_request.get("stream") is None
    assert repair_request["response_format"] == {"type": "json_object"}
    assert repair_request["extra_body"] == {"enable_thinking": False}


def test_write_brief_raises_when_repair_also_fails() -> None:
    client = _FakeClient("不是 JSON", repair_text='{"question": ""}')

    with pytest.raises((json.JSONDecodeError, ValueError)):
        write_research_brief(
            "钠离子电池发展到了什么程度？",
            client=client,  # type: ignore[arg-type]
            model="qwen3.7-max",
        )


def test_write_brief_rejects_empty_stream() -> None:
    client = _FakeClient("")

    with pytest.raises(RuntimeError, match="empty LLM response"):
        write_research_brief(
            "钠离子电池发展到了什么程度？",
            client=client,  # type: ignore[arg-type]
            model="qwen3.7-max",
        )


def test_clarify_propagates_transport_errors_without_plain_chat_retry() -> None:
    class _BoomCompletions:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []

        def create(self, **kwargs: Any) -> object:
            self.requests.append(kwargs)
            raise RuntimeError("401 unauthorized")

    completions = _BoomCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    with pytest.raises(RuntimeError, match="401 unauthorized"):
        decide_clarification(
            "钠离子电池发展到了什么程度？",
            client=client,  # type: ignore[arg-type]
            model="qwen3.7-max",
        )

    assert len(completions.requests) == 1
    assert completions.requests[0]["response_format"] == {"type": "json_object"}


def test_clarify_keeps_json_mode_with_thinking_off() -> None:
    payload = {
        "need_clarification": False,
        "question": "",
        "assessment": "研究对象明确，缺口由 Brief 展开。",
    }
    completions = _FakeCompletions("", repair_text=json.dumps(payload, ensure_ascii=False))
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    decision = decide_clarification(
        "钠离子电池发展到了什么程度？",
        client=client,  # type: ignore[arg-type]
        model="qwen3.7-max",
    )

    assert decision.need_clarification is False
    (request,) = completions.requests
    assert request.get("stream") is None
    assert request["response_format"] == {"type": "json_object"}
    assert request["extra_body"] == {"enable_thinking": False}
