"""S4 + ask-flow — a RISK question now ASKS L1 vs L2 first (human-in-the-loop), then on
the reply samples the precomputed risk (L1) or computes Hazard x Vulnerability (L2).
Two turns on one thread_id (the checkpointer resumes). No network / no real LLM (StubClient).
"""
from app.graph import graph as gm


def _graph_cfg(client, thread):
    return gm._build_graph(), {"configurable": {"thread_id": thread, "client": client, "model": "stub"}}


def test_risk_question_asks_then_answers_l2(aoi, make_client, monkeypatch, log):
    """[stub] turn 1 asks L1 vs L2 (computes nothing); turn 2 reply '2' computes L2 and answers."""
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.combine, "combine_l2", lambda a, hazard, **k: aoi["hazard_flood"])
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda a, layer: a.get(layer) or a["hazard_flood"])
    client = make_client(("tool", "roads_in_hazard",
                          {"place": "Testville", "hazard_layers": ["risk_flood_l2"]}))
    graph, cfg = _graph_cfg(client, "ask-l2")

    t1 = graph.invoke({"messages": [{"role": "user", "content": "road at flood risk in Testville?"}]}, cfg)
    q = t1["messages"][-1]["content"]
    log("TURN1", q)
    assert "1)" in q and "2)" in q                          # presented both options
    assert t1.get("awaiting_choice")                        # paused for the choice
    assert t1.get("result") is None                         # nothing computed yet
    assert any("asking L1 vs L2" in t for t in t1.get("trace", []))

    t2 = graph.invoke({"messages": [{"role": "user", "content": "2"}]}, cfg)
    res = t2.get("result") or {}
    log("TURN2", f"{res.get('method')} source={res.get('source')} len={res.get('length_km')}")
    assert res.get("method") == "roads_in_hazard"
    assert "risk_flood_l2" in res.get("source", "")         # answered off the computed L2 grid
    assert t2.get("awaiting_choice") is None                # choice consumed


def test_risk_choice_l1_samples_precomputed(aoi, make_client, monkeypatch):
    """[stub] replying '1' samples the precomputed risk (risk_flood), not the computed L2 grid."""
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda a, layer: a.get(layer) or a["hazard_flood"])
    client = make_client(("tool", "roads_in_hazard",
                          {"place": "Testville", "hazard_layers": ["risk_flood_l2"]}))
    graph, cfg = _graph_cfg(client, "ask-l1")

    graph.invoke({"messages": [{"role": "user", "content": "road at flood risk in Testville?"}]}, cfg)
    t2 = graph.invoke({"messages": [{"role": "user", "content": "1"}]}, cfg)
    res = t2.get("result") or {}
    src = res.get("source", "")
    assert "risk_flood" in src and "_l2" not in src         # L1 = the precomputed risk, not the computed grid
