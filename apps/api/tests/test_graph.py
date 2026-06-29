"""The graph flow with a stub LLM: the success path is grounded + traced, decline /
no-place / fetch-failure short-circuit to a direct answer with no second LLM call,
and the checkpointer keeps multi-turn memory.
"""
import json

from app.graph import graph as gm
from app.graph.geo import store


def _cfg(client, thread):
    return {"configurable": {"thread_id": thread, "client": client, "model": "stub"}}


def _patch_fetch(monkeypatch, aoi):
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda place, layer: aoi[layer])


def test_success_path_grounded_and_traced(aoi, make_client, monkeypatch, tmp_path, log):
    """route picks the op, operate computes the number, finalize quotes it, and the trace marks it grounded."""
    _patch_fetch(monkeypatch, aoi)
    expected = store.roads_in_hazard(aoi, "hazard_flood")["length_km"]
    client = make_client(("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))

    log("INPUT", "user: 'flooded roads in Testville?'")
    log("ROUTE", "stub LLM -> tool_call roads_in_hazard(place=Testville, hazard_layers=[hazard_flood])")
    log("OPERATE", f"real store.roads_in_hazard -> {expected} km (the only place a number is born)")
    graph, cfg = gm._build_graph(), _cfg(client, "ok")
    graph.invoke({"messages": [{"role": "user", "content": "flooded roads in Testville?"}]}, cfg)  # agent asks
    out = graph.invoke({"messages": [{"role": "user", "content": "1"}]}, cfg)  # choose exposure (raw hazard)
    answer = out["messages"][-1]["content"]
    log("ANSWER", answer)
    log("CALLS", f"{client.calls} (route + finalize)")

    rec = json.loads(next((tmp_path / "traces").glob("*.json")).read_text())
    log("TRACE", f"grounded={rec['grounded']} tokens={rec['tokens']}")
    log("CHECK", f"answer quotes {expected}; 2 LLM calls; trace grounded")
    assert str(expected) in answer
    assert client.calls == 2
    assert len(out["usage"]) == 2
    assert rec["grounded"] is True
    assert rec["tool_result"]["length_km"] == expected


def test_decline_returns_text_without_compute(make_client, log):
    """When the model declines (unavailable layer), finalize returns the text directly — no second LLM call."""
    client = make_client(("text", "I don't have a homeless layer."))
    log("INPUT", "user: 'homeless in Testville?'")
    log("ROUTE", "stub LLM -> plain text (no tool_call) => error/decline path")
    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "homeless in Testville?"}]}, _cfg(client, "decline"))
    log("ANSWER", out["messages"][-1]["content"])
    log("CALLS", f"{client.calls} (route only)")
    log("CHECK", "answer == the decline text; exactly 1 LLM call; no result computed")
    assert out["messages"][-1]["content"] == "I don't have a homeless layer."
    assert client.calls == 1
    assert out.get("result") is None


def test_no_place_refuses(make_client, log):
    """A tool call with no place can't be run; the graph refuses and asks for a place — no fetch/compute."""
    client = make_client(("tool", "roads_in_hazard", {"hazard_layers": ["hazard_flood"]}))
    log("INPUT", "user: 'flooded roads?'  (no place named)")
    log("ROUTE", "stub LLM -> tool_call with no `place` => refusal")
    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "flooded roads?"}]}, _cfg(client, "noplace"))
    log("ANSWER", out["messages"][-1]["content"])
    log("CALLS", f"{client.calls} (route only)")
    log("CHECK", "answer asks for a place; exactly 1 LLM call")
    assert client.calls == 1
    assert "place" in out["messages"][-1]["content"].lower()


def test_fetch_failure_refuses_without_finalize_llm(make_client, monkeypatch, log):
    """If fetch fails (unresolvable place), finalize returns the failure verbatim — no second LLM call."""
    def boom(*a, **k):
        raise ValueError("no administrative boundary for 'Atlantis'")
    monkeypatch.setattr(gm.ingest, "ensure_aoi", boom)
    monkeypatch.setattr(gm.ingest, "source_raster", lambda layer="hazard_flood": "x")
    client = make_client(("tool", "roads_in_hazard", {"place": "Atlantis", "hazard_layers": ["hazard_flood"]}))

    log("INPUT", "user: 'flooded roads in Atlantis?'")
    log("FETCH", "ensure_aoi raises ValueError('no administrative boundary...') => error path")
    graph, cfg = gm._build_graph(), _cfg(client, "fail")
    graph.invoke({"messages": [{"role": "user", "content": "flooded roads in Atlantis?"}]}, cfg)  # agent asks
    out = graph.invoke({"messages": [{"role": "user", "content": "1"}]}, cfg)  # choose exposure -> fetch boom
    log("ANSWER", out["messages"][-1]["content"])
    log("CALLS", f"{client.calls} (route only; finalize made no LLM call)")
    log("CHECK", "answer says 'No data ...'; exactly 1 LLM call")
    assert client.calls == 1
    assert "No data" in out["messages"][-1]["content"]


def test_multi_turn_memory(aoi, make_client, monkeypatch, log):
    """The checkpointer keeps history by thread_id: two turns accumulate user/assistant/user/assistant."""
    _patch_fetch(monkeypatch, aoi)
    client = make_client(("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    graph, cfg = gm._build_graph(), _cfg(client, "mem")
    log("TURN 1", "user: 'q1'  (thread_id=mem)")
    graph.invoke({"messages": [{"role": "user", "content": "q1"}]}, cfg)
    log("TURN 2", "user: 'q2'  (same thread_id)")
    out = graph.invoke({"messages": [{"role": "user", "content": "q2"}]}, cfg)
    roles = [m["role"] for m in out["messages"]]
    log("HISTORY", roles)
    log("CHECK", "roles == ['user','assistant','user','assistant']")
    assert roles == ["user", "assistant", "user", "assistant"]


def test_place_followup_after_no_place_decline(aoi, make_client, monkeypatch, log):
    """Turn 1 declines because no place was named; turn 2 supplies just the place. The agent
    must route the original request afresh — NOT re-emit the stale decline. Regresses the bug
    where a prior turn's `error` persisted in the checkpointer and re-fired finalize."""
    _patch_fetch(monkeypatch, aoi)
    monkeypatch.setattr(gm.combine, "combine_l2", lambda a, hazard, **k: aoi["hazard_flood"])
    graph, thread = gm._build_graph(), "placefollow"

    # Turn 1: no place -> the model returns a plain-text request for a place (decline path).
    decline = make_client(("text", "I need a place — e.g. Battambang or Siem Reap?"))
    log("TURN 1", "user: 'schools at high flood risk?'  (no place) -> decline asks for a place")
    t1 = graph.invoke({"messages": [{"role": "user", "content": "schools at high flood risk?"}]},
                      _cfg(decline, thread))
    log("ANSWER 1", t1["messages"][-1]["content"])
    assert "place" in t1["messages"][-1]["content"].lower()
    assert t1.get("error")                                   # decline set an error this turn

    # Turn 2 (same thread): user names just the place; a fresh route now yields a tool call.
    route = make_client(("tool", "count_in_hazard",
                         {"place": "Siem Reap", "hazard_layers": ["hazard_flood"], "layer": "schools"}))
    log("TURN 2", "user: 'Siem Reap'  (same thread_id) -> should route, not repeat the decline")
    t2 = graph.invoke({"messages": [{"role": "user", "content": "Siem Reap"}]}, _cfg(route, thread))
    msg = t2["messages"][-1]["content"]
    log("ANSWER 2", msg)
    log("CHECK", "stale decline gone; error cleared; now asking exposure/L1/L2 for Siem Reap")
    assert "I need a place" not in msg                       # the old decline must NOT re-appear
    assert t2.get("error") is None                           # stale error was cleared on the fresh turn
    assert "1)" in msg and "2)" in msg                       # routed -> now asks the 3-way
    assert t2.get("awaiting_choice")
