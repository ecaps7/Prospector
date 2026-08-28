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
from prospector.agents.prompts.report_verifier import (
    report_quality_messages,
    report_verifier_messages,
)
from prospector.agents.usage import record_response_usage
from prospector.schemas.claims import (
    BridgeStatementDecision,
    DerivedStatementDecision,
    EvidenceStatementDecision,
    ReportQualityDecision,
    ReportQualityReminder,
    ReportRequirementFailure,
    ReportVerifierFindings,
    ReportVerifierSnapshot,
    StatementDecision,
    StatementFailure,
    VerdictStatus,
)

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

_QUALITY_ADAPTER: TypeAdapter[ReportQualityDecision] = TypeAdapter(ReportQualityDecision)
_STATEMENT_ADAPTER: TypeAdapter[StatementDecision] = TypeAdapter(StatementDecision)


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


def _parse_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _encode_excerpt_ids(payload: dict[str, Any]) -> dict[str, str]:
    """Replace candidate excerpt UUIDs with short codes (E1, E2, …) for the model.

    ``known_conflicts`` keeps the same codes when a conflict excerpt is also a
    candidate; non-candidate conflict excerpts lose their id so the model has
    only one namespace it is allowed to write into ``pairs``. Winning sides are
    marked on each conflict excerpt instead of a parallel UUID list.

    Returns a mapping from short code → real UUID string. Mutates *payload*.
    """
    code_map: dict[str, str] = {}
    id_to_code: dict[str, str] = {}
    for i, excerpt in enumerate(payload.get("candidate_excerpts", []), 1):
        code = f"E{i}"
        real_id = str(excerpt["excerpt_id"])
        code_map[code] = real_id
        id_to_code[real_id] = code
        excerpt["excerpt_id"] = code
    for conflict in payload.get("known_conflicts") or []:
        if not isinstance(conflict, dict):
            continue
        winning = {str(value) for value in conflict.pop("winning_excerpt_ids", None) or []}
        raw_excerpts = conflict.get("excerpts")
        if not isinstance(raw_excerpts, list):
            continue
        rewritten: list[dict[str, Any]] = []
        for excerpt in raw_excerpts:
            if not isinstance(excerpt, dict):
                continue
            real_id = str(excerpt.pop("excerpt_id", "") or "")
            if real_id in id_to_code:
                excerpt["excerpt_id"] = id_to_code[real_id]
            excerpt["winning"] = real_id in winning
            rewritten.append(excerpt)
        conflict["excerpts"] = rewritten
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


def _reported_pair_ids(payload: object) -> set[UUID]:
    if not isinstance(payload, dict) or not isinstance(payload.get("pairs"), list):
        return set()
    reported: set[UUID] = set()
    for pair in payload["pairs"]:
        if not isinstance(pair, dict):
            continue
        excerpt_id = _parse_uuid(pair.get("excerpt_id"))
        if excerpt_id is not None:
            reported.add(excerpt_id)
    return reported


def _strip_non_candidate_pairs(payload: object, allowed: set[UUID]) -> None:
    """Drop pairs the model is not allowed to name. Mutates *payload*."""
    if not isinstance(payload, dict) or not isinstance(payload.get("pairs"), list):
        return
    payload["pairs"] = [
        pair
        for pair in payload["pairs"]
        if isinstance(pair, dict) and _parse_uuid(pair.get("excerpt_id")) in allowed
    ]


def _pair_coverage_violation(
    statement_id: str,
    allowed: set[UUID],
    reported: set[UUID],
    excerpt_code_map: dict[str, str],
) -> str | None:
    """Name a candidate-cover miss using the short codes the model was shown."""
    if reported == allowed:
        return None
    code_by_id = {UUID(real_id): code for code, real_id in excerpt_code_map.items()}
    missing = sorted(code_by_id[value] for value in allowed - reported)
    extra_note = (
        "多出非候选 id，pairs 只能包含 candidate_excerpts 中的短代码"
        if reported - allowed
        else "多出 无"
    )
    return (
        f"pairs must cover exactly the candidate excerpts for {statement_id}："
        f"缺少 {missing or '无'}，{extra_note}"
    )


def _contract_violation(
    statement_id: str,
    kind: str,
    decision: StatementDecision,
    excerpt_code_map: dict[str, str],
    known_conflict_keys: set[str],
    has_premises: bool,
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
    if isinstance(decision, (EvidenceStatementDecision, DerivedStatementDecision)):
        allowed = {UUID(real_id) for real_id in excerpt_code_map.values()}
        coverage = _pair_coverage_violation(
            statement_id,
            allowed,
            {pair.excerpt_id for pair in decision.pairs},
            excerpt_code_map,
        )
        if coverage is not None:
            return coverage
        unexpected_conflicts = sorted(set(decision.conflict_keys) - known_conflict_keys)
        if unexpected_conflicts:
            return (
                f"conflict_keys for {statement_id} must come from known_conflicts; "
                f"unexpected: {unexpected_conflicts}"
            )
        if (
            isinstance(decision, DerivedStatementDecision)
            and decision.status == "pass"
            and not has_premises
            and not any(pair.relation == "support" for pair in decision.pairs)
        ):
            return "a direct-excerpt-only derived pass requires at least one support relation"
    return None


def _deterministic_derived_structure_failure(
    statement: dict[str, Any],
) -> DerivedStatementDecision | None:
    """Reject a declared premise that cannot carry an evidence chain.

    The graph may be arbitrarily deep. This check only enforces that every declared
    premise is the kind of statement that can itself trace back to evidence.
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
            status="unsupported",
            reason=(
                f"推理前提 {', '.join(ungrounded)} 不承载证据；"
                "derived 结论必须最终落到 evidence 或 derived 前提。"
            ),
        )
    return None


def _failed_premise_decision(statement: dict[str, Any]) -> DerivedStatementDecision:
    failed = [
        str(premise["statement_id"])
        for premise in statement["premises"]
        if not premise.get("passed", False)
    ]
    return DerivedStatementDecision(
        statement_id=str(statement["statement_id"]),
        inference_note="推理依赖未通过核验的前提",
        status="unsupported",
        reason=(
            "前提硬闸门："
            + ", ".join(failed or ["至少一个前提"])
            + " 未通过核验；依赖该前提的判断不能作为已验证结论。"
        ),
    )


def decisions_from_statement_checks(statement_checks: object) -> list[StatementDecision]:
    """Rebuild sentence decisions from a persisted verifier run, ignoring the quality row."""
    if not isinstance(statement_checks, list):
        return []
    decisions: list[StatementDecision] = []
    for item in statement_checks:
        if not isinstance(item, dict) or item.get("kind") == "report_quality":
            continue
        decisions.append(_STATEMENT_ADAPTER.validate_python(item))
    return decisions


def materialize_findings(
    *,
    revision: int,
    round_number: int,
    decisions: list[StatementDecision],
    allowed_excerpt_ids: list[UUID],
    requirement_failures: list[ReportRequirementFailure] | None = None,
    quality_reminders: list[ReportQualityReminder] | None = None,
) -> ReportVerifierFindings:
    failures: list[StatementFailure] = []
    passed: list[str] = []
    for decision in decisions:
        status: VerdictStatus = decision.status
        if status == "pass":
            passed.append(decision.statement_id)
            continue
        allowed: list[UUID] = []
        if decision.kind in {"evidence", "derived"}:
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
        requirement_failures=list(requirement_failures or []),
        passed_statement_ids=passed,
        quality_reminders=list(quality_reminders or []),
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

    @staticmethod
    def _validate_decision(payload: object, expected_kind: str) -> StatementDecision:
        """Validate against the statement's known shape, without union-error noise."""
        if expected_kind == "evidence":
            return EvidenceStatementDecision.model_validate(payload)
        if expected_kind == "derived":
            return DerivedStatementDecision.model_validate(payload)
        return BridgeStatementDecision.model_validate(payload)

    @staticmethod
    def _validation_failure(exc: ValidationError) -> str:
        """Return the first actionable schema error for the model's correction turn."""
        error = exc.errors(include_url=False, include_input=False)[0]
        location = ".".join(str(part) for part in error["loc"])
        message = str(error["msg"])
        return f"{location}: {message}" if location else message

    def _verify_one(self, statement: dict[str, Any]) -> tuple[str, StatementDecision, object]:
        statement_id = str(statement["statement_id"])
        kind = str(statement["kind"])

        # Encode excerpt_id UUIDs → short codes to prevent LLM hallucination.
        # A derived judgement may now cite Excerpts directly as well as premise statements.
        excerpt_code_map = _encode_excerpt_ids(statement)

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
                    payload = json.loads(_strip_code_fences(decoded_content))
                except (TypeError, ValueError) as exc:
                    failure = str(exc)
                else:
                    allowed_ids = {UUID(real_id) for real_id in excerpt_code_map.values()}
                    if kind in {"evidence", "derived"}:
                        _strip_non_candidate_pairs(payload, allowed_ids)
                        coverage = _pair_coverage_violation(
                            statement_id,
                            allowed_ids,
                            _reported_pair_ids(payload),
                            excerpt_code_map,
                        )
                    else:
                        coverage = None
                    if coverage is not None:
                        contract_failure = coverage
                        failure = coverage
                    else:
                        try:
                            candidate = self._validate_decision(payload, kind)
                        except ValidationError as exc:
                            contract_failure = self._validation_failure(exc)
                            failure = contract_failure
                        else:
                            # Checked here rather than after the loop: a readable verdict
                            # that breaks the contract is one correction away from usable.
                            contract_failure = _contract_violation(
                                statement_id,
                                kind,
                                candidate,
                                excerpt_code_map,
                                {
                                    str(conflict["conflict_key"])
                                    for conflict in statement.get("known_conflicts", [])
                                },
                                bool(statement.get("premises")),
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

    def _verify_quality(
        self,
        report_context: dict[str, Any],
        decisions: list[StatementDecision],
    ) -> tuple[ReportQualityDecision, object]:
        """Check whole-report requirements and non-blocking organization reminders."""

        quality_context = {
            **report_context,
            "statement_checks": [
                {
                    "statement_id": decision.statement_id,
                    "kind": decision.kind,
                    "status": decision.status,
                    "reason": decision.reason,
                }
                for decision in decisions
            ],
        }
        messages = report_quality_messages(quality_context)
        valid_statement_ids = {
            str(statement["statement_id"])
            for scope in report_context.get("scopes", [])
            for paragraph in scope.get("paragraphs", [])
            for statement in paragraph.get("statements", [])
        }
        valid_paragraph_ids = {
            str(paragraph["paragraph_id"])
            for scope in report_context.get("scopes", [])
            for paragraph in scope.get("paragraphs", [])
        }
        attempts: list[dict[str, object]] = []
        parse_errors: list[str] = []
        attempt_messages = messages
        for attempt_index in range(2):
            content, finish_reason = self._complete(attempt_messages)
            attempt: dict[str, object] = {
                "content": content,
                "finish_reason": finish_reason,
            }
            attempts.append(attempt)
            contract_failure: str | None = None
            if finish_reason == "length":
                failure = "answer was cut off"
            elif not content.strip():
                failure = "empty content"
            else:
                try:
                    decision = _QUALITY_ADAPTER.validate_python(
                        json.loads(_strip_code_fences(content))
                    )
                except (ValidationError, TypeError, ValueError) as exc:
                    failure = str(exc)
                else:
                    reported_ids = {
                        statement_id
                        for finding in [*decision.requirement_failures, *decision.reminders]
                        for statement_id in finding.statement_ids
                    }
                    unexpected = sorted(reported_ids - valid_statement_ids)
                    reported_paragraph_ids = {
                        paragraph_id
                        for finding in decision.requirement_failures
                        for paragraph_id in finding.paragraph_ids
                    }
                    unexpected_paragraphs = sorted(reported_paragraph_ids - valid_paragraph_ids)
                    if not unexpected and not unexpected_paragraphs:
                        return decision, {"attempts": attempts}
                    contract_failure = "report review locations must exist in the report; "
                    if unexpected:
                        contract_failure += f"unexpected statement_ids: {unexpected}. "
                    if unexpected_paragraphs:
                        contract_failure += f"unexpected paragraph_ids: {unexpected_paragraphs}."
                    failure = contract_failure
            attempt["parse_error"] = failure
            parse_errors.append(failure)
            if attempt_index == 0:
                attempt_messages = _retry_messages(
                    messages,
                    contract_failure=contract_failure,
                )

        raise ReportVerifierOutputError(
            "invalid report quality decision: "
            f"first attempt failed: {parse_errors[0]}; "
            f"second attempt failed: {parse_errors[1]}",
            {"attempts": attempts},
        )

    def _verify_statements(
        self,
        snapshot: ReportVerifierSnapshot,
    ) -> tuple[list[StatementDecision], dict[str, object]]:
        """Complete stage one in premise-safe waves before any whole-report review."""
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
            decision = _deterministic_derived_structure_failure(payload)
            if decision is None:
                continue
            decisions_by_id[statement_id] = decision
            raw_outputs[statement_id] = {
                "deterministic_rule": "derived_premise_grounding",
                "premise_depth": payload["premise_depth"],
            }
            remaining.pop(statement_id)

        while remaining and not errors:
            before = set(remaining)
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
                for premise in payload["premises"]:
                    premise_id = premise["statement_id"]
                    premise["passed"] = (
                        premise_id in passed_in_run
                        if premise_id in premise_index
                        else premise.get("passed", False)
                    )
                payload["premises_all_passed"] = all(
                    premise["passed"] for premise in payload["premises"]
                )
                if (
                    payload["kind"] == "derived"
                    and payload["premises"]
                    and not payload["premises_all_passed"]
                ):
                    decision = _failed_premise_decision(payload)
                    decisions_by_id[statement_id] = decision
                    raw_outputs[statement_id] = {
                        "deterministic_rule": "failed_premise",
                        "premise_depth": payload["premise_depth"],
                    }
                    remaining.pop(statement_id)
                    continue
                payloads.append(payload)

            if payloads:
                workers = min(self.max_workers, len(payloads))
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
        return ordered, raw_outputs

    def verify(self, snapshot: ReportVerifierSnapshot) -> ReportVerifierModelResult:
        """Run sentence faithfulness first, then whole-report quality.

        A stage-two-only revision reuses prior sentence decisions and only
        re-runs the whole-report review.
        """
        if snapshot.skip_statement_verification:
            ordered = list(snapshot.reused_statement_decisions)
            raw_outputs: dict[str, object] = {
                decision.statement_id: {"reused_from_prior_revision": True} for decision in ordered
            }
        else:
            ordered, raw_outputs = self._verify_statements(snapshot)
        quality = ReportQualityDecision()
        if snapshot.report_context:
            try:
                quality, raw_quality = self._verify_quality(
                    snapshot.report_context,
                    ordered,
                )
            except ReportVerifierOutputError as exc:
                raw_outputs["__report_quality__"] = exc.raw_output
                raise ReportVerifierOutputError(str(exc), raw_outputs) from exc
            raw_outputs["__report_quality__"] = raw_quality

        findings = materialize_findings(
            revision=snapshot.revision,
            round_number=snapshot.round,
            decisions=ordered,
            allowed_excerpt_ids=list(snapshot.allowed_excerpt_ids),
            requirement_failures=quality.requirement_failures,
            quality_reminders=quality.reminders,
        )
        return ReportVerifierModelResult(
            findings=findings, decisions=ordered, raw_outputs=raw_outputs
        )
