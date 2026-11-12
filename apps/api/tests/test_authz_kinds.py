"""Approved-but-restricted visibility for the other kinds — a vector layer, a feed and a
document — enforced the way the raster catalog enforces it (tests/test_authz.py), by a
REAL OpenFGA store: the owner submits with visibility 'hub', a reviewer approves, the
owner's hub reads it through the real reading path, another hub and anonymous find
nothing, and the gate's own raw readers still hold every row. The upstreams are files on
the same real local http server the raster tests use; the only stand-in is the hash
embedder the document tests always run with (no embedding key in the suite)."""
import copy
import json
import shutil

import pytest

from app.config import get_settings
from app.contrib import staging
from app.graph.geo import vectors
from app.mcp import feeds, fetch, registry
from app.rag.embed import ProviderEmbedder
from app.rag.store import Corpus

from test_authz import A1, A2, ANON, OWNER, REVIEWER, _as, env, fga_server, file_host  # noqa: F401
from test_sprint_vectors_and_gate import RIVERS                       # the vector fixture's layer
from test_staging import TEXT as BULLETIN, HashEmbedder               # the document fixture's stand-ins
from test_staging_feeds import NOAA_TEXT as PSL_TEXT                  # the feed fixture's upstream

pytestmark = pytest.mark.skipif(shutil.which("openfga") is None,
                                reason="the openfga binary is not installed (brew install openfga)")


def _serve(file_host, name, text) -> str:
    root, base = file_host
    (root / name).write_text(text)
    return f"{base}/{name}"


@pytest.fixture
def vec_env(env, file_host, monkeypatch):
    """test_sprint_vectors_and_gate's vec_env on top of the store, the GeoJSON served for real."""
    monkeypatch.setattr(get_settings(), "grp_mattermost_webhook", "")
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    yield _serve(file_host, "rivers.geojson", json.dumps(RIVERS))
    staging.STAGED_VECTORS.clear()


@pytest.fixture
def feed_env(env, file_host, monkeypatch):
    """test_staging_feeds' env on top of the store, the PSL series served for real."""
    s = get_settings()
    monkeypatch.setattr(s, "feeds_conf_dir", env / "feeds")
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    snapshot = copy.deepcopy(registry.FEEDS)
    staging.STAGED_FEEDS.clear()
    monkeypatch.setattr(staging, "_STAGED_LOADED", False)
    yield _serve(file_host, "nina1.anom.data", PSL_TEXT)
    registry.FEEDS.clear()
    registry.FEEDS.update(snapshot)
    staging.STAGED_FEEDS.clear()


@pytest.fixture
def doc_env(env, file_host, monkeypatch):
    """test_staging's env on top of the store, the bulletin served for real."""
    s = get_settings()
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(s, "rag_min_relevance", -1.0)       # hash embedder: no real scores
    monkeypatch.setattr(ProviderEmbedder, "embed", lambda self, texts: HashEmbedder().embed(texts))
    return _serve(file_host, "bulletin.txt", BULLETIN)


def _submit(kind, manifest):
    return _as(OWNER, lambda: staging.submit(kind, {**manifest, "visibility": "hub"}, OWNER))


def _approve(cid):
    ok = _as(REVIEWER, lambda: staging.approve(cid, REVIEWER))
    assert ok["status"] == "approved", ok
    return ok


def test_a_hub_vector_layer_is_read_by_its_hub_only(vec_env, log):
    """vectors.visible is the one read seam for every answering reader (the gazetteer, the
    risk assets, the summarise step): staged, owner and reviewers; approved with
    visibility 'hub', the owner's hub; never the other hub or anonymous."""
    up = _submit("vector", {"layer": "test_rivers", "url": vec_env, "title": "Test rivers (synthetic)",
                            "description": "synthetic lines", "source": "test fixture",
                            "license": "CC0-1.0", "vintage": "2026-10", "name_field": "NOMBRE",
                            "group_field": "CATEGORIA", "countries": ["Testland"]})
    log("OUTPUT", {k: up.get(k) for k in ("status", "contribution_id", "sharing", "problems")})
    assert up["status"] == "staged" and up["sharing"]["hub"] == "mekong"
    cid = up["contribution_id"]
    sees = lambda who: "test_rivers" in _as(who, vectors.visible)
    assert sees(OWNER) and sees(REVIEWER) and not sees(A1) and not sees(A2)
    _approve(cid)
    assert vectors.landed()["test_rivers"]["contribution_id"] == cid     # the gate's raw registry keeps the id
    assert sees(OWNER) and sees(REVIEWER) and sees(A1)
    assert not sees(A2) and not sees(ANON)
    # the tool text that names contributed layers follows the same answer
    names = lambda who: [n.split(" ")[0] for n in _as(who, registry._contributed_vector_names)]
    assert "test_rivers" in names(A1) and "test_rivers" not in names(A2)


def test_a_hub_feed_is_queried_by_its_hub_only(feed_env, log):
    """feeds.query answers a hidden landed feed exactly as an unknown one, and its
    'available' list, the capabilities map and the pack readers all read the same filtered
    registry; the raw dict the gate reads keeps the row."""
    ds = "x44_nino12_sst"
    up = _submit("feed", {"dataset": ds, "title": "Nino 1+2 SST anomaly (monthly)",
                          "description": "Monthly SST anomaly in the Nino 1+2 region.",
                          "source": "NOAA PSL", "validation": "single-agency",
                          "residency": "external call-out", "cadence": "monthly",
                          "adapter": "generic_table",
                          "fetch": {"url": feed_env, "index_name": "Nino1+2",
                                    "units": "degrees C anomaly", "missing_below": -90},
                          "usage_notes": "Driver signal only; noisier than Nino 3.4."})
    log("OUTPUT", {k: up.get(k) for k in ("status", "contribution_id", "sharing", "problems")})
    assert up["status"] == "staged" and up["sharing"]["hub"] == "mekong"
    cid = up["contribution_id"]
    query = lambda who: _as(who, lambda: feeds.query(ds, {}))
    assert query(OWNER)["status"] == "ok" and query(REVIEWER)["status"] == "ok"
    assert query(A1)["status"] == "declined"
    _approve(cid)
    assert registry.FEEDS[ds]["contribution_id"] == cid                  # the gate's raw dict keeps the id
    mine = query(A1)
    assert mine["status"] == "ok" and "staged" not in mine["passport"]
    assert mine["passport"]["query"] or mine["records"]
    for who in (A2, ANON):
        out = query(who)
        log("OUTPUT", f"{who.label}: {out['status']} — {out.get('note')}")
        assert out["status"] == "declined" and "unknown dataset" in out["note"]
        assert ds not in out["available"]
    assert ds in _as(A1, registry.visible_feeds) and ds not in _as(A2, registry.visible_feeds)
    assert ds in _as(A1, lambda: registry.capabilities()["feeds"])
    assert ds not in _as(ANON, lambda: registry.capabilities()["feeds"])
    assert ds not in _as(A2, lambda: feeds.describe())


def test_a_hub_document_is_searched_by_its_hub_only(doc_env, log):
    """corpus_search, corpus_document and the archive routes agree: a landed document
    with visibility 'hub' is found by the owner's hub alone. Needs the landed metadata to
    keep its contribution_id (contrib/staging._land_document)."""
    from fastapi import HTTPException
    from app.api.routes import rag as rag_route
    from app.food_security import routes as fs_routes
    up = _submit("document", {"pack": "food-security", "url": doc_env, "source": "NOAA CPC",
                              "title": "ENSO Diagnostic Discussion Sep 2026", "pub_date": "2026-09",
                              "temporal": "forecast", "validation": "single-agency",
                              "usage_notes": "September 2026 snapshot only."})
    log("OUTPUT", {k: up.get(k) for k in ("status", "contribution_id", "sharing", "problems")})
    assert up["status"] == "staged" and up["sharing"]["hub"] == "mekong"
    cid, doc_id = up["contribution_id"], up["preview"]["doc_id"]

    def reads(who) -> bool:
        hits = _as(who, lambda: fetch.search("El Nino outlook Kenya maize", k=5))
        found = doc_id in {h["passport"]["doc_id"] for h in hits.get("hits", [])}
        by_id = _as(who, lambda: fetch.document(doc_id))
        assert (by_id["status"] == "ok") is found                       # by id agrees with search
        served = []
        for route in (lambda: fs_routes.rag_document(doc_id),
                      lambda: rag_route.archived_document("food-security", doc_id)):
            try:
                served.append(_as(who, route).__class__.__name__ == "FileResponse")
            except HTTPException as exc:
                served.append(exc.status_code != 404)
        assert served == [found, found], served                         # the archive routes agree
        return found

    assert reads(OWNER) and reads(REVIEWER) and not reads(A1)
    _approve(cid)
    landed = Corpus("food-security").find(doc_id)["metadata"]           # the gate's raw reader
    assert "staged_by" not in landed
    assert landed.get("contribution_id") == cid, "the landed metadata must keep contribution_id"
    assert reads(OWNER) and reads(REVIEWER) and reads(A1)
    assert not reads(A2) and not reads(ANON)
