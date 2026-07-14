"""Shared Worker tool context and result protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ToolContext:
    job_id: UUID
    task_id: UUID
    worker_id: str
    task_question: str
    tool_call_id: str


class WorkerTool(Protocol):
    name: str

    async def __call__(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]: ...
