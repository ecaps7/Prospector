from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from openai import APIConnectionError, BadRequestError

from prospector.agents import streaming
from prospector.agents.streaming import STREAM_ATTEMPTS, stream_text
from prospector.agents.usage import collect_usage


class FakeRepository:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def record_usage(
        self,
        job_id: UUID,
        *,
        component: str,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        tool_calls: int = 0,
        task_id: UUID | None = None,
    ) -> None:
        self.rows.append({"input_tokens": input_tokens, "output_tokens": output_tokens})


def _chunk(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=text))],
    )


def _usage_chunk(prompt: int, completion: int) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
        choices=[],
    )


def _dropped_stream(parts: list[str], error: Exception) -> Iterator[SimpleNamespace]:
    """A stream that delivers some content and then loses the connection."""
    for part in parts:
        yield _chunk(part)
    raise error


class _ScriptedClient:
    """chat.completions.create returning one scripted stream per call."""

    def __init__(self, streams: list[Any]) -> None:
        self.streams = list(streams)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        stream = self.streams.pop(0)
        if isinstance(stream, Exception):
            raise stream
        return stream


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(streaming, "_sleep", lambda _seconds: None)


def _call(client: Any, **kwargs: Any) -> str:
    return stream_text(
        client,
        agent="report_writer",
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "写一段"}],
        temperature=0.2,
        extra_body={"thinking": {"type": "enabled"}},
        **kwargs,
    )


def test_dropped_stream_replays_the_whole_turn() -> None:
    dropped = httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body"
    )
    client = _ScriptedClient(
        [
            _dropped_stream(["半句"], dropped),
            iter([_chunk("完整的一轮回答")]),
        ]
    )

    # The half-delivered attempt is discarded: a turn is only usable whole, and the
    # model cannot be asked to continue an answer the runtime never fully received.
    assert _call(client) == "完整的一轮回答"
    assert len(client.calls) == 2
    assert client.calls[0]["messages"] == client.calls[1]["messages"]
    assert client.calls[1]["stream"] is True


def test_retry_covers_the_sdk_wrapped_connection_error() -> None:
    wrapped = APIConnectionError(request=httpx.Request("POST", "https://api.example/v1"))
    client = _ScriptedClient([wrapped, iter([_chunk("回答")])])

    assert _call(client) == "回答"
    assert len(client.calls) == 2


def test_persistent_drop_raises_after_the_last_attempt() -> None:
    dropped = httpx.ReadError("connection reset")
    client = _ScriptedClient([_dropped_stream([], dropped) for _ in range(STREAM_ATTEMPTS)])

    with pytest.raises(httpx.ReadError):
        _call(client)
    assert len(client.calls) == STREAM_ATTEMPTS


def test_model_side_errors_are_not_retried() -> None:
    refusal = BadRequestError(
        "context length exceeded",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.example/v1")),
        body=None,
    )
    client = _ScriptedClient([refusal, iter([_chunk("never reached")])])

    # Replaying an identical request that the provider already rejected only burns
    # another call; only a lost connection is worth re-asking.
    with pytest.raises(BadRequestError):
        _call(client)
    assert len(client.calls) == 1


def test_only_the_completed_attempt_reports_usage() -> None:
    repository = FakeRepository()
    client = _ScriptedClient(
        [
            _dropped_stream(["半句"], httpx.RemoteProtocolError("incomplete chunked read")),
            iter([_chunk("回答"), _usage_chunk(1200, 340)]),
        ]
    )

    with collect_usage(repository, uuid4(), "report_writer"):
        assert _call(client) == "回答"

    assert repository.rows == [{"input_tokens": 1200, "output_tokens": 340}]


def test_retry_waits_between_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr(streaming, "_sleep", delays.append)
    client = _ScriptedClient(
        [
            _dropped_stream([], httpx.RemoteProtocolError("drop")),
            _dropped_stream([], httpx.RemoteProtocolError("drop")),
            iter([_chunk("回答")]),
        ]
    )

    assert _call(client, attempts=3) == "回答"
    assert delays == [
        streaming.STREAM_RETRY_DELAY_SECONDS,
        streaming.STREAM_RETRY_DELAY_SECONDS * 2,
    ]
