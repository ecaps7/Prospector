#!/usr/bin/env python3
"""对一次 examples/ 运行产物做离线引用血缘审计。

只读取运行目录下的 report.json 与 report.md，检查引用血缘本应保证的七条不变量。
脚本刻意只用标准库、且不 import prospector 的任何模块——审计不应依赖被审计的代码。
不连数据库、不出网、不需要 API key。

用法：
    python examples/verify_lineage.py examples/<run-directory>/
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# 与 src/prospector/schemas/report.py 保持一致；此处独立重述，不从被审计的包里 import。
MAX_PREMISE_DEPTH = 2
EVIDENCE_BEARING_KINDS = {"evidence", "derived"}
TERMINAL_STATUSES = {"verified", "partial"}

INLINE_CITATION = re.compile(r"\[\^(\d+)\](?!:)")
CITATION_DEFINITION = re.compile(r"^\[\^(\d+)\]:", re.MULTILINE)

Problems = list[str]


def _statements(draft: dict[str, Any]) -> list[dict[str, Any]]:
    """草稿中的全部语句，按文档顺序：引言 → 各章节 → 结论。"""
    paragraphs: list[dict[str, Any]] = list(draft["introduction"])
    for section in draft["sections"]:
        paragraphs.extend(section["paragraphs"])
    paragraphs.extend(draft["conclusion"])
    return [statement for paragraph in paragraphs for statement in paragraph["statements"]]


def check_citation_numbers(payload: dict[str, Any], statements: list[dict[str, Any]]) -> Problems:
    """1 引用编号闭合：正文用到的每个编号都能在 sources 中找到对应条目。"""
    valid = {source["citation_number"] for source in payload["sources"]}
    known_ids = {statement["statement_id"] for statement in statements}
    problems: Problems = []
    for statement_id, numbers in payload["statement_citations"].items():
        if statement_id not in known_ids:
            problems.append(f"statement_citations 含草稿中不存在的语句 {statement_id}")
        for number in numbers:
            if number not in valid:
                problems.append(f"{statement_id} 的引用编号 [^{number}] 在 sources 中不存在")
    return problems


def check_excerpt_ownership(payload: dict[str, Any]) -> Problems:
    """2 Excerpt 归属唯一：每个被引 Excerpt 恰好归属一个来源条目。"""
    owners: dict[str, list[int]] = {}
    for source in payload["sources"]:
        for excerpt_id in source["excerpt_ids"]:
            owners.setdefault(excerpt_id, []).append(source["citation_number"])

    problems: Problems = [
        f"Excerpt {excerpt_id} 同时归属多个来源 {sorted(numbers)}"
        for excerpt_id, numbers in sorted(owners.items())
        if len(numbers) > 1
    ]
    for statement_id, excerpt_ids in payload["citation_excerpt_ids"].items():
        for excerpt_id in excerpt_ids:
            if excerpt_id not in owners:
                problems.append(f"{statement_id} 引用的 Excerpt {excerpt_id} 未归属任何来源")
    return problems


def check_source_snapshots(payload: dict[str, Any]) -> Problems:
    """3 源快照可定位：每个来源都带 URI 与版本号，且至少有一条 Excerpt 支撑。"""
    problems: Problems = []
    for source in payload["sources"]:
        number = source["citation_number"]
        if not source.get("source_uri"):
            problems.append(f"来源 [^{number}] 缺少 source_uri")
        version = source.get("document_version")
        if not isinstance(version, int) or version < 1:
            problems.append(f"来源 [^{number}] 的 document_version 非法：{version!r}")
        if not source.get("excerpt_ids"):
            problems.append(f"来源 [^{number}] 没有任何 Excerpt 支撑")
    return problems


def check_citations_within_candidates(
    payload: dict[str, Any], statements: list[dict[str, Any]]
) -> Problems:
    """4 已验证引用不超出候选：渲染出的引用必须是 Writer 当初提出的候选的子集。"""
    candidates = {
        statement["statement_id"]: set(statement["candidate_excerpt_ids"])
        for statement in statements
    }
    problems: Problems = []
    for statement_id, excerpt_ids in payload["citation_excerpt_ids"].items():
        allowed = candidates.get(statement_id)
        if allowed is None:
            problems.append(f"citation_excerpt_ids 含草稿中不存在的语句 {statement_id}")
            continue
        for excerpt_id in excerpt_ids:
            if excerpt_id not in allowed:
                problems.append(f"{statement_id} 的已验证引用 {excerpt_id} 不在 Writer 候选集中")
    return problems


def check_failed_statements(payload: dict[str, Any], statements: list[dict[str, Any]]) -> Problems:
    """5 未通过句不带引用：这是 partial 语义的核心，且状态字段必须与之自洽。"""
    failed = list(payload["failed_statement_ids"])
    status = payload["verification_status"]
    known_ids = {statement["statement_id"] for statement in statements}
    problems: Problems = []

    for statement_id in failed:
        if statement_id not in known_ids:
            problems.append(f"failed_statement_ids 含草稿中不存在的语句 {statement_id}")
            continue
        numbers = payload["statement_citations"].get(statement_id) or []
        if numbers:
            problems.append(f"未通过验证的 {statement_id} 仍携带引用角标 {numbers}")

    if status not in TERMINAL_STATUSES:
        problems.append(f"verification_status 不是终态：{status!r}")
    else:
        expected = "partial" if failed else "verified"
        if status != expected:
            problems.append(
                f"verification_status={status!r} 与 {len(failed)} 条未通过语句不一致，"
                f"应为 {expected!r}"
            )
    return problems


def check_statement_kinds(statements: list[dict[str, Any]]) -> Problems:
    """6 分型与前提深度：分型规则自洽，前提承载证据，推理链不超过允许深度。"""
    kinds: dict[str, str] = {}
    depths: dict[str, int] = {}
    problems: Problems = []

    for statement in statements:
        statement_id = statement["statement_id"]
        kind = statement["kind"]
        excerpts = statement["candidate_excerpt_ids"]
        premises = statement["premise_statement_ids"]

        if statement_id in kinds:
            problems.append(f"statement_id 重复：{statement_id}")

        if kind == "evidence":
            if not excerpts:
                problems.append(f"{statement_id} 是 evidence 却没有候选 Excerpt")
            if premises:
                problems.append(f"{statement_id} 是 evidence 却引用了前提语句")
        elif kind == "derived":
            if not premises:
                problems.append(f"{statement_id} 是 derived 却没有前提语句")
            if excerpts:
                problems.append(f"{statement_id} 是 derived 却直接引用 Excerpt")
        elif excerpts or premises:
            problems.append(f"{statement_id} 是 {kind}，不应携带证据或前提")

        unknown = [premise for premise in premises if premise not in kinds]
        if unknown:
            problems.append(f"{statement_id} 的前提未在其之前出现：{', '.join(unknown)}")
        groundless = [
            premise
            for premise in premises
            if premise in kinds and kinds[premise] not in EVIDENCE_BEARING_KINDS
        ]
        if groundless:
            problems.append(f"{statement_id} 的前提不承载证据：{', '.join(groundless)}")

        kinds[statement_id] = kind
        if kind == "derived":
            resolved = [depths[premise] for premise in premises if premise in depths]
            depth = 1 + max(resolved, default=0)
        else:
            depth = 0
        depths[statement_id] = depth
        if depth > MAX_PREMISE_DEPTH:
            problems.append(f"{statement_id} 的推理链深度 {depth} 超过上限 {MAX_PREMISE_DEPTH}")
    return problems


def check_markdown_agrees(markdown: str, payload: dict[str, Any]) -> Problems:
    """7 Markdown 与 JSON 一致：排除产物被人工编辑过的可能。"""
    inline = {int(value) for value in INLINE_CITATION.findall(markdown)}
    defined = {int(value) for value in CITATION_DEFINITION.findall(markdown)}
    from_json = {
        number for numbers in payload["statement_citations"].values() for number in numbers
    }
    in_sources = {source["citation_number"] for source in payload["sources"]}

    problems: Problems = []
    if inline != from_json:
        problems.append(
            "正文角标与 JSON 的 statement_citations 不一致："
            f"仅在 Markdown={sorted(inline - from_json)}，仅在 JSON={sorted(from_json - inline)}"
        )
    if defined != in_sources:
        problems.append(
            "「来源」节的脚注定义与 JSON 的 sources 不一致："
            f"仅在 Markdown={sorted(defined - in_sources)}，"
            f"仅在 JSON={sorted(in_sources - defined)}"
        )
    return problems


def audit(payload: dict[str, Any], markdown: str) -> list[tuple[str, Problems]]:
    statements = _statements(payload["draft"])
    return [
        ("引用编号闭合", check_citation_numbers(payload, statements)),
        ("Excerpt 归属唯一", check_excerpt_ownership(payload)),
        ("源快照可定位", check_source_snapshots(payload)),
        ("已验证引用不超出候选", check_citations_within_candidates(payload, statements)),
        ("未通过句不带引用", check_failed_statements(payload, statements)),
        ("分型与前提深度", check_statement_kinds(statements)),
        ("Markdown 与 JSON 一致", check_markdown_agrees(markdown, payload)),
    ]


def _summarise(payload: dict[str, Any], statements: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for statement in statements:
        counts[statement["kind"]] = counts.get(statement["kind"], 0) + 1
    breakdown = "，".join(f"{kind} {count}" for kind, count in sorted(counts.items()))
    return (
        f"状态 {payload['verification_status']}"
        f" · 语句 {len(statements)}（{breakdown}）"
        f" · 来源 {len(payload['sources'])}"
        f" · 未通过 {len(payload['failed_statement_ids'])}"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    run_directory = Path(argv[1])
    report_json = run_directory / "report.json"
    report_markdown = run_directory / "report.md"
    missing = [path for path in (report_json, report_markdown) if not path.is_file()]
    if missing:
        for path in missing:
            print(f"缺少产物：{path}", file=sys.stderr)
        return 2

    payload = json.loads(report_json.read_text(encoding="utf-8"))
    markdown = report_markdown.read_text(encoding="utf-8")

    print(f"Prospector 引用血缘审计 · {run_directory}")
    print(f"  {_summarise(payload, _statements(payload['draft']))}")
    print()

    results = audit(payload, markdown)
    for index, (name, problems) in enumerate(results, start=1):
        mark = "✗" if problems else "✓"
        print(f"  {mark} {index} {name}")
        for problem in problems:
            print(f"      - {problem}")

    failed = sum(1 for _, problems in results if problems)
    print()
    print(f"结论：{len(results)} 项检查，{len(results) - failed} 通过，{failed} 失败")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
