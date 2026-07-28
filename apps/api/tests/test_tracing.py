"""tracing.py in isolation: the hazard/risk prefix split, the countable-asset guess, and
the two trace-event builders (make_trace_event_router for the three LLM-calling outcomes,
make_trace_event_no_llm for the apply_choice resume) — each checked against the actual
required-field set in the trace schema's baseStep + routeStep, not just spot-checked.
"""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.graph import tracing

# Mirrors trace schema's $defs.baseStep.required + $defs.routeStep's own required list.
# A change to either side (code or schema) that drops a key should break this test.
_REQUIRED_FIELDS = {
    "step", "started_at", "ended_at", "duration", "summary",           # baseStep
    "node", "llm_provider", "model_used", "tokens", "user_drawn_area", "drawn_area_type",
    "messages", "available_assets", "error", "derived_tool_calls", "derived_place",
    "derived_countable_assets", "derived_hazard_layers_used", "derived_risk_layers_used",
}


def _tool_call(call_id, name, args):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def _llm_response(content=None, tool_calls=None, prompt_tokens=10, completion_tokens=5):
    """A stand-in for the OpenAI SDK's ChatCompletion — same shape StubClient builds
    in conftest.py, but constructed directly so these tests don't need a real graph run."""
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


def _config():
    return {"configurable": {"provider": "gemini", "model": "gemini-2.5-flash"}}


def assert_valid_route_step(event, log=None):
    """Every field baseStep/routeStep requires is present (schema `required` only checks
    presence, not non-null — several are legitimately null on some kinds)."""
    missing = _REQUIRED_FIELDS - event.keys()
    if log:
        log("SCHEMA CHECK", f"missing={missing or 'none'}")
    assert not missing, f"trace event missing required field(s): {missing}"
    assert event["node"] == "router"
    assert event["kind"] in {"apply_choice", "declined", "missing_place", "routed"}
    assert set(event["available_assets"]) == {"available_tools", "countable", "hazard_layers", "risk_layers"}
    assert {"in", "out", "total", "cost"} <= set(event["tokens"])


# --- pure helpers -----------------------------------------------------------------------

def test_split_data_layers_by_prefix(log):
    """hazard_/risk_ prefixes (including the _l2 suffix variant) split correctly; anything
    else (e.g. a BYOD layer) lands in neither bucket."""
    layers = ["hazard_flood", "risk_flood", "risk_flood_l2", "byod_abc123"]
    hazard, risk = tracing._split_data_layers_by_prefix(layers)
    log("SPLIT", f"hazard={hazard} risk={risk}")
    assert hazard == ["hazard_flood"]
    assert risk == ["risk_flood", "risk_flood_l2"]


def test_split_data_layers_by_prefix_empty(log):
    assert tracing._split_data_layers_by_prefix([]) == ([], [])


def test_derive_countable_assets_from_layer_arg(log):
    """count_features/count_in_hazard name the asset explicitly via `layer`."""
    assert tracing._derive_countable_assets({"layer": "schools"}, "count_in_hazard") == ["schools"]


def test_derive_countable_assets_roads_implied_by_tool_name(log):
    """roads_in_hazard has no `layer` arg — "roads" is implied by the tool itself."""
    assert tracing._derive_countable_assets({}, "roads_in_hazard") == ["roads"]


def test_derive_countable_assets_neither(log):
    assert tracing._derive_countable_assets({}, "count_features") == []


# --- make_trace_event_router: declined / missing_place / routed ------------------------

_AVAILABLE_LAYERS = {"hazard_flood": "flood hazard raster", "risk_flood": "precomputed flood risk"}
_MESSAGES = [{"role": "system", "content": "you are a routing agent"},
             {"role": "user", "content": "how many schools flood in Battambang?"}]


def test_router_event_declined(log):
    """No tool call at all -> kind=declined, error carried through, assistant message is text."""
    resp = _llm_response(content="I can't answer that with the data I have.", tool_calls=None)
    event = tracing.make_trace_event_router(
        start_time=100.0, end_time=100.6, started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:00:00.6+00:00", state={"events": []}, config=_config(),
        llm_response=resp, messages=_MESSAGES, available_layers=_AVAILABLE_LAYERS,
        error="I can't answer that with the data I have.")
    log("EVENT", {k: event[k] for k in ("kind", "error", "derived_tool_calls")})
    assert_valid_route_step(event, log)
    assert event["kind"] == "declined"
    assert event["error"] == "I can't answer that with the data I have."
    assert event["derived_tool_calls"] is None
    assert event["derived_place"] is None
    assert event["derived_hazard_layers_used"] == []
    assert event["messages"][0] == {"role": "system", "type": "text", "content": "you are a routing agent"}
    assert event["messages"][-1] == {"role": "assistant", "type": "text",
                                     "content": "I can't answer that with the data I have."}
    assert event["duration"] == pytest.approx(600)  # (100.6 - 100.0) * 1000, modulo float error


def test_router_event_missing_place(log):
    """A tool call with no place -> kind=missing_place, error set, tool call still captured."""
    resp = _llm_response(tool_calls=[_tool_call("c1", "roads_in_hazard", {"hazard_layers": ["hazard_flood"]})])
    event = tracing.make_trace_event_router(
        start_time=0.0, end_time=0.4, started_at="t0", ended_at="t1",
        state={"events": []}, config=_config(), llm_response=resp, messages=_MESSAGES,
        available_layers=_AVAILABLE_LAYERS, error="Name a place (a city or district) and I'll check it.")
    log("EVENT", {k: event[k] for k in ("kind", "error", "derived_place")})
    assert_valid_route_step(event, log)
    assert event["kind"] == "missing_place"
    assert event["error"] == "Name a place (a city or district) and I'll check it."
    assert event["derived_place"] is None                          # no place in the args
    assert event["derived_tool_calls"] == [
        {"id": "c1", "function_name": "roads_in_hazard", "function_args": {"hazard_layers": ["hazard_flood"]}}]
    assert event["messages"][-1] == {"role": "assistant", "type": "tool_call"}  # no payload here


def test_router_event_routed(log):
    """A full tool call (place + layer + hazard) -> kind=routed, error None, everything derived."""
    args = {"place": "Battambang", "hazard_layers": ["hazard_flood"], "layer": "schools", "min_severity": 3}
    resp = _llm_response(tool_calls=[_tool_call("c1", "count_in_hazard", args)])
    event = tracing.make_trace_event_router(
        start_time=0.0, end_time=1.2, started_at="t0", ended_at="t1",
        state={"events": []}, config=_config(), llm_response=resp, messages=_MESSAGES,
        available_layers=_AVAILABLE_LAYERS, error=None)
    log("EVENT", {k: event[k] for k in ("kind", "derived_place", "derived_countable_assets")})
    assert_valid_route_step(event, log)
    assert event["kind"] == "routed"
    assert event["error"] is None
    assert event["derived_place"] == "Battambang"
    assert event["derived_hazard_layers_used"] == ["hazard_flood"]
    assert event["derived_countable_assets"] == ["schools"]
    tool_call = event["derived_tool_calls"][0]
    assert tool_call["id"] == "c1"
    assert tool_call["function_args"]["min_severity"] == 3          # parsed to a real int, not a JSON string
    assert isinstance(tool_call["function_args"], dict)


def test_router_event_drawn_area_bbox_list(log):
    """req_geometry can be a [minLon,minLat,maxLon,maxLat] bbox list (Mode 2), not just a
    GeoJSON dict — must not crash calling .get() on a list."""
    resp = _llm_response(tool_calls=[_tool_call("c1", "count_features", {"layer": "schools"})])
    event = tracing.make_trace_event_router(
        start_time=0.0, end_time=0.1, started_at="t0", ended_at="t1",
        state={"events": [], "req_geometry": [103.0, 13.0, 103.5, 13.5]}, config=_config(),
        llm_response=resp, messages=_MESSAGES, available_layers=_AVAILABLE_LAYERS, error=None)
    assert event["user_drawn_area"] is True
    assert event["drawn_area_type"] == "rectangle"


def test_router_event_drawn_area_geojson_dict(log):
    """req_geometry as an actual GeoJSON dict still reports its own "type"."""
    resp = _llm_response(tool_calls=[_tool_call("c1", "count_features", {"layer": "schools"})])
    event = tracing.make_trace_event_router(
        start_time=0.0, end_time=0.1, started_at="t0", ended_at="t1",
        state={"events": [], "req_geometry": {"type": "Polygon", "coordinates": []}}, config=_config(),
        llm_response=resp, messages=_MESSAGES, available_layers=_AVAILABLE_LAYERS, error=None)
    assert event["user_drawn_area"] is True
    assert event["drawn_area_type"] == "Polygon"


def test_router_event_no_drawn_area(log):
    resp = _llm_response(content="declined", tool_calls=None)
    event = tracing.make_trace_event_router(
        start_time=0.0, end_time=0.1, started_at="t0", ended_at="t1",
        state={"events": [], "req_geometry": None}, config=_config(),
        llm_response=resp, messages=_MESSAGES, available_layers=_AVAILABLE_LAYERS, error="declined")
    assert event["user_drawn_area"] is False
    assert event["drawn_area_type"] is None


def test_router_event_step_counts_existing_events(log):
    """`step` is the position in the turn's step list so far — reads state, never mutates it."""
    resp = _llm_response(content="declined", tool_calls=None)
    prior = [{"node": "router", "kind": "declined"}]  # pretend one step already ran this turn
    event = tracing.make_trace_event_router(
        start_time=0.0, end_time=0.1, started_at="t0", ended_at="t1",
        state={"events": prior}, config=_config(), llm_response=resp, messages=_MESSAGES,
        available_layers=_AVAILABLE_LAYERS, error="declined")
    assert event["step"] == 1
    assert prior == [{"node": "router", "kind": "declined"}]  # untouched


# --- make_trace_event_no_llm: apply_choice ----------------------------------------------

def test_no_llm_event_apply_choice(log):
    """Resuming a paused exposure/risk choice: no LLM fields populated, tokens legitimately
    zero, and the chosen option is reverse-matched from awaiting_choice for the narrative."""
    resumed_delta = {
        "operation": "count_in_hazard", "place": "Siem Reap",
        "op_args": {"layer": "schools", "min_severity": 3}, "tiffs": ["risk_flood"],
        "tool_call_id": "c1", "req_geometry": None, "awaiting_choice": None, "_resume": True,
        "trace": ["choice → risk-L1: risk_flood"],
    }
    awaiting_choice = {
        "operation": "count_in_hazard", "place": "Siem Reap",
        "op_args": {"layer": "schools", "min_severity": 3}, "tool_call_id": "c1", "req_geometry": None,
        "options": [("exposure", "hazard_flood", "Exposure — raw hazard footprint"),
                    ("risk-L1", "risk_flood", "Risk — official precomputed")],
    }
    state = {"messages": [{"role": "user", "content": "2"}], "events": []}
    event = tracing.make_trace_event_no_llm(
        start_time=0.0, end_time=0.02, started_at="t0", ended_at="t1", state=state,
        config=_config(), resumed_delta=resumed_delta, awaiting_choice=awaiting_choice)
    log("EVENT", {k: event[k] for k in ("kind", "summary", "llm_provider", "tokens")})
    assert_valid_route_step(event, log)
    assert event["kind"] == "apply_choice"
    assert event["llm_provider"] is None
    assert event["model_used"] is None
    assert event["tokens"] == {"in": 0, "out": 0, "total": 0, "cost_in": 0, "cost_out": 0, "cost": 0}
    assert event["derived_tool_calls"] is None
    assert event["derived_place"] == "Siem Reap"
    assert event["derived_risk_layers_used"] == ["risk_flood"]
    assert event["derived_hazard_layers_used"] == []
    assert event["derived_countable_assets"] == ["schools"]
    assert event["messages"] == [{"role": "user", "type": "text", "content": "2"}]
    assert "risk-L1" in event["summary"]                            # the picked option's label surfaced
    assert event["error"] is None
    assert all(v is None for v in event["available_assets"].values())  # nothing was offered this turn


# --- make_trace_event_operate: success / failure ----------------------------------------

_REQUIRED_FIELDS_OPERATE = {
    "step", "started_at", "ended_at", "duration", "summary",           # baseStep
    "node", "why", "operation", "min_severity", "result", "error",
}


def assert_valid_operate_step(event, log=None):
    missing = _REQUIRED_FIELDS_OPERATE - event.keys()
    if log:
        log("SCHEMA CHECK", f"missing={missing or 'none'}")
    assert not missing, f"trace event missing required field(s): {missing}"
    assert event["node"] == "operate"


def test_operate_event_success_with_severity(log):
    """count_in_hazard/roads_in_hazard-shaped result: min_severity and by_severity both
    come from the real result dict, value is the caller's already-computed num."""
    result = {"length_km": 4.2, "total_road_km": 10.0, "by_severity": {"3": 4.2},
              "hazard": "hazard_flood", "place": "Testville", "min_severity": 3,
              "source": "hazard_flood.tif × roads", "method": "roads_in_hazard"}
    event = tracing.make_trace_event_operate(
        start_time=0.0, end_time=0.05, started_at="t0", ended_at="t1", state={"events": []},
        operation="roads_in_hazard", min_severity=3, result=result, num=4.2, error=None)
    log("EVENT", {k: event[k] for k in ("operation", "result", "error")})
    assert_valid_operate_step(event, log)
    assert event["error"] is None
    assert event["result"] == {"method": "roads_in_hazard", "value": 4.2,
                               "by_severity": {"3": 4.2}, "source": "hazard_flood.tif × roads"}
    assert event["min_severity"] == 3


def test_operate_event_success_no_severity_concept(log):
    """count_features has no severity concept at all — min_severity/by_severity both None,
    no crash from calling .get() on keys that were never in the result dict."""
    result = {"count": 7, "layer": "schools", "place": "Testville",
              "source": "OSM schools", "method": "count_features"}
    event = tracing.make_trace_event_operate(
        start_time=0.0, end_time=0.05, started_at="t0", ended_at="t1", state={"events": []},
        operation="count_features", min_severity=None, result=result, num=7, error=None)
    assert_valid_operate_step(event, log)
    assert event["min_severity"] is None
    assert event["result"] == {"method": "count_features", "value": 7,
                               "by_severity": None, "source": "OSM schools"}


def test_operate_event_failure(log):
    """On failure, result is None (per the schema's own "null if the op failed") and
    min_severity is passed through verbatim — this function doesn't re-derive it."""
    event = tracing.make_trace_event_operate(
        start_time=0.0, end_time=0.01, started_at="t0", ended_at="t1", state={"events": []},
        operation="roads_in_hazard", min_severity=None, result=None, num=None,
        error="No data for that request: no administrative boundary for 'Atlantis'")
    log("EVENT", {k: event[k] for k in ("result", "error")})
    assert_valid_operate_step(event, log)
    assert event["result"] is None
    assert event["min_severity"] is None
    assert "Atlantis" in event["error"]


def test_operate_event_step_counts_existing_events(log):
    prior = [{"node": "router", "kind": "routed"}]
    event = tracing.make_trace_event_operate(
        start_time=0.0, end_time=0.01, started_at="t0", ended_at="t1", state={"events": prior},
        operation="count_features", min_severity=None, result={"method": "count_features", "count": 1,
        "source": "OSM"}, num=1, error=None)
    assert event["step"] == 1
    assert prior == [{"node": "router", "kind": "routed"}]  # untouched


# --- _usage: cost is per-million-token pricing -------------------------------------------

def test_usage_cost_uses_per_million_pricing(log):
    """price_in/price_out are USD per million tokens — cost must divide before multiplying."""
    resp = _llm_response(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    tokens = tracing._usage(resp, price_in=3.0, price_out=15.0)
    log("TOKENS", tokens)
    assert tokens["cost_in"] == pytest.approx(3.0)
    assert tokens["cost_out"] == pytest.approx(15.0)
    assert tokens["cost"] == pytest.approx(18.0)


# --- make_trace_event_finalize: error_echo / llm_phrase -----------------------------------

_REQUIRED_FIELDS_FINALIZE = {
    "step", "started_at", "ended_at", "duration", "summary",           # baseStep
    "node", "why", "kind", "error", "llm_provider", "model_used", "tokens",
    "llm_response", "grounded",
}


def assert_valid_finalize_step(event, log=None):
    missing = _REQUIRED_FIELDS_FINALIZE - event.keys()
    if log:
        log("SCHEMA CHECK", f"missing={missing or 'none'}")
    assert not missing, f"trace event missing required field(s): {missing}"
    assert event["node"] == "finalize"


def test_finalize_event_error_echo(log):
    """No LLM call: tokens/provider/model/grounded are all None."""
    event = tracing.make_trace_event_finalize(
        start_time=0.0, end_time=0.01, started_at="t0", ended_at="t1", state={"events": []},
        config=_config(), answer="No data for that request: unknown place",
        resp=None, grounded=None, error="No data for that request: unknown place")
    log("EVENT", {k: event[k] for k in ("kind", "tokens", "grounded")})
    assert_valid_finalize_step(event, log)
    assert event["kind"] == "error_echo"
    assert event["llm_provider"] is None
    assert event["tokens"] is None
    assert event["grounded"] is None
    assert event["llm_response"] == "No data for that request: unknown place"


def test_finalize_event_llm_phrase(log):
    """A real phrasing call: tokens/provider/model populated, grounded passed through from
    the caller (trace.record()'s own check), not recomputed here."""
    resp = _llm_response(content="12 hospitals are in Battambang.")
    event = tracing.make_trace_event_finalize(
        start_time=0.0, end_time=0.3, started_at="t0", ended_at="t1", state={"events": []},
        config=_config(), answer="12 hospitals are in Battambang.",
        resp=resp, grounded=True, error=None)
    log("EVENT", {k: event[k] for k in ("kind", "tokens", "grounded")})
    assert_valid_finalize_step(event, log)
    assert event["kind"] == "llm_phrase"
    assert event["error"] is None
    assert event["llm_provider"] == "gemini"
    assert event["grounded"] is True
    assert event["llm_response"] == "12 hospitals are in Battambang."


def test_finalize_event_step_counts_existing_events(log):
    prior = [{"node": "router"}, {"node": "operate"}]
    event = tracing.make_trace_event_finalize(
        start_time=0.0, end_time=0.01, started_at="t0", ended_at="t1", state={"events": prior},
        config=_config(), answer="ok", resp=None, grounded=None, error="ok")
    assert event["step"] == 2


# --- make_trace_event_resolve: passthrough / asked / auto_single / no_data ---------------

_REQUIRED_FIELDS_RESOLVE = {
    "step", "started_at", "ended_at", "duration", "summary",           # baseStep
    "node", "why", "decision", "hazard", "options", "byod_passthrough",
    "awaiting_choice_set", "question_asked", "error",
}
_OPTIONS = [{"key": "exposure", "layer": "hazard_flood", "label": "Exposure"},
            {"key": "risk-L1", "layer": "risk_flood", "label": "Risk L1"}]


def assert_valid_resolve_step(event, log=None):
    missing = _REQUIRED_FIELDS_RESOLVE - event.keys()
    if log:
        log("SCHEMA CHECK", f"missing={missing or 'none'}")
    assert not missing, f"trace event missing required field(s): {missing}"
    assert event["node"] == "resolve"


def test_resolve_event_passthrough_no_hazard(log):
    """No hazard layer at all (e.g. count_features) -> passthrough, byod_passthrough False."""
    event = tracing.make_trace_event_resolve(
        start_time=0.0, end_time=0.01, started_at="t0", ended_at="t1", state={"events": []},
        hazard=None, options=None, byod_passthrough=False, awaiting_choice_set=False,
        question_asked=None, error=None)
    assert_valid_resolve_step(event, log)
    assert event["decision"] == "passthrough_no_hazard"
    assert event["error"] is None


def test_resolve_event_passthrough_byod(log):
    """A BYOD layer never matches the hazard_/risk_ prefix check either -> same passthrough
    decision, byod_passthrough True distinguishes why."""
    event = tracing.make_trace_event_resolve(
        start_time=0.0, end_time=0.01, started_at="t0", ended_at="t1", state={"events": []},
        hazard=None, options=None, byod_passthrough=True, awaiting_choice_set=False,
        question_asked=None, error=None)
    assert_valid_resolve_step(event, log)
    assert event["decision"] == "passthrough_no_hazard"
    assert event["byod_passthrough"] is True


def test_resolve_event_asked(log):
    """Two or more options -> pauses and asks; question_asked carries the actual text."""
    event = tracing.make_trace_event_resolve(
        start_time=0.0, end_time=0.02, started_at="t0", ended_at="t1", state={"events": []},
        hazard="flood", options=_OPTIONS, byod_passthrough=None, awaiting_choice_set=True,
        question_asked="For **flood** in Testville, how would you like me to answer?", error=None)
    log("EVENT", {k: event[k] for k in ("decision", "awaiting_choice_set")})
    assert_valid_resolve_step(event, log)
    assert event["decision"] == "asked"
    assert event["awaiting_choice_set"] is True
    assert "flood" in event["question_asked"]


def test_resolve_event_auto_single(log):
    """Exactly one path -> used without asking, awaiting_choice_set False."""
    event = tracing.make_trace_event_resolve(
        start_time=0.0, end_time=0.01, started_at="t0", ended_at="t1", state={"events": []},
        hazard="drought", options=_OPTIONS[:1], byod_passthrough=None,
        awaiting_choice_set=False, question_asked=None, error=None)
    assert_valid_resolve_step(event, log)
    assert event["decision"] == "auto_single"
    assert event["awaiting_choice_set"] is False


def test_resolve_event_no_data(log):
    """Neither exposure nor risk available -> refuses, error set."""
    event = tracing.make_trace_event_resolve(
        start_time=0.0, end_time=0.01, started_at="t0", ended_at="t1", state={"events": []},
        hazard="fire", options=[], byod_passthrough=None, awaiting_choice_set=False,
        question_asked=None, error="I don't have the data to assess fire in Testville.")
    assert_valid_resolve_step(event, log)
    assert event["decision"] == "no_data"
    assert "fire" in event["error"]


def test_resolve_event_step_counts_existing_events(log):
    prior = [{"node": "router"}]
    event = tracing.make_trace_event_resolve(
        start_time=0.0, end_time=0.01, started_at="t0", ended_at="t1", state={"events": prior},
        hazard=None, options=None, byod_passthrough=False, awaiting_choice_set=False,
        question_asked=None, error=None)
    assert event["step"] == 1


# --- make_trace_event_fetch: success / failure, api_calls vs downloads split ------------

_REQUIRED_FIELDS_FETCH = {
    "step", "started_at", "ended_at", "duration", "summary",           # baseStep
    "node", "why", "mode", "aoi", "layers_fetched", "rasters_clipped",
    "l2_computed", "api_calls", "downloads", "error",
}


def assert_valid_fetch_step(event, log=None):
    missing = _REQUIRED_FIELDS_FETCH - event.keys()
    if log:
        log("SCHEMA CHECK", f"missing={missing or 'none'}")
    assert not missing, f"trace event missing required field(s): {missing}"
    assert event["node"] == "fetch"


def test_fetch_event_success_place_lookup(log):
    io = [{"kind": "api", "api": "Nominatim", "op": "geocode", "query": "Testville", "n_results": 1},
          {"kind": "clip", "layer": "hazard_flood", "dest": "/x/hazard_flood.tif", "was_cached": False}]
    event = tracing.make_trace_event_fetch(
        start_time=0.0, end_time=0.5, started_at="t0", ended_at="t1", state={"events": []},
        mode="place_lookup", aoi={"name": "Testville", "area_km2": 12, "how": "geocoded"},
        layers_fetched=None, rasters_clipped=["hazard_flood"], l2_computed=[],
        drained_io_events=io, error=None)
    assert_valid_fetch_step(event, log)
    assert event["mode"] == "place_lookup"
    assert event["error"] is None
    assert len(event["api_calls"]) == 1 and event["api_calls"][0]["api"] == "Nominatim"
    assert len(event["downloads"]) == 1 and event["downloads"][0]["kind"] == "clip"


def test_fetch_event_success_drawn_area_with_l2(log):
    event = tracing.make_trace_event_fetch(
        start_time=0.0, end_time=0.2, started_at="t0", ended_at="t1", state={"events": []},
        mode="drawn_area", aoi={"name": "drawn area", "area_km2": 5, "how": "drawn"},
        layers_fetched=["roads"], rasters_clipped=[], l2_computed=["risk_flood_l2"],
        drained_io_events=[], error=None)
    assert_valid_fetch_step(event, log)
    assert event["l2_computed"] == ["risk_flood_l2"]
    assert event["api_calls"] == []                 # cached AOI -> nothing external happened
    assert event["downloads"] == []


def test_fetch_event_failure(log):
    event = tracing.make_trace_event_fetch(
        start_time=0.0, end_time=0.05, started_at="t0", ended_at="t1", state={"events": []},
        mode="place_lookup", aoi={"name": None, "area_km2": None, "how": None},
        layers_fetched=None, rasters_clipped=[], l2_computed=[], drained_io_events=[],
        error="No data for that request: could not find 'Atlantis'")
    assert_valid_fetch_step(event, log)
    assert event["error"] is not None
    assert event["aoi"] == {"name": None, "area_km2": None, "how": None}


def test_fetch_event_splits_api_vs_downloads(log):
    """A mixed drain: Nominatim + Overpass go to api_calls, download/clip go to downloads."""
    io = [{"kind": "api", "api": "Nominatim"}, {"kind": "api", "api": "Overpass"},
          {"kind": "download", "api": "Google Drive", "layer": "hazard_flood", "was_cached": True},
          {"kind": "clip", "layer": "hazard_flood", "was_cached": False}]
    event = tracing.make_trace_event_fetch(
        start_time=0.0, end_time=0.1, started_at="t0", ended_at="t1", state={"events": []},
        mode="place_lookup", aoi={"name": "X", "area_km2": 1, "how": "geocoded"},
        layers_fetched=None, rasters_clipped=["hazard_flood"], l2_computed=[],
        drained_io_events=io, error=None)
    assert len(event["api_calls"]) == 2
    assert len(event["downloads"]) == 2


def test_fetch_event_step_counts_existing_events(log):
    prior = [{"node": "router"}, {"node": "resolve"}]
    event = tracing.make_trace_event_fetch(
        start_time=0.0, end_time=0.01, started_at="t0", ended_at="t1", state={"events": prior},
        mode="place_lookup", aoi={"name": "X", "area_km2": 1, "how": "geocoded"},
        layers_fetched=None, rasters_clipped=[], l2_computed=[], drained_io_events=[], error=None)
    assert event["step"] == 2


# --- _summarize_aoi: never leak the raw path bundle into a trace event ------------------

_RAW_AOI_BUNDLE = {"name": "Testville", "area_km2": 12, "how": "geocoded", "counts": {"roads": 4},
                   "admin": "/cache/testville/admin.geojson", "roads": "/cache/testville/roads.geojson",
                   "hazard_flood": "/cache/testville/hazard_flood.tif"}


def test_summarize_aoi_full(log):
    view = tracing._summarize_aoi(_RAW_AOI_BUNDLE)
    log("VIEW", view)
    assert view == {"name": "Testville", "area_km2": 12, "how": "geocoded"}
    assert "admin" not in view and "hazard_flood" not in view          # no raw paths leak through


def test_summarize_aoi_none(log):
    assert tracing._summarize_aoi(None) == {"name": None, "area_km2": None, "how": None}


def test_summarize_aoi_missing_keys(log):
    assert tracing._summarize_aoi({"name": "X"}) == {"name": "X", "area_km2": None, "how": None}


def test_fetch_event_aoi_is_summarized_from_raw_bundle(log):
    """make_trace_event_fetch takes the RAW bundle now, not a pre-built view — raw paths
    must never reach the returned event."""
    event = tracing.make_trace_event_fetch(
        start_time=0.0, end_time=0.1, started_at="t0", ended_at="t1", state={"events": []},
        mode="place_lookup", aoi=_RAW_AOI_BUNDLE, layers_fetched=None, rasters_clipped=[],
        l2_computed=[], drained_io_events=[], error=None)
    assert event["aoi"] == {"name": "Testville", "area_km2": 12, "how": "geocoded"}


def test_fetch_event_aoi_none_on_failure(log):
    """Matches graph.py's except-branch call shape: aoi=None, since ensure_aoi() may never
    have returned (or even been assigned) before raising."""
    event = tracing.make_trace_event_fetch(
        start_time=0.0, end_time=0.05, started_at="t0", ended_at="t1", state={"events": []},
        mode="place_lookup", aoi=None, layers_fetched=None, rasters_clipped=[], l2_computed=[],
        drained_io_events=[], error="No data for that request: could not find 'Atlantis'")
    assert event["aoi"] == {"name": None, "area_km2": None, "how": None}


# --- ingest's IOCollector / emit in isolation --------------------------------------------

from app.graph.geo import ingest as ingest_mod


def test_emit_noop_when_nothing_installed(log):
    """No collector installed -> emit() is a silent no-op, no crash."""
    ingest_mod.emit({"kind": "api", "api": "Nominatim"})   # must not raise


def test_collector_records_and_drains(log):
    c = ingest_mod.IOCollector()
    token = ingest_mod.install(c)
    ingest_mod.emit({"kind": "api", "api": "Nominatim"})
    ingest_mod.emit({"kind": "clip", "layer": "hazard_flood"})
    ingest_mod.uninstall(token)
    drained = c.drain()
    assert len(drained) == 2
    assert c.drain() == []                                     # drain empties


def test_collectors_do_not_leak_across_installs(log):
    """One collector's events must not appear in an unrelated, later-installed collector."""
    c1 = ingest_mod.IOCollector()
    t1 = ingest_mod.install(c1)
    ingest_mod.emit({"kind": "api", "api": "Nominatim"})
    ingest_mod.uninstall(t1)

    c2 = ingest_mod.IOCollector()
    t2 = ingest_mod.install(c2)
    ingest_mod.emit({"kind": "clip", "layer": "hazard_flood"})
    ingest_mod.uninstall(t2)

    assert c1.drain() == [{"kind": "api", "api": "Nominatim"}]
    assert c2.drain() == [{"kind": "clip", "layer": "hazard_flood"}]


# --- build_trace_envelope: the per-turn top-level envelope -------------------------------

from app.config import get_settings

_ROUTE_STEP = {"node": "router", "duration": 500, "tokens": {"in": 100, "out": 20, "total": 120, "cost": 0.01}}
_RESOLVE_STEP = {"node": "resolve", "duration": 5}                    # no tokens key at all
_OPERATE_STEP = {"node": "operate", "duration": 10}                   # no tokens key at all
_FINALIZE_STEP_LLM = {"node": "finalize", "duration": 300, "tokens": {"in": 50, "out": 30, "total": 80, "cost": 0.005}}
_FINALIZE_STEP_ERROR = {"node": "finalize", "duration": 1, "tokens": None}   # error_echo branch


def test_build_trace_envelope_sums_duration_over_all_steps(log):
    events = [_ROUTE_STEP, _RESOLVE_STEP, _OPERATE_STEP, _FINALIZE_STEP_LLM]
    envelope = tracing.build_trace_envelope(events, thread_id="t1", trace_id="tr1")
    log("TOTAL_DURATION", envelope["total_duration"])
    assert envelope["total_duration"] == 500 + 5 + 10 + 300


def test_build_trace_envelope_sums_tokens_only_where_present(log):
    events = [_ROUTE_STEP, _RESOLVE_STEP, _OPERATE_STEP, _FINALIZE_STEP_LLM]
    envelope = tracing.build_trace_envelope(events, thread_id="t1", trace_id="tr1")
    assert envelope["total_tokens"]["in"] == 100 + 50
    assert envelope["total_tokens"]["out"] == 20 + 30
    assert envelope["total_tokens"]["total"] == 120 + 80


def test_build_trace_envelope_skips_finalize_error_echo_tokens_none(log):
    """A finalize error_echo step's tokens: None must not raise or get coerced into the sum."""
    events = [_ROUTE_STEP, _FINALIZE_STEP_ERROR]
    envelope = tracing.build_trace_envelope(events, thread_id="t1", trace_id="tr1")
    assert envelope["total_tokens"]["in"] == 100                       # only the route step counted


def test_build_trace_envelope_cost_summed_from_precomputed_values(log):
    """No settings/config involved at all — cost is read straight from each step's own
    already-computed tokens['cost'], never re-derived from price_in/price_out here."""
    events = [_ROUTE_STEP, _FINALIZE_STEP_LLM]
    envelope = tracing.build_trace_envelope(events, thread_id="t1", trace_id="tr1")
    assert envelope["total_tokens"]["cost"] == 0.01 + 0.005


def test_build_trace_envelope_empty_events(log):
    envelope = tracing.build_trace_envelope([], thread_id="t1", trace_id="tr1")
    assert envelope["total_duration"] == 0
    assert envelope["total_tokens"] == {"in": 0, "out": 0, "total": 0, "cost": 0}
    assert envelope["steps"] == []


def test_build_trace_envelope_top_level_shape(log):
    envelope = tracing.build_trace_envelope([_ROUTE_STEP], thread_id="t1", trace_id="tr1")
    log("ENVELOPE_KEYS", set(envelope))
    assert set(envelope) == {"thread_id", "trace_id", "created_at", "total_duration", "total_tokens", "steps"}
    assert envelope["thread_id"] == "t1"
    assert envelope["trace_id"] == "tr1"
    datetime.fromisoformat(envelope["created_at"])                     # parses cleanly, doesn't raise


# --- write_trace_envelope: persisted alongside record()'s own file ----------------------

def test_write_trace_envelope_creates_file_named_by_trace_id(log):
    envelope = tracing.build_trace_envelope([_ROUTE_STEP], thread_id="t1", trace_id="tr-abc")
    tracing.write_trace_envelope(envelope)
    written = list(get_settings().traces_dir.glob("tr-abc.envelope.json"))
    log("WRITTEN", written)
    assert len(written) == 1


def test_write_trace_envelope_content_round_trips(log):
    envelope = tracing.build_trace_envelope([_ROUTE_STEP], thread_id="t1", trace_id="tr-roundtrip")
    tracing.write_trace_envelope(envelope)
    path = get_settings().traces_dir / "tr-roundtrip.envelope.json"
    assert json.loads(path.read_text()) == envelope


def test_write_trace_envelope_creates_traces_dir_if_missing(log, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "traces_dir", tmp_path / "not-yet-created")
    envelope = tracing.build_trace_envelope([], thread_id="t1", trace_id="tr-mkdir")
    tracing.write_trace_envelope(envelope)                       # must not raise
    assert (tmp_path / "not-yet-created" / "tr-mkdir.envelope.json").exists()


# --- topology drift guard: the frontend hardcodes this graph's shape --------------------

def _frontend_topology():
    """Parse the node and edge tables the web app hardcodes in graphTopology.ts.

    The frontend draws the execution-flow diagram from a hand-transcribed copy of
    _build_graph()'s topology, so that it can lay nodes out and highlight the path a turn
    actually took. Reading the real file here — rather than
    restating the expected shape in this test — is what makes this a drift guard instead
    of a second copy to keep in sync.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    source = (repo_root / "apps/web/src/lib/trace/graphTopology.ts").read_text()

    nodes = set(re.findall(r"^\s*\{ id: '(\w+)', kind: 'node'", source, re.MULTILINE))
    edges = {
        (match.group(1), match.group(2))
        for match in re.finditer(r"from: '(\w+)',\s*\n\s*to: '(\w+)',", source)
    }
    return nodes, edges


# The frontend gives the two LangGraph sentinels friendlier names, and splits __end__ in
# two so the paused branch can be drawn as its own box. Everything else is 1:1.
_TS_TO_LANGGRAPH = {"start": "__start__", "end": "__end__", "ask_end": "__end__"}


def test_graph_topology_matches_frontend(log):
    """graphTopology.ts must describe the same nodes and edges as _build_graph().

    Fails the moment a node or an edge is added, removed, or rerouted in graph.py without
    the diagram being updated — the price of hardcoding the topology in the frontend, paid
    here rather than discovered as a wrong picture in the UI.
    """
    from app.graph import get_graph

    drawable = get_graph().get_graph()
    backend_nodes = {n for n in drawable.nodes if not n.startswith("__")}
    backend_edges = {(e.source, e.target) for e in drawable.edges}

    frontend_nodes, frontend_edges_raw = _frontend_topology()
    frontend_edges = {
        (_TS_TO_LANGGRAPH.get(source, source), _TS_TO_LANGGRAPH.get(target, target))
        for source, target in frontend_edges_raw
    }

    log("BACKEND NODES", sorted(backend_nodes))
    log("FRONTEND NODES", sorted(frontend_nodes))
    assert frontend_nodes == backend_nodes, (
        "apps/web/src/lib/trace/graphTopology.ts is out of date with graph.py's nodes"
    )

    log("BACKEND EDGES", sorted(backend_edges))
    log("FRONTEND EDGES", sorted(frontend_edges))
    assert frontend_edges == backend_edges, (
        "apps/web/src/lib/trace/graphTopology.ts is out of date with graph.py's edges"
    )
