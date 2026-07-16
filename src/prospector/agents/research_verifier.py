"""Strong-model adapter for the Research Verifier."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from openai import OpenAI
from pydantic import ValidationError

from prospector.agents.llm import get_openai_client, mid_model, strong_model
from prospector.agents.prompts.research_verifier import research_verifier_messages
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


def _repair_prompt(broken_output: str) -> str:
    schema = json.dumps(VerifierLlmDecision.model_json_schema(), ensure_ascii=False)
    return f"""下面输出本应是符合 JSON Schema 的单个 JSON 对象。
只修复 JSON 语法或结构，不得增删缺口、冲突裁决、引用 ID，不得改写任何判断。

JSON Schema：
{schema}

待修复输出：
{broken_output}

只输出修复后的 JSON。"""


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
        stream = self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=messages,  # type: ignore[arg-type]
            stream=True,
            extra_body={"enable_thinking": True},
        )
        parts: list[str] = []
        for chunk in stream:
            if chunk.choices:
                text = getattr(chunk.choices[0].delta, "content", None)
                if text:
                    parts.append(text)
        return "".join(parts)

    def _repair_content(self, broken_output: str) -> str:
        response = self.client.chat.completions.create(
            model=self.repair_model,
            temperature=0.0,
            messages=[{"role": "user", "content": _repair_prompt(broken_output)}],
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        )
        if not getattr(response, "choices", None):
            return ""
        return response.choices[0].message.content or ""

    @staticmethod
    def _parse_llm(content: str) -> VerifierLlmDecision:
        return VerifierLlmDecision.model_validate(json.loads(_strip_code_fences(content)))

    def verify(self, snapshot: dict[str, Any]) -> VerifierModelResult:
        messages = research_verifier_messages(snapshot)
        content = self._stream_content(messages)
        raw: object = {"role": "assistant", "content": content}
        if not content.strip():
            raise VerifierOutputError("Verifier returned empty content", raw)
        try:
            llm_decision = self._parse_llm(content)
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as first_error:
            repaired = self._repair_content(content)
            raw = {"role": "assistant", "content": content, "repaired_content": repaired}
            try:
                llm_decision = self._parse_llm(repaired)
            except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise VerifierOutputError(
                    f"invalid Verifier decision: {first_error}; repair failed: {exc}", raw
                ) from exc
        try:
            decision = materialize_verifier_decision(
                llm_decision,
                assertion_excerpt_map_from_snapshot(snapshot),
            )
        except ValueError as exc:
            raise VerifierOutputError(
                f"invalid Verifier conflict binding: {exc}", raw
            ) from exc
        return VerifierModelResult(full_prompt=messages, raw_output=raw, decision=decision)
