"""Environment configuration — fail fast on missing DB / S3 settings."""

from __future__ import annotations

from functools import lru_cache
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Fake tenant constants (preflight §5) — real multi-tenant isolation is M4.
DEFAULT_WORKSPACE_ID = UUID("00000000-0000-4000-8000-000000000001")
DEFAULT_USER_ID = UUID("00000000-0000-4000-8000-000000000002")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(..., alias="DATABASE_URL")
    s3_endpoint: str = Field(..., alias="S3_ENDPOINT")
    s3_access_key: str = Field(..., alias="S3_ACCESS_KEY")
    s3_secret_key: str = Field(..., alias="S3_SECRET_KEY")
    s3_bucket: str = Field(..., alias="S3_BUCKET")
    prospector_api_token: str = Field(default="dev-token-change-me", alias="PROSPECTOR_API_TOKEN")

    # Placeholders — not required for M0 runtime paths
    prospector_api_url: str = Field(default="http://localhost:8000", alias="PROSPECTOR_API_URL")
    prospector_llm_base_url: str = Field(default="", alias="PROSPECTOR_LLM_BASE_URL")
    prospector_llm_api_key: str = Field(default="", alias="PROSPECTOR_LLM_API_KEY")
    prospector_llm_model_strong: str = Field(
        default="qwen3.7-max",
        alias="PROSPECTOR_LLM_MODEL_STRONG",
    )
    prospector_llm_model_mid: str = Field(
        default="qwen3.7-plus",
        alias="PROSPECTOR_LLM_MODEL_MID",
    )
    exa_api_key: str = Field(default="", alias="EXA_API_KEY")
    pageindex_root: str = Field(default="", alias="PAGEINDEX_ROOT")
    step_b_sleep_seconds: float = Field(default=0.0, alias="PROSPECTOR_STEP_B_SLEEP_SECONDS")

    workspace_id: UUID = DEFAULT_WORKSPACE_ID
    user_id: UUID = DEFAULT_USER_ID


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def clear_settings_cache() -> None:
    get_settings.cache_clear()
