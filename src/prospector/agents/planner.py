"""Forced-schema Planner model adapter and closed message-thread helpers."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Protocol

from openai import OpenAI
from pydantic import ValidationError

from prospector.agents.llm import NO_THINKING_EXTRA_BODY, get_openai_client, mid_model, strong_model
from prospector.agents.prompts.planner import planner_brief_message, planner_system_prompt
from prospector.agents.usage import record_response_usage, record_usage_value
from prospector.deterministic.budget import ResearchLimits
from prospector.schemas.brief import ResearchBrief
from prospector.schemas.decisions import PlannerDecision

PlannerMessage = dict[str, Any]

_FORBIDDEN_RUNTIME_KEYS = {
    "document_text",
    "full_text",
    "document_view",
    "worker_messages",
    "worker_trace",
}


@dataclass(frozen=True, slots=True)
class PlannerModelResult:
    raw_output: object
    decision: PlannerDecision


class PlannerModel(Protocol):
    def decide(self, messages: list[PlannerMessage]) -> PlannerModelResult: ...


class PlannerOutputError(ValueError):
    def __init__(self, message: str, raw_output: object) -> None:
        super().__init__(message)
        self.raw_output = raw_output


def initial_planner_messages(
    brief: ResearchBrief,
    limits: ResearchLimits,
) -> list[PlannerMessage]:
    return [
        {"role": "system", "content": planner_system_prompt()},
        {"role": "user", "content": planner_brief_message(brief, limits)},
    ]


def _contains_forbidden_key(value: object) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in _FORBIDDEN_RUNTIME_KEYS:
                return key
            found = _contains_forbidden_key(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _contains_forbidden_key(nested)
            if found:
                return found
    return None


def append_decision(
    messages: list[PlannerMessage],
    decision_or_raw: object,
) -> list[PlannerMessage]:
    updated = copy.deepcopy(messages)
    if isinstance(decision_or_raw, PlannerDecision):
        selected = getattr(decision_or_raw, decision_or_raw.decision)
        assert selected is not None
        payload: object = {
            "decision": decision_or_raw.decision,
            decision_or_raw.decision: selected.model_dump(mode="json"),
        }
    else:
        payload = decision_or_raw
    updated.append(
        {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False, default=str)}
    )
    return updated


def append_runtime_feedback(
    messages: list[PlannerMessage],
    *,
    feedback_type: str,
    payload: dict[str, Any],
) -> list[PlannerMessage]:
    allowed = {
        "worker_projection",
        "rejection",
        "schema_error",
        "verifier_gap",
        "research_state",
        "reflection_recorded",
    }
    if feedback_type not in allowed:
        raise ValueError(f"planner thread rejects feedback type: {feedback_type}")
    forbidden = _contains_forbidden_key(payload)
    if forbidden:
        raise ValueError(f"planner thread rejects runtime key: {forbidden}")
    updated = copy.deepcopy(messages)
    updated.append(
        {
            "role": "user",
            "content": json.dumps(
                {"runtime_feedback": feedback_type, **payload},
                ensure_ascii=False,
                default=str,
            ),
        }
    )
    return updated


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    return stripped.strip()


def _repair_prompt(broken_output: str) -> str:
    schema = json.dumps(PlannerDecision.model_json_schema(), ensure_ascii=False)
    return f"""下面是一段本应为合法 JSON 的模型输出，但解析或校验失败。
请把它修复为符合 JSON Schema 的单个 JSON 对象后输出。
只允许修复语法和结构（引号、逗号、字段名、包裹层级、去除多余文本），
不得增删任务、改写研究内容或补充新事实。

JSON Schema：
{schema}

待修复输出：
{broken_output}

只输出修复后的 JSON 对象，不要任何其他文本。"""


class OpenAIPlannerModel:
    """Deep-thinking Planner.

    结构化输出与深度思考模式互斥（见供应商文档"深度思考模式下的替代方案"）：
    深度思考必须以 stream=True 调用且不能设置 response_format / strict 工具，
    因此这里流式收集正文并解析 JSON；解析失败时按文档建议用轻量模型
    （response_format=json_object，关闭思考）修复一次，仍失败才抛 PlannerOutputError。
    """

    def __init__(
        self,
        client: OpenAI | None = None,
        model: str | None = None,
        repair_model: str | None = None,
    ) -> None:
        self.client = client or get_openai_client()
        self.model = model or strong_model()
        self.repair_model = repair_model or mid_model()

    def _stream_content(self, messages: list[PlannerMessage]) -> str:
        stream = self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
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
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            text = getattr(delta, "content", None)
            if text:
                parts.append(text)
        record_usage_value(usage, self.model)
        return "".join(parts)

    def _repair_content(self, broken_output: str) -> str:
        response = self.client.chat.completions.create(
            model=self.repair_model,
            temperature=0.0,
            messages=[{"role": "user", "content": _repair_prompt(broken_output)}],
            response_format={"type": "json_object"},
            extra_body=NO_THINKING_EXTRA_BODY,
        )
        record_response_usage(response, self.repair_model)
        if not getattr(response, "choices", None):
            return ""
        return response.choices[0].message.content or ""

    @staticmethod
    def _parse_decision(content: str) -> PlannerDecision:
        return PlannerDecision.model_validate(json.loads(_strip_code_fences(content)))

    def decide(self, messages: list[PlannerMessage]) -> PlannerModelResult:
        content = self._stream_content(messages)
        raw: object = {"role": "assistant", "content": content}
        if not content.strip():
            raise PlannerOutputError("Planner returned empty content", raw)
        try:
            decision = self._parse_decision(content)
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as first_error:
            repaired = self._repair_content(content)
            raw = {"role": "assistant", "content": content, "repaired_content": repaired}
            try:
                decision = self._parse_decision(repaired)
            except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PlannerOutputError(
                    f"invalid Planner decision: {first_error}; repair failed: {exc}", raw
                ) from exc
        return PlannerModelResult(raw_output=raw, decision=decision)
