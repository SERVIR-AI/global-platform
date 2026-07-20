"""A11.1 — the real thing: real Claude, real embeddings, the real seeded corpus,
the real Crop Monitor feed. Opt-in: SYNTHESIS_LIVE=1 (needs both provider keys
and the seeded corpus in cache/)."""

import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

pytestmark = pytest.mark.skipif(
    os.environ.get("SYNTHESIS_LIVE", "").lower() not in ("1", "true", "yes")
    or not get_settings().anthropic_api_key or not get_settings().openai_api_key
    or not (get_settings().cache_dir / "rag" / "food-security").exists(),
    reason="set SYNTHESIS_LIVE=1 (needs Anthropic + OpenAI keys AND the seeded corpus: "
           "python -m app.food_security.seed_corpus)")


def test_live_el_nino_brief(log):
    """The demo question, end to end: structured, cited, caveated — and grounded."""
    r = TestClient(app).post("/api/food-security/chat", json={
        "question": ("A strong El Nino is developing — what should I expect for maize "
                     "in Zambia this season, and how much should I trust it?"),
        "verbose": True})
    body = r.json()
    assert r.status_code == 200, r.text
    log("DECLINED", body["declined"])
    assert body["declined"] is False, body.get("decline_reason")
    log("MODEL", f"{body['provider']}:{body['model']}  usage={body['usage']}")
    log("GROUNDED", body["grounded"])
    log("EVIDENCE", body["evidence"])
    for line in body["brief"].splitlines():
        log("", line)
    assert body["grounded"]["passed"]
    assert body["evidence"]["forecast_hits"] >= 1
    assert body["evidence"]["retrospective_hits"] >= 1
    assert len(body["citations"]) >= 3
    assert "## Sources" in body["brief"]
