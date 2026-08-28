"""Report Writer contracts, wire-format assembly, and deterministic rendering."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from prospector.agents import streaming
from prospector.agents.prompts.report_writer import (
    continuation_message,
    report_writer_messages,
    report_writer_revision_messages,
    retry_message,
)
from prospector.agents.report_writer import OpenAIReportWriter, ReportWriterOutputError
from prospector.deterministic.citation_render import (
    UNVERIFIED_MARKER,
    render_verified_report,
)
from prospector.deterministic.excerpt_text import CLIP_MARKER, writer_excerpt_limit
from prospector.deterministic.statement_patches import (
    StatementPatchError,
    apply_report_patches,
)
from prospector.reporting.render import render_report_draft
from prospector.schemas.claims import (
    ReportQualityReminder,
    ReportRequirementFailure,
    ReportVerifierFindings,
    StatementFailure,
)
from prospector.schemas.report import (
    ReportDraft,
    ReportParagraph,
    ReportStatement,
    WriterSnapshot,
    validate_writer_draft,
)
from prospector.schemas.report_stream import ReportStreamAssembler

EXCERPT_ID = UUID("10000000-0000-0000-0000-000000000001")
OTHER_EXCERPT_ID = UUID("10000000-0000-0000-0000-000000000002")
TASK_ID = UUID("50000000-0000-0000-0000-000000000001")
EXCERPT_BODY = "该口径下的年度数值为 42，统计区间为 2026 全年，由发布方自行披露。"


def _snapshot(effort: str = "quick") -> WriterSnapshot:
    return WriterSnapshot.model_validate(
        {
            "job_id": "20000000-0000-0000-0000-000000000001",
            "brief": {
                "question": "测试深度研究报告",
                "brief_text": "比较竞争解释、反例和适用边界。",
                "output_format": "report_with_citations",
                "language": "zh",
                "effort": effort,
            },
            "final_plan_summary": [
                {
                    "version": 1,
                    "tasks": [
                        {
                            "id": str(TASK_ID),
                            "question": "这条线索要回答的研究问题。",
                            "expected_evidence": "一条带口径的直接证据。",
                            "stop_reason": "expected_evidence_satisfied",
                        }
                    ],
                }
            ],
            "evidence_cards": [
                {
                    "assertion_id": "30000000-0000-0000-0000-000000000001",
                    "task_id": str(TASK_ID),
                    "assertion_statement": "公开材料记录了一个带时间口径的事实。",
                    "excerpts": [
                        {
                            "excerpt_id": str(EXCERPT_ID),
                            "text": EXCERPT_BODY,
                            "source": {
                                "title": "公开报告",
                                "author": "Example Publisher",
                                "published_at": "2026-07-01",
                                "source_uri": "https://example.test/report",
                                "document_version": 1,
                            },
                        }
                    ],
                }
            ],
            "conflicts": [],
            "minor_gaps": [],
        }
    )


def _material(snapshot: WriterSnapshot) -> Any:
    """The frozen material block the Writer is handed, parsed back out of the prompt."""
    user = report_writer_messages(snapshot)[1]["content"]
    return json.loads(user.split("研究材料：\n", 1)[1])


def _intro_payload() -> list[dict[str, object]]:
    return [
        {
            "paragraph_id": "p_intro",
            "statements": [
                {
                    "statement_id": "s_intro",
                    "text": "核心答案",
                    "kind": "elaboration",
                    "candidate_excerpt_ids": [],
                    "premise_statement_ids": [],
                }
            ],
        }
    ]


def _draft() -> ReportDraft:
    return ReportDraft.model_validate(
        {
            "title": "深度研究报告",
            "introduction": _intro_payload(),
            "sections": [
                {
                    "section_id": "sec_answer",
                    "title": "核心判断",
                    "paragraphs": [
                        {
                            "paragraph_id": "p_answer",
                            "statements": [
                                {
                                    "statement_id": "s_fact",
                                    "text": "公开材料记录了一个事实。",
                                    "kind": "evidence",
                                    "candidate_excerpt_ids": [str(EXCERPT_ID)],
                                    "premise_statement_ids": [],
                                },
                                {
                                    "statement_id": "s_analysis",
                                    "text": "这一事实需要结合适用边界理解。",
                                    "kind": "derived",
                                    "candidate_excerpt_ids": [],
                                    "premise_statement_ids": ["s_fact"],
                                },
                            ],
                        }
                    ],
                }
            ],
            "conclusion": [
                {
                    "paragraph_id": "p_conclusion",
                    "statements": [
                        {
                            "statement_id": "s_conclusion_1",
                            "text": "总结",
                            "kind": "derived",
                            "candidate_excerpt_ids": [],
                            "premise_statement_ids": ["s_analysis"],
                        },
                        {
                            "statement_id": "s_conclusion_2",
                            "text": "收束",
                            "kind": "derived",
                            "candidate_excerpt_ids": [],
                            "premise_statement_ids": ["s_fact"],
                        },
                    ],
                }
            ],
        }
    )


def _line(**payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _first_turn_lines() -> list[str]:
    return [
        _line(record="title", text="深度研究报告"),
        _line(record="introduction"),
        _line(record="paragraph"),
        _line(
            record="statement",
            statement_id="s_intro",
            text="核心答案",
            kind="elaboration",
        ),
        _line(record="section", title="核心判断"),
        _line(record="paragraph"),
        _line(
            record="statement",
            statement_id="s_fact",
            text="公开材料记录了一个事实。",
            kind="evidence",
            candidate_excerpt_ids=["e_01"],
        ),
    ]


def _second_turn_lines() -> list[str]:
    return [
        _line(
            record="statement",
            statement_id="s_analysis",
            text="这一事实需要结合适用边界理解。",
            kind="derived",
            premise_statement_ids=["s_fact"],
        ),
        _line(record="conclusion"),
        _line(record="paragraph"),
        _line(
            record="statement",
            statement_id="s_conclusion",
            text="总结全文判断与边界。",
            kind="derived",
            premise_statement_ids=["s_analysis"],
        ),
        _line(record="end"),
    ]


def _dropped_stream(error: Exception) -> Iterator[SimpleNamespace]:
    """A stream that opens, delivers a fragment, and then dies."""
    yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="半句"))])
    raise error


class _FakeStreamClient:
    """Minimal chat.completions.create stub yielding one scripted turn per call.

    A turn given as an exception stands for a connection lost mid-answer.
    """

    def __init__(self, turns: list[str | Exception]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, object]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: object) -> object:
        messages = kwargs.get("messages")
        if isinstance(messages, list):
            kwargs["messages"] = [dict(message) for message in messages]
        self.calls.append(kwargs)
        text = self.turns.pop(0)
        if isinstance(text, Exception):
            return _dropped_stream(text)
        chunk = SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))])
        return iter([chunk])

    def last_message(self, call_index: int) -> dict[str, str]:
        """Last chat message of a recorded call, typed for the assertions below."""
        messages = self.calls[call_index]["messages"]
        assert isinstance(messages, list)
        return cast(dict[str, str], messages[-1])


def test_writer_prompt_is_stream_contract_and_carries_excerpt_text() -> None:
    """The Writer reads the passage, not just the Assertion's one-line compression.

    With only one-liners the single thing it can do is transcribe them one per sentence,
    which is how a research report degenerates into a chronicle. Document body text is
    still out of reach (D12); the Excerpt a citation resolves to is not.
    """
    messages = report_writer_messages(_snapshot())
    prompt = "\n".join(message["content"] for message in messages)

    assert "每行只输出 1 个完整、合法的 JSON 对象" in prompt
    assert '"record":"conclusion"' in prompt
    assert "不得编造材料中没有的内容" in prompt
    assert "此前已输出的 statement_id" in prompt
    assert "推理链最多两层" not in prompt
    assert "可以很长、很具体" not in prompt
    assert "报告完成后才输出 `end`" in prompt
    assert "必须直接回答 `brief.question`" in prompt
    assert "遵守 `brief.user_constraints`" in prompt
    assert "narrative_plan" not in prompt
    assert '"excerpt_id"' in prompt
    assert EXCERPT_BODY in messages[1]["content"]
    # Excerpt ids are shown as short aliases, never raw UUIDs the model could corrupt.
    assert '"e_01"' in messages[1]["content"]
    assert str(EXCERPT_ID) not in messages[1]["content"]


def test_writer_prompt_examples_are_single_line_json_without_empty_reference_fields() -> None:
    system = report_writer_messages(_snapshot())[0]["content"]
    example_lines = [line.strip() for line in system.splitlines() if line.strip().startswith("{")]

    assert example_lines
    assert all("#" not in line for line in example_lines)
    assert all(isinstance(json.loads(line), dict) for line in example_lines)
    assert '"candidate_excerpt_ids":[]' not in system
    assert '"premise_statement_ids":[]' not in system
    assert "`paragraph` 表示开始一个新段落" in system
    assert "kind 只说明这句话应该如何核验" in system
    assert "不要为了区分事实与判断" in system
    assert "分析、概括、比较、解释或判断" in system


def test_writer_material_groups_findings_by_research_question() -> None:
    """Structure to build on, instead of one flat pile ordered by the calendar."""
    material = _material(_snapshot())

    groups = material["research_groups"]
    assert [group["research_question"] for group in groups] == ["这条线索要回答的研究问题。"]
    assert groups[0]["findings"] == [
        {
            "assertion_id": "30000000-0000-0000-0000-000000000001",
            "statement": "公开材料记录了一个带时间口径的事实。",
            "excerpt_ids": ["e_01"],
        }
    ]
    # The passage lives once in the library the findings point into, not inline per card.
    assert [item["excerpt_id"] for item in material["excerpt_library"]] == ["e_01"]
    assert material["excerpt_library"][0]["text"] == EXCERPT_BODY
    assert "text" not in groups[0]["findings"][0]


def test_writer_receives_user_constraints_as_binding_brief_fields() -> None:
    payload = _snapshot().model_dump(mode="json")
    payload["brief"]["user_constraints"] = {
        "time_range": "2024 至 2026 年",
        "regions": ["中国"],
        "comparison_targets": [],
        "source_rules": ["只使用公开来源"],
        "exclusions": ["不讨论投资建议"],
        "deliverable_rules": ["结论先行"],
    }

    material = _material(WriterSnapshot.model_validate(payload))

    assert material["brief"]["user_constraints"] == payload["brief"]["user_constraints"]


def test_writer_material_renders_a_shared_excerpt_once() -> None:
    """Assertions outnumber Excerpts and share them; paying per card would multiply cost."""
    payload = _snapshot().model_dump(mode="json")
    second = json.loads(json.dumps(payload["evidence_cards"][0]))
    second["assertion_id"] = "30000000-0000-0000-0000-000000000009"
    second["assertion_statement"] = "同一段原文支撑的另一条结论。"
    payload["evidence_cards"].append(second)

    material = _material(WriterSnapshot.model_validate(payload))

    assert len(material["research_groups"][0]["findings"]) == 2
    assert len(material["excerpt_library"]) == 1


def test_writer_material_clips_a_long_excerpt_without_rewriting_it() -> None:
    """Clipping is a marked middle cut, so the passage stays recognizable and auditable."""
    payload = _snapshot().model_dump(mode="json")
    body = "开头的关键结论。" + "中段的铺垫内容。" * 500 + "结尾的关键口径。"
    payload["evidence_cards"][0]["excerpts"][0]["text"] = body

    material = _material(WriterSnapshot.model_validate(payload))

    text = material["excerpt_library"][0]["text"]
    assert len(text) <= writer_excerpt_limit(1)
    assert text.startswith("开头的关键结论。")
    assert text.endswith("结尾的关键口径。")
    assert CLIP_MARKER in text


def test_writer_prompt_has_no_writing_method_or_worked_domain() -> None:
    """Length quotas made the model pad, and padding is where unsourced content enters.

    The old prompt also carried a fully worked example from one subject area, which
    framed every unrelated question in that example's terms.
    """
    prompt = "\n".join(message["content"] for message in report_writer_messages(_snapshot()))

    for length_mandate in ("万字", "300-500", "10-15", "至少 3 条"):
        assert length_mandate not in prompt
    for leaked_domain in ("短视频", "青少年", "留守儿童", "海马体", "多巴胺"):
        assert leaked_domain not in prompt
    for writing_method in (
        "每段先立论点",
        "一条 finding 不等于一句正文",
        "章节标题必须",
        "归纳是本职工作",
        "引言同样先写 evidence",
        "长度应当是研究深度的结果",
    ):
        assert writing_method not in prompt


def test_writer_prompt_aliases_conflict_excerpt_ids() -> None:
    snapshot_payload = _snapshot().model_dump(mode="json")
    snapshot_payload["evidence_cards"].append(
        {
            "assertion_id": "30000000-0000-0000-0000-000000000002",
            "task_id": str(TASK_ID),
            "assertion_statement": "另一份材料记录了不同条件下的事实。",
            "excerpts": [
                {
                    "excerpt_id": str(OTHER_EXCERPT_ID),
                    "text": "另一口径下的年度数值为 37。",
                    "source": {
                        "title": "另一份公开报告",
                        "author": "Example Publisher",
                        "published_at": "2026-07-02",
                        "source_uri": "https://example.test/other-report",
                        "document_version": 1,
                    },
                }
            ],
        }
    )
    snapshot_payload["conflicts"] = [
        {
            "conflict_key": "sha256:test",
            "disputed_point": "不同条件下的结果是否一致？",
            "excerpt_ids": [str(EXCERPT_ID), str(OTHER_EXCERPT_ID)],
            "decision": "adjudicated",
            "winning_excerpt_ids": [str(OTHER_EXCERPT_ID)],
            "rationale": "以更新的口径为准。",
        }
    ]

    messages = report_writer_messages(WriterSnapshot.model_validate(snapshot_payload))
    material = messages[1]["content"]

    assert '"excerpt_ids": ["e_01", "e_02"]' in material
    assert '"winning_excerpt_ids": ["e_02"]' in material
    assert str(EXCERPT_ID) not in material
    assert str(OTHER_EXCERPT_ID) not in material


@pytest.mark.parametrize("effort", ["quick", "standard", "deep"])
def test_short_report_is_not_rejected_by_effort(effort: str) -> None:
    validate_writer_draft(_snapshot(effort), _draft())


def test_derived_statement_can_only_reference_earlier_statements() -> None:
    with pytest.raises(ValidationError, match="earlier statements"):
        ReportDraft.model_validate(
            {
                "title": "非法草稿",
                "introduction": _intro_payload(),
                "sections": [
                    {
                        "section_id": "sec_invalid",
                        "title": "非法章节",
                        "paragraphs": [
                            {
                                "paragraph_id": "p_invalid",
                                "statements": [
                                    {
                                        "statement_id": "s_analysis",
                                        "text": "先写结论。",
                                        "kind": "derived",
                                        "candidate_excerpt_ids": [],
                                        "premise_statement_ids": ["s_future"],
                                    },
                                    {
                                        "statement_id": "s_future",
                                        "text": "后写事实。",
                                        "kind": "evidence",
                                        "candidate_excerpt_ids": [str(EXCERPT_ID)],
                                        "premise_statement_ids": [],
                                    },
                                ],
                            }
                        ],
                    }
                ],
                "conclusion": [
                    {
                        "paragraph_id": "p_conclusion",
                        "statements": [
                            {
                                "statement_id": "s_conclusion_1",
                                "text": "总结已有材料。",
                                "kind": "derived",
                                "candidate_excerpt_ids": [],
                                "premise_statement_ids": ["s_future"],
                            },
                            {
                                "statement_id": "s_conclusion_2",
                                "text": "收束全文。",
                                "kind": "derived",
                                "candidate_excerpt_ids": [],
                                "premise_statement_ids": ["s_conclusion_1"],
                            },
                        ],
                    }
                ],
            }
        )


def _draft_payload_with_section_statements(
    statements: list[dict[str, object]],
) -> dict[str, object]:
    payload = _draft().model_dump(mode="json")
    payload["sections"][0]["paragraphs"][0]["statements"] = statements
    for statement in payload["conclusion"][0]["statements"]:
        statement["premise_statement_ids"] = [str(statements[0]["statement_id"])]
        statement["kind"] = "derived"
    return payload


def test_writer_accepts_a_derived_statement_with_an_ungrounded_premise() -> None:
    """Grounding is a Report Verifier judgement, not a Writer rejection."""
    draft = ReportDraft.model_validate(
        _draft_payload_with_section_statements(
            [
                {
                    "statement_id": "s_bridge",
                    "text": "一段不带出处的展开。",
                    "kind": "elaboration",
                    "candidate_excerpt_ids": [],
                    "premise_statement_ids": [],
                },
                {
                    "statement_id": "s_on_bridge",
                    "text": "在没有出处的句子上继续推理。",
                    "kind": "derived",
                    "candidate_excerpt_ids": [],
                    "premise_statement_ids": ["s_bridge"],
                },
            ]
        )
    )

    assert (
        next(
            statement.text
            for statement in draft.statements()
            if statement.statement_id == "s_on_bridge"
        )
        == "在没有出处的句子上继续推理。"
    )


def test_writer_accepts_derived_with_excerpt_or_hybrid_grounding() -> None:
    excerpt_only = ReportStatement(
        statement_id="s_excerpt_only",
        text="直接综合原文得出判断。",
        kind="derived",
        candidate_excerpt_ids=[EXCERPT_ID],
    )
    hybrid = ReportStatement(
        statement_id="s_hybrid",
        text="结合原文和前提得出判断。",
        kind="derived",
        candidate_excerpt_ids=[EXCERPT_ID],
        premise_statement_ids=["s_fact"],
    )

    assert excerpt_only.candidate_excerpt_ids == [EXCERPT_ID]
    assert excerpt_only.premise_statement_ids == []
    assert hybrid.candidate_excerpt_ids == [EXCERPT_ID]
    assert hybrid.premise_statement_ids == ["s_fact"]


def test_writer_rejects_derived_without_any_grounding() -> None:
    with pytest.raises(ValidationError, match="candidate_excerpt_ids or premise_statement_ids"):
        ReportStatement(
            statement_id="s_ungrounded",
            text="没有任何依据的判断。",
            kind="derived",
        )


def test_writer_accepts_a_reasoning_chain_deeper_than_two_steps() -> None:
    draft = ReportDraft.model_validate(
        _draft_payload_with_section_statements(
            [
                {
                    "statement_id": "s_root",
                    "text": "材料记录的事实。",
                    "kind": "evidence",
                    "candidate_excerpt_ids": [str(EXCERPT_ID)],
                    "premise_statement_ids": [],
                },
                {
                    "statement_id": "s_step_1",
                    "text": "第一层推理。",
                    "kind": "derived",
                    "candidate_excerpt_ids": [],
                    "premise_statement_ids": ["s_root"],
                },
                {
                    "statement_id": "s_step_2",
                    "text": "第二层推理。",
                    "kind": "derived",
                    "candidate_excerpt_ids": [],
                    "premise_statement_ids": ["s_step_1"],
                },
                {
                    "statement_id": "s_step_3",
                    "text": "第三层推理，已经离材料太远。",
                    "kind": "derived",
                    "candidate_excerpt_ids": [],
                    "premise_statement_ids": ["s_step_2"],
                },
            ]
        )
    )

    assert (
        next(
            statement.text
            for statement in draft.statements()
            if statement.statement_id == "s_step_3"
        )
        == "第三层推理，已经离材料太远。"
    )


def test_assembler_accepts_an_ungrounded_chain_for_report_verifier() -> None:
    assembler = ReportStreamAssembler(_snapshot())
    assembler.consume("\n".join(_first_turn_lines()))

    outcome = assembler.consume(
        _line(
            record="statement",
            statement_id="s_on_bridge",
            text="以引言里的展开句作为推理前提。",
            kind="derived",
            premise_statement_ids=["s_intro"],
        )
    )

    assert outcome.error is None
    assert not assembler.done


def test_introduction_statements_are_verified_like_every_other_sentence() -> None:
    draft = _draft()

    assert [statement.statement_id for statement in draft.statements()][0] == "s_intro"
    assert draft.body_char_count() == sum(len(statement.text) for statement in draft.statements())


def test_paragraph_can_contain_one_statement() -> None:
    payload = _draft().model_dump(mode="json")
    payload["sections"][0]["paragraphs"][0]["statements"] = [
        payload["sections"][0]["paragraphs"][0]["statements"][0]
    ]
    payload["conclusion"][0]["statements"][0]["premise_statement_ids"] = ["s_fact"]

    ReportDraft.model_validate(payload)


def test_present_both_does_not_require_adjacent_evidence_statements() -> None:
    snapshot_payload = _snapshot().model_dump(mode="json")
    snapshot_payload["evidence_cards"].append(
        {
            "assertion_id": "30000000-0000-0000-0000-000000000002",
            "task_id": str(TASK_ID),
            "assertion_statement": "另一份材料记录了不同条件下的事实。",
            "excerpts": [
                {
                    "excerpt_id": str(OTHER_EXCERPT_ID),
                    "text": "另一口径下的年度数值为 37。",
                    "source": {
                        "title": "另一份公开报告",
                        "author": "Example Publisher",
                        "published_at": "2026-07-02",
                        "source_uri": "https://example.test/other-report",
                        "document_version": 1,
                    },
                }
            ],
        }
    )
    snapshot_payload["conflicts"] = [
        {
            "conflict_key": "sha256:test",
            "disputed_point": "不同条件下的结果是否一致？",
            "excerpt_ids": [str(EXCERPT_ID), str(OTHER_EXCERPT_ID)],
            "decision": "present_both",
            "winning_excerpt_ids": [],
            "rationale": "需要忠实呈现不同条件。",
        }
    ]
    draft_payload = _draft().model_dump(mode="json")
    statements = draft_payload["sections"][0]["paragraphs"][0]["statements"]
    statements.append(
        {
            "statement_id": "s_other_fact",
            "text": "另一条件下出现了不同结果。",
            "kind": "evidence",
            "candidate_excerpt_ids": [str(OTHER_EXCERPT_ID)],
            "premise_statement_ids": [],
        }
    )

    validate_writer_draft(
        WriterSnapshot.model_validate(snapshot_payload),
        ReportDraft.model_validate(draft_payload),
    )


def test_renderer_keeps_paragraphs_and_generates_stable_candidate_citations() -> None:
    rendered = render_report_draft(_snapshot(), _draft())

    assert "草稿预览：正文和引用尚未逐句验证" in rendered.markdown
    assert "## 引言\n\n核心答案" in rendered.markdown
    assert "## 综合结论\n\n总结收束" in rendered.markdown
    assert "公开材料记录了一个事实" in rendered.markdown
    assert "这一事实需要结合适用边界理解" in rendered.markdown
    assert "[^1]" in rendered.markdown
    assert rendered.markdown.count("[^1]:") == 1
    assert '"verification_status": "pending"' in rendered.json_text


def test_assembler_folds_multi_turn_stream_and_assigns_runtime_ids() -> None:
    assembler = ReportStreamAssembler(_snapshot())

    first = assembler.consume("\n".join(_first_turn_lines()))
    assert first.error is None
    assert not assembler.done

    second = assembler.consume("\n".join(_second_turn_lines()))
    assert second.error is None
    assert assembler.done

    draft = assembler.build()
    assert draft.title == "深度研究报告"
    assert [section.section_id for section in draft.sections] == ["sec_001"]
    # Paragraph ids follow document order, so the introduction claims the first one.
    assert draft.introduction[0].paragraph_id == "p_001"
    assert draft.sections[0].paragraphs[0].paragraph_id == "p_002"
    assert draft.conclusion[0].paragraph_id == "p_003"
    assert [statement.statement_id for statement in draft.statements()] == [
        "s_intro",
        "s_fact",
        "s_analysis",
        "s_conclusion",
    ]
    # Wire aliases are mapped back to the real excerpt UUIDs in the draft.
    assert draft.statements()[1].candidate_excerpt_ids == [EXCERPT_ID]
    validate_writer_draft(_snapshot(), draft)


def test_assembler_treats_truncated_last_line_as_resumable() -> None:
    assembler = ReportStreamAssembler(_snapshot())
    truncated = "\n".join([*_first_turn_lines(), '{"record": "statement", "statement_id": "s_ana'])

    outcome = assembler.consume(truncated)

    assert outcome.error is None
    assert outcome.accepted == len(_first_turn_lines())
    assert assembler.last_accepted == "statement s_fact"

    final = assembler.consume("\n".join(_second_turn_lines()))
    assert final.error is None
    assert assembler.done


def test_assembler_rejects_bad_record_but_keeps_earlier_records() -> None:
    assembler = ReportStreamAssembler(_snapshot())
    lines = _first_turn_lines()
    lines.insert(
        1,
        _line(
            record="statement",
            statement_id="s_orphan",
            text="没有任何范围打开就出现的语句。",
            kind="evidence",
            candidate_excerpt_ids=["e_01"],
        ),
    )

    outcome = assembler.consume("\n".join(lines))

    assert outcome.accepted == 1
    assert outcome.error is not None
    assert "section" in outcome.error

    recovery = assembler.consume("\n".join([*_first_turn_lines()[1:], *_second_turn_lines()]))
    assert recovery.error is None
    assert assembler.done


def test_assembler_requires_paragraph_before_its_first_statement() -> None:
    assembler = ReportStreamAssembler(_snapshot())
    assembler.consume(_line(record="title", text="深度研究报告"))
    assembler.consume(_line(record="introduction"))

    outcome = assembler.consume(
        _line(
            record="statement",
            statement_id="s_intro",
            text="没有先开始段落。",
            kind="elaboration",
        )
    )

    assert outcome.error is not None
    assert "必须先输出 paragraph" in outcome.error


def test_assembler_rejects_unknown_excerpt_and_forward_premise() -> None:
    assembler = ReportStreamAssembler(_snapshot())
    assembler.consume("\n".join(_first_turn_lines()))

    unknown_excerpt = assembler.consume(
        _line(
            record="statement",
            statement_id="s_bad_excerpt",
            text="引用了材料之外的摘录。",
            kind="evidence",
            candidate_excerpt_ids=["e_99"],
        )
    )
    assert unknown_excerpt.error is not None
    assert "excerpt" in unknown_excerpt.error
    assert "e_99" in unknown_excerpt.error

    forward_premise = assembler.consume(
        _line(
            record="statement",
            statement_id="s_bad_premise",
            text="引用了尚未出现的前提。",
            kind="derived",
            premise_statement_ids=["s_future"],
        )
    )
    assert forward_premise.error is not None
    assert "s_future" in forward_premise.error


def test_assembler_rejects_end_before_conclusion_and_section_after_conclusion() -> None:
    assembler = ReportStreamAssembler(_snapshot())
    assembler.consume("\n".join(_first_turn_lines()))

    early_end = assembler.consume(_line(record="end"))
    assert early_end.error is not None
    assert "conclusion" in early_end.error

    assembler.consume(_line(record="conclusion"))
    nested_section = assembler.consume(_line(record="section", title="不允许的新章节"))
    assert nested_section.error is not None
    assert not assembler.done


def test_writer_loop_continues_across_turns_and_disables_json_mode() -> None:
    client = _FakeStreamClient(["\n".join(_first_turn_lines()), "\n".join(_second_turn_lines())])
    writer = OpenAIReportWriter(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

    result = writer.write(_snapshot())

    assert len(client.calls) == 2
    assert all("response_format" not in call for call in client.calls)
    assert client.last_message(1)["content"] == continuation_message("statement s_fact")
    assert result.draft.title == "深度研究报告"
    assert result.raw_output == [
        "\n".join(_first_turn_lines()),
        "\n".join(_second_turn_lines()),
    ]


def test_requirement_failure_rewrites_the_complete_report() -> None:
    client = _FakeStreamClient(["\n".join([*_first_turn_lines(), *_second_turn_lines()])])
    writer = OpenAIReportWriter(client=client, model="qwen-fake-model")  # type: ignore[arg-type]
    findings = ReportVerifierFindings(
        round=1,
        revision=1,
        requirement_failures=[
            ReportRequirementFailure(
                kind="user_constraint",
                statement_ids=[],
                reason="报告没有遵守用户要求的结论先行结构。",
            )
        ],
    )

    result = writer.revise(_snapshot(), _draft(), findings)

    prompt = result.full_prompt[1]["content"]
    assert "重写完整报告" in prompt
    assert '"requirement_failures"' in prompt
    assert "结论先行结构" in prompt
    sent_messages = cast(list[dict[str, str]], client.calls[0]["messages"])
    assert "patch_statement" not in sent_messages[1]["content"]
    assert result.draft.title == "深度研究报告"


def test_local_requirement_failure_rewrites_only_the_named_paragraph() -> None:
    paragraph_patch = "\n".join(
        [
            _line(
                record="patch_paragraph",
                paragraph_id="p_answer",
                statements=[
                    {
                        "statement_id": "s_fact",
                        "text": "公开材料记录了一个带明确口径的事实。",
                        "kind": "evidence",
                        "candidate_excerpt_ids": ["e_01"],
                    },
                    {
                        "statement_id": "s_analysis",
                        "text": "这一事实只适用于材料明确给出的范围。",
                        "kind": "derived",
                        "premise_statement_ids": ["s_fact"],
                    },
                ],
            ),
            _line(record="end"),
        ]
    )
    client = _FakeStreamClient([paragraph_patch])
    writer = OpenAIReportWriter(client=client, model="qwen-fake-model")  # type: ignore[arg-type]
    findings = ReportVerifierFindings(
        round=1,
        revision=1,
        requirement_failures=[
            ReportRequirementFailure(
                kind="overall_calibration",
                repair_scope="paragraph",
                paragraph_ids=["p_answer"],
                statement_ids=["s_analysis"],
                reason="局部判断没有保留材料范围。",
            )
        ],
    )

    original = _draft()
    result = writer.revise(_snapshot(), original, findings)

    assert result.draft.introduction == original.introduction
    assert result.draft.conclusion == original.conclusion
    assert result.draft.sections[0].paragraphs[0].statements[0].text.endswith("带明确口径的事实。")
    prompt = result.full_prompt[1]["content"]
    assert '"p_answer"' in prompt
    assert "patch_paragraph" in result.full_prompt[0]["content"]


def test_paragraph_patch_that_breaks_an_external_premise_is_rejected_atomically() -> None:
    draft = _draft()
    replacement = ReportParagraph(
        paragraph_id="p_answer",
        statements=[
            ReportStatement(
                statement_id="s_fact",
                text="只保留事实，删除被结论引用的分析句。",
                kind="evidence",
                candidate_excerpt_ids=[EXCERPT_ID],
            )
        ],
    )

    with pytest.raises(ValidationError, match="derived premises must reference earlier"):
        apply_report_patches(
            draft,
            statement_patches=[],
            paragraph_patches=[replacement],
            allowed_paragraph_ids={"p_answer"},
        )


def test_multiple_paragraph_patches_apply_in_one_atomic_revision() -> None:
    draft = _draft()
    intro = ReportParagraph(
        paragraph_id="p_intro",
        statements=[
            ReportStatement(
                statement_id="s_intro",
                text="修订后的核心答案。",
                kind="elaboration",
            )
        ],
    )
    answer = ReportParagraph(
        paragraph_id="p_answer",
        statements=[
            ReportStatement(
                statement_id="s_fact",
                text="修订后的事实。",
                kind="evidence",
                candidate_excerpt_ids=[EXCERPT_ID],
            ),
            ReportStatement(
                statement_id="s_analysis",
                text="修订后的局部判断。",
                kind="derived",
                premise_statement_ids=["s_fact"],
            ),
        ],
    )

    patched = apply_report_patches(
        draft,
        statement_patches=[],
        paragraph_patches=[intro, answer],
        allowed_paragraph_ids={"p_intro", "p_answer"},
    )

    assert patched.introduction[0].statements[0].text == "修订后的核心答案。"
    assert patched.sections[0].paragraphs[0].statements[1].text == "修订后的局部判断。"
    assert patched.conclusion == draft.conclusion


def test_unknown_and_overlapping_paragraph_patches_are_rejected() -> None:
    draft = _draft()
    unknown = ReportParagraph(
        paragraph_id="p_unknown",
        statements=[
            ReportStatement(
                statement_id="s_new",
                text="未知段落。",
                kind="elaboration",
            )
        ],
    )
    with pytest.raises(StatementPatchError, match="unknown paragraph_id"):
        apply_report_patches(
            draft,
            statement_patches=[],
            paragraph_patches=[unknown],
            allowed_paragraph_ids={"p_unknown"},
        )

    original_answer = draft.sections[0].paragraphs[0]
    sentence_patch = original_answer.statements[0].model_copy(
        update={"text": "与段落补丁冲突的句子补丁。"}
    )
    with pytest.raises(StatementPatchError, match="overlap"):
        apply_report_patches(
            draft,
            statement_patches=[sentence_patch],
            paragraph_patches=[original_answer],
            allowed_statement_ids={"s_fact"},
            allowed_paragraph_ids={"p_answer"},
        )


def test_writer_replays_a_turn_whose_stream_was_cut_mid_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider hanging up mid-answer costs one turn, not the whole Job.

    This is the failure that used to end a research run after its evidence was already
    gathered and verified: the report was the only thing left to produce.
    """
    monkeypatch.setattr(streaming, "_sleep", lambda _seconds: None)
    dropped = httpx.RemoteProtocolError(
        "peer closed connection without sending complete message body"
    )
    client = _FakeStreamClient(
        [
            "\n".join(_first_turn_lines()),
            dropped,
            "\n".join(_second_turn_lines()),
        ]
    )
    writer = OpenAIReportWriter(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

    result = writer.write(_snapshot())

    assert len(client.calls) == 3
    # The replay re-asks the identical question -- the dropped fragment is never fed back
    # to the model as if it had been accepted.
    assert client.calls[1]["messages"] == client.calls[2]["messages"]
    assert result.raw_output == [
        "\n".join(_first_turn_lines()),
        "\n".join(_second_turn_lines()),
    ]
    assert result.draft.title == "深度研究报告"


def test_writer_loop_feeds_validation_error_back_for_localized_retry() -> None:
    bad_turn = "\n".join(
        [
            *_first_turn_lines(),
            _line(
                record="statement",
                statement_id="s_fact",
                text="重复的 statement_id。",
                kind="evidence",
                candidate_excerpt_ids=["e_01"],
            ),
        ]
    )
    client = _FakeStreamClient([bad_turn, "\n".join(_second_turn_lines())])
    writer = OpenAIReportWriter(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

    result = writer.write(_snapshot())

    assert len(client.calls) == 2
    feedback = client.last_message(1)["content"]
    assert feedback.startswith("你最近一轮输出存在问题：")
    assert "statement s_fact" in feedback
    assert retry_message("x", "y").startswith("你最近一轮输出存在问题：x")
    assert result.draft.title == "深度研究报告"


def test_writer_loop_fails_after_repeated_errors() -> None:
    bad_line = _line(
        record="statement",
        statement_id="s_orphan",
        text="孤儿语句。",
        kind="evidence",
        candidate_excerpt_ids=[],
    )
    client = _FakeStreamClient([bad_line] * 5)
    writer = OpenAIReportWriter(client=client, model="qwen-fake-model")  # type: ignore[arg-type]

    with pytest.raises(ReportWriterOutputError, match="after retries"):
        writer.write(_snapshot())


@pytest.mark.parametrize(
    ("excerpt_count", "expected"),
    [
        (1, 1500),  # a small Job gets the full per-passage ceiling
        (132, 1212),  # a standard Job splits the total budget
        (4000, 400),  # past the floor, passages stay usable and the block grows
    ],
)
def test_writer_excerpt_limit_splits_a_total_budget(excerpt_count: int, expected: int) -> None:
    """Evidence volume varies by an order of magnitude; a fixed cap fits only one size."""
    assert writer_excerpt_limit(excerpt_count) == expected


def test_weak_source_caveat_rides_on_the_finding_it_applies_to() -> None:
    """Attribution belongs in the sentence citing the number, not in a closing disclaimer.

    Research Verifier already names which Assertions rest on a weak carrier. Left in the
    gap list keyed by id, that warning reaches the report as a footnote about the report;
    attached to the finding, it reaches the sentence that uses the number.
    """
    payload = _snapshot().model_dump(mode="json")
    payload["minor_gaps"] = [
        {
            "kind": "source_credibility",
            "severity": "minor",
            "description": "该数字仅见于聚合站转述，无一手来源佐证。",
            "related_assertion_ids": ["30000000-0000-0000-0000-000000000001"],
        },
        {
            "kind": "plan_coverage",
            "severity": "minor",
            "description": "某个方向未能取得公开数据。",
            "related_assertion_ids": ["30000000-0000-0000-0000-000000000001"],
        },
    ]

    material = _material(WriterSnapshot.model_validate(payload))
    finding = material["research_groups"][0]["findings"][0]

    assert finding["source_caveat"] == "该数字仅见于聚合站转述，无一手来源佐证。"
    # A coverage gap describes what the research missed, not how hard one number is.
    assert "某个方向未能取得公开数据。" not in finding["source_caveat"]
    assert "写明转述关系" in report_writer_messages(_snapshot())[0]["content"]


def test_findings_without_a_credibility_gap_carry_no_caveat() -> None:
    finding = _material(_snapshot())["research_groups"][0]["findings"][0]

    assert "source_caveat" not in finding


def test_partial_report_marks_which_sentences_did_not_pass() -> None:
    """A reader has to be able to see which claims were checked.

    Withholding the footnote is invisible: premise-only derived statements carry none,
    so an unverified judgement looks exactly like a sound one and the banner only says
    that some sentence somewhere failed.
    """
    rendered = render_verified_report(
        _snapshot(),
        _draft(),
        citation_map={"s_fact": [EXCERPT_ID]},
        verification_status="partial",
        failed_statement_ids=["s_analysis"],
    )

    assert f"这一事实需要结合适用边界理解。{UNVERIFIED_MARKER}" in rendered.markdown
    # The sentences that did pass are left clean, footnote and all.
    assert f"公开材料记录了一个事实。[^1]{UNVERIFIED_MARKER}" not in rendered.markdown
    assert "公开材料记录了一个事实。[^1]" in rendered.markdown
    assert f"总结{UNVERIFIED_MARKER}" not in rendered.markdown
    # The banner explains the marker instead of only announcing that something failed.
    assert UNVERIFIED_MARKER in rendered.markdown.splitlines()[0]
    # The failed statement still gets no verified citation.
    assert json.loads(rendered.json_text)["failed_statement_ids"] == ["s_analysis"]


def test_fully_verified_report_carries_no_marker() -> None:
    rendered = render_verified_report(
        _snapshot(),
        _draft(),
        citation_map={"s_fact": [EXCERPT_ID]},
        verification_status="verified",
    )

    assert UNVERIFIED_MARKER not in rendered.markdown


def test_quality_reminders_are_exported_without_making_the_report_partial() -> None:
    reminder = {
        "kind": "repetition",
        "location": "综合结论",
        "statement_ids": ["s_conclusion"],
        "reason": "结论重复前文，没有增加新的综合。",
    }
    rendered = render_verified_report(
        _snapshot(),
        _draft(),
        citation_map={"s_fact": [EXCERPT_ID]},
        verification_status="verified",
        quality_reminders=[reminder],
    )

    payload = json.loads(rendered.json_text)
    assert payload["verification_status"] == "verified"
    assert payload["failed_statement_ids"] == []
    assert payload["quality_reminders"] == [reminder]


def test_unresolved_report_requirement_is_partial_without_mislabeling_sentences() -> None:
    requirement = {
        "kind": "core_answer",
        "statement_ids": ["s_conclusion"],
        "reason": "结论没有回答核心问题。",
    }
    rendered = render_verified_report(
        _snapshot(),
        _draft(),
        citation_map={"s_fact": [EXCERPT_ID]},
        verification_status="partial",
        requirement_failures=[requirement],
    )

    payload = json.loads(rendered.json_text)
    assert payload["verification_status"] == "partial"
    assert payload["failed_statement_ids"] == []
    assert payload["requirement_failures"] == [requirement]
    assert "不能标记为已验证" in rendered.markdown.splitlines()[0]
    assert UNVERIFIED_MARKER not in rendered.markdown


def test_quality_reminders_never_enter_sentence_revision_instructions() -> None:
    findings = ReportVerifierFindings(
        round=1,
        revision=1,
        failures=[
            StatementFailure(
                statement_id="s_fact",
                kind="evidence",
                status="unsupported",
                reason="数字与原文不符。",
                allowed_excerpt_ids=[EXCERPT_ID],
            )
        ],
        quality_reminders=[
            ReportQualityReminder(
                kind="repetition",
                location="综合结论",
                statement_ids=["s_conclusion"],
                reason="结论重复前文。",
            )
        ],
    )

    prompt = report_writer_revision_messages(_snapshot(), _draft(), findings)[1]["content"]

    assert '"statement_id": "s_fact"' in prompt
    assert "结论重复前文" not in prompt
    assert '"quality_reminders"' not in prompt
