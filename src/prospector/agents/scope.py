"""Scope agent: clarify once when necessary, then expand the research question."""

from __future__ import annotations

import json
import re

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from prospector.agents.llm import get_openai_client, mid_model, no_thinking_extra_body
from prospector.agents.prompts.scope import clarify_prompt, write_brief_prompt
from prospector.config import Settings
from prospector.obs.logging import get_logger
from prospector.schemas.brief import (
    ClarifyDecision,
    EffortLevel,
    ResearchBrief,
    ScopeOutcome,
)

log = get_logger("prospector.scope")

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _strip_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = _FENCE_RE.sub("", cleaned).strip()
    return cleaned


def _parse_json_model[T: BaseModel](text: str, model_type: type[T]) -> T:
    payload = json.loads(_strip_fences(text))
    return model_type.model_validate(payload)


def _normalize_clarification(
    question: str | None,
    answer: str | None,
) -> tuple[str, str] | None:
    if question is None and answer is None:
        return None
    if question is None or answer is None:
        raise ValueError(
            "clarification_question and clarification_answer must be provided together"
        )
    clarified_question = question.strip()
    clarified_answer = answer.strip()
    if not clarified_question:
        raise ValueError("clarification_question must not be blank")
    if not clarified_answer:
        raise ValueError("clarification_answer must not be blank")
    return clarified_question, clarified_answer


def _chat_json(
    client: OpenAI,
    *,
    model: str,
    user_prompt: str,
    label: str,
) -> str:
    """Ask for a JSON object. Prefer json_object mode; fall back to plain chat."""
    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": user_prompt},
    ]
    log.info("llm.call", label=label, model=model)
    try:
        response = client.chat.completions.create(
            model=model,
            temperature=0.0,
            messages=messages,
            response_format={"type": "json_object"},
            extra_body=no_thinking_extra_body(model),
        )
    except Exception:
        log.debug("llm.call.json_object_fallback", label=label, model=model)
        response = client.chat.completions.create(
            model=model,
            temperature=0.0,
            messages=messages,
            extra_body=no_thinking_extra_body(model),
        )
    content = None
    if getattr(response, "choices", None):
        content = response.choices[0].message.content
    if not content:
        raise RuntimeError(f"empty LLM response for {label}")
    usage = getattr(response, "usage", None)
    if usage:
        log.debug("llm.call.tokens", label=label, total_tokens=usage.total_tokens)
    return content


def decide_clarification(
    question: str,
    *,
    client: OpenAI | None = None,
    model: str | None = None,
    settings: Settings | None = None,
) -> ClarifyDecision:
    log.info("scope.clarify", question_len=len(question))
    openai = client or get_openai_client(settings)
    model_name = model or mid_model(settings)
    raw = _chat_json(
        openai,
        model=model_name,
        user_prompt=clarify_prompt(question),
        label="clarify_decision",
    )
    decision = _parse_json_model(raw, ClarifyDecision)
    next_step = "clarify" if decision.need_clarification else "write_brief"
    log.info(
        "scope.decision",
        need_clarification=decision.need_clarification,
        next=next_step,
    )
    return decision


def write_research_brief(
    question: str,
    *,
    clarification_question: str | None = None,
    clarification_answer: str | None = None,
    assessment: str | None = None,
    previous_brief: ResearchBrief | None = None,
    revision_note: str | None = None,
    language: str = "zh",
    effort: EffortLevel = "standard",
    client: OpenAI | None = None,
    model: str | None = None,
    settings: Settings | None = None,
) -> ResearchBrief:
    text = question.strip()
    if not text:
        raise ValueError("question must not be blank")
    clarification = _normalize_clarification(clarification_question, clarification_answer)
    if (previous_brief is None) != (revision_note is None):
        raise ValueError("previous_brief and revision_note must be provided together")
    if revision_note is not None and not revision_note.strip():
        raise ValueError("revision_note must not be blank")

    openai = client or get_openai_client(settings)
    model_name = model or mid_model(settings)
    clarified_question, clarified_answer = clarification or (None, None)
    raw = _chat_json(
        openai,
        model=model_name,
        user_prompt=write_brief_prompt(
            text,
            clarification_question=clarified_question,
            clarification_answer=clarified_answer,
            assessment=assessment,
            previous_brief=previous_brief,
            revision_note=revision_note,
            language=language,
            effort=effort,
        ),
        label="research_brief",
    )
    log.info("scope.brief", effort=effort, language=language, question_len=len(text))
    brief = _parse_json_model(raw, ResearchBrief)
    # Honor caller effort unless the model already set a valid one from prompt.
    if brief.effort != effort:
        brief = brief.model_copy(update={"effort": effort})
    log.info("scope.brief", result="done", brief_len=len(brief.brief_text))
    return brief


def run_scope(
    question: str,
    *,
    clarification_question: str | None = None,
    clarification_answer: str | None = None,
    language: str = "zh",
    effort: EffortLevel = "standard",
    client: OpenAI | None = None,
    model: str | None = None,
    settings: Settings | None = None,
) -> ScopeOutcome:
    """Ask at most once, or expand the original question plus its clarification."""
    text = question.strip()
    if not text:
        raise ValueError("question must not be blank")
    clarification = _normalize_clarification(clarification_question, clarification_answer)

    log.info(
        "scope.run",
        question_len=len(text),
        effort=effort,
        language=language,
        has_clarification=clarification is not None,
    )

    openai = client or get_openai_client(settings)
    model_name = model or mid_model(settings)

    if clarification is not None:
        clarified_question, clarified_answer = clarification
        brief = write_research_brief(
            text,
            clarification_question=clarified_question,
            clarification_answer=clarified_answer,
            language=language,
            effort=effort,
            client=openai,
            model=model_name,
            settings=settings,
        )
        return ScopeOutcome(kind="brief_pending", brief=brief)

    decision = decide_clarification(text, client=openai, model=model_name, settings=settings)
    if decision.need_clarification:
        return ScopeOutcome(
            kind="clarify",
            clarification_question=decision.question.strip(),
        )

    brief = write_research_brief(
        text,
        assessment=decision.assessment,
        language=language,
        effort=effort,
        client=openai,
        model=model_name,
        settings=settings,
    )
    return ScopeOutcome(kind="brief_pending", brief=brief)
