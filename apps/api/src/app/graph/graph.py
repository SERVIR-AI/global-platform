"""The flood-exposure agent as a hand-rolled LangGraph.

    START -> route -> fetch -> operate -> finalize -> END

route uses OpenAI tool-calling purely as structured extraction: it picks the
operation, extracts the place, and selects the hazard layer(s) from the catalog
descriptions — it runs nothing. fetch acquires the OSM data + clipped raster.
operate runs the deterministic spatial op (the only place a number is computed).
finalize phrases the answer, quoting that number and citing its source.

The LLM never computes a number. On a decline (no place / unavailable layer) or a
fetch/compute failure, `error` is set and finalize returns it verbatim with no LLM
call. State holds OpenAI-format message dicts and file paths, so it stays
JSON-serializable for the checkpointer (multi-turn memory keyed by thread_id).
"""

from __future__ import annotations

import json
import time
from functools import lru_cache
from typing import Annotated, Any, TypedDict, NotRequired

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from . import prompts
from .geo import byod_registry, combine, ingest, operations, resolver, tiffs, trace


def _add(left: list | None, right: list | None) -> list:
    return (left or []) + (right or [])


# The structured trace must be PER-TURN, but the checkpointer persists channels across turns.
# So `events` uses a reducer that starts fresh when the first node of a turn (route) emits the
# reset sentinel, and appends otherwise — giving each turn its own step list.
_EVENTS_RESET = "__events_reset__"


def _events_reducer(left: list | None, right: list | None) -> list:
    if right and right[0] == _EVENTS_RESET:
        return list(right[1:])
    return (left or []) + (right or [])


class InputState(TypedDict):
    """The only keys a caller must supply to graph.invoke() — everything else is internal."""
    messages: list[dict]
    req_geometry: NotRequired[dict | list | None]       # Mode 2: a drawn AOI from the request
    req_hazard: NotRequired[str | None]          # explicit hazard from the request (e.g. a UI button)


class State(TypedDict):  # total=True by default: keys are required unless NotRequired
    # Reducer keys: kept as bare Annotated (required) so LangGraph reliably detects
    # the _add reducer. Always present — messages comes in via InputState, and route
    # writes usage on every path.
    messages: Annotated[list[dict], _add]  # OpenAI-format conversation dicts
    usage: Annotated[list[dict], _add]     # per-LLM-call {in, out} token counts
    
    # Flow-dependent keys: absent on the decline/error short-circuits, so NotRequired.
    # Read them with state.get(...)
    operation: NotRequired[str]            # which store op to run
    place: NotRequired[str]                # extracted location
    op_args: NotRequired[dict]             # remaining op params (layer, min_severity)
    tiffs: NotRequired[list[str]]          # hazard layers the op needs
    tool_call_id: NotRequired[str]         # so finalize can build the tool message
    aoi: NotRequired[dict]                 # the ensure_aoi bundle of paths
    result: NotRequired[dict]              # the computed result
    error: NotRequired[str]                # set on refusal/failure -> straight to finalize
    trace: Annotated[list[str], _add]      # plain-text step narration (returned when verbose)
    events: Annotated[list[dict], _events_reducer]  # structured per-turn step events (the rich exportable trace)
    awaiting_choice: dict | None           # set when the agent asked L1-vs-L2; resumes on the reply
    _resume: bool                          # transient: this turn applied a pending L1/L2 choice


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    if not u:
        return {"in": 0, "out": 0}
    return {"in": getattr(u, "prompt_tokens", 0) or 0, "out": getattr(u, "completion_tokens", 0) or 0}


def _aoi_summary(aoi: dict) -> dict:
    """A compact view of the (large) aoi bundle for a step's state_changes."""
    import os
    return {"name": aoi.get("name"), "area_km2": aoi.get("area_km2"), "how": aoi.get("how"),
            "slug": os.path.basename(os.path.dirname(aoi["admin"])) if aoi.get("admin") else None,
            "counts": aoi.get("counts")}


def _summarize_delta(delta: dict) -> dict:
    """What the node wrote, summarized for the trace: append-channels noted as appended(n),
    the bulky aoi reduced to name/slug/counts, everything else kept verbatim."""
    out = {}
    for k, v in delta.items():
        if k == "events":
            continue
        if k in ("messages", "usage", "trace"):
            out[k] = f"appended({len(v)})" if isinstance(v, list) else "appended"
        elif k == "aoi" and isinstance(v, dict):
            out[k] = _aoi_summary(v)
        else:
            out[k] = v
    return out


def _step(node: str, t0: float, started: str, delta: dict, reset: bool = False, **extra) -> dict:
    """Attach one structured step event to a node's state delta (under the `events` channel).
    `extra` carries node-specific semantic fields (summary, why, llm, parsed, ...). `reset=True`
    (used by route, the turn's first node) starts a fresh per-turn step list."""
    event = {"node": node, "started_at": started, "ended_at": trace.now_iso(),
             "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
             **extra, "state_changes": _summarize_delta(delta)}
    return {**delta, "events": ([_EVENTS_RESET, event] if reset else [event])}


def _last_user(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content") or ""
    return ""


def _layer_note(layers: dict[str, str]) -> str:
    if not layers:
        return "No hazard layers are available; do not call a hazard operation."
    listed = "\n".join(f"- {name}: {desc}" for name, desc in layers.items())
    return ("Hazard layers available — pick the one(s) a question needs by matching these "
            "descriptions, and pass them as `hazard_layers`. If none fit, do not call a tool:\n"
            + listed)


def _apply_choice(state: State) -> dict:
    """The user just answered the exposure/L1/L2 question — apply it (no LLM call) and resume."""
    p: dict = state["awaiting_choice"]
    options = p["options"]                       # [(key, layer, label), ...]
    reply = _last_user(state["messages"]).strip().lower()
    chosen = None
    if reply[:1].isdigit():                      # picked by number
        i = int(reply[:1]) - 1
        if 0 <= i < len(options):
            chosen = options[i]
    if chosen is None:                           # picked by keyword
        kw = {"exposure": ("exposure", "raw", "hazard", "flooded", "expos"),
              "risk-L1": ("l1", "precomp", "official", "adpc"),
              "risk-L2": ("l2", "recomp", "comput", "custom", "weight")}
        chosen = next((o for o in options if any(k in reply for k in kw.get(o[0], ()))), None)
    chosen = chosen or options[0]
    return {"operation": p["operation"], "place": p.get("place"),
            "op_args": p.get("op_args") or {}, "tiffs": [chosen[1]],
            "tool_call_id": p.get("tool_call_id"), "req_geometry": p.get("req_geometry"),
            "awaiting_choice": None, "_resume": True,
            "trace": [f"choice → {chosen[0]}: {chosen[1]}"]}


def _route_menu(thread_id) -> dict:
    """The layers the route node offers: the built-in catalog plus any BYOD layers the user
    uploaded and verified in THIS thread. Per-thread, so one user's layer never leaks into
    another's menu (or the tool-schema enum that constrains which layer the model may pick)."""
    layers = tiffs.descriptions()
    if thread_id:
        layers = {**layers, **byod_registry.descriptions_for(thread_id)}
    return layers


def route(state: State, config) -> dict:
    """
    Ask the LLM to pick an operation, extract a place, and select hazard layers.
    If we're resuming a pending L1/L2 choice, apply it without an LLM call.
    Uses OpenAI tool-calling as structured extraction only — nothing is executed here.
    On decline (no tool call) or missing place, sets 'error' to skip fetch/operate.

    Args:
        state (State): current graph state with at least a 'messages' list
        config (dict): LangGraph config with 'configurable' keys 'client' and 'model'

    Returns:
        dict: partial State update with one of:
            - {'operation', 'place', 'op_args', 'tiffs', 'tool_call_id', 'usage'} on success
            - {'error', 'usage'} on model decline or missing place
    """

    t0, started = time.perf_counter(), trace.now_iso()
    if state.get("awaiting_choice"):
        pending = state["awaiting_choice"]
        delta = _apply_choice(state)
        chosen_layer = (delta.get("tiffs") or [None])[0]
        chosen = next((o for o in pending["options"] if o[1] == chosen_layer), None)
        return _step("route", t0, started, delta, reset=True, kind="apply_choice",
                     summary=f"Applied your choice: {chosen[0] if chosen else chosen_layer}",
                     why="You answered the exposure-vs-risk question, so the agent resumes with "
                         "your pick — no new model call.",
                     reply=_last_user(state["messages"]).strip(),
                     options_offered=[{"key": o[0], "layer": o[1]} for o in pending["options"]],
                     chosen=({"key": chosen[0], "layer": chosen[1]} if chosen else {"layer": chosen_layer}))
    client = config["configurable"]["client"]
    model = config["configurable"]["model"]
    provider = config["configurable"].get("provider")
    geom = state.get("req_geometry")

    layers = _route_menu(config["configurable"].get("thread_id"))
    system = prompts.system_prompt() + "\n\n" + _layer_note(layers)
    if geom is not None:
        system += ("\n\nThe user has drawn an area on the map, so the area is already given "
                   "— call the tool for the hazard/asset they ask about and pass "
                   "place='drawn area'; you do not need to name a place.")
    messages = [{"role": "system", "content": system}, *state["messages"]]
    tools = operations.schema(list(layers))
    resp = client.chat.completions.create(model=model, messages=messages, tools=tools, max_tokens=600)

    msg = resp.choices[0].message
    # Trace detail: what the model saw and returned (for end-user transparency).
    llm = {"provider": provider, "model": model, "tokens": _usage(resp)}
    request_info = {"tools_offered": [t["function"]["name"] for t in tools],
                    "available_layers": layers, "drawn_area": geom is not None}
    response_info = {"raw_content": msg.content,
                     "tool_calls": [{"name": tc.function.name, "arguments": tc.function.arguments}
                                    for tc in (msg.tool_calls or [])]}
    # Fresh routing turn: clear last turn's transients. The checkpointer keeps every non-append
    # channel, so a prior turn's `error` would re-fire finalize (re-emitting an old refusal even
    # after we now route correctly), and a prior `result`/`aoi` would attach a stale map. Reset
    # them so this turn stands on its own. (Choice-resume returns above and is unaffected.)
    out = {"usage": [_usage(resp)], "trace": [f'question: "{_last_user(state["messages"])}"'],
           "error": None, "result": None, "aoi": None, "operation": None, "tiffs": [],
           "op_args": {}, "place": None, "tool_call_id": None}
    if not msg.tool_calls:                       # model declined -> plain-text reply
        out["error"] = msg.content or "I can't answer that with the data I have."
        out["trace"].append("route → declined (no tool call)")
        return _step("route", t0, started, out, reset=True, kind="llm_route", declined=True,
                     summary="The model declined — answered in plain text, ran no tool",
                     why="Your question didn't map to an available tool or layer, so the agent "
                         "answers directly instead of inventing data.",
                     llm=llm, request=request_info, response=response_info, decline_reason=out["error"])

    call = msg.tool_calls[0]
    args = json.loads(call.function.arguments)
    place = args.pop("place", None)
    selected = args.pop("hazard_layers", [])
    override = False
    if state.get("req_hazard"):                  # explicit hazard (e.g. a UI button) overrides
        forced = tiffs.resolve(state["req_hazard"])
        if forced:
            selected = [forced]
            override = True
    if not place and geom is None:               # a drawn area makes a place name unnecessary
        out["error"] = "Name a place (a city or district) and I'll check it."
        out["trace"].append(f"route → {call.function.name} but no place named — refusing")
        return _step("route", t0, started, out, reset=True, kind="llm_route", declined=False,
                     summary=f"Chose {call.function.name} but no place was named — asking for one",
                     why="A hazard/asset question needs a place (or a drawn area); none was given, "
                         "so the agent asks rather than guessing.",
                     llm=llm, request=request_info, response=response_info,
                     parsed={"operation": call.function.name, "place": None,
                             "hazard_layers": selected, "op_args": args})

    out.update(operation=call.function.name, place=place or "drawn area", op_args=args,
               tiffs=selected, tool_call_id=call.id, _resume=False)
    out["trace"].append(
        f"route → {call.function.name}(place={place!r}, hazard_layers={selected}, {args})"
        "  [the model extracts these; it computes no numbers]")
    return _step("route", t0, started, out, reset=True, kind="llm_route", declined=False,
                 summary=f"Picked {call.function.name} for {out['place']}",
                 why="The model matched your question to a tool and extracted the place and layer(s); "
                     "it computes no numbers itself.",
                 llm=llm, request=request_info, response=response_info,
                 parsed={"operation": call.function.name, "place": out["place"],
                         "hazard_layers": selected, "op_args": args,
                         "min_severity": args.get("min_severity")},
                 byod_layer=any(str(l).startswith("byod_") for l in selected),
                 req_hazard_override=override)


def resolve(state: State) -> dict:
    """For any hazard question, ASK the user how to answer it — exposure vs precomputed risk
    (L1) vs recomputed risk (L2) — and pause (human-in-the-loop). With one path, use it; with
    none, refuse cleanly. The LLM only picks the hazard; the agent never guesses exposure vs risk."""
    t0, started = time.perf_counter(), trace.now_iso()
    tiff_layers = state.get("tiffs") or []
    layer = next((l for l in tiff_layers if l.startswith(("hazard_", "risk_"))), None)
    if not layer:                                  # count_features (no hazard) or a BYOD layer -> fetch
        byod = any(str(l).startswith("byod_") for l in tiff_layers)
        return _step("resolve", t0, started, {}, decision="passthrough_no_hazard",
                     byod_passthrough=byod, awaiting_choice_set=False,
                     summary=("Using your uploaded layer directly" if byod else "No hazard choice needed"),
                     why=("A user-uploaded layer has one meaning, so the agent skips the exposure/risk question."
                          if byod else "This question reads no hazard raster, so there's nothing to choose."))
    hz = resolver._logical(layer)
    options = resolver.options_for(layer)
    opts_view = [{"key": o[0], "layer": o[1], "label": o[2]} for o in options]
    if len(options) >= 2:                          # a real choice -> ask
        # Blank line between options so each renders on its own line (markdown collapses
        # single newlines into one paragraph); the UI also shows these as buttons.
        lines = "\n\n".join(f"**{i + 1})** {label}" for i, (_, _, label) in enumerate(options))
        where = state.get("place") or "the drawn area"
        q = (f"For **{hz}** in {where}, how would you like me to answer?\n\n{lines}\n\n"
             f"Reply with the number (1–{len(options)}) — or tap an option below.")
        awaiting = {"operation": state.get("operation"), "place": state.get("place"),
                    "op_args": state.get("op_args") or {}, "tool_call_id": state.get("tool_call_id"),
                    "req_geometry": state.get("req_geometry"), "options": options}
        delta = {"messages": [{"role": "assistant", "content": q}], "awaiting_choice": awaiting,
                 "trace": [f"resolve → asking {hz}: {', '.join(o[0] for o in options)}; paused for the user"]}
        return _step("resolve", t0, started, delta, hazard=hz, options=opts_view,
                     decision="asked", awaiting_choice_set=True,
                     summary=f"Asked how to answer {hz}: " + " / ".join(o[0] for o in options),
                     why=f"{hz.capitalize()} can be answered as exposure or as risk; the agent never "
                         "guesses which you want, so it pauses and asks.")
    if options:                                    # exactly one path -> use it, no question
        delta = {"tiffs": [options[0][1]],
                 "trace": [f"resolve → only {options[0][0]} available for {hz}"]}
        return _step("resolve", t0, started, delta, hazard=hz, options=opts_view,
                     decision="auto_single", awaiting_choice_set=False,
                     summary=f"Only one way to answer {hz} ({options[0][0]}) — used it without asking",
                     why=f"Just one data path exists for {hz}, so there's nothing to choose.")
    delta = {"error": f"I don't have the data to assess {hz} in {state.get('place') or 'that area'}.",
             "trace": [f"resolve → no data for {hz}"]}
    return _step("resolve", t0, started, delta, hazard=hz, options=opts_view,
                 decision="no_data", awaiting_choice_set=False,
                 summary=f"No data to assess {hz} — refusing",
                 why=f"Neither an exposure nor a risk layer is available for {hz}, so the agent refuses "
                     "rather than guessing.")


def _needed_layers(state: State):
    """Only the OSM asset layer(s) the chosen op actually reads — so a roads question
    doesn't trigger a buildings fetch (the dominant drawn-AOI latency). None = fetch all."""
    op = state.get("operation")
    if op == "roads_in_hazard":
        return ["roads"]
    layer = (state.get("op_args") or {}).get("layer")
    if op in ("count_in_hazard", "count_features") and layer in ingest.ASSET_LAYERS:
        return [layer]
    return None


def fetch(state: State) -> dict:
    """Acquire the OSM data + clip the selected hazard rasters to the AOI (place or drawn geometry)."""
    t0, started = time.perf_counter(), trace.now_iso()
    geom = state.get("req_geometry")
    mode = "drawn_area" if geom is not None else "place_lookup"
    # Install a collector for THIS node's own ingest calls (set + read in this thread), so the
    # deep Nominatim/Overpass/Drive events are captured and attached to this step.
    collector = trace.TraceCollector()
    token = trace.set_collector(collector)
    try:
        needed = _needed_layers(state)
        aoi = (ingest.ensure_aoi(geometry=geom, layers=needed) if geom is not None
               else ingest.ensure_aoi(state["place"], layers=needed))
        clipped, l2 = [], []
        for layer in state.get("tiffs") or []:
            if layer.startswith("risk_") and layer.endswith("_l2"):     # computed Layer-2 risk grid
                hazard = "hazard_" + layer[len("risk_"):-len("_l2")]    # risk_flood_l2 -> hazard_flood
                aoi = {**aoi, layer: combine.combine_l2(aoi, hazard)}
                l2.append(layer)
            else:
                aoi = {**aoi, layer: ingest.hazard_clip(aoi, layer)}
                clipped.append(layer)
        c = aoi.get("counts") or {}
        narration = [f"boundary → {aoi['name']}  [{aoi.get('how') or 'cached AOI'}]",
                     f"exposure (OSM) → roads {c.get('roads', '?')} · hospitals {c.get('hospitals', '?')} · "
                     f"schools {c.get('schools', '?')} · buildings {c.get('buildings', '?')}"]
        if state.get("tiffs"):
            bits = [f"{l} (computed: Hazard × Vulnerability)" if l.startswith("risk_") and l.endswith("_l2")
                    else f"{l} clipped" for l in state["tiffs"]]
            narration.append("hazard/risk raster → " + ", ".join(bits))
        io = collector.drain()
        api_calls = [e for e in io if e.get("kind") == "api"]
        downloads = [e for e in io if e.get("kind") != "api"]
        delta = {"aoi": aoi, "trace": narration}
        return _step("fetch", t0, started, delta, mode=mode,
                     aoi={"name": aoi.get("name"), "area_km2": aoi.get("area_km2"), "how": aoi.get("how")},
                     api_calls=api_calls, downloads=downloads,
                     layers_fetched=list(needed) if needed else list(ingest.ASSET_LAYERS),
                     rasters_clipped=clipped, l2_computed=l2,
                     summary=f"Loaded {aoi.get('name')} — {len(api_calls)} API call(s), {len(downloads)} file op(s)",
                     why="Fetched the area boundary and the layers this question needs, downloading any "
                         "raster that wasn't already cached.")
    except Exception as e:                        # unresolvable place, too large, Overpass down
        delta = {"error": f"No data for that request: {e}", "trace": [f"fetch → failed: {e}"]}
        return _step("fetch", t0, started, delta, mode=mode,
                     api_calls=[ev for ev in collector.drain() if ev.get("kind") == "api"],
                     summary=f"Couldn't get the data: {e}",
                     why="The place couldn't be resolved, the area was too large, or a data source "
                         "was unavailable.")
    finally:
        trace.reset(token)


def operate(state: State) -> dict:
    """Run the deterministic spatial operation — the only place a number is computed.

    Args:
        state (State): must contain 'operation', 'aoi', and 'op_args'

    Returns:
        dict: {'result': result_dict} on success, or {'error': str} on failure
    """
    t0, started = time.perf_counter(), trace.now_iso()
    try:
        result = operations.dispatch(state.get("operation"), state.get("aoi"),
                                     hazard_layers=state.get("tiffs"), **state["op_args"])
        num = result.get("length_km", result.get("count"))
        line = f"overlay (deterministic, no LLM) → {result['method']} = {num}"
        if result.get("by_severity"):
            line += f"  by_severity={result['by_severity']}"
        delta = {"result": result, "trace": [line]}
        return _step("operate", t0, started, delta, operation=state.get("operation"),
                     min_severity=(state.get("op_args") or {}).get("min_severity"),
                     result={"method": result.get("method"), "value": num,
                             "by_severity": result.get("by_severity"), "source": result.get("source")},
                     summary=f"Computed {result.get('method')} = {num}",
                     why="This is the only place a number is produced — a deterministic spatial "
                         "overlay, no model involved.")
    except Exception as e:
        delta = {"error": f"No data for that request: {e}", "trace": [f"operate → failed: {e}"]}
        return _step("operate", t0, started, delta, operation=state.get("operation"),
                     summary=f"Computation failed: {e}",
                     why="The overlay couldn't be computed for the fetched data.")


def finalize(state: State, config) -> dict:
    """Phrase the answer (quoting the number + source). On error, return it without an LLM call."""
    t0, started = time.perf_counter(), trace.now_iso()
    question = _last_user(state["messages"])

    if state.get("error"):
        answer = state["error"]
        trace.record(question, answer, state.get("usage") or [], args=state.get("op_args"))
        delta = {"messages": [{"role": "assistant", "content": answer}]}
        return _step("finalize", t0, started, delta, kind="error_echo", error=answer,
                     summary="Returned the refusal/failure message as-is",
                     why="On a refusal or data failure the agent returns the message verbatim — no "
                         "model call, so nothing is fabricated.")

    client = config["configurable"]["client"]
    model = config["configurable"]["model"]
    result = state["result"]

    # Rebuild the tool-call exchange so the model phrases from the real result.
    arguments = json.dumps({"place": state["place"], **(state.get("op_args") or {})})
    assistant = {"role": "assistant", "content": None, "tool_calls": [{
        "id": state["tool_call_id"], "type": "function",
        "function": {"name": state["operation"], "arguments": arguments}}]}
    tool_msg = {"role": "tool", "tool_call_id": state["tool_call_id"], "content": str(result)}

    system = prompts.system_prompt()
    if state.get("req_geometry"):
        system += ("\n\nThe user drew the area on the map; the result is for that drawn area. "
                   "Phrase it naturally (e.g. 'in the selected area') and do not ask for or "
                   "mention a missing place name.")
    messages = [{"role": "system", "content": system},
                *state["messages"], assistant, tool_msg]
    resp = client.chat.completions.create(model=model, messages=messages, max_tokens=400)
    answer = resp.choices[0].message.content or ""

    usages = (state.get("usage") or []) + [_usage(resp)]
    trace.record(question, answer, usages, result=result, args=state.get("op_args"))
    number = result.get("count", result.get("length_km"))
    grounded = str(number) in answer.replace(",", "")
    delta = {"messages": [{"role": "assistant", "content": answer}], "usage": [_usage(resp)]}
    return _step("finalize", t0, started, delta, kind="llm_phrase",
                 llm={"provider": config["configurable"].get("provider"), "model": model,
                      "tokens": _usage(resp)},
                 answer_excerpt=answer[:280], grounded=grounded,
                 summary="Wrote the final answer, quoting the computed number",
                 why="The model only phrases the result; the number it quotes is the one computed in "
                     "the operate step.")


def _after_route(state: State) -> str:
    if state.get("error"):
        return "finalize"
    return "fetch" if state.get("_resume") else "resolve"   # _resume = a choice was just applied


def _after_resolve(state: State) -> str:
    if state.get("error"):
        return "finalize"
    return "ask_end" if state.get("awaiting_choice") else "fetch"


def _after_fetch(state: State) -> str:
    return "finalize" if state.get("error") else "operate"


def _build_graph():
    builder = StateGraph(State, input_schema=InputState)
    builder.add_node("route", route)
    builder.add_node("resolve", resolve)
    builder.add_node("fetch", fetch)
    builder.add_node("operate", operate)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", _after_route,
                                  {"resolve": "resolve", "fetch": "fetch", "finalize": "finalize"})
    builder.add_conditional_edges("resolve", _after_resolve,
                                  {"fetch": "fetch", "ask_end": END, "finalize": "finalize"})
    builder.add_conditional_edges("fetch", _after_fetch, {"operate": "operate", "finalize": "finalize"})
    builder.add_edge("operate", "finalize")
    builder.add_edge("finalize", END)
    # MemorySaver is in-process; swap for a persistent checkpointer when durability
    # across restarts is needed. Multi-turn memory is keyed by thread_id.
    return builder.compile(checkpointer=MemorySaver())


@lru_cache
def get_graph():
    """Compiled graph singleton — compiled once, reused across requests."""
    return _build_graph()
