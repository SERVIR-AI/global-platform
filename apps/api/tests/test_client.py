"""The provider registry: missing keys fail clearly (4xx, not 500), defaults wire
through, and the OpenAI-compatible base URLs match the providers' documented ones.
"""
import pytest

from app.config import Settings
from app.llm import client, default_model


def _keyless(monkeypatch):
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    s = Settings(_env_file=None)
    monkeypatch.setattr(client, "get_settings", lambda: s)
    return s


def test_missing_key_raises_clear_error(monkeypatch, log):
    """With no key set, build_client refuses with a message naming the env var to set (-> 4xx, not 500)."""
    _keyless(monkeypatch)
    log("CALL", "client.build_client('gemini')  # no GOOGLE_API_KEY in env")
    with pytest.raises(client.MissingAPIKey) as exc:
        client.build_client("gemini")
    log("OUTPUT", f"raised MissingAPIKey: {exc.value}")
    log("CHECK", "error names GOOGLE_API_KEY")
    assert "GOOGLE_API_KEY" in str(exc.value)


def test_build_client_with_key(monkeypatch, log):
    """With a key present, build_client returns an OpenAI client pointed at the provider's base_url."""
    s = _keyless(monkeypatch)
    monkeypatch.setattr(s, "google_api_key", "test-key")
    c = client.build_client("gemini")
    log("CALL", "client.build_client('gemini')  # GOOGLE_API_KEY=test-key")
    log("OUTPUT", f"client.base_url = {c.base_url}")
    log("CHECK", f"base_url matches settings.gemini_base_url ({s.gemini_base_url})")
    assert str(c.base_url).rstrip("/") == s.gemini_base_url.rstrip("/")


def test_default_base_urls_match_docs(log):
    """The default base URLs are the providers' documented OpenAI-compatible endpoints."""
    s = Settings(_env_file=None)
    log("OUTPUT", f"openai = {s.openai_base_url}")
    log("OUTPUT", f"claude = {s.claude_base_url}")
    log("OUTPUT", f"gemini = {s.gemini_base_url}")
    log("CHECK", "all three equal the documented endpoints")
    assert s.openai_base_url == "https://api.openai.com/v1"
    assert s.claude_base_url == "https://api.anthropic.com/v1/"
    assert s.gemini_base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"


def test_default_model_wiring(log):
    """default_model(provider) returns that provider's configured default model."""
    from app.config import get_settings
    s = get_settings()
    for p in ("claude", "openai", "gemini"):
        log("OUTPUT", f"default_model({p!r}) = {default_model(p)}")
    log("CHECK", "each matches the corresponding settings.<provider>_model")
    assert default_model("claude") == s.claude_model
    assert default_model("openai") == s.openai_model
    assert default_model("gemini") == s.gemini_model
