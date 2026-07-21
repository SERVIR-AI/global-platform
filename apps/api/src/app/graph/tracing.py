import json

from langchain_core.runnables import RunnableConfig
from ..config import get_settings
from .geo import operations, registry

def _last_user_message(messages: list) -> dict | None:
    return next((m for m in reversed(messages) if m.get("role") == "user"), None)

def _usage(resp, price_in, price_out) -> dict:
    u = getattr(resp, "usage", None)
    if not u:
        return {"in": 0, "out": 0, "total": 0, "cost_in": 0, "cost_out": 0, "cost": 0}
    price_in, price_out = price_in / 1_000_000, price_out / 1_000_000  # price is per million tokens
    tokens = {
        "in": getattr(u, "prompt_tokens", 0) or 0,
        "out": getattr(u, "completion_tokens", 0) or 0,
    }
    tokens["total"] = tokens["in"] + tokens["out"]
    tokens["cost_in"] = tokens["in"]*price_in
    tokens["cost_out"] = tokens["out"]*price_out
    tokens["cost"] = tokens["cost_in"] + tokens["cost_out"]
    return tokens

def get_tool_calls(tool_call_schema):
    return [item["function"]["name"] for item in tool_call_schema]

def _split_data_layers_by_prefix(layers) -> tuple[list, list]:
    """hazard_<x> vs risk_<x>[_l2] — the naming convention every layer name follows."""
    hazard = [l for l in layers if l.startswith("hazard_")]
    risk = [l for l in layers if l.startswith("risk_")]
    return hazard, risk

def _drawn_area_type(geometry) -> str | None:
    """req_geometry is either a GeoJSON dict (has its own "type") or a 4-element
    [minLon,minLat,maxLon,maxLat] bbox list/tuple (Mode 2 — see ingest.py's own
    isinstance(geometry, (list, tuple)) and len(geometry) == 4 check)."""
    if isinstance(geometry, (list, tuple)):
        return "rectangle" if len(geometry) == 4 else None
    if isinstance(geometry, dict):
        return geometry.get("type")
    return None

def _summarize_aoi(aoi: dict | None) -> dict:
    """Compact aoi view for fetchStep — never the full path bundle. aoi is None on the
    failure branch (ensure_aoi never returned one that turn)."""
    aoi = aoi or {}
    return {"name": aoi.get("name"), "area_km2": aoi.get("area_km2"), "how": aoi.get("how")}

def _derive_countable_assets(call_args: dict, function_name: str | None) -> list[str]:
    """Guess which countable asset (roads/hospitals/schools/buildings) a tool call
    targets. count_features/count_in_hazard name it via `layer`; roads_in_hazard has
    no such arg — "roads" is implied by the tool itself, not sent as data. Best-effort,
    not confirmed against the user."""
    if call_args.get("layer"):
        return [call_args["layer"]]
    if function_name == "roads_in_hazard":
        return ["roads"]
    return []

def make_trace_event_router(
        start_time: float,
        end_time: float,
        started_at: str,
        ended_at: str,
        state: dict,
        config: RunnableConfig,
        llm_response,
        messages: list,
        available_layers: dict,
        error: str | None,
) -> dict:
    """Build one routeStep trace event for route()'s LLM-calling branches (declined /
    missing_place / routed — NOT apply_choice, which has no LLM call and uses
    make_trace_event_no_llm instead).

    `error` should be exactly what route() set on out["error"] this turn (None on the
    success path). Combined with whether the model returned a tool call, this determines
    `kind`:
      - no tool call                -> "declined"
      - tool call, error is set     -> "missing_place"
      - tool call, error is None    -> "routed"
    """
    settings = get_settings()
    state = state or {}
    configurable = config["configurable"]
    llm_provider = configurable["provider"]
    model_used = configurable["model"]
    tokens = _usage(llm_response, price_in=settings.price_in, price_out=settings.price_out)

    geometry = state.get("req_geometry", None)
    user_drawn_area = bool(geometry)
    drawn_area_type = _drawn_area_type(geometry)

    system_message = messages[0]
    user_message = _last_user_message(messages)
    llm_message = llm_response.choices[0].message

    messages_out = [{"role": "system", "type": "text", "content": system_message.get("content")}]
    if user_message is not None:
        messages_out.append({"role": "user", "type": "text", "content": user_message.get("content")})

    if llm_message.tool_calls:
        messages_out.append({"role": "assistant", "type": "tool_call"})
        derived_tool_calls = [
            {"id": tool_call.id, "function_name": tool_call.function.name, "function_args": json.loads(tool_call.function.arguments)}
            for tool_call in llm_message.tool_calls
        ]
    else:
        messages_out.append({"role": "assistant", "type": "text", "content": llm_message.content})
        derived_tool_calls = None

    tool_call_schema = operations.schema(list(available_layers))
    available_tools = get_tool_calls(tool_call_schema)
    countable_assets = list(registry.COUNTABLE)
    available_hazard_layers, available_risk_layers = _split_data_layers_by_prefix(available_layers)

    # derived_tool_calls is None on the declined branch (no tool call at all) — a real,
    # reachable case now that this function covers all three LLM-calling outcomes, not
    # just the success path.
    primary_call = derived_tool_calls[0] if derived_tool_calls else None
    call_args = primary_call["function_args"] if primary_call else {}
    function_name = primary_call["function_name"] if primary_call else None

    derived_place = call_args.get("place")
    derived_hazard_layers_used, derived_risk_layers_used = _split_data_layers_by_prefix(call_args.get("hazard_layers") or [])
    derived_countable_assets = _derive_countable_assets(call_args, function_name)

    if derived_tool_calls is None:
        kind = "declined"
        summary = "Router received a text reply with no tool call"
        why = "The model didn't match the question to any available tool, so it answered directly instead."
    elif error is not None:
        kind = "missing_place"
        summary = f"Router matched `{function_name}` but no place was named"
        why = "A place (or a drawn area) is needed to answer, and neither was given, so the router is asking for one instead of guessing."
    else:
        kind = "routed"
        summary = f"Router matched the question to `{function_name}` for '{derived_place}'"
        why = "This step only extracts the tool call and its arguments from the model; no computation happens here."

    trace_event = {
        "node": "router",
        "step": len(state.get("events", [])),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": (end_time - start_time)*1000,
        "summary": summary,
        "why": why,
        "kind": kind,
        "llm_provider": llm_provider,
        "model_used": model_used,
        "user_drawn_area": user_drawn_area,
        "drawn_area_type": drawn_area_type,
        "tokens": tokens,
        "messages": messages_out,
        "derived_tool_calls": derived_tool_calls,
        "available_assets": {
            "available_tools": available_tools,
            "countable": countable_assets,
            "hazard_layers": available_hazard_layers,
            "risk_layers": available_risk_layers,
        },
        "derived_place": derived_place,
        "derived_countable_assets": derived_countable_assets,
        "derived_hazard_layers_used": derived_hazard_layers_used,
        "derived_risk_layers_used": derived_risk_layers_used,
        "error": error,
    }
    return trace_event

def make_trace_event_no_llm(
        start_time: float,
        end_time: float,
        started_at: str,
        ended_at: str,
        state: dict,
        config: RunnableConfig,
        resumed_delta: dict,
        awaiting_choice: dict,
) -> dict:
    """Build one routeStep trace event for route()'s apply_choice branch — resuming a
    paused exposure/risk choice with no LLM call this turn.

    resumed_delta is exactly what _apply_choice(state) returned. awaiting_choice is
    state["awaiting_choice"] as read BEFORE _apply_choice ran (it clears that key in
    its own return value, so the caller must capture it first).
    """
    settings = get_settings()
    state = state or {}

    tokens = _usage(None, price_in=settings.price_in, price_out=settings.price_out)

    geometry = resumed_delta.get("req_geometry")
    user_drawn_area = bool(geometry)
    drawn_area_type = _drawn_area_type(geometry)

    user_message = _last_user_message(state["messages"])
    messages_out = [{"role": "user", "type": "text",
                      "content": user_message.get("content") if user_message else None}]

    derived_place = resumed_delta.get("place")
    derived_hazard_layers_used, derived_risk_layers_used = _split_data_layers_by_prefix(resumed_delta.get("tiffs") or [])
    derived_countable_assets = _derive_countable_assets(resumed_delta.get("op_args") or {}, resumed_delta.get("operation"))

    chosen_layer = (resumed_delta.get("tiffs") or [None])[0]
    chosen_option = next((o for o in (awaiting_choice.get("options") or []) if o[1] == chosen_layer), None)
    chosen_label = chosen_option[0] if chosen_option else chosen_layer

    trace_event = {
        "node": "router",
        "step": len(state.get("events", [])),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": (end_time - start_time)*1000,
        "summary": f"Applied the user's choice: {chosen_label}",
        "why": "This turn resumed a paused exposure/risk question and applied the user's "
               "reply deterministically — no model call was made.",
        "kind": "apply_choice",
        "llm_provider": None,
        "model_used": None,
        "user_drawn_area": user_drawn_area,
        "drawn_area_type": drawn_area_type,
        "tokens": tokens,
        "messages": messages_out,
        "derived_tool_calls": None,
        "available_assets": {
            "available_tools": None,
            "countable": None,
            "hazard_layers": None,
            "risk_layers": None,
        },
        "derived_place": derived_place,
        "derived_countable_assets": derived_countable_assets,
        "derived_hazard_layers_used": derived_hazard_layers_used,
        "derived_risk_layers_used": derived_risk_layers_used,
        "error": None,
    }
    return trace_event

def make_trace_event_operate(
        start_time: float,
        end_time: float,
        started_at: str,
        ended_at: str,
        state: dict,
        operation: str | None,
        min_severity: int | None,
        result: dict | None,
        num: int | float | None,
        error: str | None,
) -> dict:
    """Build one operateStep trace event. operate() never calls an LLM.

    `result` is the raw store.py dict (None on failure); 
    `num` is operate()'s own already-computed result.get("length_km", result.get("count")).
    `min_severity` is passed in rather than read from `result` here. 
    on success it's `result.get("min_severity")`; 
    on failure there's no `result` to read it from, so the caller falls back to 
    `op_args` instead - passing None when the model never explicitly supplied one.
    """
    state = state or {}

    if result is not None:
        result_out = {"method": result.get("method"), "value": num,
                      "by_severity": result.get("by_severity"), "source": result.get("source")}
        summary = f"Computed {result.get('method')} = {num}"
        why = "This is the only step that produces a number — a deterministic overlay, no model involved."
    else:
        result_out = None
        summary = f"Couldn't compute {operation}" if operation else "Couldn't compute the result"
        why = f"The overlay failed: {error}"

    trace_event = {
        "node": "operate",
        "step": len(state.get("events", [])),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": (end_time - start_time)*1000,
        "summary": summary,
        "why": why,
        "operation": operation,
        "min_severity": min_severity,
        "result": result_out,
        "error": error,
    }
    return trace_event

def make_trace_event_finalize(
        start_time: float,
        end_time: float,
        started_at: str,
        ended_at: str,
        state: dict,
        config: RunnableConfig,
        answer: str,
        resp,
        grounded: bool | None,
        error: str | None,
) -> dict:
    """Build one finalizeStep event. kind is "error_echo" if error is set, else "llm_phrase"."""
    state = state or {}
    kind = "error_echo" if error is not None else "llm_phrase"

    if kind == "error_echo":
        llm_provider = model_used = tokens = None
        summary = "Returned the refusal/failure message as-is"
        why = "No model call - the message is already final."
    else:
        settings = get_settings()
        configurable = config["configurable"]
        llm_provider = configurable["provider"]
        model_used = configurable["model"]
        tokens = _usage(resp, price_in=settings.price_in, price_out=settings.price_out)
        summary = "Phrased the final answer from the computed result"
        why = "The model only phrases the result; it doesn't compute the number."

    trace_event = {
        "node": "finalize",
        "step": len(state.get("events", [])),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": (end_time - start_time)*1000,
        "summary": summary,
        "why": why,
        "kind": kind,
        "error": error,
        "llm_provider": llm_provider,
        "model_used": model_used,
        "tokens": tokens,
        "llm_response": answer,
        "grounded": grounded,
    }
    return trace_event

def make_trace_event_resolve(
        start_time: float,
        end_time: float,
        started_at: str,
        ended_at: str,
        state: dict,
        hazard: str | None,
        options: list[dict] | None,
        byod_passthrough: bool | None,
        awaiting_choice_set: bool,
        question_asked: str | None,
        error: str | None,
) -> dict:
    """Build one resolveStep event.

    hazard: logical hazard name, None only on passthrough (no hazard layer, or a BYOD one).
    options: the [{key, layer, label}] choices offered; None on passthrough.
    byod_passthrough: True/False on passthrough (was it a BYOD layer, or just no hazard at
    all); None on every other branch, where it doesn't apply.
    awaiting_choice_set: True only when the graph paused here for the user's reply.

    decision is derived, not passed in:
      hazard is None       -> "passthrough_no_hazard"
      error is set         -> "no_data"
      awaiting_choice_set  -> "asked"
      else                 -> "auto_single"
    """
    state = state or {}

    if hazard is None:
        decision = "passthrough_no_hazard"
        summary = "Using your uploaded layer directly" if byod_passthrough else "No hazard choice needed"
        why = ("A user-uploaded layer has one meaning, so there's nothing to ask." if byod_passthrough
               else "This question reads no hazard raster, so there's nothing to choose.")
    elif error is not None:
        decision = "no_data"
        summary = f"No data to assess {hazard} — refusing"
        why = "Neither an exposure nor a risk layer is available for this hazard."
    elif awaiting_choice_set:
        decision = "asked"
        summary = f"Asked how to answer {hazard}"
        why = "Exposure vs risk is never guessed — the agent pauses and asks."
    else:
        decision = "auto_single"
        summary = f"Only one way to answer {hazard} — used it without asking"
        why = "Just one data path exists, so there's nothing to choose."

    trace_event = {
        "node": "resolve",
        "step": len(state.get("events", [])),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": (end_time - start_time)*1000,
        "summary": summary,
        "why": why,
        "decision": decision,
        "hazard": hazard,
        "options": options,
        "byod_passthrough": byod_passthrough,
        "awaiting_choice_set": awaiting_choice_set,
        "question_asked": question_asked,
        "error": error,
    }
    return trace_event

def make_trace_event_fetch(
        start_time: float,
        end_time: float,
        started_at: str,
        ended_at: str,
        state: dict,
        mode: str,
        aoi: dict | None,
        layers_fetched: list[str] | None,
        rasters_clipped: list[str],
        l2_computed: list[str],
        drained_io_events: list[dict],
        error: str | None,
) -> dict:
    """Build one fetchStep event. drained_io_events is the raw list from the IOCollector
    installed around this turn's ingest calls; split here into api_calls (kind == "api")
    and downloads (everything else), matching trace_schema.json's own description.

    aoi is the RAW ensure_aoi bundle (or None on failure) — this function derives the
    compact {name, area_km2, how} view itself via _summarize_aoi, same as every other
    builder derives its own summary fields internally.
    """
    state = state or {}
    aoi_view = _summarize_aoi(aoi)
    api_calls = [e for e in drained_io_events if e.get("kind") == "api"]
    downloads = [e for e in drained_io_events if e.get("kind") != "api"]

    if error is not None:
        summary = f"Couldn't fetch the data: {error}"
        why = "The AOI/raster fetch failed, so nothing downstream can compute a number."
    else:
        summary = f"Fetched {aoi_view.get('name')} ({len(rasters_clipped) + len(l2_computed)} raster(s))"
        why = "Acquires the OSM assets and clips the hazard/risk rasters this question needs; no LLM calls."

    trace_event = {
        "node": "fetch",
        "step": len(state.get("events", [])),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": (end_time - start_time)*1000,
        "summary": summary,
        "why": why,
        "mode": mode,
        "aoi": aoi_view,
        "layers_fetched": layers_fetched,
        "rasters_clipped": rasters_clipped,
        "l2_computed": l2_computed,
        "api_calls": api_calls,
        "downloads": downloads,
        "error": error,
    }
    return trace_event
