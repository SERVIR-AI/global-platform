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
from .geo import ingest, operations, tiffs, trace


def _add(left: list | None, right: list | None) -> list:
    """LangGraph list-append reducer used on the 'messages' and 'usage' state keys.

    Args:
        left (list | None): existing list (or None on first update)
        right (list | None): new items to append

    Returns:
        list: concatenated list
    """
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


def _usage(resp) -> dict:
    """Extract prompt and completion token counts from an OpenAI response object.

    Args:
        resp: OpenAI ChatCompletion response object

    Returns:
        dict: {'in': int, 'out': int} token counts (both 0 if usage is unavailable)
    """
    u = getattr(resp, "usage", None)
    if not u:
        return {"in": 0, "out": 0}
    return {"in": getattr(u, "prompt_tokens", 0) or 0, "out": getattr(u, "completion_tokens", 0) or 0}


def _last_user(messages: list[dict]) -> str:
    """Return the content of the most recent user message in the conversation.

    Args:
        messages (list[dict]): OpenAI-format message dicts with 'role' and 'content' keys

    Returns:
        str: content string of the last user message, or '' if none found
    """
    for m in reversed(messages):
        if m.get("role") == "user":
            return m.get("content") or ""
    return ""


def _layer_note(layers: dict[str, str]) -> str:
    """Format the hazard-layer catalog as a system-prompt block for the route node.

    Args:
        layers (dict[str, str]): {layer_key: description} from tiffs.descriptions()

    Returns:
        str: instruction text listing available layers, or a refusal message if empty
    """
    if not layers:
        return "No hazard layers are available; do not call a hazard operation."
    listed = "\n".join(f"- {name}: {desc}" for name, desc in layers.items())
    return ("Hazard layers available — pick the one(s) a question needs by matching these "
            "descriptions, and pass them as `hazard_layers`. If none fit, do not call a tool:\n"
            + listed)


def route(state: State, config) -> dict:
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
    client = config["configurable"]["client"]
    model = config["configurable"]["model"]

    layers = tiffs.descriptions()
    system = prompts.system_prompt() + "\n\n" + _layer_note(layers)
    messages = [{"role": "system", "content": system}, *state["messages"]]
    resp = client.chat.completions.create(
        model=model, messages=messages, tools=operations.schema(list(layers)), max_tokens=600)

    msg = resp.choices[0].message
    out = {"usage": [_usage(resp)]}
    if not msg.tool_calls:                       # model declined -> plain-text reply
        out["error"] = msg.content or "I can't answer that with the data I have."
        return out

    call = msg.tool_calls[0]
    args = json.loads(call.function.arguments)
    place = args.pop("place", None)
    selected = args.pop("hazard_layers", [])
    if not place:
        out["error"] = "Name a place (a city or district) and I'll check it."
        return out

    out.update(operation=call.function.name, place=place, op_args=args,
               tiffs=selected, tool_call_id=call.id)
    return out


def fetch(state: State) -> dict:
    """Download the source raster(s) and fetch/cache OSM data for the extracted place.

    Args:
        state (State): must contain 'place' and optionally 'tiffs' (hazard layer keys)

    Returns:
        dict: {'aoi': bundle_dict} on success, or {'error': str} if the place can't be resolved
    """
    try:
        for layer in state.get("tiffs") or []:
            ingest.source_raster(layer)          # download-on-demand + validates the layer
        return {"aoi": ingest.ensure_aoi(state["place"])}
    except Exception as e:                        # unresolvable place, too large, Overpass down
        return {"error": f"No data for that request: {e}"}


def operate(state: State) -> dict:
    """Run the deterministic spatial operation — the only place a number is computed.

    Args:
        state (State): must contain 'operation', 'aoi', and 'op_args'

    Returns:
        dict: {'result': result_dict} on success, or {'error': str} on failure
    """
    try:
        return {"result": operations.dispatch(state["operation"], state["aoi"], **state["op_args"])}
    except Exception as e:
        return {"error": f"No data for that request: {e}"}


def finalize(state: State, config) -> dict:
    """Phrase the answer for the user, quoting the computed number and its source.

    If 'error' is set in state, returns the error string directly with no LLM call.
    Otherwise, replays the tool-call exchange so the model phrases from the real result.

    Args:
        state (State): full graph state; uses 'error', 'result', 'messages', 'place',
                       'operation', 'op_args', 'tool_call_id', 'usage'
        config (dict): LangGraph config with 'configurable' keys 'client' and 'model'

    Returns:
        dict: partial State update with {'messages': [assistant_message_dict], 'usage': [...]}
    """
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

    messages = [{"role": "system", "content": prompts.system_prompt()},
                *state["messages"], assistant, tool_msg]
    resp = client.chat.completions.create(model=model, messages=messages, max_tokens=400)
    answer = resp.choices[0].message.content or ""

    usages = (state.get("usage") or []) + [_usage(resp)]
    trace.record(question, answer, usages, result=result, args=state.get("op_args"))
    return {"messages": [{"role": "assistant", "content": answer}], "usage": [_usage(resp)]}


def _after_route(state: State) -> str:
    """Conditional edge after route: skip to 'finalize' on error, otherwise proceed to 'fetch'.

    Args:
        state (State): current graph state

    Returns:
        str: next node name ('finalize' or 'fetch')
    """
    return "finalize" if state.get("error") else "fetch"


def _after_fetch(state: State) -> str:
    """Conditional edge after fetch: skip to 'finalize' on error, otherwise proceed to 'operate'.

    Args:
        state (State): current graph state

    Returns:
        str: next node name ('finalize' or 'operate')
    """
    return "finalize" if state.get("error") else "operate"


def _build_graph():
    """Wire up and compile the StateGraph with an in-process MemorySaver checkpointer.

    Returns:
        langgraph.graph.CompiledGraph: compiled graph ready to invoke
    """
    builder = StateGraph(State)
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
