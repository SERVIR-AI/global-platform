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
from functools import lru_cache
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from . import prompts
from .geo import combine, ingest, operations, resolver, tiffs, trace


def _add(left: list | None, right: list | None) -> list:
    return (left or []) + (right or [])


class State(TypedDict, total=False):
    messages: Annotated[list[dict], _add]  # OpenAI-format conversation dicts
    operation: str | None                  # which store op to run
    place: str | None                      # extracted location
    op_args: dict                          # remaining op params (layer, min_severity)
    tiffs: list[str]                       # hazard layers the op needs
    tool_call_id: str | None               # so finalize can build the tool message
    aoi: dict | None                       # the ensure_aoi bundle of paths
    result: dict | None                    # the computed result
    error: str | None                      # set on refusal/failure -> straight to finalize
    usage: Annotated[list[dict], _add]     # per-LLM-call {in, out} token counts
    trace: Annotated[list[str], _add]      # plain-text step narration (returned when verbose)
    req_geometry: dict | list | None       # Mode 2: a drawn AOI from the request
    req_hazard: str | None                 # explicit hazard from the request (e.g. a UI button)
    plan: list | None                      # resolver L1/L2 LayerPlan(s), recorded for the trace
    awaiting_choice: dict | None           # set when the agent asked L1-vs-L2; resumes on the reply
    _resume: bool                          # transient: this turn applied a pending L1/L2 choice


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    if not u:
        return {"in": 0, "out": 0}
    return {"in": getattr(u, "prompt_tokens", 0) or 0, "out": getattr(u, "completion_tokens", 0) or 0}


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
    p = state["awaiting_choice"]
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


def route(state: State, config) -> dict:
    """LLM picks the operation + place + hazard layers. Records intent; runs nothing.
    If we're resuming a pending L1/L2 choice, apply it without an LLM call."""
    if state.get("awaiting_choice"):
        return _apply_choice(state)
    client = config["configurable"]["client"]
    model = config["configurable"]["model"]
    geom = state.get("req_geometry")

    layers = tiffs.descriptions()
    system = prompts.system_prompt() + "\n\n" + _layer_note(layers)
    if geom is not None:
        system += ("\n\nThe user has drawn an area on the map, so the area is already given "
                   "— call the tool for the hazard/asset they ask about and pass "
                   "place='drawn area'; you do not need to name a place.")
    messages = [{"role": "system", "content": system}, *state["messages"]]
    resp = client.chat.completions.create(
        model=model, messages=messages, tools=operations.schema(list(layers)), max_tokens=600)

    msg = resp.choices[0].message
    out = {"usage": [_usage(resp)], "trace": [f'question: "{_last_user(state["messages"])}"']}
    if not msg.tool_calls:                       # model declined -> plain-text reply
        out["error"] = msg.content or "I can't answer that with the data I have."
        out["trace"].append("route → declined (no tool call)")
        return out

    call = msg.tool_calls[0]
    args = json.loads(call.function.arguments)
    place = args.pop("place", None)
    selected = args.pop("hazard_layers", [])
    if state.get("req_hazard"):                  # explicit hazard (e.g. a UI button) overrides
        forced = tiffs.resolve(state["req_hazard"])
        if forced:
            selected = [forced]
    if not place and geom is None:               # a drawn area makes a place name unnecessary
        out["error"] = "Name a place (a city or district) and I'll check it."
        out["trace"].append(f"route → {call.function.name} but no place named — refusing")
        return out

    out.update(operation=call.function.name, place=place or "drawn area", op_args=args,
               tiffs=selected, tool_call_id=call.id, _resume=False)
    out["trace"].append(
        f"route → {call.function.name}(place={place!r}, hazard_layers={selected}, {args})"
        "  [the model extracts these; it computes no numbers]")
    return out


def resolve(state: State) -> dict:
    """For any hazard question, ASK the user how to answer it — exposure vs precomputed risk
    (L1) vs recomputed risk (L2) — and pause (human-in-the-loop). With one path, use it; with
    none, refuse cleanly. The LLM only picks the hazard; the agent never guesses exposure vs risk."""
    layer = next((l for l in (state.get("tiffs") or []) if l.startswith(("hazard_", "risk_"))), None)
    if not layer:
        return {}                                  # e.g. count_features (no hazard) -> fetch
    hz = resolver._logical(layer)
    options = resolver.options_for(layer)
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
        return {"messages": [{"role": "assistant", "content": q}], "awaiting_choice": awaiting,
                "trace": [f"resolve → asking {hz}: {', '.join(o[0] for o in options)}; paused for the user"]}
    if options:                                    # exactly one path -> use it, no question
        return {"tiffs": [options[0][1]],
                "trace": [f"resolve → only {options[0][0]} available for {hz}"]}
    return {"error": f"I don't have the data to assess {hz} in {state.get('place') or 'that area'}.",
            "trace": [f"resolve → no data for {hz}"]}


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
    try:
        geom = state.get("req_geometry")
        needed = _needed_layers(state)
        aoi = (ingest.ensure_aoi(geometry=geom, layers=needed) if geom is not None
               else ingest.ensure_aoi(state["place"], layers=needed))
        for layer in state.get("tiffs") or []:
            if layer.startswith("risk_") and layer.endswith("_l2"):     # computed Layer-2 risk grid
                hazard = "hazard_" + layer[len("risk_"):-len("_l2")]    # risk_flood_l2 -> hazard_flood
                aoi = {**aoi, layer: combine.combine_l2(aoi, hazard)}
            else:
                aoi = {**aoi, layer: ingest.hazard_clip(aoi, layer)}
        c = aoi.get("counts") or {}
        trace = [f"boundary → {aoi['name']}  [{aoi.get('how') or 'cached AOI'}]",
                 f"exposure (OSM) → roads {c.get('roads', '?')} · hospitals {c.get('hospitals', '?')} · "
                 f"schools {c.get('schools', '?')} · buildings {c.get('buildings', '?')}"]
        if state.get("tiffs"):
            bits = [f"{l} (computed: Hazard × Vulnerability)" if l.startswith("risk_") and l.endswith("_l2")
                    else f"{l} clipped" for l in state["tiffs"]]
            trace.append("hazard/risk raster → " + ", ".join(bits))
        return {"aoi": aoi, "trace": trace}
    except Exception as e:                        # unresolvable place, too large, Overpass down
        return {"error": f"No data for that request: {e}", "trace": [f"fetch → failed: {e}"]}


def operate(state: State) -> dict:
    """Run the deterministic spatial op over the bundle — the only number-producing step."""
    try:
        result = operations.dispatch(state["operation"], state["aoi"],
                                     hazard_layers=state.get("tiffs"), **state["op_args"])
        num = result.get("length_km", result.get("count"))
        line = f"overlay (deterministic, no LLM) → {result['method']} = {num}"
        if result.get("by_severity"):
            line += f"  by_severity={result['by_severity']}"
        return {"result": result, "trace": [line]}
    except Exception as e:
        return {"error": f"No data for that request: {e}", "trace": [f"operate → failed: {e}"]}


def finalize(state: State, config) -> dict:
    """Phrase the answer (quoting the number + source). On error, return it without an LLM call."""
    question = _last_user(state["messages"])

    if state.get("error"):
        answer = state["error"]
        trace.record(question, answer, state.get("usage") or [], args=state.get("op_args"))
        return {"messages": [{"role": "assistant", "content": answer}]}

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
    return {"messages": [{"role": "assistant", "content": answer}], "usage": [_usage(resp)]}


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
    builder = StateGraph(State)
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
