"""OpenAI-compatible LLM client factory (preflight §4.1)."""

from __future__ import annotations

import httpx
from openai import AsyncOpenAI, OpenAI

from prospector.config import Settings, get_settings


class LlmNotConfiguredError(RuntimeError):
    """Raised when LLM base URL or API key is missing."""


LLM_TIMEOUT = httpx.Timeout(
    connect=10.0,
    read=600.0,
    write=120.0,
    pool=120.0,
)


def _model_family(model: str) -> str:
    normalized = model.strip().lower()
    if "deepseek" in normalized:
        return "deepseek"
    if "qwen" in normalized:
        return "qwen"
    raise ValueError(
        f"Unsupported LLM model family for {model!r}; expected a DeepSeek or Qwen model"
    )


def thinking_extra_body(model: str) -> dict[str, object]:
    """Return the provider-specific body that enables model thinking."""
    if _model_family(model) == "deepseek":
        return {"thinking": {"type": "enabled"}}
    return {"enable_thinking": True}


def no_thinking_extra_body(model: str) -> dict[str, object]:
    """Return the provider-specific body that disables model thinking."""
    if _model_family(model) == "deepseek":
        return {"thinking": {"type": "disabled"}}
    return {"enable_thinking": False}


def require_llm_settings(settings: Settings | None = None) -> Settings:
    cfg = settings or get_settings()
    if not cfg.prospector_llm_base_url.strip():
        raise LlmNotConfiguredError("PROSPECTOR_LLM_BASE_URL is required")
    if not cfg.prospector_llm_api_key.strip():
        raise LlmNotConfiguredError("PROSPECTOR_LLM_API_KEY is required")
    return cfg


def get_openai_client(settings: Settings | None = None) -> OpenAI:
    cfg = require_llm_settings(settings)
    return OpenAI(
        base_url=cfg.prospector_llm_base_url.rstrip("/"),
        api_key=cfg.prospector_llm_api_key,
        timeout=LLM_TIMEOUT,
    )


def get_async_openai_client(settings: Settings | None = None) -> AsyncOpenAI:
    cfg = require_llm_settings(settings)
    return AsyncOpenAI(
        base_url=cfg.prospector_llm_base_url.rstrip("/"),
        api_key=cfg.prospector_llm_api_key,
        timeout=LLM_TIMEOUT,
    )


def mid_model(settings: Settings | None = None) -> str:
    cfg = require_llm_settings(settings)
    return cfg.prospector_llm_model_mid


def strong_model(settings: Settings | None = None) -> str:
    cfg = require_llm_settings(settings)
    return cfg.prospector_llm_model_strong
