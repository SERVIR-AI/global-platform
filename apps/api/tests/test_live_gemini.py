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


def test_live_gemini_round_trip(aoi, monkeypatch, capsys):
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda place: aoi)
    monkeypatch.setattr(gm.ingest, "source_raster", lambda layer="hazard_flood": "x")
    expected = store.roads_in_flood(aoi)["length_km"]

    r = TestClient(app).post("/api/chat", json={
        "messages": [{"role": "user", "content": "How many kilometres of road are flooded in Riverford?"}],
        "provider": "gemini"})

    assert r.status_code == 200, r.text
    body = r.json()
    with capsys.disabled():
        print(f"\n[live gemini] model={body['model']} usage={body['usage']}")
        print(f"[live gemini] answer: {body['message']['content']}")
    assert body["provider"] == "gemini"
    assert body["message"]["content"].strip()
    assert body["usage"]["total_tokens"] > 0
    assert str(expected) in body["message"]["content"]   # grounded in the real number
