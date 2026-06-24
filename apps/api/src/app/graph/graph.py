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
from typing import Annotated, Any, NotRequired, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from . import prompts
from .geo import ingest, operations, tiffs, trace


def _add(left: list | None, right: list | None) -> list:
    return (left or []) + (right or [])


class InputState(TypedDict):
    """The only keys a caller must supply to graph.invoke() — everything else is internal."""
    messages: list[dict]


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


def route(state: State, config: RunnableConfig) -> dict:
    """Ask the LLM to pick an operation, extract a place, and select hazard layers.

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
    cfg: dict[str, Any] = config.get("configurable") or {}
    client = cfg["client"]
    model = cfg["model"]

    layers = tiffs.descriptions()
    system = prompts.system_prompt() + "\n\n" + _layer_note(layers)
    messages = [{"role": "system", "content": system}, *state["messages"]]
    resp = client.chat.completions.create(
        model=model, messages=messages, tools=operations.schema(list(layers)), max_tokens=600)

    msg = resp.choices[0].message
    out: dict[str, Any] = {"usage": [_usage(resp)], "trace": [f'question: "{_last_user(state["messages"])}"']}
    if not msg.tool_calls:                       # model declined -> plain-text reply
        out["error"] = msg.content or "I can't answer that with the data I have."
        out["trace"].append("route → declined (no tool call)")
        return out

    call = msg.tool_calls[0]
    args = json.loads(call.function.arguments)
    place = args.pop("place", None)
    selected = args.pop("hazard_layers", [])
    if not place:
        out["error"] = "Name a place (a city or district) and I'll check it."
        out["trace"].append(f"route → {call.function.name} but no place named — refusing")
        return out

    out.update(operation=call.function.name, place=place, op_args=args,
               tiffs=selected, tool_call_id=call.id)
    out["trace"].append(
        f"route → {call.function.name}(place={place!r}, hazard_layers={selected}, {args})"
        "  [the model extracts these; it computes no numbers]")
    return out


def fetch(state: State) -> dict:
    """Acquire the OSM data + clip the selected hazard rasters to the place."""
    try:
        aoi = ingest.ensure_aoi(state.get("place"))
        for layer in state.get("tiffs") or []:
            aoi = {**aoi, layer: ingest.hazard_clip(state.get("place"), layer)}
        c = aoi.get("counts") or {}
        trace = [f"boundary → {aoi['name']}  [{aoi.get('how') or 'cached AOI'}]",
                 f"exposure (OSM) → roads {c.get('roads', '?')} · hospitals {c.get('hospitals', '?')} · "
                 f"schools {c.get('schools', '?')} · buildings {c.get('buildings', '?')}"]
        if state.get("tiffs"):
            trace.append(f"hazard raster → {', '.join(state['tiffs'])} clipped to the AOI")
        return {"aoi": aoi, "trace": trace}
    except Exception as e:                        # unresolvable place, too large, Overpass down
        return {"error": f"No data for that request: {e}", "trace": [f"fetch → failed: {e}"]}


def operate(state: State) -> dict:
    """Run the deterministic spatial operation — the only place a number is computed.

    Args:
        state (State): must contain 'operation', 'aoi', and 'op_args'

    Returns:
        dict: {'result': result_dict} on success, or {'error': str} on failure
    """
    try:
        result = operations.dispatch(state.get("operation"), state.get("aoi"),
                                     hazard_layers=state.get("tiffs"), **state["op_args"])
        num = result.get("length_km", result.get("count"))
        line = f"overlay (deterministic, no LLM) → {result['method']} = {num}"
        if result.get("by_severity"):
            line += f"  by_severity={result['by_severity']}"
        return {"result": result, "trace": [line]}
    except Exception as e:
        return {"error": f"No data for that request: {e}", "trace": [f"operate → failed: {e}"]}


def finalize(state: State, config: RunnableConfig) -> dict:
    """Phrase the answer (quoting the number + source). On error, return it without an LLM call."""
    question = _last_user(state["messages"])

    error = state.get("error")
    if error:
        trace.record(question, error, state.get("usage") or [], args=state.get("op_args"))
        return {"messages": [{"role": "assistant", "content": error}]}

    cfg: dict[str, Any] = config.get("configurable") or {}
    client = cfg["client"]
    model = cfg["model"]
    result = state.get("result")

    # Rebuild the tool-call exchange so the model phrases from the real result.
    tool_call_id = state.get("tool_call_id")
    arguments = json.dumps({"place": state.get("place"), **(state.get("op_args") or {})})
    assistant = {"role": "assistant", "content": None, "tool_calls": [{
        "id": tool_call_id, "type": "function",
        "function": {"name": state.get("operation"), "arguments": arguments}}]}
    tool_msg = {"role": "tool", "tool_call_id": tool_call_id, "content": str(result)}

    messages = [{"role": "system", "content": prompts.system_prompt()},
                *state["messages"], assistant, tool_msg]
    resp = client.chat.completions.create(model=model, messages=messages, max_tokens=400)
    answer = resp.choices[0].message.content or ""

    usages = (state.get("usage") or []) + [_usage(resp)]
    trace.record(question, answer, usages, result=result, args=state.get("op_args"))
    return {"messages": [{"role": "assistant", "content": answer}], "usage": [_usage(resp)]}


def _after_route(state: State) -> str:
    return "finalize" if state.get("error") else "fetch"


def _after_fetch(state: State) -> str:
    return "finalize" if state.get("error") else "operate"


def _build_graph():
    """Wire up and compile the StateGraph with an in-process MemorySaver checkpointer.

    Returns:
        langgraph.graph.CompiledGraph: compiled graph ready to invoke
    """
    builder = StateGraph(State, input_schema=InputState)
    builder.add_node("route", route)
    builder.add_node("fetch", fetch)
    builder.add_node("operate", operate)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "route")
    builder.add_conditional_edges("route", _after_route, {"fetch": "fetch", "finalize": "finalize"})
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
