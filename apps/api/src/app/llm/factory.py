"""Provider factory.

`get_chat_model` returns a LangChain `BaseChatModel` for a given provider. Using
LangChain's chat-model interface means the rest of the app (and the LangGraph
nodes) is provider-agnostic — swapping Claude for OpenAI or Gemini is a config
change, not a code change.

To add a provider:
  1. Add it to `Provider` in config.py.
  2. Add a builder function below and register it in `_BUILDERS`.
  3. Add its default model + API-key setting in config.py.
"""

from __future__ import annotations

from typing import Callable

from langchain_core.language_models.chat_models import BaseChatModel

from ..config import Provider, get_settings


def _with_key(kwargs: dict, name: str, value: str | None) -> dict:
    """Only pass the key when we have one, so the LangChain integration can fall
    back to its own environment-variable lookup (e.g. ANTHROPIC_API_KEY)."""
    if value:
        kwargs[name] = value
    return kwargs


def _build_claude(model: str | None, **kwargs) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    settings = get_settings()
    return ChatAnthropic(
        model=model or settings.claude_model,
        **_with_key(kwargs, "api_key", settings.anthropic_api_key),
    )


def _build_openai(model: str | None, **kwargs) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    settings = get_settings()
    return ChatOpenAI(
        model=model or settings.openai_model,
        **_with_key(kwargs, "api_key", settings.openai_api_key),
    )


def _build_gemini(model: str | None, **kwargs) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    settings = get_settings()
    return ChatGoogleGenerativeAI(
        model=model or settings.gemini_model,
        **_with_key(kwargs, "google_api_key", settings.google_api_key),
    )


_BUILDERS: dict[Provider, Callable[..., BaseChatModel]] = {
    "claude": _build_claude,
    "openai": _build_openai,
    "gemini": _build_gemini,
}


def get_chat_model(
    provider: Provider | None = None,
    model: str | None = None,
    **kwargs,
) -> BaseChatModel:
    """Return a chat model for `provider` (defaults to settings.default_provider).

    `model` overrides the provider's default model. Extra kwargs (temperature,
    max_tokens, ...) are forwarded to the underlying LangChain integration.
    """
    settings = get_settings()
    provider = provider or settings.default_provider

    builder = _BUILDERS.get(provider)
    if builder is None:
        supported = ", ".join(sorted(_BUILDERS))
        raise ValueError(f"Unsupported provider {provider!r}. Supported: {supported}.")

    return builder(model, **kwargs)
