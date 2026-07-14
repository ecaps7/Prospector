"""The only evidence-producing Worker tool."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from prospector.deterministic.segment import select_excerpts
from prospector.schemas.evidence import FindingInput
from prospector.store.object_store import ObjectStore
from prospector.store.repositories import ResearchRepository
from prospector.tools.base import ToolContext


class SaveFindingsArguments(BaseModel):
    doc_id: UUID
    findings: list[FindingInput] = Field(..., min_length=1)


class SaveFindingsTool:
    name = "save_findings"

    def __init__(
        self,
        repository: ResearchRepository,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store or ObjectStore(repository.settings)

    async def __call__(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        parsed = SaveFindingsArguments.model_validate(arguments)
        document = await asyncio.to_thread(self.repository.get_document, parsed.doc_id)
        prefix = f"s3://{self.object_store.bucket}/"
        if not document.storage_ref.startswith(prefix):
            raise ValueError("document storage_ref does not belong to configured object store")
        key = document.storage_ref[len(prefix) :]
        raw = await asyncio.to_thread(self.object_store.get_bytes, key)
        full_text = raw.decode("utf-8")
        selected = [
            (finding, excerpt, locator)
            for finding in parsed.findings
            for excerpt, locator in select_excerpts(full_text, finding.para_ids)
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
        "description": "Persist exact source paragraphs and bind them to assertions atomically.",
        "parameters": SaveFindingsArguments.model_json_schema(),
    },
}
