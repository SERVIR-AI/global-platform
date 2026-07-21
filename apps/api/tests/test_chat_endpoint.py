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
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda place, layer: aoi[layer])
    stub = make_client(("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    monkeypatch.setattr(chat_route, "build_client", lambda provider: stub)
    expected = store.roads_in_hazard(aoi, "hazard_flood")["length_km"]

    log("REQUEST", "POST /api/chat provider=gemini  'flooded roads in Testville?'")
    log("OPERATE", f"real store.roads_in_hazard -> {expected} km")
    r1 = client.post("/api/chat", json={
        "messages": [{"role": "user", "content": "flooded roads in Testville?"}], "provider": "gemini"})
    tid = r1.json()["thread_id"]                          # turn 1 -> the agent asks exposure / L1 / L2
    r = client.post("/api/chat", json={
        "messages": [{"role": "user", "content": "1"}], "provider": "gemini", "thread_id": tid})  # exposure
    body = r.json()
    log("STATUS", r.status_code)
    log("BODY", body)
    log("CHECK", f"200; provider echoed 'gemini'; answer quotes {expected}; usage + thread_id present")
    assert r.status_code == 200
    assert body["provider"] == "gemini"
    assert str(expected) in body["message"]["content"]
    assert body["usage"]["total_tokens"] > 0
    assert body["thread_id"]


def test_trace_events_surfaced_in_response(aoi, make_client, monkeypatch, log):
    """Structured trace events reach the API response as trace_events — currently
    unconditional (not gated by verbose, unlike the older plain-text `trace`). This
    request needs a hazard choice, so the turn produces two steps: router, then resolve
    pausing to ask exposure/L1/L2."""
    from app.api.routes import chat as chat_route
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda place, layer: aoi[layer])
    stub = make_client(("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    monkeypatch.setattr(chat_route, "build_client", lambda provider: stub)

    r = client.post("/api/chat", json={
        "messages": [{"role": "user", "content": "flooded roads in Testville?"}], "provider": "gemini"})
    body = r.json()
    log("TRACE_EVENTS", body.get("trace_events"))
    assert r.status_code == 200
    assert body["trace_events"] is not None
    assert [e["node"] for e in body["trace_events"]] == ["router", "resolve"]
    router_event = body["trace_events"][0]
    assert router_event["kind"] == "routed"
    assert router_event["llm_provider"] == "gemini"
    assert router_event["derived_place"] == "Testville"
    assert body["trace_events"][1]["decision"] == "asked"


def test_drawn_area_followup_reuses_geometry(aoi, make_client, monkeypatch, log):
    """[endpoint] Mode 2 multi-turn: draw an area, then ask about 'the same area' with geometry=null
    (exactly what the UI sends after drawing once). The drawn AOI must persist — chat.py must not
    overwrite it with null, so the follow-up never fails with 'could not find drawn area'."""
    from app.api.routes import chat as chat_route

    def fake_aoi(place=None, geometry=None, layers=None):
        if geometry is not None:
            return aoi
        if place == "drawn area":                            # the bug: geocoding the literal string
            raise ValueError("could not find 'drawn area' (try 'City, Country')")
        return aoi
    monkeypatch.setattr(gm.ingest, "ensure_aoi", fake_aoi)
    stub = make_client(("tool", "count_features", {"layer": "schools"}))
    monkeypatch.setattr(chat_route, "build_client", lambda provider: stub)

    # Turn 1: a drawn polygon (bbox) + a question about it -> must compute (use the drawn area).
    r1 = client.post("/api/chat", json={
        "messages": [{"role": "user", "content": "schools at flood risk here?"}],
        "geometry": [103.0, 13.0, 103.5, 13.5]})
    c1 = r1.json().get("message", {}).get("content") or ""
    log("TURN 1 content", c1)
    assert r1.status_code == 200 and "count_features" in c1   # used the drawn area, did not ask for a place
    tid = r1.json()["thread_id"]

    # Turn 2: 'same area', geometry=null (the UI clears the draw after the first send).
    r2 = client.post("/api/chat", json={
        "messages": [{"role": "user", "content": "what about buildings in the same area?"}],
        "geometry": None, "thread_id": tid})
    content = r2.json().get("message", {}).get("content") or ""
    log("TURN 2 content", content)
    assert r2.status_code == 200 and "count_features" in content  # reused the drawn area
    assert "could not find" not in content and "No data" not in content
