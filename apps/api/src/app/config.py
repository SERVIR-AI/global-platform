"""Application settings, loaded from the environment (and an optional .env file).

Everything here is overridable via environment variables, so the same image runs
locally and in production without code changes.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Provider = Literal["claude", "openai", "gemini"]
# Vertex is embeddings-only here: it serves them on its native endpoint and
# authenticates with short-lived tokens, so it is not an OpenAI-compat chat provider.
EmbeddingProvider = Literal["claude", "openai", "gemini", "vertex", "onprem"]

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
    # Origins allowed to call the API from a browser. Accepts `*`, a
    # comma-separated list, or a JSON array.
    # localhost and 127.0.0.1 are DIFFERENT origins to a browser: omitting either
    # blocks the trust chrome's resolver fetch, and it fails closed to "unverified".
    # Deployed, '*' is legitimate: a consuming app on ITS own origin has to be able
    # to resolve our receipts.
    # NoDecode: without it pydantic-settings JSON-decodes the env value before any
    # validator runs, so CORS_ORIGINS=* crashes the app at import rather than
    # reaching the parser below.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
        ]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v):
        """Accept `*`, a comma-separated list, or a JSON array. Every one of these
        is a plausible thing to type into a deploy config, and the wrong guess is a
        crash at import rather than a bad value."""
        if not isinstance(v, str):
            return v
        v = v.strip()
        if v.startswith("["):
            return json.loads(v)
        return [o.strip() for o in v.split(",") if o.strip()]

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

    # --- Food security ---
    # Hub-adjustable crop calendars (the Call-2 ministry ask); per-request
    # overrides are cited in the brief as ADJUSTED.
    crop_calendar_path: Path = _REPO_ROOT / "conf" / "crop_calendar.yml"

    # --- Food security (GEOGLAM Crop Monitor) ---
    # The CMET Global_SHP FeatureServer: per-region crop-condition expert
    # assessments, one layer per month. Layer ids are discovered live, never derived.
    cropmonitor_url: str = (
        "https://data.cropmonitor.org/arcgis/rest/services/CMET/Global_SHP/FeatureServer")
    # Hours a cached service response stays fresh (0 disables caching). Assessments
    # are monthly, so a long TTL keeps repeat demo runs off the service entirely.
    cropmonitor_cache_ttl_hours: float = 24.0
    # The host sends a leaf-only TLS chain, so plain certifi can't build a path.
    # This published intermediate is appended to the certifi bundle at runtime to
    # complete the chain; if the file is absent, plain certifi is used. Setting
    # CROPMONITOR_VERIFY_TLS=false is the last-ditch fallback, never the default.
    cropmonitor_ca_extra: Path = _REPO_ROOT / "conf" / "cropmonitor_ca.pem"
    cropmonitor_verify_tls: bool = True

    # --- RAG engine (the shared document library) ---
    # Embeddings go through a provider's OpenAI-compat /embeddings endpoint.
    # Separate from default_provider because claude serves no embeddings; swap
    # the backend entirely by handing Corpus any object with .embed(texts).
    embedding_provider: EmbeddingProvider = "gemini"
    embedding_model: str = "gemini-embedding-001"
    # Truncate the embedding to this width (Vertex `outputDimensionality`). Pinning
    # 1536 keeps the stored matrix the same size as the previous corpus instead of
    # doubling it; 0/None means "whatever the model returns".
    embedding_dimensions: int | None = 1536

    # --- Vertex AI (embeddings) ---
    # Project defaults to whatever ADC resolves to (the runtime service account when
    # deployed), so this only needs setting when they differ.
    vertex_project: str | None = None
    vertex_location: str = "us-central1"
    # Cosine floor below which retrieval returns nothing and the caller declines
    # ("no relevant document") — never a weak match dressed up as an answer.
    #
    # THE FLOOR IS MODEL-SPECIFIC AND MUST BE RE-MEASURED WHEN THE EMBEDDER CHANGES.
    # Measured 2026-07-28 against the food-security corpus on
    # vertex:gemini-embedding-001 @1536d — in-corpus top-1 0.744-0.789, off-corpus
    # 0.505-0.607, so 0.68 sits in the gap. The previous 0.5 was calibrated for
    # openai:text-embedding-3-small, whose range runs lower; left at 0.5 under
    # Gemini, 3 of 5 deliberately off-topic queries came back ABOVE the floor —
    # i.e. the decline guarantee silently stops working.
    # Still a small sample: widen it when the corpus grows (A4).
    rag_min_relevance: float = 0.68

    # --- LLM defaults ---
    # Which provider to use when a request doesn't specify one.
    default_provider: Provider = "gemini"

    # Default model per provider; a request may override `model` per call.
    claude_model: str = "claude-opus-4-8"
    openai_model: str = "gpt-4o"
    gemini_model: str = "gemini-3.1-flash-lite"

    # Each provider is reached through its OpenAI-compatible endpoint, so it's one
    # SDK with a different base_url per provider. Override to add a custom endpoint.
    claude_base_url: str = "https://api.anthropic.com/v1/"
    openai_base_url: str = "https://api.openai.com/v1"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    # A locally hosted OpenAI-compatible server. Nothing leaves the network when this
    # is the configured embedding provider.
    onprem_base_url: str = "http://127.0.0.1:8081/v1"
    onprem_model: str = "BAAI/bge-small-en-v1.5"

    # --- Figma (live design tokens) ---
    # Read-only. Variables API needs Enterprise; on this plan we read the document
    # tree instead, so mcp/figma.py reports what it cannot resolve.
    figma_token: str | None = None
    figma_file_key: str = "Fq6ffqege1xqUeCDyLYTfK"

    # --- Provider credentials ---
    # Only the provider actually requested needs its key set.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    google_api_key: str | None = None

    # USD per million tokens, for the per-query cost line in the trace. 0 = unpriced.
    price_in: float = 0.0
    price_out: float = 0.0


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance — import this, don't construct Settings() directly."""
    return Settings()
