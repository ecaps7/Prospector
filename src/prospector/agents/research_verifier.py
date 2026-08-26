"""Strong-model adapter for the Research Verifier."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from openai import OpenAI
from pydantic import ValidationError

from prospector.agents.llm import (
    get_openai_client,
    mid_model,
    no_thinking_extra_body,
    strong_model,
    thinking_extra_body,
)
from prospector.agents.prompts.research_verifier import research_verifier_messages
from prospector.agents.streaming import stream_text
from prospector.agents.usage import record_response_usage
from prospector.schemas.verifier import (
    VerifierDecision,
    VerifierLlmDecision,
    assertion_excerpt_map_from_snapshot,
    materialize_verifier_decision,
)


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


def _repair_prompt(broken_output: str, syntax_error: str = "") -> str:
    """Form-only repair: braces and quotes, never judgement.

    The repair model never sees the snapshot, so it must only be asked questions that are
    answerable without it. Anything about *what the Verifier decided* goes back to the
    Verifier instead (see ``_contract_retry_message``).
    """
    schema = json.dumps(VerifierLlmDecision.model_json_schema(), ensure_ascii=False)
    error_section = f"\n解析器报告的错误：\n{syntax_error}\n" if syntax_error else ""
    return f"""下面输出本应是符合 JSON Schema 的单个 JSON 对象，但它不是合法 JSON。
只修复 JSON 语法或结构，不得增删缺口、冲突裁决、引用 ID，不得改写任何判断。
{error_section}
JSON Schema：
{schema}

待修复输出：
{broken_output}

只输出修复后的 JSON。"""


def _contract_retry_message(validation_error: str) -> str:
    """Ask the Verifier itself to restate its judgement legally, snapshot still in thread."""
    return f"""你刚才的输出是合法 JSON，但不满足契约约束：
{validation_error}

请重新输出完整的判断 JSON。你可以坚持原本的结论，只需把它表达成合法形式；
除修正该冲突所必需的改动外，不要改写其它判断。

提示：source_credibility 缺口的 related_assertion_ids 表示"这个缺口涉及哪些断言"，
不表示"要作废哪些断言"——是否作废由 assertion_dispositions 单独表达，
minor 缺口可以只点名而不废证。

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

    def _repair_content(self, broken_output: str, syntax_error: str = "") -> str:
        response = self.client.chat.completions.create(
            model=self.repair_model,
            temperature=0.0,
            messages=[{"role": "user", "content": _repair_prompt(broken_output, syntax_error)}],
            response_format={"type": "json_object"},
            extra_body=no_thinking_extra_body(self.repair_model),
        )
        record_response_usage(response, self.repair_model)
        if not getattr(response, "choices", None):
            return ""
        return response.choices[0].message.content or ""

    @staticmethod
    def _parse_llm(content: str) -> VerifierLlmDecision:
        return VerifierLlmDecision.model_validate(json.loads(_strip_code_fences(content)))

    def _decide(self, content: str, snapshot: dict[str, Any]) -> VerifierDecision:
        """Parse one answer and bind its assertion references to authoritative excerpt ids.

        Binding belongs here rather than after the retry: naming assertions that do not
        resolve to two distinct excerpts is a reference mistake of exactly the same kind as
        any other contract violation, and it is repaired the same way -- by telling the model
        that made it. Left outside, it killed Jobs that had already finished their research.
        """
        llm_decision = self._parse_llm(content)
        return materialize_verifier_decision(
            llm_decision,
            assertion_excerpt_map_from_snapshot(snapshot),
        )

    def verify(self, snapshot: dict[str, Any]) -> VerifierModelResult:
        messages = research_verifier_messages(snapshot)
        content = self._stream_content(messages)
        raw: object = {"role": "assistant", "content": content}
        if not content.strip():
            raise VerifierOutputError("Verifier returned empty content", raw)
        try:
            decision = self._decide(content, snapshot)
        except json.JSONDecodeError as syntax_error:
            # Broken JSON is a formatting failure: the cheap model can close the braces
            # without seeing the snapshot, because no judgement changes.
            repaired = self._repair_content(content, str(syntax_error))
            raw = {"role": "assistant", "content": content, "repaired_content": repaired}
            try:
                decision = self._decide(repaired, snapshot)
            except (ValidationError, TypeError, ValueError) as exc:
                raise VerifierOutputError(
                    f"malformed Verifier JSON: {syntax_error}; repair failed: {exc}", raw
                ) from exc
        except (ValidationError, TypeError, ValueError) as contract_error:
            # A contract violation is a judgement failure, so it goes back to the model
            # that made the judgement, with the snapshot still in the thread. Handing it to
            # the cheap repair model would be asking it to re-decide which evidence to
            # discard while looking at neither the evidence nor the question.
            retry_messages = [
                *messages,
                {"role": "assistant", "content": content},
                {"role": "user", "content": _contract_retry_message(str(contract_error))},
            ]
            retried = self._stream_content(retry_messages)
            raw = {"role": "assistant", "content": content, "retried_content": retried}
            try:
                decision = self._decide(retried, snapshot)
            except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise VerifierOutputError(
                    f"invalid Verifier decision: {contract_error}; retry failed: {exc}", raw
                ) from exc
        return VerifierModelResult(full_prompt=messages, raw_output=raw, decision=decision)
