"""Live contract check for model-generated save_findings arguments."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from prospector.agents.llm import (
    LlmNotConfiguredError,
    get_async_openai_client,
    mid_model,
    require_llm_settings,
)
from prospector.config import clear_settings_cache, get_settings
from prospector.tools.save_findings import SAVE_FINDINGS_SCHEMA, SaveFindingsArguments

pytestmark = pytest.mark.live

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def _load_env_and_require_llm() -> None:
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
    clear_settings_cache()
    try:
        get_settings()
        require_llm_settings()
    except (LlmNotConfiguredError, Exception) as exc:
        pytest.skip(f"LLM / settings not available: {exc}")


async def test_model_returns_save_findings_arguments_matching_the_schema() -> None:
    response = await get_async_openai_client().chat.completions.create(
        model=mid_model(),
        temperature=0.0,
        messages=[
            {
                "role": "user",
                "content": (
                    "调用 save_findings 保存两条断言。doc_id 为 "
                    "774bb4d8-0f37-44e9-9922-082a8821a0cd；"
                    "view_id 为 29e692e6-169b-4f78-adf0-34a38630d766；"
                    "第一条使用 source_id h1，断言为第一条事实；"
                    "第二条使用 source_id h2，断言为第二条事实；标签均为空。"
                ),
            }
        ],
        tools=[SAVE_FINDINGS_SCHEMA],  # type: ignore[list-item]
        tool_choice={
            "type": "function",
            "function": {"name": "save_findings"},
        },
        parallel_tool_calls=True,
        extra_body={"enable_thinking": False},
    )
    calls = response.choices[0].message.tool_calls or []

    assert len(calls) == 1
    function = getattr(calls[0], "function", None)
    assert function is not None
    arguments = json.loads(function.arguments)
    parsed = SaveFindingsArguments.model_validate(arguments)

    assert len(parsed.findings) == 2
    assert parsed.view_id.hex == "29e692e6169b4f78adf034a38630d766"
    assert all(finding.topic_tags == [] for finding in parsed.findings)
