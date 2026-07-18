"""Research Verifier schema, transport, and graph-control unit tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from prospector.agents.prompts.research_verifier import research_verifier_messages
from prospector.agents.research_verifier import (
    OpenAIResearchVerifier,
    VerifierModelResult,
    VerifierOutputError,
)
from prospector.deterministic.budget import limits_for_effort
from prospector.flow.research_graph import (
    ResearchGraphServices,
    VerifierMajorGapError,
    _verifier_node,
)
from prospector.flow.state import initial_research_state
from prospector.runtime.timeline import ResearchTimelineRenderer
from prospector.schemas.verifier import (
    AssertionDisposition,
    ConflictJudgement,
    VerifierDecision,
    VerifierLlmDecision,
    conflict_key,
    effective_unusable_assertion_ids,
    materialize_conflict_resolutions,
    materialize_verifier_decision,
    validate_verifier_references,
)

TASK_ID = UUID("10000000-0000-0000-0000-000000000001")
ASSERTION_ID = UUID("20000000-0000-0000-0000-000000000001")
ASSERTION_B = UUID("20000000-0000-0000-0000-000000000002")
EXCERPT_A = UUID("30000000-0000-0000-0000-000000000001")
EXCERPT_B = UUID("30000000-0000-0000-0000-000000000002")


def _gaps(
    *,
    severity: str = "minor",
    kind: str = "plan_coverage",
) -> list[dict[str, object]]:
    return [
        {
            "kind": kind,
            "severity": severity,
            "related_task_ids": [str(TASK_ID)],
            "related_assertion_ids": [str(ASSERTION_ID)],
            "related_excerpt_ids": [str(EXCERPT_A)],
            "description": "关键比较口径尚未核实",
            "attempted_paths": ["查找公开材料"],
            "why_insufficient": "现有证据无法支持核心比较",
            "recommended_research": "补查独立来源并统一口径",
        }
    ]


def _llm_decision(
    release: str = "pass",
    *,
    severity: str = "minor",
    kind: str = "plan_coverage",
    dispositions: list[dict[str, object]] | None = None,
) -> VerifierLlmDecision:
    return VerifierLlmDecision.model_validate(
        {
            "release_decision": release,
            "decision_reason": (
                "现有证据存在影响结论的重大缺口"
                if release == "needs_research"
                else "现有证据足以履行 Plan"
            ),
            "brief_alignment": "misaligned" if kind == "brief_alignment" else "aligned",
            "coverage_rationale": "已逐项对照 Plan。",
            "brief_alignment_rationale": "研究仍围绕用户问题。",
            "credibility_rationale": "关键结论具有直接来源。",
            "gaps": _gaps(severity=severity, kind=kind),
            "conflict_judgements": [],
            "assertion_dispositions": dispositions or [],
        }
    )


def _decision(
    release: str = "pass",
    *,
    severity: str = "minor",
    kind: str = "plan_coverage",
    dispositions: list[dict[str, object]] | None = None,
) -> VerifierDecision:
    return materialize_verifier_decision(
        _llm_decision(release, severity=severity, kind=kind, dispositions=dispositions),
        {},
    )


def _unusable_disposition(assertion_id: UUID = ASSERTION_ID) -> dict[str, object]:
    return {
        "assertion_id": str(assertion_id),
        "status": "unusable",
        "reason": "来源为伪学术 UGC，定量数字不可采信。",
    }

def test_decision_requires_major_gap_exactly_when_research_is_needed() -> None:
    with pytest.raises(ValidationError, match="pass must not contain major gaps"):
        _decision("pass", severity="major")
    with pytest.raises(ValidationError, match="needs_research requires"):
        VerifierLlmDecision.model_validate(
            {
                **_llm_decision().model_dump(mode="json"),
                "release_decision": "needs_research",
                "gaps": [],
            }
        )


def test_misalignment_requires_matching_major_gap() -> None:
    payload = _llm_decision().model_dump(mode="json")
    payload["brief_alignment"] = "misaligned"
    with pytest.raises(ValidationError, match="major brief_alignment gap"):
        VerifierLlmDecision.model_validate(payload)


def test_conflict_judgement_enforces_winner_contract() -> None:
    base = {
        "disputed_point": "市场规模口径",
        "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
        "rationale": "来源口径不同。",
    }
    payload = _llm_decision().model_dump(mode="json")
    payload["conflict_judgements"] = [
        {**base, "decision": "present_both", "winning_assertion_ids": [str(ASSERTION_ID)]}
    ]
    with pytest.raises(ValidationError, match="must not select winners"):
        VerifierLlmDecision.model_validate(payload)

    payload["conflict_judgements"] = [
        {**base, "decision": "adjudicated", "winning_assertion_ids": []}
    ]
    with pytest.raises(ValidationError, match="requires winning_assertion_ids"):
        VerifierLlmDecision.model_validate(payload)


def test_materialize_conflict_binds_assertion_excerpts() -> None:
    judgement = ConflictJudgement.model_validate(
        {
            "disputed_point": "市场规模口径",
            "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
            "decision": "adjudicated",
            "winning_assertion_ids": [str(ASSERTION_ID)],
            "rationale": "官方披露优先。",
        }
    )
    resolutions = materialize_conflict_resolutions(
        [judgement],
        {ASSERTION_ID: [EXCERPT_A], ASSERTION_B: [EXCERPT_B]},
    )
    assert len(resolutions) == 1
    assert resolutions[0].excerpt_ids == [EXCERPT_A, EXCERPT_B]
    assert resolutions[0].winning_excerpt_ids == [EXCERPT_A]
    assert resolutions[0].decision == "adjudicated"


def test_materialize_conflict_rejects_unknown_assertion() -> None:
    judgement = ConflictJudgement.model_validate(
        {
            "disputed_point": "口径",
            "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
            "decision": "present_both",
            "winning_assertion_ids": [],
            "rationale": "并陈。",
        }
    )
    with pytest.raises(ValueError, match="assertion outside"):
        materialize_conflict_resolutions([judgement], {ASSERTION_ID: [EXCERPT_A]})


def test_materialize_conflict_rejects_fewer_than_two_excerpts() -> None:
    judgement = ConflictJudgement.model_validate(
        {
            "disputed_point": "口径",
            "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
            "decision": "present_both",
            "winning_assertion_ids": [],
            "rationale": "并陈。",
        }
    )
    with pytest.raises(ValueError, match="two distinct excerpts"):
        materialize_conflict_resolutions(
            [judgement],
            {ASSERTION_ID: [EXCERPT_A], ASSERTION_B: [EXCERPT_A]},
        )


def test_reference_validation_rejects_ids_outside_job() -> None:
    decision = _decision("needs_research", severity="major")
    with pytest.raises(ValueError, match="task outside"):
        validate_verifier_references(
            decision,
            task_ids=set(),
            assertion_ids={ASSERTION_ID},
            excerpt_ids={EXCERPT_A},
        )


def test_source_credibility_gap_requires_unusable_dispositions() -> None:
    with pytest.raises(ValidationError, match="must be marked unusable"):
        _llm_decision(
            "needs_research",
            severity="major",
            kind="source_credibility",
            dispositions=[],
        )


def test_source_credibility_gap_rejects_excerpt_only_pointers() -> None:
    payload = _llm_decision(
        "needs_research",
        severity="major",
        dispositions=[_unusable_disposition()],
    ).model_dump(mode="json")
    payload["gaps"] = [
        {
            **_gaps(severity="major", kind="source_credibility")[0],
            "related_assertion_ids": [],
            "related_excerpt_ids": [str(EXCERPT_A)],
        }
    ]
    with pytest.raises(ValidationError, match="related_assertion_ids"):
        VerifierLlmDecision.model_validate(payload)


def test_pass_may_include_unusable_dispositions_without_major_gap() -> None:
    decision = _decision(
        "pass",
        dispositions=[_unusable_disposition()],
    )
    assert decision.release_decision == "pass"
    assert decision.assertion_dispositions[0].status == "unusable"


def test_duplicate_disposition_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate assertion disposition"):
        _llm_decision(
            "pass",
            dispositions=[_unusable_disposition(), _unusable_disposition()],
        )


def test_effective_unusable_last_write_wins() -> None:
    unusable = AssertionDisposition(
        assertion_id=ASSERTION_ID,
        status="unusable",
        reason="伪学术",
    )
    restored = AssertionDisposition(
        assertion_id=ASSERTION_ID,
        status="restored",
        reason="误判纠正",
    )
    assert effective_unusable_assertion_ids([(1, [unusable])]) == {ASSERTION_ID}
    assert effective_unusable_assertion_ids([(1, [unusable]), (2, [restored])]) == set()


def test_reference_validation_rejects_unknown_disposition_assertion() -> None:
    decision = VerifierDecision.model_validate(
        {
            **_decision("pass", dispositions=[]).model_dump(mode="json"),
            "gaps": [],
            "assertion_dispositions": [_unusable_disposition()],
        }
    )
    with pytest.raises(ValueError, match="Assertion disposition references"):
        validate_verifier_references(
            decision,
            task_ids={TASK_ID},
            assertion_ids=set(),
            excerpt_ids={EXCERPT_A},
        )


def test_conflict_key_is_order_independent() -> None:
    assert conflict_key([EXCERPT_A, EXCERPT_B]) == conflict_key([EXCERPT_B, EXCERPT_A])


def test_prompt_uses_source_metadata_without_tier_field_or_full_document() -> None:
    snapshot = {
        "brief": {"question": "问题"},
        "plans": [],
        "tasks": [],
        "planner_exit": {"trigger": "planner_finish"},
        "assertions": [],
        "excerpts": [
            {
                "excerpt_id": str(EXCERPT_A),
                "url": "https://example.com/source",
                "title": "标题",
                "author": "作者",
                "published_at": "2026-07-01",
                "text": "原文片段",
            }
        ],
    }
    messages = research_verifier_messages(snapshot)
    prompt = "\n".join(message["content"] for message in messages)
    schema = json.dumps(VerifierLlmDecision.model_json_schema(), ensure_ascii=False)

    assert all(field in prompt for field in ("url", "title", "author"))
    assert '"publisher"' not in prompt
    assert '"tier"' not in prompt
    assert "没有来源 tier 分类机制" in prompt
    assert "decision_reason 必须用一句极短中文" in prompt
    assert "document_text" not in prompt
    assert "worker_trace" not in prompt
    assert "conflict_judgements" in prompt
    assert "只引用参与冲突的 assertion_id" in prompt
    assert "禁止在冲突字段填写 excerpt_id" in prompt
    assert "assertion_dispositions" in prompt
    assert "effective_unusable_assertion_ids" in prompt
    assert "status=unusable" in prompt
    assert '"conflict_resolutions"' not in schema
    conflict_def = VerifierLlmDecision.model_json_schema().get("$defs", {}).get(
        "ConflictJudgement", {}
    )
    conflict_props = conflict_def.get("properties", {})
    assert "assertion_ids" in conflict_props
    assert "winning_assertion_ids" in conflict_props
    assert "excerpt_ids" not in conflict_props
    assert "winning_excerpt_ids" not in conflict_props
    disposition_def = VerifierLlmDecision.model_json_schema().get("$defs", {}).get(
        "AssertionDisposition", {}
    )
    disposition_props = disposition_def.get("properties", {})
    assert "assertion_id" in disposition_props
    assert "excerpt_id" not in disposition_props
    assert "excerpt_ids" not in disposition_props


class _FakeCompletions:
    def __init__(self, streamed: str, repaired: str | None = None) -> None:
        self.streamed = streamed
        self.repaired = repaired
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> object:
        self.requests.append(kwargs)
        if kwargs.get("stream"):
            delta = SimpleNamespace(content=self.streamed)
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta)])])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.repaired or ""))]
        )


def test_verifier_uses_strong_thinking_and_one_structural_repair() -> None:
    valid = json.dumps(_llm_decision().model_dump(mode="json"), ensure_ascii=False)
    completions = _FakeCompletions("判断如下：" + valid, repaired=valid)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIResearchVerifier(client=cast(Any, client), model="strong", repair_model="mid")

    result = verifier.verify(
        {
            "brief": {},
            "plans": [],
            "tasks": [],
            "assertions": [],
            "excerpts": [],
        }
    )

    assert result.decision.release_decision == "pass"
    assert result.decision.conflict_resolutions == []
    first, second = completions.requests
    assert first["model"] == "strong" and first["stream"] is True
    assert first["extra_body"] == {"enable_thinking": True}
    assert second["model"] == "mid"
    assert second["response_format"] == {"type": "json_object"}
    assert second["extra_body"] == {"enable_thinking": False}


def test_verifier_binds_conflict_judgements_to_excerpt_ids() -> None:
    llm = _llm_decision().model_dump(mode="json")
    llm["conflict_judgements"] = [
        {
            "disputed_point": "市场规模口径",
            "assertion_ids": [str(ASSERTION_ID), str(ASSERTION_B)],
            "decision": "present_both",
            "winning_assertion_ids": [],
            "rationale": "口径不同，并陈。",
        }
    ]
    content = json.dumps(llm, ensure_ascii=False)
    completions = _FakeCompletions(content)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIResearchVerifier(client=cast(Any, client), model="strong", repair_model="mid")

    result = verifier.verify(
        {
            "assertions": [
                {"assertion_id": str(ASSERTION_ID), "excerpt_ids": [str(EXCERPT_A)]},
                {"assertion_id": str(ASSERTION_B), "excerpt_ids": [str(EXCERPT_B)]},
            ]
        }
    )

    assert len(result.decision.conflict_resolutions) == 1
    resolution = result.decision.conflict_resolutions[0]
    assert resolution.excerpt_ids == [EXCERPT_A, EXCERPT_B]
    assert resolution.winning_excerpt_ids == []
    assert resolution.decision == "present_both"


def test_verifier_raises_when_structural_repair_is_still_invalid() -> None:
    completions = _FakeCompletions("not json", repaired='{"release_decision":"pass"}')
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = OpenAIResearchVerifier(client=cast(Any, client), model="strong", repair_model="mid")

    with pytest.raises(VerifierOutputError, match="repair failed"):
        verifier.verify({})


class _FakeRepository:
    def __init__(self, stored: VerifierDecision | None = None) -> None:
        self.run_id = uuid4()
        self.stored = stored
        self.completed: VerifierDecision | None = None
        self.outcomes: list[dict[str, object]] = []
        self.begin_count = 0

    def get_completed_verifier_run(self, job_id: UUID, plan_version: int) -> object:
        del job_id, plan_version
        if self.stored is None:
            return None
        return {"run_id": self.run_id, "decision": self.stored}

    def build_verifier_snapshot(self, job_id: UUID, **kwargs: object) -> dict[str, object]:
        del job_id, kwargs
        return {"brief": {}, "plans": [{"version": 1}], "tasks": [], "excerpts": []}

    def begin_verifier_run(self, job_id: UUID, **kwargs: object) -> UUID:
        del job_id, kwargs
        self.begin_count += 1
        return self.run_id

    def complete_verifier_run(self, job_id: UUID, run_id: UUID, **kwargs: object) -> None:
        del job_id, run_id
        self.completed = cast(VerifierDecision, kwargs["decision"])

    def record_phase_changed(self, job_id: UUID, phase: str, **kwargs: object) -> None:
        del job_id, phase, kwargs

    def set_research_outcome(self, job_id: UUID, **kwargs: object) -> None:
        del job_id
        self.outcomes.append(kwargs)

    def list_unusable_assertion_details(
        self, job_id: UUID, dispositions: object
    ) -> list[dict[str, str]]:
        del job_id
        items = cast(list[Any], dispositions or [])
        return [
            {
                "assertion_id": str(item.assertion_id),
                "statement": "伪学术定量断言",
                "reason": item.reason.splitlines()[0],
            }
            for item in items
            if getattr(item, "status", None) == "unusable"
        ]


class _FakeVerifier:
    def __init__(self, decision: VerifierDecision) -> None:
        self.decision = decision
        self.calls = 0

    def verify(self, snapshot: dict[str, Any]) -> VerifierModelResult:
        self.calls += 1
        return VerifierModelResult(
            full_prompt=research_verifier_messages(snapshot),
            raw_output={"decision": self.decision.model_dump(mode="json")},
            decision=self.decision,
        )


def _state(*, decision_round: int = 3, limit: int = 8) -> dict[str, Any]:
    state = cast(dict[str, Any], initial_research_state(job_id=str(uuid4()), brief_id=str(uuid4())))
    state.update(
        {
            "plan_version": 1,
            "decision_round": decision_round,
            "decision_round_limit": limit,
            "verifier_trigger": "planner_finish",
            "planner_messages": [],
        }
    )
    return state


def _services(repository: _FakeRepository, verifier: _FakeVerifier) -> ResearchGraphServices:
    return ResearchGraphServices(
        repository=cast(Any, repository),
        planner=cast(Any, object()),
        worker=cast(Any, object()),
        verifier=cast(Any, verifier),
    )


def test_verifier_node_passes_to_composition_pending() -> None:
    repository = _FakeRepository()
    verifier = _FakeVerifier(_decision())

    result = _verifier_node(_services(repository, verifier))(cast(Any, _state()))

    assert result["phase"] == "composition_pending"
    assert result["outcome"] == "ready_for_writer"
    assert result["route"] == "writer"
    assert repository.completed is not None
    assert repository.outcomes[-1]["phase"] == "composition_pending"


def test_verifier_node_logs_decision_reason_after_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _FakeRepository()
    decision = _decision()
    calls: list[tuple[str, dict[str, object]]] = []

    def capture(event: str, **fields: object) -> None:
        assert repository.completed == decision
        calls.append((event, fields))

    monkeypatch.setattr(
        "prospector.flow.research_graph.log",
        SimpleNamespace(debug=capture, info=lambda *_a, **_k: None),
    )

    _verifier_node(_services(repository, _FakeVerifier(decision)))(cast(Any, _state()))

    assert calls[0][0] == "verifier.completed"
    assert calls[0][1]["message"] == decision.decision_reason
    assert calls[0][1]["outcome"] == "pass"
    assert calls[0][1]["reason_code"] == "pass"


def test_verifier_node_replans_when_major_gap_has_rounds_left() -> None:
    repository = _FakeRepository()
    verifier = _FakeVerifier(
        _decision(
            "needs_research",
            severity="major",
            kind="source_credibility",
            dispositions=[_unusable_disposition()],
        )
    )

    result = _verifier_node(_services(repository, verifier))(cast(Any, _state()))

    assert result["route"] == "planner"
    assert result["last_verifier_run_id"] == str(repository.run_id)
    content = result["planner_messages"][0]["content"]
    assert '"runtime_feedback": "verifier_gap"' in content
    assert "unusable_assertions" in content
    assert str(ASSERTION_ID) in content
    assert repository.outcomes == []


def test_verifier_node_fails_when_major_gap_has_no_rounds_left() -> None:
    repository = _FakeRepository()
    verifier = _FakeVerifier(_decision("needs_research", severity="major"))

    with pytest.raises(VerifierMajorGapError, match="关键比较口径"):
        _verifier_node(_services(repository, verifier))(
            cast(Any, _state(decision_round=8, limit=8))
        )

    assert repository.outcomes[-1] == {
        "outcome": "failed",
        "error_code": "verifier_major_gap",
        "phase": "failed",
    }


def test_verifier_node_reuses_completed_run_without_model_call() -> None:
    stored = _decision()
    repository = _FakeRepository(stored=stored)
    verifier = _FakeVerifier(stored)

    result = _verifier_node(_services(repository, verifier))(cast(Any, _state()))

    assert result["phase"] == "composition_pending"
    assert verifier.calls == 0
    assert repository.begin_count == 0


def test_timeline_renders_verifier_and_replan_events() -> None:
    renderer = ResearchTimelineRenderer(cast(Any, object()), limits_for_effort("quick"))

    assert renderer.render(
        {
            "event_type": "job.phase_changed",
            "payload": {
                "phase": "verifier",
                "plan_version": 1,
                "trigger": "planner_finish",
            },
        }
    ) == ["[研究] 研究阶段结束，等待核验（Plan v1，触发：Planner finish）"]

    assert renderer.render(
        {
            "event_type": "verifier.completed",
            "decision_round": 3,
            "payload": {
                "plan_version": 1,
                "release_decision": "needs_research",
                "decision_reason": "关键比较口径仍缺证据",
                "major_gap_count": 2,
                "minor_gap_count": 0,
                "conflict_resolution_count": 0,
                "gap_summaries": [
                    {
                        "severity": "major",
                        "kind": "plan_coverage",
                        "description": "关键比较口径尚未核实",
                        "recommended_research": "补查独立来源并统一口径",
                    },
                    {
                        "severity": "major",
                        "kind": "brief_alignment",
                        "description": "Brief 要求的反例仍缺",
                        "recommended_research": "",
                    },
                ],
            },
        }
    ) == [
        "[核验] Plan v1 不通过：2 个重大缺口（次要 0，冲突 0，废证 0）",
        "  ├─ 重大·覆盖：关键比较口径尚未核实",
        "  │     建议：补查独立来源并统一口径",
        "  └─ 重大·Brief对齐：Brief 要求的反例仍缺",
        "[核验] 返回 Planner 补查（余 5 轮）：关键比较口径仍缺证据",
    ]
    assert renderer.render(
        {
            "event_type": "replan.triggered",
            "payload": {"verifier_run_id": "12345678-abcd", "plan_version": 2},
        }
    ) == ["[重规划] Verifier 12345678 触发 Plan v2"]
    assert renderer.render(
        {
            "event_type": "verifier.completed",
            "decision_round": 4,
            "payload": {
                "plan_version": 2,
                "release_decision": "pass",
                "decision_reason": "Plan 承诺已有充分证据",
                "major_gap_count": 0,
                "minor_gap_count": 0,
                "conflict_resolution_count": 0,
                "unusable_assertion_count": 1,
                "unusable_summaries": [
                    {
                        "assertion_id": "20000000-0000-0000-0000-000000000001",
                        "reason": "伪学术来源不可采信",
                    }
                ],
            },
        }
    ) == [
        "[核验] Plan v2 通过（重大缺口 0，冲突裁决 0，废证 1）",
        "[核验] 收工：Plan 承诺已有充分证据",
        "[核验] 废证 1 条：",
        "  └─ 20000000：伪学术来源不可采信",
    ]
    assert renderer.render(
        {
            "event_type": "job.phase_changed",
            "payload": {
                "phase": "composition_pending",
                "outcome": "ready_for_writer",
                "error_code": None,
            },
        }
    ) == ["[成文] Research Verifier 已放行，等待 Writer"]
