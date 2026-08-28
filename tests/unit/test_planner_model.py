"""Planner model transport contract tests (deep-thinking JSON output + repair)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from prospector.agents.planner import (
    OpenAIPlannerModel,
    PlannerOutputError,
    append_decision,
)
from prospector.schemas.decisions import PlannerDecision


def _chunks(text: str, *, reasoning: str = "先思考一下") -> list[object]:
    """Stream chunks: reasoning deltas must be ignored, content deltas aggregated."""
    deltas: list[object] = [
        SimpleNamespace(content=None, reasoning_content=reasoning),
    ]
    step = max(1, len(text) // 3)
    for start in range(0, len(text), step):
        deltas.append(SimpleNamespace(content=text[start : start + step]))
    chunks = [SimpleNamespace(choices=[SimpleNamespace(delta=delta)]) for delta in deltas]
    return [SimpleNamespace(choices=[]), *chunks]


class _FakeCompletions:
    def __init__(self, stream_text: str, repair_text: str | None = None) -> None:
        self.stream_text = stream_text
        self.repair_text = repair_text
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            return iter(_chunks(self.stream_text))
        assert self.repair_text is not None, "unexpected non-stream (repair) call"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.repair_text))]
        )


class _FakeClient:
    def __init__(self, stream_text: str, repair_text: str | None = None) -> None:
        self.completions = _FakeCompletions(stream_text, repair_text)
        self.chat = SimpleNamespace(completions=self.completions)


def _model(client: _FakeClient) -> OpenAIPlannerModel:
    return OpenAIPlannerModel(
        client=client,  # type: ignore[arg-type]
        model="qwen-test-model",
        repair_model="qwen-test-repair-model",
    )


def _dispatch_payload() -> dict[str, object]:
    return {
        "decision": "dispatch",
        "tasks": [
            {
                "question": "核验一个明确研究对象的证据问题。",
                "expected_evidence": "获得直接证据并能判断目标是否满足。",
            }
        ],
        "reason": "继续关闭关键证据缺口",
    }


@pytest.mark.parametrize(
    ("payload", "decision_type"),
    [
        (_dispatch_payload(), "dispatch"),
        ({"decision": "finish", "reason": "现有证据已经充分"}, "finish"),
    ],
)
def test_planner_parses_streamed_json_decision(
    payload: dict[str, object],
    decision_type: str,
) -> None:
    client = _FakeClient(json.dumps(payload, ensure_ascii=False))
    result = _model(client).decide([])

    assert result.decision.decision == decision_type

    (request,) = client.completions.requests
    assert request["stream"] is True
    assert request["extra_body"] == {"enable_thinking": True}
    assert "tools" not in request
    assert "response_format" not in request


def test_planner_strips_code_fences_before_parsing() -> None:
    fenced = "```json\n" + json.dumps(_dispatch_payload(), ensure_ascii=False) + "\n```"
    client = _FakeClient(fenced)

    result = _model(client).decide([])

    assert result.decision.decision == "dispatch"
    assert len(client.completions.requests) == 1


def test_planner_uses_deepseek_thinking_parameter() -> None:
    client = _FakeClient(json.dumps(_dispatch_payload(), ensure_ascii=False))
    model = OpenAIPlannerModel(
        client=client,  # type: ignore[arg-type]
        model="deepseek-v4-flash",
        repair_model="deepseek-v4-flash",
    )

    model.decide([])

    (request,) = client.completions.requests
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}


def test_planner_repairs_broken_json_with_light_structured_model() -> None:
    valid = json.dumps(_dispatch_payload(), ensure_ascii=False)
    client = _FakeClient("经过思考，我的决定是：" + valid, repair_text=valid)

    result = _model(client).decide([])

    assert result.decision.decision == "dispatch"
    stream_request, repair_request = client.completions.requests
    assert stream_request["stream"] is True
    assert repair_request.get("stream") is None
    assert repair_request["model"] == "qwen-test-repair-model"
    assert repair_request["response_format"] == {"type": "json_object"}
    assert repair_request["extra_body"] == {"enable_thinking": False}
    assert isinstance(result.raw_output, dict)
    assert result.raw_output["repaired_content"] == valid


def test_planner_raises_when_repair_also_fails() -> None:
    client = _FakeClient("不是 JSON", repair_text='{"decision": "unknown"}')

    with pytest.raises(PlannerOutputError, match="repair failed"):
        _model(client).decide([])


def test_planner_rejects_empty_content() -> None:
    client = _FakeClient("")

    with pytest.raises(PlannerOutputError, match="empty content"):
        _model(client).decide([])


def test_planner_rejects_finish_with_dispatch_fields_even_after_repair() -> None:
    payload = {
        "decision": "finish",
        "reason": "结束",
        "tasks": [
            {
                "question": "核验公开数据",
                "expected_evidence": "至少一条直接证据",
            }
        ],
    }
    text = json.dumps(payload, ensure_ascii=False)
    client = _FakeClient(text, repair_text=text)

    with pytest.raises(PlannerOutputError, match="invalid Planner decision"):
        _model(client).decide([])


def test_planner_thread_records_only_the_selected_payload() -> None:
    decision = PlannerDecision.model_validate(_dispatch_payload())

    messages = append_decision([], decision)
    recorded = json.loads(str(messages[0]["content"]))
    assert recorded == decision.model_dump(mode="json", exclude_none=True)
