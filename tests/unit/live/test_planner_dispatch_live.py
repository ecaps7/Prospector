"""Live Planner tests — real LLM, skip when credentials missing.

Tests how the Planner decomposes a ResearchBrief into worker tasks.
"""

# ruff: noqa: E501 -- the fixture preserves a natural-language Brief verbatim.

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from prospector.agents.llm import LlmNotConfiguredError, require_llm_settings
from prospector.agents.planner import OpenAIPlannerModel, initial_planner_messages
from prospector.config import clear_settings_cache, get_settings
from prospector.deterministic.budget import ResearchLimits, limits_for_effort
from prospector.schemas.brief import ResearchBrief

pytestmark = pytest.mark.live

REPO_ROOT = Path(__file__).resolve().parents[2]


# ─────────────────────────────────────────────────────────────────────────────
# Brief — 来自 Scope 产出的 ResearchBrief（用户已填入实际研究问题）
# ─────────────────────────────────────────────────────────────────────────────
_BRIEF_TEXT = """\
超大城市的轨道交通骨干网络已相对完善，但站点与居民区、商业区之间的最后一公里接驳仍存在严重的效率瓶颈，表现为换乘时间长、步行体验差、非机动车与机动车混行、微循环公交空载率高、共享单车潮汐淤积等问题。用户希望了解不同城市如何实践解决这一问题，并在此基础上给出建议。

研究需要首先辨析几个核心概念：最后一公里微循环交通的边界（步行、非机动车、微型公交、需求响应式服务、共享出行各占什么位置）；效率瓶颈的衡量维度（时间、成本、舒适度、可达性、公平性）；以及不同城市形态（高密度老城区、新城开发区、郊区卫星城）对解决方案的约束条件。

在比较维度上，可探索的方向包括但不限于：

1. 基础设施导向路径：以步行友好改造、立体连廊、风雨连廊、站点一体化开发（TOD）为代表，假设物理空间的改善能根本性提升接驳效率。但需检验：高投入的基建改造是否真正改变了出行行为，还是仅服务了已有步行意愿的人群？

2. 运力补充导向路径：以微循环公交、社区巴士、需求响应式小巴（DRT）为代表，假设增加运力供给能填补骨干网络与目的地之间的空白。但需检验：固定线路的微循环公交在低密度区域是否陷入空载-减班-客流流失的恶性循环？DRT的算法调度在真实城市环境中是否达到理论效率？

3. 共享出行导向路径：以共享单车、共享电单车、共享滑板车为代表，假设市场化供给能灵活匹配潮汐需求。但需检验：共享单车的潮汐淤积问题是否说明市场自发调节存在系统性失灵？共享电单车的安全与路权争议如何影响其可持续性？

4. 制度与治理导向路径：以跨部门协调机制、票价一体化、数据开放平台为代表，假设瓶颈的根源不在物理空间或运力，而在治理碎片化。但需检验：票价一体化是否真正降低了出行成本，还是仅转移了财政补贴的负担？

5. 技术驱动导向路径：以MaaS（出行即服务）平台、实时信息整合、自动驾驶接驳车为代表，假设信息透明与技术升级能优化出行决策。但需检验：MaaS平台在多大程度上改变了用户的实际出行选择，而非仅仅提供信息展示？

在比较基准上，可选择的城市案例包括但不限于：东京（轨道+步行+自行车的高度整合）、新加坡（TOD+公交优先+严格的车牌管制）、哥本哈根（自行车基础设施的极致投入）、深圳（共享单车+电单车的爆发式增长与治理挑战）、波特兰（有轨电车+街区微循环）、以及中国内地的多个新一线城市（如成都、杭州、武汉）的差异化实践。

研究需要特别关注反例与失败案例：哪些城市投入大量资源但效果不彰？哪些看似创新的方案在实际运行中暴露了未预见的缺陷？哪些被广泛推崇的最佳实践在特定城市条件下失效？

此外，需考虑时间维度的影响：疫情后远程办公的普及是否改变了最后一公里的需求结构？老龄化趋势对步行友好与无障碍接驳提出了什么新要求？新能源与自动驾驶技术的成熟是否会颠覆现有的解决方案框架？

最终建议需要区分短期可操作措施与长期结构性变革，并明确不同建议适用的城市类型与前提条件。
"""

BRIEF = ResearchBrief(
    question="超大城市如何解决轨道交通站点与周边社区之间的最后一公里微循环交通效率瓶颈？不同城市的实践路径、成效与局限是什么？",
    brief_text=_BRIEF_TEXT,
    output_format="report_with_citations",
    language="zh",
    effort="deep",
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


def test_planner_dispatch_from_brief() -> None:
    """Planner 收到 Brief 后应拆分为若干 worker 任务并 dispatch。"""
    limits: ResearchLimits = limits_for_effort(BRIEF.effort)
    model = OpenAIPlannerModel()

    messages = initial_planner_messages(BRIEF, limits)

    _print_divider("Planner 输入")
    print(f"Brief question : {BRIEF.question}")
    print(f"Brief effort   : {BRIEF.effort}")
    print(f"Brief language : {BRIEF.language}")
    print(f"Brief 正文     :\n{BRIEF.brief_text}")
    print(f"\n运行时预算: 决策轮上限={limits.decision_round_limit}")
    for stage, budget in limits.stages.items():
        print(f"  {stage}: 并发={budget.max_concurrency}, 决策轮={budget.max_worker_rounds}")

    result = model.decide(messages)
    decision = result.decision

    _print_divider("Planner 决策")
    print(f"decision 类型: {decision.decision}")

    if decision.decision == "dispatch":
        dispatch = decision.dispatch
        assert dispatch is not None
        print(f"取舍理由 (reason):\n{dispatch.reason}")
        print(f"\n共派发 {len(dispatch.tasks)} 个 worker 任务:\n")
        for idx, task in enumerate(dispatch.tasks, start=1):
            print(f"--- Task {idx} ---")
            print(f"  subjects            : {task.subjects}")
            print(f"  research_stage      : {task.research_stage}")
            print(f"  research_mode       : {task.research_mode}")
            print(f"  question            : {task.question}")
            print(f"  expected_evidence   : {task.expected_evidence}")
            if task.source_policy.preferred_tiers:
                print(f"  source_policy.tiers : {task.source_policy.preferred_tiers}")
            print()

    elif decision.decision == "reflect":
        assert decision.reflect is not None
        print(f"Planner 选择反思，不派发任务:\n{decision.reflect.note}")

    elif decision.decision == "finish":
        assert decision.finish is not None
        print(f"Planner 选择结束研究:\n{decision.finish.reason}")

    # 打印完整 JSON 输出，方便调试
    _print_divider("完整 PlannerDecision JSON")
    print(json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, indent=2))
