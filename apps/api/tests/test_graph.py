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
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda place: aoi)
    monkeypatch.setattr(gm.ingest, "source_raster", lambda layer="hazard_flood": "x")


def test_success_path_grounded_and_traced(aoi, make_client, monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, aoi)
    expected = store.roads_in_flood(aoi)["length_km"]
    client = make_client(("tool", "roads_in_flood", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))

    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "flooded roads in Testville?"}]}, _cfg(client, "ok"))

    answer = out["messages"][-1]["content"]
    assert str(expected) in answer          # the LLM quoted the computed number
    assert client.calls == 2                # route + finalize
    assert len(out["usage"]) == 2

    rec = json.loads(next((tmp_path / "traces").glob("*.json")).read_text())
    assert rec["grounded"] is True
    assert rec["tool_result"]["length_km"] == expected


def test_decline_returns_text_without_compute(make_client):
    client = make_client(("text", "I don't have a homeless layer."))
    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "homeless in Testville?"}]}, _cfg(client, "decline"))
    assert out["messages"][-1]["content"] == "I don't have a homeless layer."
    assert client.calls == 1                # finalize made no LLM call
    assert out.get("result") is None


def test_no_place_refuses(make_client):
    client = make_client(("tool", "roads_in_flood", {"hazard_layers": ["hazard_flood"]}))
    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "flooded roads?"}]}, _cfg(client, "noplace"))
    assert client.calls == 1
    assert "place" in out["messages"][-1]["content"].lower()


def test_fetch_failure_refuses_without_finalize_llm(make_client, monkeypatch):
    def boom(place):
        raise ValueError("no administrative boundary for 'Atlantis'")
    monkeypatch.setattr(gm.ingest, "ensure_aoi", boom)
    monkeypatch.setattr(gm.ingest, "source_raster", lambda layer="hazard_flood": "x")
    client = make_client(("tool", "roads_in_flood", {"place": "Atlantis", "hazard_layers": ["hazard_flood"]}))

    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "flooded roads in Atlantis?"}]}, _cfg(client, "fail"))
    assert client.calls == 1
    assert "No data" in out["messages"][-1]["content"]


def test_multi_turn_memory(aoi, make_client, monkeypatch):
    _patch_fetch(monkeypatch, aoi)
    client = make_client(("tool", "roads_in_flood", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    graph, cfg = gm._build_graph(), _cfg(client, "mem")
    graph.invoke({"messages": [{"role": "user", "content": "q1"}]}, cfg)
    out = graph.invoke({"messages": [{"role": "user", "content": "q2"}]}, cfg)
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user", "assistant"]
