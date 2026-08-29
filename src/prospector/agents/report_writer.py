"""Markdown-only Report Writer adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from openai import OpenAI
from pydantic import ValidationError

from prospector.agents.llm import get_openai_client, strong_model, thinking_extra_body
from prospector.agents.prompts.report_writer import (
    report_writer_messages,
    report_writer_revision_messages,
)
from prospector.agents.streaming import stream_text
from prospector.deterministic.markdown_report import (
    MarkdownContractError,
    apply_block_replacements,
    parse_markdown,
)
from prospector.schemas.claims import AttributionRun, ReportReviewRun
from prospector.schemas.report import (
    ReportRevisionPatch,
    ResearchSynthesisRun,
    WriterSnapshot,
)

WRITER_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class ReportWriterResult:
    full_prompt: list[dict[str, str]]
    raw_output: object
    markdown: str
    patch: ReportRevisionPatch | None = None
    # Character ranges of newly written text in ``markdown``.  Empty on a first draft,
    # where every block is new; on a revision this is what the next attribution round
    # uses to tell rewritten passages from ones that keep their verdicts.
    new_regions: tuple[tuple[int, int], ...] = ()
    rejected: tuple[dict[str, object], ...] = ()


class ReportWriterModel(Protocol):
    def write(
        self, snapshot: WriterSnapshot, synthesis: ResearchSynthesisRun
    ) -> ReportWriterResult: ...

    def revise(
        self,
        snapshot: WriterSnapshot,
        synthesis: ResearchSynthesisRun,
        markdown: str,
        attribution: AttributionRun,
        review: ReportReviewRun,
        readthrough: dict[str, object] | None = None,
    ) -> ReportWriterResult: ...


class ReportWriterOutputError(ValueError):
    def __init__(self, message: str, raw_output: object) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class OpenAIReportWriter:
    def __init__(self, client: OpenAI | None = None, model: str | None = None) -> None:
        self.client = client or get_openai_client()
        self.model = model or strong_model()

    def _complete(self, messages: list[dict[str, str]]) -> ReportWriterResult:
        # One retry before giving up: a malformed draft is a formatting slip, and failing
        # the Job discards every Assertion the research phase paid for.
        last: MarkdownContractError | None = None
        content = ""
        for _ in range(WRITER_ATTEMPTS):
            content = stream_text(
                self.client,
                agent="report_writer",
                model=self.model,
                messages=messages,
                temperature=0.2,
                extra_body=thinking_extra_body(self.model),
            ).strip()
            try:
                parse_markdown(content)
            except MarkdownContractError as exc:
                last = exc
                continue
            return ReportWriterResult(full_prompt=messages, raw_output=content, markdown=content)
        raise ReportWriterOutputError(f"invalid Markdown Writer output: {last}", content)

    def write(
        self, snapshot: WriterSnapshot, synthesis: ResearchSynthesisRun
    ) -> ReportWriterResult:
        return self._complete(report_writer_messages(snapshot, synthesis))

    def revise(
        self,
        snapshot: WriterSnapshot,
        synthesis: ResearchSynthesisRun,
        markdown: str,
        attribution: AttributionRun,
        review: ReportReviewRun,
        readthrough: dict[str, object] | None = None,
    ) -> ReportWriterResult:
        """Apply the Writer's patch to the frozen report; never re-emit the whole thing.

        A full rewrite was measured on this project's own history: two revisions asked for
        19 and 39 fixes and came back with 25% and 15% of the document changed, and the
        first of them left the report with more statements and new failures than it
        started with.  Rewriting is not how coherence is kept, it is how it is re-rolled.
        """
        messages = report_writer_revision_messages(
            snapshot, synthesis, markdown, attribution, review, readthrough=readthrough
        )
        blocks = parse_markdown(markdown)
        last: Exception | None = None
        raw = ""
        for _ in range(WRITER_ATTEMPTS):
            raw = self._request(messages)
            try:
                patch = ReportRevisionPatch.model_validate_json(raw)
            except (ValidationError, ValueError) as exc:
                last = exc
                continue
            applied = apply_block_replacements(markdown, blocks, patch.replacements)
            try:
                parse_markdown(applied.markdown)
            except MarkdownContractError as exc:
                last = exc
                continue
            return ReportWriterResult(
                full_prompt=messages,
                raw_output=raw,
                markdown=applied.markdown,
                patch=patch,
                new_regions=applied.new_regions,
                rejected=applied.rejected,
            )
        raise ReportWriterOutputError(f"invalid Markdown Writer revision: {last}", raw)

    def _request(self, messages: list[dict[str, str]]) -> str:
        return stream_text(
            self.client,
            agent="report_writer",
            model=self.model,
            messages=messages,
            temperature=0.2,
            extra_body=thinking_extra_body(self.model),
        ).strip()
