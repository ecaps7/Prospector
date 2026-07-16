"""Async retry with exponential backoff + jitter for transient HTTP errors."""

from __future__ import annotations

import asyncio
import random
import urllib.error
from typing import Any, Callable, TypeVar

from prospector.obs.logging import get_logger

T = TypeVar("T")

# HTTP status codes that are worth retrying (rate-limit / server errors).
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

log = get_logger("prospector.tools.retry")


def is_retryable(exc: BaseException) -> bool:
    """Return True if *exc* represents a transient, retry-worthy failure."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_STATUSES
    if isinstance(exc, urllib.error.URLError):
        return True  # DNS, connection refused, timeout, etc.
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    return False


async def retry_async(
    fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    label: str = "call",
    **kwargs: Any,
) -> Any:
    """Call async *fn* with exponential backoff + jitter on transient errors.

    Parameters
    ----------
    fn:
        An async callable (coroutine function).
    max_attempts:
        Total number of attempts (first try + retries). Must be >= 1.
    base_delay:
        Initial backoff delay in seconds (doubles each attempt).
    max_delay:
        Cap on the computed delay.
    label:
        Human-readable label used in log messages.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not is_retryable(exc):
                raise
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            jitter = random.uniform(0, delay * 0.5)  # noqa: S311
            wait = delay + jitter
            log.warning(
                "retry.attempt",
                label=label,
                attempt=attempt,
                max_attempts=max_attempts,
                wait_s=round(wait, 2),
                error=str(exc),
            )
            await asyncio.sleep(wait)

    # Unreachable in practice, but satisfies the type checker.
    raise last_exc  # type: ignore[misc]
