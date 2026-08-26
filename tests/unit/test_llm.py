import pytest

from prospector.agents.llm import (
    LLM_TIMEOUT,
    no_thinking_extra_body,
    thinking_extra_body,
)


def test_llm_timeout_allows_long_streaming_reads() -> None:
    assert LLM_TIMEOUT.connect == 10.0
    assert LLM_TIMEOUT.read == 600.0
    assert LLM_TIMEOUT.write == 120.0
    assert LLM_TIMEOUT.pool == 120.0


@pytest.mark.parametrize(
    ("model", "thinking", "no_thinking"),
    [
        (
            "deepseek-v4-flash",
            {"thinking": {"type": "enabled"}},
            {"thinking": {"type": "disabled"}},
        ),
        (
            "qwen3.7-max",
            {"enable_thinking": True},
            {"enable_thinking": False},
        ),
    ],
)
def test_thinking_parameters_follow_model_family(
    model: str,
    thinking: dict[str, object],
    no_thinking: dict[str, object],
) -> None:
    assert thinking_extra_body(model) == thinking
    assert no_thinking_extra_body(model) == no_thinking


def test_thinking_parameters_reject_unknown_model_family() -> None:
    with pytest.raises(ValueError, match="DeepSeek or Qwen"):
        thinking_extra_body("unknown-model")
