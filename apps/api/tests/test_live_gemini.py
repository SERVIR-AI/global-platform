"""Live round trip against Gemini's OpenAI-compatible endpoint. Skipped unless
GOOGLE_API_KEY is configured. Geo is stubbed (fixture AOI) so this exercises the
real route + finalize LLM calls without depending on OSM, and confirms the answer
is grounded in the computed number.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.graph import graph as gm
from app.graph.geo import store
from app.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().google_api_key, reason="GOOGLE_API_KEY not configured")


def test_live_gemini_round_trip(aoi, monkeypatch, log):
    """Real Gemini call: route + finalize hit the live API; the answer must quote operate's number."""
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda place, layer: aoi[layer])
    expected = store.roads_in_hazard(aoi, "hazard_flood")["length_km"]

    log("REQUEST", "POST /api/chat provider=gemini  'How many km of road are flooded in Riverford?'")
    log("OPERATE", f"real store.roads_in_hazard -> {expected} km (geo stubbed; LLM is live)")
    r = TestClient(app).post("/api/chat", json={
        "messages": [{"role": "user", "content": "How many kilometres of road are flooded in Riverford?"}],
        "provider": "gemini"})
    body = r.json()
    log("STATUS", r.status_code)
    log("MODEL", f"{body['model']}  usage={body['usage']}")
    log("ANSWER", body["message"]["content"])
    log("CHECK", f"200; real Gemini answer quotes {expected} (grounded, not invented)")
    assert r.status_code == 200, r.text
    assert body["provider"] == "gemini"
    assert body["message"]["content"].strip()
    assert body["usage"]["total_tokens"] > 0
    assert str(expected) in body["message"]["content"]
