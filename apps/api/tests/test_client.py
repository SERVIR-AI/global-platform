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


def test_missing_key_raises_clear_error(monkeypatch):
    _keyless(monkeypatch)
    with pytest.raises(client.MissingAPIKey) as exc:
        client.build_client("gemini")
    assert "GOOGLE_API_KEY" in str(exc.value)


def test_build_client_with_key(monkeypatch):
    s = _keyless(monkeypatch)
    monkeypatch.setattr(s, "google_api_key", "test-key")
    c = client.build_client("gemini")
    assert str(c.base_url).rstrip("/") == s.gemini_base_url.rstrip("/")


def test_default_base_urls_match_docs():
    s = Settings(_env_file=None)
    assert s.openai_base_url == "https://api.openai.com/v1"
    assert s.claude_base_url == "https://api.anthropic.com/v1/"
    assert s.gemini_base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"


def test_default_model_wiring():
    from app.config import get_settings
    s = get_settings()
    assert default_model("claude") == s.claude_model
    assert default_model("openai") == s.openai_model
    assert default_model("gemini") == s.gemini_model
