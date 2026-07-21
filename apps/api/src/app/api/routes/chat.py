"""Chat endpoint: accepts standard LLM-style messages, runs the LangGraph app,
returns a frontend-ready JSON response.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from langchain_core.runnables import RunnableConfig

from ...config import get_settings
from ...graph import get_graph, tracing
from ...graph.geo import viz
from ...llm import MissingAPIKey, build_client, default_model
from ...schemas import ChatMessage, ChatRequest, ChatResponse, Usage

router = APIRouter()


def _usage(usages: list[dict]) -> Usage | None:
    if not usages:
        return None
    total_in = sum(u["in"] for u in usages)
    total_out = sum(u["out"] for u in usages)
    return Usage(input_tokens=total_in, output_tokens=total_out, total_tokens=total_in + total_out)


# Short, layman button labels for the 3-way ask (the agent's option keys -> what a user sees).
_CHOICE_LABELS = {
    "exposure": "Exposure — what sits in the hazard zone",
    "risk-L1": "Risk — official precomputed",
    "risk-L2": "Risk — recompute from layers",
}


def _choices(awaiting: dict | None) -> list[dict] | None:
    """Map the agent's paused options to clickable buttons: [{label, value}].

    `value` is the 1-based index the resolver already accepts as a reply, so a click
    sends the same thing a user typing "1"/"2"/"3" would.
    """
    options = (awaiting or {}).get("options")
    if not options:
        return None
    return [{"label": _CHOICE_LABELS.get(key, key), "value": str(i + 1)}
            for i, (key, *_rest) in enumerate(options)]


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    settings = get_settings()
    provider = request.provider or settings.default_provider
    model = request.model or default_model(provider)
    thread_id = request.thread_id or str(uuid.uuid4())

    try:
        client = build_client(provider)
    except MissingAPIKey as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    graph = get_graph()
    config: RunnableConfig = {"configurable": {"thread_id": thread_id, "client": client, "model": model, "provider": provider}}
    messages = [{"role": m.role, "content": m.content} for m in request.messages]

    # Only send req_geometry/req_hazard when present: a follow-up that omits them must NOT
    # overwrite the drawn AOI persisted from an earlier turn (the "same area" case).
    graph_input: dict = {"messages": messages}
    if request.geometry is not None:
        graph_input["req_geometry"] = request.geometry
    if request.hazard is not None:
        graph_input["req_hazard"] = request.hazard

    try:
        result = graph.invoke(graph_input, config)
    except Exception as exc:  # noqa: BLE001 - surface provider/runtime errors as 502
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc

    last = result["messages"][-1]
    answer = last.get("content") or "" if isinstance(last, dict) else ""

    # Additive: when the answer is a geo result, package the map-visualization layers.
    geo: dict = {}
    res, aoi = result.get("result"), result.get("aoi")
    if res and aoi:
        try:
            geo = viz.build_payload(aoi, res)
        except Exception:  # noqa: BLE001 - visualization is best-effort; never break the answer
            geo = {}

    # When the agent paused to ask (exposure vs precomputed-risk L1 vs recomputed-risk L2),
    # surface the options as buttons: the user clicks one and we send its `value` as the reply.
    choices = _choices(result.get("awaiting_choice"))

    response_id = str(uuid.uuid4())

    # Best-effort: a bug in envelope assembly/persistence must never break the actual answer.
    trace_envelope = None
    try:
        trace_envelope = tracing.build_trace_envelope(
            events=result.get("events") or [], thread_id=thread_id, trace_id=response_id)
        tracing.write_trace_envelope(trace_envelope)
    except Exception:  # noqa: BLE001 - envelope build/persist is best-effort; never break the answer
        trace_envelope = None

    return ChatResponse(
        id=response_id,
        thread_id=thread_id,
        message=ChatMessage(role="assistant", content=answer),
        provider=provider,
        model=model,
        usage=_usage(result.get("usage") or []),
        trace=result.get("trace") if request.verbose else None,
        trace_events=result.get("events"),
        trace_envelope=trace_envelope,
        choices=choices,
        **geo,
    )
