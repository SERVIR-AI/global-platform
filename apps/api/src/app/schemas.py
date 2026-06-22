"""Request and response models.

The request shape mirrors a standard LLM chat call (a list of role/content
messages). The response is structured so a frontend can render it directly:
a single assistant message plus metadata (provider, model, token usage, ids).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from .config import Provider

Role = Literal["system", "user", "assistant"]


class ChatMessage(BaseModel):
    role: Role
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(..., min_length=1)
    # Optional per-call overrides. When omitted, the server defaults apply.
    provider: Provider | None = None
    model: str | None = None
    # Pass a stable thread_id to continue a prior conversation; the graph
    # checkpointer keeps history server-side keyed by this id.
    thread_id: str | None = None


class Usage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class ChatResponse(BaseModel):
    id: str
    thread_id: str
    message: ChatMessage
    provider: Provider
    model: str
    usage: Usage | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
