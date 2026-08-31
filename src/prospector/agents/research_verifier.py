"""Strong-model adapter for the Research Verifier."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar
from uuid import UUID

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from prospector.agents.llm import (
    get_openai_client,
    mid_model,
    no_thinking_extra_body,
    strong_model,
    thinking_extra_body,
)
from prospector.agents.prompts.research_verifier import (
    research_coverage_messages,
    research_verifier_messages,
)
from prospector.agents.streaming import stream_text
from prospector.agents.usage import record_response_usage
from prospector.deterministic.model_refs import ResearchModelRefs
from prospector.deterministic.verifier_projection import (
    build_verifier_coverage_snapshot,
    resolve_coverage_decision_refs,
    resolve_evidence_review_refs,
)
from prospector.schemas.verifier import (
    VerifierCoverageDecisionRefs,
    VerifierDecision,
    VerifierEvidenceReviewRefs,
    assertion_excerpt_map_from_snapshot,
    materialize_conflict_resolutions,
    materialize_verifier_decision,
    validate_coverage_references,
    validate_evidence_review_references,
    validate_verifier_references,
    verifier_reference_ids_from_snapshot,
)

VerifierStageOutput = TypeVar("VerifierStageOutput", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class VerifierModelResult:
    full_prompt: list[dict[str, str]]
    raw_output: object
    decision: VerifierDecision


class VerifierModel(Protocol):
    def verify(self, snapshot: dict[str, Any]) -> VerifierModelResult: ...


class VerifierOutputError(ValueError):
    def __init__(self, message: str, raw_output: object) -> None:
        super().__init__(message)
        self.raw_output = raw_output


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    return stripped.strip()


def _repair_prompt(
    broken_output: str,
    output_model: type[BaseModel],
    syntax_error: str = "",
) -> str:
    """Form-only repair: braces and quotes, never judgement.

    The repair model never sees the snapshot, so it must only be asked questions that are
    answerable without it. Anything about *what the Verifier decided* goes back to the
    Verifier instead (see ``_contract_retry_message``).
    """
    schema = json.dumps(output_model.model_json_schema(), ensure_ascii=False)
    error_section = f"\n解析器报告的错误：\n{syntax_error}\n" if syntax_error else ""
    return f"""下面输出本应是符合 JSON Schema 的单个 JSON 对象，但它不是合法 JSON。
只修复 JSON 语法或结构，不得增删缺口、冲突裁决、引用 ID，不得改写任何判断。
{error_section}
JSON Schema：
{schema}

待修复输出：
{broken_output}

只输出修复后的 JSON。"""


def _contract_retry_message(stage: str, validation_error: str) -> str:
    """Ask the Verifier itself to restate its judgement legally, snapshot still in thread."""
    stage_hint = (
        "source_credibility_findings、conflicts 和 assertion_dispositions 只能引用当前快照中的 "
        "Assertion 短 ref；同一 Excerpt 上的矛盾不是来源冲突。"
        if stage == "qualification"
        else "gaps 只能引用覆盖快照中的 Task 短 ref 与 usable Assertion 短 ref；"
        "source_credibility 缺口必须点名相关 usable Assertion。"
    )
    return f"""你刚才的输出是合法 JSON，但不满足契约约束：
{validation_error}

请重新输出完整的判断 JSON。你可以坚持原本的结论，只需把它表达成合法形式；
除修正该冲突所必需的改动外，不要改写其它判断。

提示：{stage_hint}

只输出 JSON，不要 Markdown 或额外文字。"""


class OpenAIResearchVerifier:
    def __init__(
        self,
        client: OpenAI | None = None,
        model: str | None = None,
        repair_model: str | None = None,
    ) -> None:
        self.client = client or get_openai_client()
        self.model = model or strong_model()
        self.repair_model = repair_model or mid_model()

    def _stream_content(self, messages: list[dict[str, str]]) -> str:
        return stream_text(
            self.client,
            agent="research_verifier",
            model=self.model,
            messages=messages,
            temperature=0.0,
            extra_body=thinking_extra_body(self.model),
        )

    def _repair_content(
        self,
        broken_output: str,
        output_model: type[BaseModel],
        syntax_error: str = "",
    ) -> str:
        response = self.client.chat.completions.create(
            model=self.repair_model,
            temperature=0.0,
            messages=[
                {
                    "role": "user",
                    "content": _repair_prompt(broken_output, output_model, syntax_error),
                }
            ],
            response_format={"type": "json_object"},
            extra_body=no_thinking_extra_body(self.repair_model),
        )
        record_response_usage(response, self.repair_model)
        if not getattr(response, "choices", None):
            return ""
        return response.choices[0].message.content or ""

    @staticmethod
    def _parse_stage(
        content: str,
        output_model: type[VerifierStageOutput],
        validate: Callable[[VerifierStageOutput], None],
    ) -> VerifierStageOutput:
        parsed = output_model.model_validate(json.loads(_strip_code_fences(content)))
        validate(parsed)
        return parsed

    def _run_stage(
        self,
        *,
        stage: str,
        messages: list[dict[str, str]],
        output_model: type[VerifierStageOutput],
        validate: Callable[[VerifierStageOutput], None],
    ) -> tuple[VerifierStageOutput, object]:
        content = self._stream_content(messages)
        raw: object = {"role": "assistant", "content": content}
        if not content.strip():
            raise VerifierOutputError(f"Verifier {stage} returned empty content", raw)
        try:
            result = self._parse_stage(content, output_model, validate)
        except json.JSONDecodeError as syntax_error:
            repaired = self._repair_content(content, output_model, str(syntax_error))
            raw = {"role": "assistant", "content": content, "repaired_content": repaired}
            try:
                result = self._parse_stage(repaired, output_model, validate)
            except (ValidationError, TypeError, ValueError) as exc:
                raise VerifierOutputError(
                    f"malformed Verifier {stage} JSON: {syntax_error}; repair failed: {exc}",
                    raw,
                ) from exc
        except (ValidationError, TypeError, ValueError) as contract_error:
            retry_messages = [
                *messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": _contract_retry_message(stage, str(contract_error)),
                },
            ]
            retried = self._stream_content(retry_messages)
            raw = {"role": "assistant", "content": content, "retried_content": retried}
            try:
                result = self._parse_stage(retried, output_model, validate)
            except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise VerifierOutputError(
                    f"invalid Verifier {stage} decision: {contract_error}; retry failed: {exc}",
                    raw,
                ) from exc
        return result, raw

    def verify(self, snapshot: dict[str, Any]) -> VerifierModelResult:
        refs = ResearchModelRefs.from_verifier_snapshot(snapshot)
        qualification_messages = research_verifier_messages(snapshot)
        task_ids, assertion_ids, excerpt_ids = verifier_reference_ids_from_snapshot(snapshot)
        assertion_excerpts = assertion_excerpt_map_from_snapshot(snapshot)

        def validate_review(review: VerifierEvidenceReviewRefs) -> None:
            resolved = resolve_evidence_review_refs(review, refs)
            validate_evidence_review_references(resolved, assertion_ids=assertion_ids)
            # Binding is checked in the same pass that authored the conflict judgement, so
            # same-excerpt and unknown-assertion mistakes return to the evidence reviewer.
            materialize_conflict_resolutions(resolved.conflicts, assertion_excerpts)

        try:
            evidence_review_refs, qualification_raw = self._run_stage(
                stage="qualification",
                messages=qualification_messages,
                output_model=VerifierEvidenceReviewRefs,
                validate=validate_review,
            )
        except VerifierOutputError as exc:
            raise VerifierOutputError(str(exc), {"qualification": exc.raw_output}) from exc

        evidence_review = resolve_evidence_review_refs(evidence_review_refs, refs)
        coverage_snapshot = build_verifier_coverage_snapshot(snapshot, evidence_review)
        coverage_messages = research_coverage_messages(coverage_snapshot, refs)
        usable_assertion_ids = {
            UUID(str(row["assertion_id"]))
            for row in coverage_snapshot["usable_assertions"]
            if isinstance(row, dict) and row.get("assertion_id") is not None
        }

        def validate_coverage(decision: VerifierCoverageDecisionRefs) -> None:
            resolved = resolve_coverage_decision_refs(decision, refs)
            validate_coverage_references(
                resolved,
                task_ids=task_ids,
                usable_assertion_ids=usable_assertion_ids,
            )

        try:
            coverage_decision_refs, coverage_raw = self._run_stage(
                stage="coverage",
                messages=coverage_messages,
                output_model=VerifierCoverageDecisionRefs,
                validate=validate_coverage,
            )
        except VerifierOutputError as exc:
            raise VerifierOutputError(
                str(exc),
                {
                    "qualification": qualification_raw,
                    "coverage_prompt": coverage_messages,
                    "coverage": exc.raw_output,
                },
            ) from exc

        coverage_decision = resolve_coverage_decision_refs(coverage_decision_refs, refs)
        decision = materialize_verifier_decision(
            evidence_review,
            coverage_decision,
            assertion_excerpts,
        )
        validate_verifier_references(
            decision,
            task_ids=task_ids,
            assertion_ids=assertion_ids,
            excerpt_ids=excerpt_ids,
        )
        raw_output = {
            "qualification": qualification_raw,
            "coverage_prompt": coverage_messages,
            "coverage": coverage_raw,
        }
        return VerifierModelResult(
            full_prompt=qualification_messages,
            raw_output=raw_output,
            decision=decision,
        )
