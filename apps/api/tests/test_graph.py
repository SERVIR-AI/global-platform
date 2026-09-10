"""The graph flow with a stub LLM: the success path is grounded + traced, decline /
no-place / fetch-failure short-circuit to a direct answer with no second LLM call,
and the checkpointer keeps multi-turn memory.
"""

from app.graph import graph as gm
from app.graph.geo import store


def _cfg(client, thread):
    return {"configurable": {"thread_id": thread, "client": client, "model": "stub", "provider": "stub"}}


def _patch_fetch(monkeypatch, aoi):
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda place, layer: aoi[layer])


def test_add_reset_behaves_like_add_without_the_marker(log):
    """No _RESET present -> identical to plain _add: just glue right onto left."""
    assert gm._add_reset(["a"], ["b"]) == ["a", "b"]
    assert gm._add_reset(None, ["b"]) == ["b"]
    assert gm._add_reset(["a"], None) == ["a"]
    assert gm._add_reset(None, None) == []


def test_add_reset_wipes_left_and_strips_the_marker(log):
    """_RESET present -> left is discarded entirely, and the marker itself never survives
    into the returned value (it must never leak into persisted state)."""
    left = ["turn1_event_a", "turn1_event_b"]
    right = [gm._RESET, "turn2_event"]
    result = gm._add_reset(left, right)
    log("RESULT", result)
    assert result == ["turn2_event"]
    assert gm._RESET not in result


def test_success_path_grounded_and_traced(aoi, make_client, monkeypatch, log):
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

    # Groundedness and the computed number are read off the trace events themselves. This
    # invokes the graph directly, so chat.py's envelope assembly never runs — the events
    # channel is the record.
    operate_event = next(e for e in out["events"] if e["node"] == "operate")
    finalize_event = next(e for e in out["events"] if e["node"] == "finalize")
    log("TRACE", f"grounded={finalize_event['grounded']} tokens={finalize_event['tokens']}")
    log("CHECK", f"answer quotes {expected}; 2 LLM calls; trace grounded")
    assert str(expected) in answer
    assert client.calls == 2
    assert len(out["usage"]) == 2
    assert finalize_event["grounded"] is True
    assert operate_event["result"]["value"] == expected


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


def test_drawn_geometry_needs_no_place(aoi, make_client, monkeypatch, log):
    """[stub] Mode 2: a drawn `req_geometry` makes a place name unnecessary — route uses the
    drawn area instead of declining. Regresses the State refactor that dropped req_geometry from
    the graph channels, so it never reached route() and the agent kept asking for a place."""
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    client = make_client(("tool", "count_features", {"layer": "buildings"}))
    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "how many buildings are at risk of flooding here?"}],
         "req_geometry": [103.0, 13.0, 103.5, 13.5]},
        _cfg(client, "drawn"))
    log("PLACE", out.get("place"))
    log("RESULT", out.get("result"))
    assert out.get("error") is None                          # not declined for a missing place
    assert out.get("place") == "drawn area"                  # used the drawn AOI
    assert out.get("result") and out["result"]["method"] == "count_features"


def _event(out):
    """route()'s raw returned events delta is [_RESET, trace_event] — route() is always
    the turn's first node, so it always resets. This unwraps to just the event dict."""
    assert out["events"][0] == gm._RESET
    assert len(out["events"]) == 2
    return out["events"][1]


def test_route_attaches_one_event_declined(make_client, log):
    """route()'s events channel actually gets the built trace event, not just a value
    that's computed and thrown away — declined branch."""
    client = make_client(("text", "I don't have a homeless layer."))
    state = {"messages": [{"role": "user", "content": "homeless in Testville?"}], "events": []}
    out = gm.route(state, _cfg(client, "ev-decline"))
    log("EVENTS", out.get("events"))
    event = _event(out)
    assert event["node"] == "router"
    assert event["kind"] == "declined"
    assert event["error"] == out["error"]


def test_route_attaches_one_event_missing_place(make_client, log):
    """missing-place branch: same wiring, different kind, error carried into the event."""
    client = make_client(("tool", "roads_in_hazard", {"hazard_layers": ["hazard_flood"]}))
    state = {"messages": [{"role": "user", "content": "flooded roads?"}], "events": []}
    out = gm.route(state, _cfg(client, "ev-noplace"))
    log("EVENTS", out.get("events"))
    event = _event(out)
    assert event["kind"] == "missing_place"
    assert event["error"] == out["error"]


def test_route_attaches_one_event_routed(make_client, log):
    """A full tool call -> kind=routed, error None, matches what route()'s own `out` has."""
    client = make_client(("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    state = {"messages": [{"role": "user", "content": "flooded roads in Testville?"}], "events": []}
    out = gm.route(state, _cfg(client, "ev-routed"))
    log("EVENTS", out.get("events"))
    event = _event(out)
    assert event["kind"] == "routed"
    assert event["error"] is None
    assert event["derived_place"] == "Testville"
    assert event["derived_hazard_layers_used"] == ["hazard_flood"]


def test_route_attaches_one_event_apply_choice_no_llm_call(make_client, log):
    """Resuming a paused choice never calls the LLM, and gets its event via
    make_trace_event_no_llm, not make_trace_event_router."""
    awaiting = {"operation": "count_in_hazard", "place": "Siem Reap", "op_args": {"layer": "schools"},
                "tool_call_id": "c1", "req_geometry": None,
                "options": [("exposure", "hazard_flood", "Exposure"), ("risk-L1", "risk_flood", "Risk L1")]}
    state = {"messages": [{"role": "user", "content": "2"}], "awaiting_choice": awaiting, "events": []}
    client = make_client(("text", "should never be called"))
    out = gm.route(state, _cfg(client, "ev-resume"))
    log("CALLS", client.calls)
    log("EVENTS", out.get("events"))
    assert client.calls == 0                       # confirms this really took the no-LLM path
    event = _event(out)
    assert event["kind"] == "apply_choice"
    assert event["llm_provider"] is None


def test_events_reset_each_turn(aoi, make_client, monkeypatch, log):
    """Regression test: route() resets `events` every turn via the _RESET sentinel (fixed
    from the previously-documented accumulation bug), so turn 2's events must NOT carry
    turn 1's step forward. The graph's own reducer strips the sentinel automatically —
    graph.invoke()'s result never shows the raw marker, only gm.route() called directly
    (bypassing the reducer) does."""
    _patch_fetch(monkeypatch, aoi)
    client = make_client(("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    graph, cfg = gm._build_graph(), _cfg(client, "ev-reset")
    t1 = graph.invoke({"messages": [{"role": "user", "content": "flooded roads in Testville?"}]}, cfg)
    t2 = graph.invoke({"messages": [{"role": "user", "content": "1"}]}, cfg)  # choose exposure -> resumes
    log("TURN1 nodes", [e["node"] for e in t1["events"]])
    log("TURN2 nodes", [e["node"] for e in t2["events"]])
    # turn 1: route() picks the tool, resolve() pauses to ask exposure/L1/L2.
    assert [e["node"] for e in t1["events"]] == ["router", "resolve"]
    # turn 1's steps are gone; turn 2 has its own "router" (apply_choice, resuming the
    # choice), "fetch", "operate", and "finalize" (the resumed pick skips resolve() via
    # _resume, so the turn runs all the way through) — not a pileup carrying turn 1's
    # steps along too.
    assert [e["node"] for e in t2["events"]] == ["router", "fetch", "operate", "finalize"]
    assert t2["events"][0]["kind"] == "apply_choice"


def test_operate_attaches_one_event_success(aoi, log):
    """operate() attaches its event as a plain [event] — NOT [_RESET, event], since it's
    never the turn's first node."""
    expected = store.roads_in_hazard(aoi, "hazard_flood")["length_km"]
    state = {"operation": "roads_in_hazard", "aoi": aoi, "tiffs": ["hazard_flood"],
              "op_args": {}, "events": []}
    out = gm.operate(state)
    log("EVENTS", out.get("events"))
    assert len(out["events"]) == 1                 # plain [event], no sentinel
    event = out["events"][0]
    assert event["node"] == "operate"
    assert event["error"] is None
    assert event["result"]["value"] == expected


def test_operate_attaches_one_event_failure(aoi, log):
    """An unknown operation makes dispatch() raise -> result None, error carried into out."""
    state = {"operation": "bogus_op", "aoi": aoi, "tiffs": [], "op_args": {}, "events": []}
    out = gm.operate(state)
    log("EVENTS", out.get("events"))
    assert len(out["events"]) == 1
    event = out["events"][0]
    assert event["result"] is None
    assert event["error"] == out["error"]


def test_router_and_operate_events_coexist_after_reset(aoi, make_client, monkeypatch, log):
    """Ties Commits 2 and 3 together: a turn that reaches operate() ends up with both a
    router-family step and an operate step in the same turn's events, and — thanks to the
    reset — nothing left over from a prior turn."""
    _patch_fetch(monkeypatch, aoi)
    client = make_client(("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": ["hazard_flood"]}))
    graph, cfg = gm._build_graph(), _cfg(client, "ev-router-operate")
    graph.invoke({"messages": [{"role": "user", "content": "flooded roads in Testville?"}]}, cfg)
    t2 = graph.invoke({"messages": [{"role": "user", "content": "1"}]}, cfg)  # choose exposure -> resumes -> operate
    log("TURN2 nodes", [e["node"] for e in t2["events"]])
    assert {e["node"] for e in t2["events"]} == {"router", "fetch", "operate", "finalize"}


def test_drawn_area_persists_across_turns(aoi, make_client, monkeypatch, log):
    """[stub] Mode 2 multi-turn: draw an area (turn 1), then ask about 'the same area' (turn 2)
    WITHOUT re-sending geometry. The drawn AOI must persist via the checkpointer channel — never
    geocode the literal string 'drawn area'. Regresses the 'could not find drawn area' bug."""
    def fake_ensure_aoi(place=None, geometry=None, layers=None):
        if geometry is not None:
            return aoi
        if place == "drawn area":                            # the bug: geocoding the literal string
            raise ValueError("could not find 'drawn area' (try 'City, Country')")
        return aoi
    monkeypatch.setattr(gm.ingest, "ensure_aoi", fake_ensure_aoi)
    graph, thread = gm._build_graph(), "drawnmulti"

    # Turn 1: a drawn polygon + a question about it.
    c1 = make_client(("tool", "count_features", {"layer": "schools"}))
    t1 = graph.invoke({"messages": [{"role": "user", "content": "schools at flood risk here?"}],
                       "req_geometry": [103.0, 13.0, 103.5, 13.5]}, _cfg(c1, thread))
    assert t1.get("result") and t1.get("error") is None

    # Turn 2: 'same area', NO geometry re-sent; the model carries place='drawn area'.
    c2 = make_client(("tool", "count_features", {"place": "drawn area", "layer": "buildings"}))
    t2 = graph.invoke({"messages": [{"role": "user", "content": "what about buildings in the same area?"}]},
                      _cfg(c2, thread))
    log("TURN 2 result", t2.get("result"))
    log("TURN 2 error", t2.get("error"))
    assert t2.get("error") is None                           # must NOT fail with 'could not find drawn area'
    assert t2.get("result") and t2["result"]["method"] == "count_features"


def test_finalize_attaches_one_event_error(log):
    state = {"messages": [{"role": "user", "content": "flooded roads in Atlantis?"}],
              "error": "No data for that request: unknown place", "usage": [], "events": []}
    out = gm.finalize(state, _cfg(None, "fin-error"))
    log("EVENTS", out.get("events"))
    assert len(out["events"]) == 1
    event = out["events"][0]
    assert event["node"] == "finalize"
    assert event["kind"] == "error_echo"
    assert event["error"] == state["error"]


def test_finalize_attaches_one_event_llm_phrase(aoi, make_client, log):
    result = store.roads_in_hazard(aoi, "hazard_flood")
    client = make_client(("text", "unused — StubClient's finalize branch ignores this"))
    state = {"messages": [{"role": "user", "content": "flooded roads in Testville?"}],
              "place": "Testville", "operation": "roads_in_hazard", "op_args": {},
              "tool_call_id": "c1", "result": result, "usage": [], "events": []}
    out = gm.finalize(state, _cfg(client, "fin-llm"))
    log("EVENTS", out.get("events"))
    assert len(out["events"]) == 1
    event = out["events"][0]
    assert event["kind"] == "llm_phrase"
    assert event["error"] is None
    assert event["grounded"] is True                      # StubClient echoes str(result), which contains the number
    assert str(result["length_km"]) in event["llm_response"]


def test_resolve_attaches_one_event_passthrough(log):
    state = {"tiffs": [], "place": "Testville", "events": []}
    out = gm.resolve(state)
    log("EVENTS", out.get("events"))
    assert len(out["events"]) == 1
    event = out["events"][0]
    assert event["node"] == "resolve"
    assert event["decision"] == "passthrough_no_hazard"
    assert event["byod_passthrough"] is False


def test_resolve_attaches_one_event_passthrough_byod(log):
    state = {"tiffs": ["byod_abc123"], "place": "Testville", "events": []}
    out = gm.resolve(state)
    event = out["events"][0]
    assert event["decision"] == "passthrough_no_hazard"
    assert event["byod_passthrough"] is True


def test_resolve_attaches_one_event_asked(log):
    state = {"tiffs": ["hazard_flood"], "place": "Testville", "events": []}
    out = gm.resolve(state)
    log("EVENTS", out.get("events"))
    assert len(out["events"]) == 1
    event = out["events"][0]
    assert event["decision"] == "asked"
    assert event["awaiting_choice_set"] is True
    assert out.get("awaiting_choice") is not None


def test_resolve_attaches_one_event_auto_single(monkeypatch, log):
    monkeypatch.setattr(gm.resolver, "options_for", lambda layer: [("exposure", "hazard_flood", "Exposure")])
    state = {"tiffs": ["hazard_flood"], "place": "Testville", "events": []}
    out = gm.resolve(state)
    event = out["events"][0]
    assert event["decision"] == "auto_single"
    assert event["awaiting_choice_set"] is False


def test_resolve_attaches_one_event_no_data(monkeypatch, log):
    monkeypatch.setattr(gm.resolver, "options_for", lambda layer: [])
    state = {"tiffs": ["hazard_flood"], "place": "Testville", "events": []}
    out = gm.resolve(state)
    event = out["events"][0]
    assert event["decision"] == "no_data"
    assert event["error"] == out["error"]


def _patch_fetch_emitting(monkeypatch, aoi):
    """Like _patch_fetch, but the fakes also call gm.ingest.emit(...), simulating what the
    real ensure_aoi/hazard_clip do internally, so fetch()'s drain/attach can be verified
    end-to-end, not just that business logic still works."""
    def fake_ensure_aoi(*a, **k):
        gm.ingest.emit({"kind": "api", "api": "Nominatim", "op": "geocode",
                        "query": "Testville", "n_results": 1})
        return aoi
    def fake_hazard_clip(place, layer):
        gm.ingest.emit({"kind": "cache", "what": "hazard_clip", "layer": layer,
                        "dest": gm.ingest.short_path(aoi[layer]), "was_cached": False})
        return aoi[layer]
    monkeypatch.setattr(gm.ingest, "ensure_aoi", fake_ensure_aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", fake_hazard_clip)


def test_fetch_attaches_one_event_success(aoi, monkeypatch, log):
    """fetch()'s events channel gets one real fetch step event with the io events drained
    from ensure_aoi/hazard_clip folded into api_calls/cache — plain [event], no _RESET."""
    _patch_fetch_emitting(monkeypatch, aoi)
    state = {"place": "Testville", "tiffs": ["hazard_flood"], "events": []}
    out = gm.fetch(state)
    log("EVENTS", out.get("events"))
    assert len(out["events"]) == 1
    event = out["events"][0]
    assert event["node"] == "fetch"
    assert event["mode"] == "place_lookup"
    assert event["error"] is None
    assert event["rasters_clipped"] == ["hazard_flood"]
    assert any(c["api"] == "Nominatim" for c in event["api_calls"])
    assert any(c["what"] == "hazard_clip" for c in event["cache"])
    assert event["downloads"] == []               # a clip is derived locally, not downloaded
    assert out["aoi"] == aoi                                      # business logic untouched


def test_fetch_attaches_one_event_failure(monkeypatch, log):
    def boom(*a, **k):
        raise ValueError("no administrative boundary for 'Atlantis'")
    monkeypatch.setattr(gm.ingest, "ensure_aoi", boom)
    state = {"place": "Atlantis", "tiffs": [], "events": []}
    out = gm.fetch(state)
    log("EVENTS", out.get("events"))
    assert len(out["events"]) == 1
    event = out["events"][0]
    assert event["error"] is not None
    assert "Atlantis" in event["error"]
    assert event["aoi"] == {"name": None, "area_km2": None, "how": None}
    assert out["error"] == event["error"]


def test_fetch_event_mode_drawn_area(aoi, monkeypatch, log):
    _patch_fetch_emitting(monkeypatch, aoi)
    state = {"req_geometry": [103.0, 13.0, 103.5, 13.5], "tiffs": [], "events": []}
    out = gm.fetch(state)
    assert out["events"][0]["mode"] == "drawn_area"


def test_fetch_event_collector_does_not_leak_into_next_call(aoi, monkeypatch, log):
    """Regression: a second fetch() call must not see the first call's drained io events."""
    _patch_fetch_emitting(monkeypatch, aoi)
    state = {"place": "Testville", "tiffs": ["hazard_flood"], "events": []}
    gm.fetch(state)
    out2 = gm.fetch(state)
    # each call installs its own fresh collector, so counts must match, not accumulate
    assert len(out2["events"][0]["api_calls"]) == 1
