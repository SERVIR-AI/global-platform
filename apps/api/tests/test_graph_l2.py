"""The unified ask: a hazard question ASKS exposure vs precomputed-risk (L1) vs
recomputed-risk (L2) — the LLM only picks the hazard, the agent never guesses. The
reply (same thread_id) resumes and routes to the right layer. No network / real LLM (StubClient).
"""
import pytest

from app.graph import graph as gm


def _graph_cfg(client, thread):
    return gm._build_graph(), {"configurable": {"thread_id": thread, "client": client, "model": "stub"}}


@pytest.mark.parametrize("reply, expect_src", [
    ("1", "hazard_flood.tif"),       # exposure  -> raw hazard
    ("2", "risk_flood.tif"),         # risk L1   -> precomputed risk
    ("3", "risk_flood_l2.tif"),      # risk L2   -> computed risk
])
def test_hazard_question_asks_then_routes(aoi, make_client, monkeypatch, log, reply, expect_src):
    """[stub] turn 1 asks the 3 options; turn 2's number routes to exposure / L1 / L2."""
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.combine, "combine_l2", lambda a, hazard, **k: aoi["hazard_flood"])
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda a, layer: a.get(layer) or a["hazard_flood"])
    client = make_client(("tool", "roads_in_hazard",
                          {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    graph, cfg = _graph_cfg(client, f"3way-{reply}")

    t1 = graph.invoke({"messages": [{"role": "user", "content": "flood on roads in Testville?"}]}, cfg)
    q = t1["messages"][-1]["content"]
    log("ASK", q)
    assert "1)" in q and "2)" in q and "3)" in q            # exposure, L1, L2 all offered
    assert t1.get("awaiting_choice") and t1.get("result") is None

    t2 = graph.invoke({"messages": [{"role": "user", "content": reply}]}, cfg)
    res = t2.get("result") or {}
    log("ANSWER", f"{res.get('method')} source={res.get('source')}")
    assert res.get("method") == "roads_in_hazard"
    assert expect_src in res.get("source", "")              # routed to the chosen layer
    assert t2.get("awaiting_choice") is None


def test_unknown_hazard_refuses_without_asking(aoi, make_client, monkeypatch):
    """[stub] a hazard with no exposure and no risk data refuses cleanly — no question, no fetch."""
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    client = make_client(("tool", "roads_in_hazard",
                          {"place": "Testville", "hazard_layers": ["hazard_volcano"]}))
    graph, cfg = _graph_cfg(client, "novolc")
    out = graph.invoke({"messages": [{"role": "user", "content": "volcano on roads in Testville?"}]}, cfg)
    msg = out["messages"][-1]["content"].lower()
    assert "data to assess" in msg or "don't have" in msg
    assert out.get("result") is None and out.get("awaiting_choice") is None
