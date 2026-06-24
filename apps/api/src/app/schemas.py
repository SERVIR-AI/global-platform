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
    provider: Provider | None = Field(
        default=None, description="LLM provider override; defaults to the server's DEFAULT_PROVIDER.")
    model: str | None = Field(
        default=None, description="Model override; defaults to the provider's configured model.")
    thread_id: str | None = Field(
        default=None,
        description="Stable id to continue a prior conversation; the graph keeps history server-side by this id.")
    verbose: bool = Field(
        default=False,
        description="When true, the response includes `trace` — a step-by-step narration of how the answer "
                    "was produced (route → boundary → exposure → overlay), mirroring the CLI's -v output.")


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
    trace: list[str] | None = Field(
        default=None,
        description="Step-by-step narration of the run; present only when the request set verbose=true.")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
