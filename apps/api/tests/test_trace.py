"""The structured, frontend-exportable execution trace: each turn yields a per-turn
envelope (path + ordered step events) assembled by `trace.build_envelope`. Verifies the
three paths, the per-turn reset (events don't bleed across turns), the rich route detail,
and the download-vs-cache signal emitted deep inside ingest.
"""
import sys
import types

from app.config import get_settings
from app.graph import graph as gm
from app.graph.geo import drive_tifs, ingest, trace


def _cfg(client, thread):
    return {"configurable": {"thread_id": thread, "client": client, "model": "stub", "provider": "gemini"}}


def _patch_fetch(monkeypatch, aoi):
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda place, layer: aoi[layer])


def _env(out, thread="t"):
    answer = out["messages"][-1]["content"] if out.get("messages") else None
    return trace.build_envelope(out["events"], thread_id=thread, provider="gemini", model="stub",
                                usages=out.get("usage"), result=out.get("result"), answer=answer)


def test_direct_path_trace(aoi, make_client, monkeypatch, log):
    """count_features has no hazard, so resolve passes through: path 'direct', 5 steps, route detail present."""
    _patch_fetch(monkeypatch, aoi)
    client = make_client(("tool", "count_features", {"place": "Testville", "layer": "hospitals"}))
    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "hospitals in Testville?"}]}, _cfg(client, "direct"))

    nodes = [e["node"] for e in out["events"]]
    env = _env(out, "direct")
    log("NODES", nodes)
    log("PATH", env["path"])
    assert nodes == ["route", "resolve", "fetch", "operate", "finalize"]
    assert env["path"] == "direct"
    assert env["thread_id"] == "direct"

    route_ev = out["events"][0]
    assert route_ev["kind"] == "llm_route"
    assert route_ev["parsed"]["operation"] == "count_features"
    assert route_ev["parsed"]["place"] == "Testville"
    assert route_ev["llm"]["tokens"] == {"in": 10, "out": 5}      # from the stub usage
    assert route_ev["llm"]["provider"] == "gemini"
    assert "count_features" in route_ev["request"]["tools_offered"]
    assert out["events"][1]["decision"] == "passthrough_no_hazard"
    assert out["events"][-1]["kind"] == "llm_phrase"
    assert env["usage_total"]["total"] == 30                       # route(15) + finalize(15)


def test_pause_then_resume_share_thread_and_dont_bleed(aoi, make_client, monkeypatch, log):
    """Turn 1 pauses (clarify_pause, [route,resolve]); turn 2 resumes (resume, [route,fetch,operate,finalize])
    on the SAME thread; the per-turn reset keeps turn 2's events free of turn 1's."""
    _patch_fetch(monkeypatch, aoi)
    client = make_client(("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    graph, cfg = gm._build_graph(), _cfg(client, "hitl")

    out1 = graph.invoke({"messages": [{"role": "user", "content": "flooded roads in Testville?"}]}, cfg)
    env1 = _env(out1, "hitl")
    log("TURN1", [e["node"] for e in out1["events"]])
    assert [e["node"] for e in out1["events"]] == ["route", "resolve"]
    assert env1["path"] == "clarify_pause"
    resolve_ev = out1["events"][1]
    assert resolve_ev["decision"] == "asked" and resolve_ev["awaiting_choice_set"] is True
    assert [o["key"] for o in resolve_ev["options"]] == ["exposure", "risk-L1", "risk-L2"]

    out2 = graph.invoke({"messages": [{"role": "user", "content": "1"}]}, cfg)   # pick exposure
    env2 = _env(out2, "hitl")
    log("TURN2", [e["node"] for e in out2["events"]])
    assert [e["node"] for e in out2["events"]] == ["route", "fetch", "operate", "finalize"]  # resolve skipped
    assert env2["path"] == "resume"
    assert out2["events"][0]["kind"] == "apply_choice"
    assert out2["events"][0]["chosen"]["layer"] == "hazard_flood"
    assert env1["thread_id"] == env2["thread_id"] == "hitl"        # groupable on the frontend


def test_refused_path_trace(make_client, log):
    """A decline yields path 'refused', error_origin 'route', and a single error_echo finalize step."""
    client = make_client(("text", "I don't have a homeless layer."))
    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "homeless in Testville?"}]}, _cfg(client, "refuse"))
    env = _env(out, "refuse")
    log("NODES", [e["node"] for e in out["events"]])
    log("PATH/ORIGIN", (env["path"], env["error_origin"]))
    assert [e["node"] for e in out["events"]] == ["route", "finalize"]
    assert env["path"] == "refused"
    assert env["error_origin"] == "route"
    assert out["events"][-1]["kind"] == "error_echo"


def test_source_raster_emits_download_then_cache(monkeypatch, tmp_path, log):
    """ingest.source_raster emits a Google-Drive download event (was_cached:false) the first time and
    was_cached:true the second — the transparency signal the frontend shows for tiff downloads."""
    monkeypatch.setattr(get_settings(), "tiffs_dir", tmp_path)
    monkeypatch.setattr(drive_tifs, "drive_id", lambda name: "FAKEID")
    fake_gdown = types.SimpleNamespace(download=lambda id, output, quiet: open(output, "wb").close())
    monkeypatch.setitem(sys.modules, "gdown", fake_gdown)

    collector = trace.TraceCollector()
    token = trace.set_collector(collector)
    try:
        ingest.source_raster("hazard_flood")     # not present -> download
        ingest.source_raster("hazard_flood")     # present -> cache hit
    finally:
        trace.reset(token)

    downloads = [e for e in collector.io if e["kind"] == "download"]
    log("EVENTS", downloads)
    assert downloads[0]["was_cached"] is False and downloads[0]["api"] == "Google Drive"
    assert downloads[0]["drive_id"] == "FAKEID"
    assert downloads[1]["was_cached"] is True


def test_emit_is_noop_without_collector():
    """Off the request path (no collector installed), emit must be a silent no-op — never raise."""
    trace.emit({"kind": "api", "api": "Nominatim"})   # should not raise
