"""Strong-model adapter for structured deep-research report writing.

The Writer holds one continuous authoring conversation: the model emits a flat
line-record stream (see prospector.schemas.report_stream), the runtime folds it
into a ReportDraft turn by turn, asks the model to continue when a turn ends
before {"record": "end"}, and feeds validation errors back for a localized
rewrite instead of restarting the whole report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from openai import OpenAI
from pydantic import ValidationError

from prospector.agents.llm import get_openai_client, strong_model
from prospector.agents.prompts.report_writer import (
    continuation_message,
    patch_restart_message,
    report_writer_messages,
    report_writer_revision_messages,
    retry_message,
)
from prospector.agents.usage import record_usage_value
from prospector.deterministic.statement_patches import (
    apply_statement_patches,
    preceding_statement_ids,
)
from prospector.schemas.claims import ReportVerifierFindings
from prospector.schemas.report import ReportDraft, WriterSnapshot, validate_writer_draft
from prospector.schemas.report_patch import ReportPatchAssembler
from prospector.schemas.report_stream import ReportStreamAssembler, ReportStreamError

MAX_WRITER_TURNS = 16
MAX_ERROR_FEEDBACKS = 3


@dataclass(frozen=True, slots=True)
class ReportWriterResult:
    full_prompt: list[dict[str, str]]
    raw_output: object
    draft: ReportDraft


class ReportWriterModel(Protocol):
    def write(self, snapshot: WriterSnapshot) -> ReportWriterResult: ...

    def revise(
        self,
        snapshot: WriterSnapshot,
        draft: ReportDraft,
        findings: ReportVerifierFindings,
    ) -> ReportWriterResult: ...


class ReportWriterOutputError(ValueError):
    def __init__(self, message: str, raw_output: object) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class OpenAIReportWriter:
    def __init__(self, client: OpenAI | None = None, model: str | None = None) -> None:
        self.client = client or get_openai_client()
        self.model = model or strong_model()

    def _stream_content(
        self,
        messages: list[dict[str, str]],
    ) -> str:
        stream = self.client.chat.completions.create(
            model=self.model,
            temperature=0.2,
            messages=messages,  # type: ignore[arg-type]
            stream=True,
            stream_options={"include_usage": True},
            extra_body={"enable_thinking": True},
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
        record_usage_value(usage, self.model)
        return "".join(parts)

    def write(self, snapshot: WriterSnapshot) -> ReportWriterResult:
        messages = report_writer_messages(snapshot)
        assembler = ReportStreamAssembler(snapshot)
        turns: list[str] = []
        error_feedbacks = 0
        for _ in range(MAX_WRITER_TURNS):
            content = self._stream_content(messages)
            turns.append(content)
            if not content.strip():
                raise ReportWriterOutputError("Report Writer returned empty content", turns)
            messages.append({"role": "assistant", "content": content})
            outcome = assembler.consume(content)
            if assembler.done:
                break
            if outcome.error is not None:
                error_feedbacks += 1
                if error_feedbacks > MAX_ERROR_FEEDBACKS:
                    raise ReportWriterOutputError(
                        f"invalid Report Writer output after retries: {outcome.error}", turns
                    )
                feedback = retry_message(outcome.error, assembler.last_accepted)
                messages.append({"role": "user", "content": feedback})
            else:
                messages.append(
                    {"role": "user", "content": continuation_message(assembler.last_accepted)}
                )
        else:
            raise ReportWriterOutputError(
                f"Report Writer did not finish within {MAX_WRITER_TURNS} turns", turns
            )
        try:
            draft = assembler.build()
            validate_writer_draft(snapshot, draft)
        except (ValidationError, ReportStreamError, ValueError) as exc:
            raise ReportWriterOutputError(f"invalid Report Writer output: {exc}", turns) from exc
        return ReportWriterResult(full_prompt=messages, raw_output=turns, draft=draft)

    def revise(
        self,
        snapshot: WriterSnapshot,
        draft: ReportDraft,
        findings: ReportVerifierFindings,
    ) -> ReportWriterResult:
        if findings.all_passed:
            raise ReportWriterOutputError("revision requested with empty findings", findings)
        messages = report_writer_revision_messages(snapshot, draft, findings)
        allowed = {item.statement_id for item in findings.failures}
        legal_premise_ids = preceding_statement_ids(draft)

        def new_assembler() -> ReportPatchAssembler:
            return ReportPatchAssembler(
                snapshot=snapshot,
                allowed_statement_ids=allowed,
                legal_premise_ids=legal_premise_ids,
            )

        assembler = new_assembler()
        turns: list[str] = []
        error_feedbacks = 0
        for _ in range(MAX_WRITER_TURNS):
            content = self._stream_content(messages)
            turns.append(content)
            if not content.strip():
                raise ReportWriterOutputError("Report Writer revise returned empty content", turns)
            messages.append({"role": "assistant", "content": content})
            outcome = assembler.consume(content)
            error = outcome.error
            restart = False
            if error is None and assembler.done:
                try:
                    patched = apply_statement_patches(
                        draft, assembler.patches, allowed_statement_ids=allowed
                    )
                    validate_writer_draft(snapshot, patched)
                except (ValidationError, ReportStreamError, ValueError) as exc:
                    # Invariants that only hold across the finished draft -- a reasoning chain
                    # deepened by a patch, say -- cannot be judged until every patch is in.
                    # Feeding the rejection back costs one turn; raising here used to cost the
                    # entire Job, research included.
                    error = str(exc)
                    restart = True
                    assembler = new_assembler()
                else:
                    return ReportWriterResult(full_prompt=messages, raw_output=turns, draft=patched)
            if error is not None:
                error_feedbacks += 1
                if error_feedbacks > MAX_ERROR_FEEDBACKS:
                    raise ReportWriterOutputError(
                        f"invalid Report Writer revise output after retries: {error}",
                        turns,
                    )
                feedback = (
                    patch_restart_message(error)
                    if restart
                    else retry_message(error, assembler.last_accepted)
                )
                messages.append({"role": "user", "content": feedback})
            else:
                messages.append(
                    {"role": "user", "content": continuation_message(assembler.last_accepted)}
                )
        raise ReportWriterOutputError(
            f"Report Writer revise did not finish within {MAX_WRITER_TURNS} turns", turns
        )
