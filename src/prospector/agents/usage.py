"""Job-scoped persistence of provider-reported LLM usage."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


class UsageRepository(Protocol):
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
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class UsageContext:
    repository: UsageRepository
    job_id: UUID
    component: str
    task_id: UUID | None


_CURRENT_USAGE: ContextVar[UsageContext | None] = ContextVar(
    "prospector_current_usage",
    default=None,
)


@contextmanager
def collect_usage(
    repository: UsageRepository,
    job_id: UUID,
    component: str,
    *,
    task_id: UUID | None = None,
) -> Iterator[None]:
    token = _CURRENT_USAGE.set(
        UsageContext(
            repository=repository,
            job_id=job_id,
            component=component,
            task_id=task_id,
        )
    )
    try:
        yield
    finally:
        _CURRENT_USAGE.reset(token)


def record_response_usage(response: Any, model: str) -> None:
    record_usage_value(getattr(response, "usage", None), model)


def record_usage_value(usage: Any, model: str) -> None:
    context = _CURRENT_USAGE.get()
    if context is None or usage is None:
        return
    input_tokens = _token_value(usage, "prompt_tokens", "input_tokens")
    output_tokens = _token_value(usage, "completion_tokens", "output_tokens")
    if input_tokens is None or output_tokens is None:
        return
    context.repository.record_usage(
        context.job_id,
        component=context.component,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        task_id=context.task_id,
    )


def _token_value(usage: Any, *names: str) -> int | None:
    for name in names:
        value = getattr(usage, name, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(name)
        if value is not None:
            return int(value)
    return None
