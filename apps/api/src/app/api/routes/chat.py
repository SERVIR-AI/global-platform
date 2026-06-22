"""Chat endpoint: accepts standard LLM-style messages, runs the LangGraph app,
returns a frontend-ready JSON response.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from ...config import get_settings
from ...graph import get_graph
from ...schemas import ChatMessage, ChatRequest, ChatResponse, Usage

router = APIRouter()

_ROLE_TO_MESSAGE = {
    "user": HumanMessage,
    "assistant": AIMessage,
    "system": SystemMessage,
}


def _to_langchain(messages: list[ChatMessage]) -> list[BaseMessage]:
    return [_ROLE_TO_MESSAGE[m.role](content=m.content) for m in messages]


def _content_to_text(message: AIMessage) -> str:
    """Flatten message content to a string (content may be a str or block list)."""
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _extract_usage(message: AIMessage) -> Usage | None:
    """Normalize LangChain usage metadata into our Usage schema (provider-agnostic)."""
    meta = getattr(message, "usage_metadata", None)
    if not meta:
        return None
    return Usage(
        input_tokens=meta.get("input_tokens"),
        output_tokens=meta.get("output_tokens"),
        total_tokens=meta.get("total_tokens"),
    )


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    settings = get_settings()
    provider = request.provider or settings.default_provider
    thread_id = request.thread_id or str(uuid.uuid4())

    graph = get_graph()
    config = {
        "configurable": {
            "thread_id": thread_id,
            "provider": provider,
            "model": request.model,
        }
    }

    try:
        result = graph.invoke({"messages": _to_langchain(request.messages)}, config)
    except ValueError as exc:
        # e.g. unsupported provider from the factory.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface provider/runtime errors as 502
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}") from exc

    last = result["messages"][-1]
    if not isinstance(last, AIMessage):
        raise HTTPException(status_code=502, detail="Graph did not return an assistant message.")

    # The graph resolves the concrete model id at call time; report what was requested.
    resolved_model = request.model or getattr(settings, f"{provider}_model")

    return ChatResponse(
        id=getattr(last, "id", None) or str(uuid.uuid4()),
        thread_id=thread_id,
        message=ChatMessage(role="assistant", content=_content_to_text(last)),
        provider=provider,
        model=resolved_model,
        usage=_extract_usage(last),
    )
