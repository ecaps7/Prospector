"""Mid-model adapter for statement-level Report Verifier checks."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from openai import OpenAI
from pydantic import TypeAdapter, ValidationError

from prospector.agents.llm import NO_THINKING_EXTRA_BODY, get_openai_client, mid_model
from prospector.agents.prompts.report_verifier import report_verifier_messages
from prospector.agents.usage import record_response_usage
from prospector.schemas.claims import (
    BridgeStatementDecision,
    DerivedStatementDecision,
    EvidenceStatementDecision,
    ReportVerifierFindings,
    ReportVerifierSnapshot,
    StatementFailure,
    VerdictStatus,
)

MAX_VERIFY_WORKERS = 8

StatementDecision = (
    EvidenceStatementDecision | DerivedStatementDecision | BridgeStatementDecision
)
_DECISION_ADAPTER: TypeAdapter[StatementDecision] = TypeAdapter(StatementDecision)


@dataclass(frozen=True, slots=True)
class ReportVerifierModelResult:
    findings: ReportVerifierFindings
    decisions: list[StatementDecision]
    raw_outputs: dict[str, object]


class ReportVerifierModel(Protocol):
    def verify(self, snapshot: ReportVerifierSnapshot) -> ReportVerifierModelResult: ...


class ReportVerifierOutputError(ValueError):
    def __init__(self, message: str, raw_output: object) -> None:
        super().__init__(message)
        self.raw_output = raw_output


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")]
    return stripped.strip()


def _encode_excerpt_ids(payload: dict[str, Any]) -> dict[str, str]:
    """Replace excerpt_id UUIDs with short codes (E1, E2, …) in candidate_excerpts.

    Returns a mapping from short code → real UUID string.
    Mutates *payload* in-place and returns the code map.
    """
    code_map: dict[str, str] = {}
    for i, excerpt in enumerate(payload.get("candidate_excerpts", []), 1):
        code = f"E{i}"
        code_map[code] = str(excerpt["excerpt_id"])
        excerpt["excerpt_id"] = code
    return code_map


def _decode_excerpt_ids(content: str, code_map: dict[str, str]) -> str:
    """Replace short codes (E1, E2, …) back to real UUID strings in raw JSON text.

    Uses word-boundary regex to avoid accidental substring matches
    (e.g. "E1" inside "E10").
    """
    for code, real_id in code_map.items():
        content = re.sub(rf'\b{re.escape(code)}\b', real_id, content)
    return content


def _repair_prompt(
    broken_output: str,
    kind: str,
    excerpt_code_map: dict[str, str] | None = None,
) -> str:
    if kind == "evidence":
        schema = json.dumps(EvidenceStatementDecision.model_json_schema(), ensure_ascii=False)
    elif kind == "derived":
        schema = json.dumps(DerivedStatementDecision.model_json_schema(), ensure_ascii=False)
    else:
        schema = json.dumps(BridgeStatementDecision.model_json_schema(), ensure_ascii=False)
    code_hint = ""
    if excerpt_code_map:
        codes = ", ".join(excerpt_code_map)
        code_hint = f"\n注意：excerpt_id 使用短代码（{codes}），请勿修改这些代码。\n"
    return f"""下面输出本应是符合 JSON Schema 的单个 JSON 对象。
只修复 JSON 语法或结构，不得改写判定结论或 ID。{code_hint}
JSON Schema：
{schema}

待修复输出：
{broken_output}

只输出修复后的 JSON。"""


def materialize_findings(
    *,
    revision: int,
    round_number: int,
    decisions: list[StatementDecision],
    allowed_excerpt_ids: list[UUID],
) -> ReportVerifierFindings:
    failures: list[StatementFailure] = []
    passed: list[str] = []
    for decision in decisions:
        status: VerdictStatus = decision.status
        if status == "pass":
            passed.append(decision.statement_id)
            continue
        allowed: list[UUID] = []
        if decision.kind == "evidence":
            allowed = list(allowed_excerpt_ids)
        failures.append(
            StatementFailure(
                statement_id=decision.statement_id,
                kind=decision.kind,
                status=status,
                reason=decision.reason
                if not isinstance(decision, BridgeStatementDecision)
                else decision.reason,
                allowed_excerpt_ids=allowed,
            )
        )
    return ReportVerifierFindings(
        round=round_number,
        revision=revision,
        failures=failures,
        passed_statement_ids=passed,
    )


class OpenAIReportVerifier:
    def __init__(
        self,
        client: OpenAI | None = None,
        model: str | None = None,
        repair_model: str | None = None,
        max_workers: int = MAX_VERIFY_WORKERS,
    ) -> None:
        self.client = client or get_openai_client()
        self.model = model or mid_model()
        self.repair_model = repair_model or mid_model()
        self.max_workers = max_workers

    def _complete(self, messages: list[dict[str, str]]) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=messages,  # type: ignore[arg-type]
            response_format={"type": "json_object"},
            extra_body=NO_THINKING_EXTRA_BODY,
        )
        record_response_usage(response, self.model)
        if not getattr(response, "choices", None):
            return ""
        return response.choices[0].message.content or ""

    def _repair_content(
        self, broken_output: str, kind: str, excerpt_code_map: dict[str, str] | None = None
    ) -> str:
        response = self.client.chat.completions.create(
            model=self.repair_model,
            temperature=0.0,
            messages=[
                {
                    "role": "user",
                    "content": _repair_prompt(broken_output, kind, excerpt_code_map),
                }
            ],
            response_format={"type": "json_object"},
            extra_body=NO_THINKING_EXTRA_BODY,
        )
        record_response_usage(response, self.repair_model)
        if not getattr(response, "choices", None):
            return ""
        return response.choices[0].message.content or ""

    def _parse(self, content: str) -> StatementDecision:
        return _DECISION_ADAPTER.validate_python(json.loads(_strip_code_fences(content)))

    def _verify_one(self, statement: dict[str, Any]) -> tuple[str, StatementDecision, object]:
        statement_id = str(statement["statement_id"])
        kind = str(statement["kind"])

        # Encode excerpt_id UUIDs → short codes to prevent LLM hallucination
        excerpt_code_map = _encode_excerpt_ids(statement) if kind == "evidence" else {}

        messages = report_verifier_messages(statement)
        content = self._complete(messages)
        raw: object = {"role": "assistant", "content": content}
        if not content.strip():
            raise ReportVerifierOutputError(
                f"Report Verifier returned empty content for {statement_id}", raw
            )
        # Decode short codes back to real UUIDs before Pydantic parsing
        decoded_content = _decode_excerpt_ids(content, excerpt_code_map)
        try:
            decision = self._parse(decoded_content)
        except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as first_error:
            repaired = self._repair_content(content, kind, excerpt_code_map or None)
            decoded_repaired = _decode_excerpt_ids(repaired, excerpt_code_map)
            raw = {"role": "assistant", "content": content, "repaired_content": repaired}
            try:
                decision = self._parse(decoded_repaired)
            except (ValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ReportVerifierOutputError(
                    f"invalid Report Verifier decision for {statement_id}: "
                    f"{first_error}; repair failed: {exc}",
                    raw,
                ) from exc
        if decision.statement_id != statement_id:
            raise ReportVerifierOutputError(
                f"decision statement_id mismatch: expected {statement_id}, "
                f"got {decision.statement_id}",
                raw,
            )
        if decision.kind != kind:
            raise ReportVerifierOutputError(
                f"decision kind mismatch for {statement_id}: expected {kind}, "
                f"got {decision.kind}",
                raw,
            )
        if isinstance(decision, EvidenceStatementDecision):
            allowed = {UUID(real_id) for real_id in excerpt_code_map.values()}
            reported = {pair.excerpt_id for pair in decision.pairs}
            if reported != allowed:
                raise ReportVerifierOutputError(
                    f"evidence pairs must cover exactly the candidate excerpts for {statement_id}",
                    raw,
                )
        return statement_id, decision, raw

    def verify(self, snapshot: ReportVerifierSnapshot) -> ReportVerifierModelResult:
        """Verify dirty statements in premise-safe waves so derived hard gates see prior results."""
        remaining = {item.statement_id: item for item in snapshot.statements}
        premise_index = {
            item.statement_id: {premise["statement_id"] for premise in item.premises}
            for item in snapshot.statements
        }
        passed_in_run: set[str] = set()
        decisions_by_id: dict[str, StatementDecision] = {}
        raw_outputs: dict[str, object] = {}
        errors: list[str] = []

        while remaining and not errors:
            wave_ids = [
                statement_id
                for statement_id in remaining
                if premise_index[statement_id].isdisjoint(remaining)
            ]
            if not wave_ids:
                wave_ids = list(remaining)
            payloads: list[dict[str, Any]] = []
            for statement_id in wave_ids:
                payload = remaining[statement_id].model_dump(mode="json")
                payload["premises_all_passed"] = all(
                    premise["statement_id"] in passed_in_run or premise.get("passed", False)
                    for premise in payload["premises"]
                )
                payloads.append(payload)

            before = set(remaining)
            workers = min(self.max_workers, max(1, len(payloads)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(copy_context().run, self._verify_one, payload)
                    for payload in payloads
                ]
                for future in as_completed(futures):
                    try:
                        statement_id, decision, raw = future.result()
                    except ReportVerifierOutputError as exc:
                        errors.append(str(exc))
                        continue
                    decisions_by_id[statement_id] = decision
                    raw_outputs[statement_id] = raw
                    if decision.status == "pass":
                        passed_in_run.add(statement_id)
                    remaining.pop(statement_id, None)
            if remaining == before:
                break

        if errors:
            raise ReportVerifierOutputError(
                "Report Verifier failed for one or more statements: " + "; ".join(errors),
                raw_outputs,
            )
        ordered = [
            decisions_by_id[item.statement_id]
            for item in snapshot.statements
            if item.statement_id in decisions_by_id
        ]
        if len(ordered) != len(snapshot.statements):
            missing = [
                item.statement_id
                for item in snapshot.statements
                if item.statement_id not in decisions_by_id
            ]
            raise ReportVerifierOutputError(
                "missing decisions for: " + ", ".join(missing), raw_outputs
            )
        findings = materialize_findings(
            revision=snapshot.revision,
            round_number=snapshot.round,
            decisions=ordered,
            allowed_excerpt_ids=list(snapshot.allowed_excerpt_ids),
        )
        return ReportVerifierModelResult(
            findings=findings, decisions=ordered, raw_outputs=raw_outputs
        )
