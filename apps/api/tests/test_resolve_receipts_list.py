"""A living page lists what was already minted for an area instead of computing:
store.list_receipts and the read-only /api/resolve/receipts and /places routes."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import receipts as routes
from app.graph.geo import ingest
from app.mcp import store


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "grp.db")
    store.init()
    a = store.save_receipt({"question": "mangrove in Chomes?", "pack": "land-cover",
                            "target": {"place": "Chomes district", "year": "2020"}, "passed": True,
                            "minted_at": "2026-10-20T10:00:00+00:00"})
    b = store.save_receipt({"question": "change since 1996?", "pack": "land-cover",
                            "target": {"place": "chomes  DISTRICT", "year": "1996", "year_to": "2020"},
                            "passed": True, "minted_at": "2026-10-20T11:00:00+00:00"})
    store.save_receipt({"question": "flood in Bangkok?", "pack": "risk",
                        "target": {"place": "Bangkok", "hazard": "flood"}, "passed": True})
    store.save_receipt({"question": "a preview", "pack": "land-cover",
                        "target": {"place": "Chomes district"}, "passed": True, "staged_by": "someone"})
    return a, b


def test_receipts_for_an_area_match_the_name_loosely_and_never_list_previews(db):
    rows = store.list_receipts(place="Chomes District")
    assert [r["question"] for r in rows] == ["change since 1996?", "mangrove in Chomes?"]
    assert all(r["pack"] == "land-cover" and r["receipt_id"] for r in rows)
    assert store.list_receipts(place="Chomes district", pack="risk") == []
    assert [r["pack"] for r in store.list_receipts(pack="risk")] == ["risk"]
    assert len(store.list_receipts(limit=1)) == 1


def test_the_routes_serve_the_list_from_any_origin(db, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    monkeypatch.setattr(ingest, "list_boundaries",
                        lambda layer=None: {"status": "ok", "layers": [{"layer": layer or "x", "names": []}]})
    with TestClient(app) as c:
        r = c.get("/api/resolve/receipts", params={"place": "Chomes district"})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
        body = r.json()
        assert body["status"] == "ok" and len(body["receipts"]) == 2
        empty = c.get("/api/resolve/receipts", params={"place": "Nowhere"}).json()
        assert empty["status"] == "empty" and "no receipt names this area" in empty["note"]
        places = c.get("/api/resolve/places", params={"layer": "sprint_areas"}).json()
        assert places["layers"][0]["layer"] == "sprint_areas"
