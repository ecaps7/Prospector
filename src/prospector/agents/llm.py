"""OpenAI-compatible LLM client factory (preflight §4.1)."""

from __future__ import annotations

from openai import OpenAI

from prospector.config import Settings, get_settings


class LlmNotConfiguredError(RuntimeError):
    """Raised when LLM base URL or API key is missing."""


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
        timeout=120.0,
    )


def mid_model(settings: Settings | None = None) -> str:
    cfg = require_llm_settings(settings)
    return cfg.prospector_llm_model_mid
