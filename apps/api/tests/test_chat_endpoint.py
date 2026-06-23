"""The /api/chat contract: graceful 4xx on a missing/invalid provider, and a full
round trip (with a stub client) returning a grounded answer + echoed provider.
"""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.graph import graph as gm
from app.graph.geo import store
from app.main import app

client = TestClient(app)


def test_missing_key_returns_400(monkeypatch):
    monkeypatch.setattr(get_settings(), "google_api_key", None)
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "provider": "gemini"})
    assert r.status_code == 400
    assert "GOOGLE_API_KEY" in r.json()["detail"]


def test_invalid_provider_returns_422():
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "provider": "bogus"})
    assert r.status_code == 422


def test_round_trip_with_stub(aoi, make_client, monkeypatch):
    from app.api.routes import chat as chat_route
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda place: aoi)
    monkeypatch.setattr(gm.ingest, "source_raster", lambda layer="hazard_flood": "x")
    stub = make_client(("tool", "roads_in_flood", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    monkeypatch.setattr(chat_route, "build_client", lambda provider: stub)
    expected = store.roads_in_flood(aoi)["length_km"]

    r = client.post("/api/chat", json={
        "messages": [{"role": "user", "content": "flooded roads in Testville?"}], "provider": "gemini"})
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "gemini"
    assert str(expected) in body["message"]["content"]
    assert body["usage"]["total_tokens"] > 0
    assert body["thread_id"]
