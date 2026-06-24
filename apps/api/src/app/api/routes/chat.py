"""Chat endpoint: accepts standard LLM-style messages, runs the LangGraph app,
returns a frontend-ready JSON response.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from ...config import get_settings
from ...graph import get_graph
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
    config = {"configurable": {"thread_id": thread_id, "client": client, "model": model}}
    messages = [{"role": m.role, "content": m.content} for m in request.messages]

    try:
        result = graph.invoke({"messages": messages}, config)
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

    return ChatResponse(
        id=str(uuid.uuid4()),
        thread_id=thread_id,
        message=ChatMessage(role="assistant", content=answer),
        provider=provider,
        model=model,
        usage=_usage(result.get("usage") or []),
        trace=result.get("trace") if request.verbose else None,
        **geo,
    )
