"""Build an OpenAI SDK client for the requested provider.

Every provider is reached through its OpenAI-compatible endpoint, so it's one SDK
with a different (base_url, api_key, default_model) per provider — chosen by the
`provider` field on each request. Adding a new endpoint is a settings change, not
a code change.
"""
from __future__ import annotations

from dataclasses import dataclass

from openai import OpenAI

from ..config import Provider, get_settings


class MissingAPIKey(Exception):
    """The requested provider has no API key configured. Surfaced as a 4xx."""


@dataclass(frozen=True)
class _Row:
    base_url: str
    api_key: str | None
    default_model: str
    key_env: str  # the env var to name in the error when the key is missing


def _registry() -> dict[Provider, _Row]:
    """Build a mapping of each Provider to its connection details from settings.

    Returns:
        dict[Provider, _Row]: per-provider base_url, api_key, default_model, and key_env name
    """
    s = get_settings()
    return {
        "claude": _Row(s.claude_base_url, s.anthropic_api_key, s.claude_model, "ANTHROPIC_API_KEY"),
        "openai": _Row(s.openai_base_url, s.openai_api_key, s.openai_model, "OPENAI_API_KEY"),
        "gemini": _Row(s.gemini_base_url, s.google_api_key, s.gemini_model, "GOOGLE_API_KEY"),
    }


def default_model(provider: Provider) -> str:
    """Return the configured default model name for a provider.

    Args:
        provider (Provider): one of 'claude', 'openai', 'gemini'

    Returns:
        str: model identifier string (e.g. 'gemini-2.0-flash')
    """
    return _registry()[provider].default_model


def build_client(provider: Provider) -> OpenAI:
    """Build an OpenAI-compatible client for the given provider.

    All providers share the same OpenAI SDK; only base_url and api_key differ.

    Args:
        provider (Provider): one of 'claude', 'openai', 'gemini'

    Returns:
        openai.OpenAI: configured client pointed at the provider's endpoint

    Raises:
        MissingAPIKey: if the provider's API key environment variable is not set
    """
    row = _registry()[provider]
    if not row.api_key:
        raise MissingAPIKey(
            f"No API key configured for provider {provider!r} — set {row.key_env}"
        )
    return OpenAI(base_url=row.base_url, api_key=row.api_key)
