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

from prospector.agents.llm import get_openai_client, mid_model, no_thinking_extra_body
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
from prospector.schemas.report import MAX_PREMISE_DEPTH

MAX_VERIFY_WORKERS = 8
# A single verdict is a handful of fields. Leaving the response budget at the provider
# default let one runaway answer consume 8192 tokens and truncate the JSON.
MAX_DECISION_TOKENS = 800
_RETRY_INSTRUCTION = (
    "上一次调用没有产生可读取的判定。请根据同一输入重新独立判断。"
    "只输出要求的 JSON 对象；reason 和 inference_note 保持简洁，只说明主要依据。"
)
# A contract violation is a different situation from a truncated or unparseable answer: the
# verdict was readable, so the model needs to be told which rule it broke rather than simply
# asked again. Temperature is 0, so an unchanged prompt would reproduce the same answer.
_CONTRACT_RETRY_INSTRUCTION = (
    "上一次调用的判定可以读取，但不满足契约：{failure}\n"
    "请根据同一输入重新独立判断，并修正上述问题。"
    "只输出要求的 JSON 对象；reason 和 inference_note 保持简洁，只说明主要依据。"
)

StatementDecision = EvidenceStatementDecision | DerivedStatementDecision | BridgeStatementDecision
_DECISION_ADAPTER: TypeAdapter[StatementDecision] = TypeAdapter(StatementDecision)


@dataclass(frozen=True, slots=True)
class ReportVerifierModelResult:
    findings: ReportVerifierFindings
    decisions: list[StatementDecision]
    raw_outputs: dict[str, object]


class ReportVerifierModel(Protocol):
    def verify(self, snapshot: ReportVerifierSnapshot) -> ReportVerifierModelResult: ...


class ReportVerifierOutputError(ValueError):
    """The verifier and this adapter disagree about the protocol — a bug, not bad luck."""

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
        content = re.sub(rf"\b{re.escape(code)}\b", real_id, content)
    return content


def _retry_messages(
    messages: list[dict[str, str]],
    *,
    contract_failure: str | None = None,
) -> list[dict[str, str]]:
    """Retry from the original input, never from a truncated or malformed answer.

    ``contract_failure`` names a rule the previous verdict broke. It is the one case where
    the retry has to say what went wrong, because the answer itself was fine to read.
    """
    instruction = (
        _RETRY_INSTRUCTION
        if contract_failure is None
        else _CONTRACT_RETRY_INSTRUCTION.format(failure=contract_failure)
    )
    return [
        {"role": "system", "content": f"{messages[0]['content']}\n\n{instruction}"},
        messages[1],
    ]


def _contract_violation(
    statement_id: str,
    kind: str,
    decision: StatementDecision,
    excerpt_code_map: dict[str, str],
) -> str | None:
    """Describe how *decision* breaks the verdict contract, or None when it holds.

    Returned rather than raised so the caller can spend its remaining attempt on it. These
    are repairable slips -- a mistyped id, a pair set that misses one excerpt -- and each
    statement is one of a hundred-odd riding on the same run.
    """
    if decision.statement_id != statement_id:
        return (
            f"decision statement_id mismatch: expected {statement_id}, got {decision.statement_id}"
        )
    if decision.kind != kind:
        return f"decision kind mismatch for {statement_id}: expected {kind}, got {decision.kind}"
    if isinstance(decision, EvidenceStatementDecision):
        code_by_id = {UUID(real_id): code for code, real_id in excerpt_code_map.items()}
        allowed = set(code_by_id)
        reported = {pair.excerpt_id for pair in decision.pairs}
        if reported != allowed:
            # Named in the short codes the model was shown, not the UUIDs it never saw.
            missing = sorted(code_by_id[value] for value in allowed - reported)
            unexpected = sorted(str(value) for value in reported - allowed)
            return (
                f"evidence pairs must cover exactly the candidate excerpts for {statement_id}："
                f"缺少 {missing or '无'}，多出 {unexpected or '无'}"
            )
    return None


def _deterministic_derived_overreach(statement: dict[str, Any]) -> DerivedStatementDecision | None:
    """Return a non-pass verdict for a premise graph that cannot be accepted.

    Writer owns only stream shape and topological references. Report Verifier owns
    the evidence-grounding and depth policy, so these verdicts deliberately enter
    the same revision/partial-report flow as model-detected overreach.
    """
    if statement["kind"] != "derived":
        return None
    statement_id = str(statement["statement_id"])
    ungrounded = sorted(
        str(premise["statement_id"])
        for premise in statement["premises"]
        if premise["kind"] not in {"evidence", "derived"}
    )
    if ungrounded:
        return DerivedStatementDecision(
            statement_id=statement_id,
            inference_note="前提不承载可核对的证据",
            status="overreach",
            reason=(
                f"推理前提 {', '.join(ungrounded)} 不承载证据；"
                "derived 结论必须最终落到 evidence 或 derived 前提。"
            ),
        )
    depth = int(statement["premise_depth"])
    if depth > MAX_PREMISE_DEPTH:
        return DerivedStatementDecision(
            statement_id=statement_id,
            inference_note="推理链超过可验证深度",
            status="overreach",
            reason=(
                f"推理链深度为 {depth}，超过允许的 {MAX_PREMISE_DEPTH}；"
                "请改为依赖较低层前提，或收窄为材料可支持的结论。"
            ),
        )
    return None


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
        max_workers: int = MAX_VERIFY_WORKERS,
    ) -> None:
        self.client = client or get_openai_client()
        self.model = model or mid_model()
        self.max_workers = max_workers

    def _complete(self, messages: list[dict[str, str]]) -> tuple[str, str | None]:
        """Return the content plus the provider's completion reason."""
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=messages,  # type: ignore[arg-type]
            response_format={"type": "json_object"},
            max_tokens=MAX_DECISION_TOKENS,
            extra_body=no_thinking_extra_body(self.model),
        )
        record_response_usage(response, self.model)
        if not getattr(response, "choices", None):
            return "", None
        choice = response.choices[0]
        return choice.message.content or "", getattr(choice, "finish_reason", None)

    def _parse(self, content: str) -> StatementDecision:
        return _DECISION_ADAPTER.validate_python(json.loads(_strip_code_fences(content)))

    def _verify_one(self, statement: dict[str, Any]) -> tuple[str, StatementDecision, object]:
        statement_id = str(statement["statement_id"])
        kind = str(statement["kind"])

        # Encode excerpt_id UUIDs → short codes to prevent LLM hallucination
        excerpt_code_map = _encode_excerpt_ids(statement) if kind == "evidence" else {}

        messages = report_verifier_messages(statement)
        attempts: list[dict[str, object]] = []
        parse_errors: list[str] = []
        decision: StatementDecision | None = None
        attempt_messages = messages
        for attempt_index in range(2):
            content, finish_reason = self._complete(attempt_messages)
            attempt: dict[str, object] = {
                "content": content,
                "finish_reason": finish_reason,
            }
            attempts.append(attempt)
            failure: str | None
            contract_failure: str | None = None
            if finish_reason == "length":
                failure = "answer was cut off"
            elif not content.strip():
                failure = "empty content"
            else:
                decoded_content = _decode_excerpt_ids(content, excerpt_code_map)
                try:
                    candidate = self._parse(decoded_content)
                except (ValidationError, TypeError, ValueError) as exc:
                    failure = str(exc)
                else:
                    # Checked here rather than after the loop: a readable verdict that breaks
                    # the contract is one correction away from usable, and every statement
                    # shares a Job with a hundred others that already passed.
                    contract_failure = _contract_violation(
                        statement_id, kind, candidate, excerpt_code_map
                    )
                    if contract_failure is None:
                        decision = candidate
                        break
                    failure = contract_failure
            attempt["parse_error"] = failure
            parse_errors.append(failure)
            if attempt_index == 0:
                attempt_messages = _retry_messages(messages, contract_failure=contract_failure)

        raw: object = {"attempts": attempts}
        if decision is None:
            if all(attempt["finish_reason"] == "length" for attempt in attempts):
                message = (
                    f"Report Verifier answer for {statement_id} was cut off twice; "
                    "the model would not produce a bounded verdict"
                )
            else:
                message = (
                    f"invalid Report Verifier decision for {statement_id}: "
                    f"first attempt failed: {parse_errors[0]}; "
                    f"second attempt failed: {parse_errors[1]}"
                )
            raise ReportVerifierOutputError(message, raw)
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

        for statement_id, item in list(remaining.items()):
            payload = item.model_dump(mode="json")
            decision = _deterministic_derived_overreach(payload)
            if decision is None:
                continue
            decisions_by_id[statement_id] = decision
            raw_outputs[statement_id] = {
                "deterministic_rule": "derived_premise_grounding_or_depth",
                "premise_depth": payload["premise_depth"],
            }
            remaining.pop(statement_id)

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
                futures = {
                    pool.submit(copy_context().run, self._verify_one, payload): str(
                        payload["statement_id"]
                    )
                    for payload in payloads
                }
                for future in as_completed(futures):
                    submitted_id = futures[future]
                    try:
                        statement_id, decision, raw = future.result()
                    except ReportVerifierOutputError as exc:
                        errors.append(str(exc))
                        raw_outputs[submitted_id] = exc.raw_output
                        remaining.pop(submitted_id, None)
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
        missing = [
            item.statement_id
            for item in snapshot.statements
            if item.statement_id not in decisions_by_id
        ]
        if missing:
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
