"""Streamed chat completions that survive a dropped connection.

Deep thinking must run with ``stream=True``, and a stream is exactly where the
OpenAI SDK's own retry budget stops applying: once the response headers are in,
the body is read by our own iteration, so a provider that closes the connection
mid-answer surfaces here as a raw transport error. One such drop -- ``peer closed
connection without sending complete message body`` -- used to end a Job that had
already spent thirty minutes finishing its research.

Every caller folds the model's answer turn by turn, which makes the turn the
natural retry unit: replaying an interrupted turn re-asks one question and keeps
everything already accepted.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from openai import APIConnectionError, OpenAI

from prospector.agents.usage import record_usage_value
from prospector.obs.logging import get_logger

log = get_logger("prospector.llm")

STREAM_ATTEMPTS = 3
"""Attempts per turn, the first call included."""

STREAM_RETRY_DELAY_SECONDS = 2.0
"""Delay before the first replay; doubled for each further one."""

# What a dropped stream looks like. httpx raises transport errors straight out of the
# body iteration (RemoteProtocolError for a truncated chunked read, ReadTimeout for a
# stalled one); the SDK wraps some of them in APIConnectionError, APITimeoutError
# included. Anything else -- a refusal, a 4xx, a malformed answer -- is the model's
# reply, not a lost connection, and must reach the caller unchanged.
DROPPED_STREAM_ERRORS: tuple[type[BaseException], ...] = (
    httpx.TransportError,
    APIConnectionError,
)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def stream_text(
    client: OpenAI,
    *,
    agent: str,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    temperature: float,
    extra_body: Mapping[str, Any] | None = None,
    attempts: int = STREAM_ATTEMPTS,
) -> str:
    """Stream one turn and return its content, replaying the turn if the stream drops.

    Usage is recorded for the attempt that completes. A dropped attempt reports none --
    the provider only sends its usage chunk last -- so its tokens go unaccounted.
    """
    for attempt in range(1, attempts + 1):
        try:
            return _stream_once(
                client,
                model=model,
                messages=messages,
                temperature=temperature,
                extra_body=extra_body,
            )
        except DROPPED_STREAM_ERRORS as exc:
            if attempt == attempts:
                log.error(
                    "llm.stream_dropped",
                    agent=agent,
                    model=model,
                    attempt=attempt,
                    attempts=attempts,
                    message=str(exc),
                )
                raise
            delay = STREAM_RETRY_DELAY_SECONDS * 2 ** (attempt - 1)
            log.warning(
                "llm.stream_retry",
                agent=agent,
                model=model,
                attempt=attempt,
                attempts=attempts,
                retry_in=f"{delay:.0f}s",
                message=str(exc),
            )
            _sleep(delay)
    raise AssertionError("unreachable: the last attempt either returns or raises")


def _stream_once(
    client: OpenAI,
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    temperature: float,
    extra_body: Mapping[str, Any] | None,
) -> str:
    stream = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=messages,  # type: ignore[arg-type]
        stream=True,
        stream_options={"include_usage": True},
        extra_body=dict(extra_body) if extra_body is not None else None,
    )
    parts: list[str] = []
    usage = None
    for chunk in stream:
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        if chunk.choices:
            text = getattr(chunk.choices[0].delta, "content", None)
            if text:
                parts.append(text)
    record_usage_value(usage, model)
    return "".join(parts)
