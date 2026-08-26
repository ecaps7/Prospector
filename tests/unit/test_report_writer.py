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
    retry_message,
)
from prospector.agents.report_writer import OpenAIReportWriter, ReportWriterOutputError
from prospector.deterministic.excerpt_text import CLIP_MARKER, writer_excerpt_limit
from prospector.reporting.render import render_report_draft
from prospector.schemas.report import (
    ReportDraft,
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
                            "research_stage": "deep_dive",
                            "research_mode": "factual",
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

    assert "每行输出 1 个 JSON 对象" in prompt
    assert '"record":"conclusion"' in prompt
    assert "不得引入研究材料之外的内容" in prompt
    assert "只能引用此前已输出的" in prompt
    assert "推理链最多两层" not in prompt
    assert "后续 Report Verifier 逐句核对" in prompt
    assert "含有具体数字、年份、机构、人物、地点或事件" in prompt
    assert "必须写成 evidence" in prompt
    assert "可以很长、很具体" not in prompt
    assert "未完成禁止输出 end" in prompt
    assert "narrative_plan" not in prompt
    assert '"excerpt_id"' in prompt
    assert EXCERPT_BODY in messages[1]["content"]
    # Excerpt ids are shown as short aliases, never raw UUIDs the model could corrupt.
    assert '"e_01"' in messages[1]["content"]
    assert str(EXCERPT_ID) not in messages[1]["content"]


def test_writer_material_groups_findings_by_research_question() -> None:
    """Structure to build on, instead of one flat pile ordered by the calendar."""
    material = _material(_snapshot())

    groups = material["research_groups"]
    assert [group["research_question"] for group in groups] == ["这条线索要回答的研究问题。"]
    assert groups[0]["research_stage"] == "deep_dive"
    assert groups[0]["findings"] == [
        {
            "statement": "公开材料记录了一个带时间口径的事实。",
            "excerpt_ids": ["e_01"],
        }
    ]
    # The passage lives once in the library the findings point into, not inline per card.
    assert [item["excerpt_id"] for item in material["excerpt_library"]] == ["e_01"]
    assert material["excerpt_library"][0]["text"] == EXCERPT_BODY
    assert "text" not in groups[0]["findings"][0]


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


def test_writer_prompt_mandates_no_length_targets_and_no_worked_domain() -> None:
    """Length quotas made the model pad, and padding is where unsourced content enters.

    The old prompt also carried a fully worked example from one subject area, which
    framed every unrelated question in that example's terms.
    """
    prompt = "\n".join(message["content"] for message in report_writer_messages(_snapshot()))

    for length_mandate in ("万字", "300-500", "10-15", "至少 3 条"):
        assert length_mandate not in prompt
    for leaked_domain in ("短视频", "青少年", "留守儿童", "海马体", "多巴胺"):
        assert leaked_domain not in prompt
    assert "长度应当是研究深度的结果" in prompt


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
