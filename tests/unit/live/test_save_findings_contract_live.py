"""Live contract check for model-generated save_findings arguments."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from prospector.agents.llm import (
    LlmNotConfiguredError,
    get_async_openai_client,
    mid_model,
    no_thinking_extra_body,
    require_llm_settings,
)
from prospector.agents.prompts.research_worker import worker_system_prompt
from prospector.agents.research_worker import (
    WORKER_ACTION_RESPONSE_FORMAT,
    WORKER_ACTION_SCHEMA,
    WorkerAction,
)
from prospector.config import clear_settings_cache, get_settings

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


async def test_model_returns_strict_save_action_matching_the_schema() -> None:
    response = await get_async_openai_client().chat.completions.create(
        model=mid_model(),
        temperature=0.0,
        messages=[
            {
                "role": "system",
                "content": worker_system_prompt(action_schema=WORKER_ACTION_SCHEMA),
            },
            {
                "role": "user",
                "content": (
                    "输出一个 save 动作 JSON，保存两条断言。"
                    "第一条使用 source_ref s1:h1，断言为第一条事实；"
                    "第二条使用 source_ref s1:h2，断言为第二条事实；标签均为空。"
                    "searches 必须为空数组，finish 必须为 null。只输出 JSON。"
                ),
            },
        ],
        response_format=WORKER_ACTION_RESPONSE_FORMAT,
        extra_body=no_thinking_extra_body(mid_model()),
    )
    content = response.choices[0].message.content
    assert content is not None
    parsed = WorkerAction.model_validate_json(content)

    assert parsed.action == "save"
    assert parsed.searches == []
    assert parsed.finish is None
    assert len(parsed.save_batches) == 1
    batch = parsed.save_batches[0]
    assert len(batch.findings) == 2
    assert batch.findings[0].source_refs == ["s1:h1"]
    assert all(finding.topic_tags == [] for finding in batch.findings)
