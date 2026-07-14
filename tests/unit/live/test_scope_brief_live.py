"""Live Scope tests — real LLM, skip when credentials missing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from prospector.agents.llm import LlmNotConfiguredError, require_llm_settings
from prospector.agents.scope import run_scope
from prospector.config import clear_settings_cache, get_settings

pytestmark = pytest.mark.live

REPO_ROOT = Path(__file__).resolve().parents[2]

# ─────────────────────────────────────────────────────────────────────────────
# 短问题：“生物技术”在不同语境下指向完全不同的研究领域
# （医疗生物技术 / 工业生物技术 / 农业生物技术 / 生物安全），
# Brief 无法同时覆盖各解读方向而不沦为泛泛概述，应触发 clarify 路径。
# ─────────────────────────────────────────────────────────────────────────────
SHORT_QUESTION = "帮我研究一下生物技术。"

# ─────────────────────────────────────────────────────────────────────────────
# 长问题：研究对象、目的、输出要求均已明确给出，Scope 应直接产出 Brief，
# 不应再追问用户。
# ─────────────────────────────────────────────────────────────────────────────
LONG_QUESTION = (
    "随着超大城市（Megacities）的持续扩张，轨道交通等骨干网络已相对完善，"
    "但连接居民区、商业区与轨道交通站点的\u201c最后一公里\u201d（First/Last Mile）"
    "微循环交通依然存在严重的效率瓶颈。"
    "请你研究一下不同的城市对这一问题是如何实践的，并给出你的建议。"
)


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


def _print_divider(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def test_short_question_triggers_clarification() -> None:
    """“生物技术”指向多个完全不同的研究领域，Scope 应请求澄清。"""
    outcome = run_scope(SHORT_QUESTION)

    _print_divider("短问题测试结果")
    print(f"原始问题：{SHORT_QUESTION}")
    print(f"模型决策：{outcome.kind!r}")

    assert outcome.kind == "clarify", (
        f"预期 kind='clarify'，实际得到 {outcome.kind!r}；"
        "“生物技术”在不同语境下指向完全不同的研究领域，Scope 应追问方向而非泛泛展开"
    )
    assert outcome.clarification_question, "澄清问题不得为空"

    q = outcome.clarification_question.strip()
    print(f"澄清问题：{q}")
    assert len(q) > 5, f"澄清问题过短，可能没有实质内容：{q!r}"

    # 澄清应指向领域辨析，而非泛泛要求"说得更具体"
    assert any(
        token in q
        for token in (
            "生物技术",
            "基因",
            "合成生物",
            "农业",
            "工业",
            "医疗",
            "制药",
            "安全",
            "方向",
            "领域",
            "方面",
            "关注",
            "聚焦",
        )
    ), f"澄清问题未触及领域辨析：{q!r}"


def test_long_question_yields_brief_directly() -> None:
    """长问题已充分具体，Scope 应直接产出 Brief，不请求澄清。"""
    outcome = run_scope(LONG_QUESTION, language="zh", effort="standard")

    _print_divider("长问题测试结果")
    print(f"原始问题：{LONG_QUESTION}")
    print(f"模型决策：{outcome.kind!r}")

    assert outcome.kind == "brief_pending", (
        f"预期 kind='brief_pending'，实际得到 {outcome.kind!r}；"
        "长问题已明确研究对象与目的，Scope 不应再追问"
    )
    assert outcome.brief is not None, "brief_pending 必须携带 brief"

    brief = outcome.brief
    print(f"Brief 标题（question）：{brief.question}")
    print(f"Brief effort：{brief.effort}")
    print(f"Brief language：{brief.language}")
    print(f"Brief 正文（brief_text）：\n{brief.brief_text}")

    # 基本字段完整性
    assert len(brief.brief_text) >= 80, (
        f"brief_text 过短（{len(brief.brief_text)} 字符），深度研究 Brief 应充分展开研究空间"
    )
    assert brief.language.startswith("zh"), f"language 应为中文，实际：{brief.language!r}"
    assert brief.effort == "standard", f"effort 应为 standard，实际：{brief.effort!r}"

    # 用户明确要求的语义锚点：Brief 必须保留这些核心概念
    text = brief.brief_text
    assert any(t in text for t in ("最后一公里", "First", "Last Mile", "last mile")), (
        f"brief_text 未保留用户明确的研究对象'最后一公里'：{text!r}"
    )
    assert any(t in text for t in ("城市", "实践", "案例", "经验")), (
        f"brief_text 未体现'不同城市实践'这一比较维度：{text!r}"
    )
    assert any(t in text for t in ("轨道交通", "站点", "微循环")), (
        f"brief_text 未保留交通接驳场景：{text!r}"
    )
