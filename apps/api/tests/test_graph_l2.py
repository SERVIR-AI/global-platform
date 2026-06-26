"""S4 — Layer-2 risk wired into the chat. A flood-RISK question selects the computed
`risk_flood_l2` layer; fetch builds it via combine_l2 (stubbed here) and the existing
roads/count ops sample it like any 1-5 grid. No network / no real LLM (StubClient).
"""
from app.graph import graph as gm


def _cfg(client):
    return {"configurable": {"thread_id": "r", "client": client, "model": "stub"}}


def test_risk_question_answers_off_computed_l2_grid(aoi, make_client, monkeypatch, log):
    """S4.T1/T2 [stub] a flood-risk question runs combine_l2 and answers off risk_flood_l2."""
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    # the risk layer is computed (not downloaded) -> return the fixture's 1-5 grid
    monkeypatch.setattr(gm.combine, "combine_l2", lambda a, hazard, **k: aoi["hazard_flood"])
    client = make_client(("tool", "roads_in_hazard",
                          {"place": "Testville", "hazard_layers": ["risk_flood_l2"]}))

    log("INPUT", "user: 'how much road is at flood risk in Testville?'  -> layer risk_flood_l2")
    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "how much road is at flood risk in Testville?"}]},
        _cfg(client))
    res = out.get("result") or {}
    log("RESULT", f"{res.get('method')}  source={res.get('source')}  length_km={res.get('length_km')}")

    assert res.get("method") == "roads_in_hazard"
    assert "risk_flood_l2" in res.get("source", "")                 # answered off the computed risk grid
    assert 0 <= res["length_km"] <= res["total_road_km"]            # a sane number
    assert any("computed: Hazard" in t for t in out.get("trace", []))   # trace names the computation
    assert any("resolve →" in t for t in out.get("trace", []))          # resolver recorded the L1/L2 choice
