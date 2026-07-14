"""Forced-schema Planner model adapter and closed message-thread helpers."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Protocol

from openai import OpenAI
from pydantic import ValidationError

from prospector.agents.llm import get_openai_client, strong_model
from prospector.agents.prompts.planner import planner_brief_message, planner_system_prompt
from prospector.deterministic.budget import ResearchLimits
from prospector.schemas.brief import ResearchBrief
from prospector.schemas.decisions import PlannerDecision

PlannerMessage = dict[str, Any]

_FORBIDDEN_RUNTIME_KEYS = {
    "document_text",
    "full_text",
    "compressed_view",
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
    payload = (
        decision_or_raw.model_dump(mode="json")
        if isinstance(decision_or_raw, PlannerDecision)
        else decision_or_raw
    )
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
        "budget",
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


def _coerce_stringified_fields(value: object) -> object:
    """Recursively parse string values that look like JSON objects/arrays.

    Some providers (e.g. QianwenAI with strict=True) return nested object fields
    as JSON-encoded strings instead of inline objects. This helper normalises them.
    """
    if isinstance(value, dict):
        return {k: _coerce_stringified_fields(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_stringified_fields(v) for v in value]
    if isinstance(value, str) and len(value) > 1 and value[0] in "{[":
        try:
            return _coerce_stringified_fields(json.loads(value))
        except json.JSONDecodeError:
            return value
    return value


class OpenAIPlannerModel:
    tool_name = "submit_planner_decision"

    def __init__(self, client: OpenAI | None = None, model: str | None = None) -> None:
        self.client = client or get_openai_client()
        self.model = model or strong_model()

    def decide(self, messages: list[PlannerMessage]) -> PlannerModelResult:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=messages,  # type: ignore[arg-type]
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": self.tool_name,
                        "description": "Submit exactly one Planner decision.",
                        "parameters": PlannerDecision.model_json_schema(),
                        "strict": True,
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": self.tool_name}},
            parallel_tool_calls=False,
            extra_body={"enable_thinking": False},
        )
        message = response.choices[0].message
        tool_calls = message.tool_calls or []
        raw: object = message.model_dump(mode="json")
        call = tool_calls[0] if len(tool_calls) == 1 else None
        function = getattr(call, "function", None)
        if function is None or function.name != self.tool_name:
            raise PlannerOutputError("Planner must make exactly one forced decision tool call", raw)
        arguments = function.arguments
        try:
            parsed = _coerce_stringified_fields(json.loads(arguments))
            decision = PlannerDecision.model_validate(parsed)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise PlannerOutputError(f"invalid Planner decision: {exc}", raw) from exc
        return PlannerModelResult(raw_output=raw, decision=decision)
