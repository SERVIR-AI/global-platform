"""Application settings, loaded from the environment (and an optional .env file).

Everything here is overridable via environment variables, so the same image runs
locally and in production without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["claude", "openai", "gemini"]

# Paths derived from this file's location (apps/api/src/app/config.py) so the app
# resolves config/data no matter which directory uvicorn is launched from.
_API_ROOT = Path(__file__).resolve().parents[2]   # apps/api
_REPO_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_API_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service ---
    app_name: str = "global-risk-platform-api"
    # Origins allowed to call the API from a browser. Override in prod with a
    # comma-separated env var, e.g. CORS_ORIGINS='["https://app.example.com"]'.
    cors_origins: list[str] = Field(
        default=["http://localhost:5173", "http://localhost:8080", "http://127.0.0.1:8080"]
    )

    # --- Workspace paths ---
    # conf/ and cache/ live at the repo root. Defaults are absolute (resolved from
    # _REPO_ROOT) so launch directory doesn't matter; override via env if needed.
    cache_dir: Path = _REPO_ROOT / "cache"
    tiffs_dir: Path = cache_dir / "tiffs"
    traces_dir: Path = cache_dir / "traces"
    prompts_path: Path = _REPO_ROOT / "conf" / "prompts.yml"
    tiffs_config_path: Path = _REPO_ROOT / "conf" / "tiffs.yml"
    raster_schema_path: Path = _REPO_ROOT / "conf" / "raster_schema.yml"
    risk_l2_config_path: Path = _REPO_ROOT / "conf" / "risk_l2.yml"

    # --- LLM defaults ---
    # Which provider to use when a request doesn't specify one.
    default_provider: Provider = "gemini"

    # Default model per provider; a request may override `model` per call.
    claude_model: str = "claude-opus-4-8"
    openai_model: str = "gpt-4o"
    gemini_model: str = "gemini-2.5-flash"

    # Each provider is reached through its OpenAI-compatible endpoint, so it's one
    # SDK with a different base_url per provider. Override to add a custom endpoint.
    claude_base_url: str = "https://api.anthropic.com/v1/"
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"

    # --- Provider credentials ---
    # Only the provider actually requested needs its key set.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    google_api_key: str | None = None

    # USD per million tokens, for the per-query cost line in the trace. 0 = unpriced.
    price_in: float = 0.0
    price_out: float = 0.0

    # Whether trace events carry the full text of the prompts sent to the model. Off keeps
    # the shape — one entry per message, with its role — and nulls every body, so the trace
    # still reports how many messages were sent without carrying their contents.
    trace_prompts: bool = True


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance — import this, don't construct Settings() directly."""
    return Settings()
