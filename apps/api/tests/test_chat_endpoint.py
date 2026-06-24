"""The /api/chat contract: graceful 4xx on a missing/invalid provider, and a full
round trip (with a stub client) returning a grounded answer + echoed provider.
"""
from fastapi.testclient import TestClient

from app.config import get_settings
from app.graph import graph as gm
from app.graph.geo import store
from app.main import app

client = TestClient(app)


def test_missing_key_returns_400(monkeypatch, log):
    """A request for a provider with no key returns 400 (not 500) with a clear message."""
    monkeypatch.setattr(get_settings(), "google_api_key", None)
    log("REQUEST", "POST /api/chat provider=gemini  (GOOGLE_API_KEY unset)")
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "provider": "gemini"})
    log("STATUS", r.status_code)
    log("DETAIL", r.json()["detail"])
    log("CHECK", "status 400; detail names GOOGLE_API_KEY")
    assert r.status_code == 400
    assert "GOOGLE_API_KEY" in r.json()["detail"]


def test_invalid_provider_returns_422(log):
    """An unknown provider is rejected by request validation (422), per the schema's Literal."""
    log("REQUEST", "POST /api/chat provider=bogus")
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}], "provider": "bogus"})
    log("STATUS", r.status_code)
    log("CHECK", "status 422 (schema validation)")
    assert r.status_code == 422


def test_round_trip_with_stub(aoi, make_client, monkeypatch, log):
    """End-to-end through the endpoint (stub LLM, fixture geo): grounded answer + echoed provider + usage."""
    from app.api.routes import chat as chat_route
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda place: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda place, layer: aoi[layer])
    stub = make_client(("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    monkeypatch.setattr(chat_route, "build_client", lambda provider: stub)
    expected = store.roads_in_hazard(aoi, "hazard_flood")["length_km"]

    log("REQUEST", "POST /api/chat provider=gemini  'flooded roads in Testville?'")
    log("OPERATE", f"real store.roads_in_hazard -> {expected} km")
    r = client.post("/api/chat", json={
        "messages": [{"role": "user", "content": "flooded roads in Testville?"}], "provider": "gemini"})
    body = r.json()
    log("STATUS", r.status_code)
    log("BODY", body)
    log("CHECK", f"200; provider echoed 'gemini'; answer quotes {expected}; usage + thread_id present")
    assert r.status_code == 200
    assert body["provider"] == "gemini"
    assert str(expected) in body["message"]["content"]
    assert body["usage"]["total_tokens"] > 0
    assert body["thread_id"]
