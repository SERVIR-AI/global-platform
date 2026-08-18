"""Recovery behaviour that is VISIBLE in the trace envelope.

The backend already recovers from several kinds of failure — a dead Overpass mirror, a
raster it doesn't need to re-download, a node that can't proceed, a question it doesn't
need to ask. Each of those leaves a specific mark in the envelope, and these tests pin
that mark: assert the recovery happened, then capture the real envelope it produced.

The captured JSON is what `docs/TRACE_RECOVERY.md` quotes — no excerpt in that document
is hand-written. Regenerate them all with:

    GRP_TRACE_CAPTURE_DIR=/tmp/traces uv run pytest apps/api/tests/test_trace_recovery.py

With the variable unset (the normal suite run) `_capture` is a no-op, so nothing is
written anywhere and the tests stay hermetic.
"""
import json
import os
import shutil

import requests
from fastapi.testclient import TestClient

from app.config import get_settings
from app.graph import graph as gm
from app.graph.geo import drive_tifs, ingest
from app.main import app

client = TestClient(app)


def _capture(name: str, payload) -> None:
    """Write one captured envelope (or excerpt) to GRP_TRACE_CAPTURE_DIR, if set."""
    out_dir = os.environ.get("GRP_TRACE_CAPTURE_DIR")
    if not out_dir:
        return
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{name}.json"), "w") as f:
        json.dump(payload, f, indent=2)


def _stub_llm(monkeypatch, make_client, route_response):
    """Install a stub OpenAI client on the chat route; returns it so calls can be counted."""
    from app.api.routes import chat as chat_route
    stub = make_client(route_response)
    monkeypatch.setattr(chat_route, "build_client", lambda provider: stub)
    return stub


def _ask(content, **body):
    return client.post("/api/chat", json={"messages": [{"role": "user", "content": content}],
                                          "provider": "gemini", **body})


def _step(envelope, node):
    return next(s for s in envelope["steps"] if s["node"] == node)


# --- 1. Overpass mirror failover + exponential backoff ------------------------------------
#
# ingest.py:142-160. The only test here that lets ensure_aoi run for real: the whole point
# is that _overpass()'s own retry loop reaches emit(), so `mirror_used` and `attempts` in
# the fetch step are produced by the real recovery code, not by a stubbed collector.

_ADMIN_HIT = {
    "class": "boundary", "type": "administrative", "display_name": "Testville, Cambodia",
    "lon": "0.005", "lat": "0.005",
    "geojson": {"type": "Polygon",
                "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]]},
}

_SCHOOL_NODES = [{"type": "node", "id": 1, "lat": 0.003, "lon": 0.003, "tags": {"name": "A"}},
                 {"type": "node", "id": 2, "lat": 0.007, "lon": 0.007, "tags": {"name": "B"}}]


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def test_overpass_mirror_failover_visible_in_trace(aoi, make_client, monkeypatch, tmp_path, log):
    """Mirror 1 is down for good, mirror 3 raises, mirror 2 recovers only on the second
    round — so the turn survives a full failed sweep plus a backoff, and the fetch step
    records WHICH mirror answered and on WHICH attempt."""
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path / "cache")
    _stub_llm(monkeypatch, make_client,
              ("tool", "count_features", {"place": "Testville", "layer": "schools"}))

    posts, slept = [], []
    monkeypatch.setattr(ingest.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(ingest.requests, "get", lambda *a, **k: _Resp(200, [_ADMIN_HIT]))

    def fake_post(url, **kwargs):
        posts.append(url)
        round_no = posts.count(url)
        if url == ingest.OVERPASS_MIRRORS[0]:
            return _Resp(504)                      # primary mirror: overloaded, permanently
        if url == ingest.OVERPASS_MIRRORS[2]:
            raise requests.ConnectionError("connection reset")
        if round_no == 1:
            return _Resp(429)                      # backup mirror: rate-limited on round 1
        return _Resp(200, {"elements": _SCHOOL_NODES})

    monkeypatch.setattr(ingest.requests, "post", fake_post)

    r = _ask("how many schools are in Testville?")
    envelope = r.json()["trace_envelope"]
    api_calls = _step(envelope, "fetch")["api_calls"]
    overpass = next(c for c in api_calls if c["api"] == "Overpass")
    log("API CALLS", api_calls)
    log("BACKOFF", f"slept {slept}s between rounds")

    assert r.status_code == 200
    assert overpass["mirror_used"] == ingest.OVERPASS_MIRRORS[1]   # failed over off the primary
    assert overpass["attempts"] == 2                               # succeeded on the second round
    assert slept == [1]                                            # 2**0 == 1s of backoff
    assert _step(envelope, "operate")["result"]["value"] == 2      # the answer still came out
    _capture("01_overpass_failover", {"api_calls": api_calls})


# --- 2. Cache fallback: was_cached on download + clip -------------------------------------

def test_cache_reuse_visible_as_was_cached(aoi, make_client, monkeypatch, tmp_path, log):
    """The source raster is already in the tiff cache (no Drive download) and, on a repeat
    of the same question, the AOI clip is already on disk too — both recorded as
    was_cached on the fetch step's downloads[]."""
    monkeypatch.setattr(get_settings(), "tiffs_dir", tmp_path / "tiffs")
    (tmp_path / "tiffs").mkdir()
    shutil.copy(aoi["hazard_flood"], tmp_path / "tiffs" / "hazard_flood.tif")
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    _stub_llm(monkeypatch, make_client,
              ("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))

    # Turn 1 asks exposure/L1/L2; turn 2 answers "1" (exposure) and does the real clip.
    tid = _ask("flooded roads in Testville?").json()["thread_id"]
    first = _ask("1", thread_id=tid).json()["trace_envelope"]
    downloads_first = _step(first, "fetch")["downloads"]
    log("FIRST TURN", downloads_first)

    download = next(d for d in downloads_first if d["kind"] == "download")
    clip = next(d for d in downloads_first if d["kind"] == "clip")
    assert download["was_cached"] is True          # source raster served from the tiff cache
    assert clip["was_cached"] is False             # this AOI had never been clipped

    # Same question again on a fresh thread: the clip is now on disk, so it is reused and
    # source_raster is never even reached.
    tid2 = _ask("flooded roads in Testville?").json()["thread_id"]
    second = _ask("1", thread_id=tid2).json()["trace_envelope"]
    downloads_second = _step(second, "fetch")["downloads"]
    log("SECOND TURN", downloads_second)

    assert [d["kind"] for d in downloads_second] == ["clip"]
    assert downloads_second[0]["was_cached"] is True
    _capture("02_cache_reuse", {"first_turn_downloads": downloads_first,
                                "second_turn_downloads": downloads_second})


# --- 3. Error-branch rerouting: the run stops cleanly at the node that failed --------------

def test_error_branch_short_circuits_at_router(aoi, make_client, monkeypatch, log):
    """_after_route -> finalize. The model declined, so resolve/fetch/operate never ran and
    finalize echoed the message with no model call: tokens and grounded are null, not 0."""
    stub = _stub_llm(monkeypatch, make_client,
                     ("text", "I can't answer that with the data I have."))

    r = _ask("what's the weather like tomorrow?")
    envelope = r.json()["trace_envelope"]
    nodes = [s["node"] for s in envelope["steps"]]
    log("STEPS", nodes)

    assert nodes == ["router", "finalize"]                  # three nodes skipped entirely
    assert _step(envelope, "router")["kind"] == "declined"
    finalize = _step(envelope, "finalize")
    assert finalize["kind"] == "error_echo"
    assert finalize["llm_provider"] is None and finalize["tokens"] is None
    assert finalize["grounded"] is None                     # no number was computed to check
    assert stub.calls == 1                                  # only the route call was ever made
    _capture("03a_error_at_router", envelope)


def test_error_branch_short_circuits_at_fetch(aoi, make_client, monkeypatch, log):
    """_after_fetch -> finalize. The place could not be resolved, so operate is skipped and
    the fetch step carries the error plus an all-null aoi summary."""
    def boom(*a, **k):
        raise ValueError("could not find 'Atlantis' (try 'City, Country')")
    monkeypatch.setattr(gm.ingest, "ensure_aoi", boom)
    stub = _stub_llm(monkeypatch, make_client,
                     ("tool", "count_features", {"place": "Atlantis", "layer": "schools"}))

    r = _ask("how many schools are in Atlantis?")
    envelope = r.json()["trace_envelope"]
    nodes = [s["node"] for s in envelope["steps"]]
    log("STEPS", nodes)

    assert nodes == ["router", "resolve", "fetch", "finalize"]   # operate skipped
    fetch = _step(envelope, "fetch")
    assert "Atlantis" in fetch["error"]
    assert fetch["aoi"] == {"name": None, "area_km2": None, "how": None}
    assert _step(envelope, "finalize")["kind"] == "error_echo"
    assert stub.calls == 1                                       # no phrasing call
    _capture("03b_error_at_fetch", envelope)


# --- 4. resolve auto_single: a question the agent didn't need to ask ----------------------

def test_resolve_auto_single_skips_the_question(aoi, make_client, monkeypatch, log):
    """With only the raw hazard raster in the Drive catalog there is exactly one way to
    answer a flood question, so resolve uses it instead of pausing — decision=auto_single,
    awaiting_choice_set=false, no question_asked.

    The shipped catalog carries exposure + L1 + L2 for every hazard, so this branch is
    unreachable there; the catalog is narrowed here to a deployment that has only the
    hazard layer. resolver.options_for still runs for real.
    """
    monkeypatch.setattr(drive_tifs, "DRIVE_TIFS", {"hazard_flood.tif": "fake-id"})
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda a, layer: a["hazard_flood"])
    _stub_llm(monkeypatch, make_client,
              ("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))

    r = _ask("flooded roads in Testville?")
    envelope = r.json()["trace_envelope"]
    resolve = _step(envelope, "resolve")
    log("RESOLVE", {k: resolve[k] for k in ("decision", "hazard", "options", "summary")})

    assert resolve["decision"] == "auto_single"
    assert resolve["awaiting_choice_set"] is False
    assert resolve["question_asked"] is None
    assert [o["key"] for o in resolve["options"]] == ["exposure"]
    assert [s["node"] for s in envelope["steps"]] == ["router", "resolve", "fetch",
                                                      "operate", "finalize"]
    _capture("04_resolve_auto_single", resolve)


# --- 5. resolve byod_passthrough: the user's own layer needs no question -------------------

def test_resolve_byod_passthrough_uses_the_upload_directly(aoi, make_client, monkeypatch,
                                                           tif_writer, byod_env, log):
    """A verified upload has one meaning, so there is nothing to choose: resolve passes
    straight through with byod_passthrough=true and fetch clips the user's own layer."""
    import numpy as np
    from app.graph.geo import byod_registry

    layer, report = byod_registry.register(
        "byod-thread", hazard_label="flood", severity_scale="0-5",
        src_path=tif_writer(np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16")))
    assert report.ok

    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda a, l: a.get(l) or a["hazard_flood"])
    _stub_llm(monkeypatch, make_client,
              ("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": [layer]}))

    r = _ask("flooded roads in Testville using my layer?", thread_id="byod-thread")
    envelope = r.json()["trace_envelope"]
    resolve = _step(envelope, "resolve")
    log("RESOLVE", {k: resolve[k] for k in ("decision", "byod_passthrough", "summary")})

    assert resolve["decision"] == "passthrough_no_hazard"
    assert resolve["byod_passthrough"] is True
    assert resolve["awaiting_choice_set"] is False
    assert _step(envelope, "fetch")["rasters_clipped"] == [layer]
    _capture("05_resolve_byod_passthrough",
             {"resolve": resolve, "fetch_rasters_clipped": _step(envelope, "fetch")["rasters_clipped"]})
