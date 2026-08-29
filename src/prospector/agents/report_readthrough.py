# ruff: noqa: E501
"""Final read-through: the one check that reads the report as prose.

Nothing else in the chain looks at how the report reads.  Claim Attribution judges spans
against material, and Whole-report Review judges whether the answer holds together as an
argument -- its prompt says outright that a difference in style cannot block.  Coherence
was left to the assumption that a revision which re-emits the whole document keeps it,
and that assumption does not survive contact with the history: rewrites triggered by 19
and 39 fixes came back with a quarter and a sixth of the document changed.

So coherence gets its own pass, and it is the cheapest one in the chain: it reads the
report and nothing else -- no Excerpts, no material, no verdicts.  It runs once, after
the facts have stopped moving, and it can only ask for text to be repaired; it can never
decide the report is unfit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prospector.agents.llm import get_openai_client, strong_model, thinking_extra_body
from prospector.deterministic.markdown_report import parse_markdown

READTHROUGH_ATTEMPTS = 2


class ReadthroughFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["dangling_reference", "broken_transition", "summary_mismatch", "orphaned_passage"]
    block_ids: list[str] = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)


class ReadthroughOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[ReadthroughFinding] = Field(default_factory=list)
    audit_notes: list[dict[str, Any]] = Field(default_factory=list)


class ReadthroughOutputError(ValueError):
    def __init__(self, message: str, raw_output: object) -> None:
        super().__init__(message)
        self.raw_output = raw_output


@dataclass(frozen=True, slots=True)
class ReadthroughResult:
    full_prompt: list[dict[str, str]]
    raw_output: str
    output: ReadthroughOutput


class ReadthroughModel(Protocol):
    def read_through(self, markdown: str) -> ReadthroughResult: ...


def readthrough_messages(markdown: str) -> list[dict[str, str]]:
    blocks = parse_markdown(markdown)
    system = """你通读这份报告，只看行文是否读得通。

事实是否属实、结论是否成立，都已经在别的环节判定过，不在你的职责范围内。不要评价观点、不要要求补充材料、不要提出风格偏好。

只报告读者会实际卡住的地方：

- **dangling_reference**：指代落空。“这一转变”“上述做法”“该协议”找不到它指的东西，或者指向了别的东西。
- **broken_transition**：连接词不成立。“因此”前后没有因果，“相比之下”两边没有对比，“不仅如此”前面没有“此”。
- **summary_mismatch**：开头的总述、阶段小结或结论，与正文实际写的内容对不上（数量对不上、分类对不上、口径对不上）。
- **orphaned_passage**：某段明显缺少上下文，像是前后有内容被移走了。

每条必须给出具体的 block_ids 和读者会怎样卡住。读得通就返回空 findings，不要为了有产出而挑毛病。

最终只输出符合 output_schema 的单个 JSON 对象。"""
    payload = {
        "blocks": [
            {"block_id": block.block_id, "kind": block.kind, "text": block.text} for block in blocks
        ],
        "output_schema": ReadthroughOutput.model_json_schema(),
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


class OpenAIReadthrough:
    def __init__(self, client: OpenAI | None = None, model: str | None = None) -> None:
        self.client = client or get_openai_client()
        self.model = model or strong_model()

    def read_through(self, markdown: str) -> ReadthroughResult:
        prompt = readthrough_messages(markdown)
        known = {block.block_id for block in parse_markdown(markdown)}
        last: ReadthroughOutputError | None = None
        raw = ""
        for _ in range(READTHROUGH_ATTEMPTS):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=cast(Any, prompt),
                temperature=0.1,
                extra_body=thinking_extra_body(self.model),
            )
            raw = response.choices[0].message.content or ""
            try:
                output = ReadthroughOutput.model_validate_json(raw)
            except (ValidationError, ValueError) as exc:
                last = ReadthroughOutputError(f"invalid read-through output: {exc}", raw)
                continue
            unknown = [
                block_id
                for item in output.findings
                for block_id in item.block_ids
                if block_id not in known
            ]
            if unknown:
                last = ReadthroughOutputError(
                    f"read-through referenced unknown blocks: {unknown}", raw
                )
                continue
            return ReadthroughResult(full_prompt=prompt, raw_output=raw, output=output)
        raise cast(ReadthroughOutputError, last)
