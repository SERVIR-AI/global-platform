"""Application settings, loaded from the environment (and an optional .env file).

Everything here is overridable via environment variables, so the same image runs
locally and in production without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["claude", "openai", "gemini"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service ---
    app_name: str = "retreat-platform-api"
    # Origins allowed to call the API from a browser. Override in prod with a
    # comma-separated env var, e.g. CORS_ORIGINS='["https://app.example.com"]'.
    cors_origins: list[str] = Field(
        default=["http://localhost:5173", "http://localhost:8080", "http://127.0.0.1:8080"]
    )

    # --- LLM defaults ---
    # Which provider to use when a request doesn't specify one.
    default_provider: Provider = "claude"

    # Default model per provider; a request may override `model` per call.
    claude_model: str = "claude-opus-4-8"
    openai_model: str = "gpt-4o"
    gemini_model: str = "gemini-2.0-flash"

    # Optional system prompt prepended to every conversation.
    system_prompt: str | None = None

    # --- Provider credentials ---
    # These are read by the LangChain integrations. Set whichever providers you use.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    google_api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance — import this, don't construct Settings() directly."""
    return Settings()
