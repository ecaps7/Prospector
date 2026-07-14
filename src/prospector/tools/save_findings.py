"""The only evidence-producing Worker tool."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prospector.schemas.evidence import FindingInput
from prospector.store.repositories import ResearchRepository
from prospector.tools.base import ToolContext


class SaveFindingsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: UUID
    view_id: UUID
    findings: list[FindingInput] = Field(..., min_length=1)


class SaveFindingsTool:
    name = "save_findings"

    def __init__(self, repository: ResearchRepository) -> None:
        self.repository = repository

    async def __call__(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        parsed = SaveFindingsArguments.model_validate(arguments)
        document = await asyncio.to_thread(self.repository.get_document, parsed.doc_id)
        view = await asyncio.to_thread(self.repository.get_document_view, parsed.view_id)
        if view.job_id != context.job_id or view.task_id != context.task_id:
            raise ValueError("document view does not belong to the current task")
        if view.doc_id != document.doc_id or view.doc_version != document.version:
            raise ValueError("document view does not match the requested document version")

        allowed_ids = {source_id for item in view.items for source_id in item.source_ids}
        requested_ids = {
            source_id for finding in parsed.findings for source_id in finding.source_ids
        }
        unknown_ids = sorted(requested_ids - allowed_ids)
        if unknown_ids:
            raise ValueError(f"source ids are not present in document view: {unknown_ids}")

        selected: list[tuple[FindingInput, str, dict[str, object]]]
        highlights = {source_id: item.text for item in view.items for source_id in item.source_ids}
        selected = [
            (
                finding,
                highlights[source_id],
                {
                    "kind": "exa_highlight",
                    "view_id": str(view.view_id),
                    "highlight_id": source_id,
                },
            )
            for finding in parsed.findings
            for source_id in finding.source_ids
        ]
        assertions, inserted = await asyncio.to_thread(
            self.repository.save_findings,
            job_id=context.job_id,
            task_id=context.task_id,
            document=document,
            findings=selected,
            worker_id=context.worker_id,
            tool_call_id=context.tool_call_id,
        )
        return {
            "inserted": inserted,
            "assertion_ids": [str(assertion.assertion_id) for assertion in assertions],
        }


SAVE_FINDINGS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "save_findings",
        "description": (
            "Persist source items from one web_fetch view and bind them to assertions atomically."
        ),
        "parameters": SaveFindingsArguments.model_json_schema(),
        "strict": True,
    },
}
