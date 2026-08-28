"""Scope agent: clarify once when necessary, then expand the research question."""

from __future__ import annotations

import json
import re

from openai import OpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from prospector.agents.llm import (
    get_openai_client,
    mid_model,
    no_thinking_extra_body,
    strong_model,
    thinking_extra_body,
)
from prospector.agents.prompts.scope import clarify_prompt, write_brief_prompt
from prospector.agents.streaming import stream_text
from prospector.agents.usage import record_response_usage
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
    """Ask for a JSON object. Transport errors propagate; there is no plain-chat retry."""
    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": user_prompt},
    ]
    log.info("llm.call", label=label, model=model)
    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=messages,
        response_format={"type": "json_object"},
        extra_body=no_thinking_extra_body(model),
    )
    record_response_usage(response, model)
    content = None
    if getattr(response, "choices", None):
        content = response.choices[0].message.content
    if not content:
        raise RuntimeError(f"empty LLM response for {label}")
    return content


def _repair_brief_prompt(broken_output: str) -> str:
    schema = json.dumps(ResearchBrief.model_json_schema(), ensure_ascii=False)
    return f"""下面是一段本应为合法 JSON 的模型输出，但解析或校验失败。
请把它修复为符合 JSON Schema 的单个 JSON 对象后输出。
只允许修复语法和结构（引号、逗号、字段名、包裹层级、去除多余文本），
不得改写研究内容或补充新事实。

JSON Schema：
{schema}

待修复输出：
{broken_output}

只输出修复后的 JSON 对象，不要任何其他文本。"""


def _stream_brief_json(
    client: OpenAI,
    *,
    model: str,
    user_prompt: str,
) -> str:
    """Thinking-mode Brief: stream without response_format, then parse JSON.

    供应商约束（百炼「思考模式模型如何结构化输出」、错误码
    ``Json mode response is not supported when enable_thinking is true`` /
    ``parameter.enable_thinking only support stream call``）：深度思考必须以
    stream=True 调用，且不能依赖 response_format。解析失败时按文档两步法，
    用关闭思考的 json_object 调用修复一次。
    """
    log.info("llm.call", label="research_brief", model=model)
    content = stream_text(
        client,
        agent="scope",
        model=model,
        messages=[{"role": "user", "content": user_prompt}],
        temperature=0.0,
        extra_body=thinking_extra_body(model),
    )
    if not content.strip():
        raise RuntimeError("empty LLM response for research_brief")
    return content


def _repair_brief_json(client: OpenAI, *, model: str, broken_output: str) -> str:
    response = client.chat.completions.create(
        model=model,
        temperature=0.0,
        messages=[{"role": "user", "content": _repair_brief_prompt(broken_output)}],
        response_format={"type": "json_object"},
        extra_body=no_thinking_extra_body(model),
    )
    record_response_usage(response, model)
    if not getattr(response, "choices", None):
        return ""
    return response.choices[0].message.content or ""


def _parse_research_brief(content: str, *, client: OpenAI, repair_model: str) -> ResearchBrief:
    try:
        return _parse_json_model(content, ResearchBrief)
    except (ValidationError, TypeError, ValueError, json.JSONDecodeError):
        repaired = _repair_brief_json(client, model=repair_model, broken_output=content)
        return _parse_json_model(repaired, ResearchBrief)


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
    content = _stream_brief_json(
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
    )
    log.info("scope.brief", effort=effort, language=language, question_len=len(text))
    brief = _parse_research_brief(content, client=openai, repair_model=model_name)
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
    model_name = model or strong_model(settings)

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
