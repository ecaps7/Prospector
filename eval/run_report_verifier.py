"""Run the small, human-labelled Report Verifier evaluation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from prospector.agents.llm import LlmNotConfiguredError, mid_model, require_llm_settings
from prospector.agents.report_verifier import OpenAIReportVerifier, ReportVerifierOutputError
from prospector.schemas.claims import ReportVerifierSnapshot

DEFAULT_CASE = Path(__file__).parent / "cases" / "report_verifier_basic.json"


def _load_case(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("评测案例的顶层必须是 JSON 对象")
    return payload


def _accepted_statuses(expectation: dict[str, Any]) -> set[str]:
    statuses = expectation.get("accepted_statuses")
    if (
        not isinstance(statuses, list)
        or not statuses
        or not all(isinstance(status, str) for status in statuses)
    ):
        raise ValueError("每条预期必须包含非空的 accepted_statuses 字符串列表")
    return set(statuses)


def main() -> int:
    case_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CASE
    try:
        case = _load_case(case_path)
        snapshot = ReportVerifierSnapshot.model_validate(case["snapshot"])
        expectations = case["expectations"]
        if not isinstance(expectations, list) or not expectations:
            raise ValueError("评测案例必须包含非空的 expectations 列表")
        expected_by_id = {
            str(expectation["statement_id"]): _accepted_statuses(expectation)
            for expectation in expectations
        }
        statement_ids = {statement.statement_id for statement in snapshot.statements}
        if set(expected_by_id) != statement_ids:
            raise ValueError("expectations 必须恰好覆盖 snapshot 中的全部 statement_id")
        require_llm_settings()
    except (KeyError, OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        print(f"评测案例无效：{exc}", file=sys.stderr)
        return 2
    except LlmNotConfiguredError as exc:
        print(f"模型未配置：{exc}", file=sys.stderr)
        return 2

    started = perf_counter()
    try:
        result = OpenAIReportVerifier().verify(snapshot)
    except ReportVerifierOutputError as exc:
        print(f"Report Verifier 未能完成评测：{exc}", file=sys.stderr)
        return 2
    elapsed = perf_counter() - started

    actual_by_id = {decision.statement_id: decision.status for decision in result.decisions}
    rows: list[tuple[str, str, str, bool]] = []
    for statement in snapshot.statements:
        accepted = expected_by_id[statement.statement_id]
        actual = actual_by_id.get(statement.statement_id, "missing")
        rows.append(
            (
                statement.statement_id,
                " / ".join(sorted(accepted)),
                actual,
                actual in accepted,
            )
        )

    print(f"\nReport Verifier 小型评测：{case['name']}")
    print(f"模型：{mid_model()}")
    print()
    print(f"{'statement_id':<30} {'预期':<26} {'实际':<15} 结果")
    print("-" * 86)
    for statement_id, expected, actual, passed in rows:
        marker = "通过" if passed else "失败"
        print(f"{statement_id:<30} {expected:<26} {actual:<15} {marker}")

    passed_count = sum(passed for *_, passed in rows)
    print()
    print(f"结果：{passed_count} / {len(rows)} 通过")
    print(f"耗时：{elapsed:.2f} 秒")
    return 0 if passed_count == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
