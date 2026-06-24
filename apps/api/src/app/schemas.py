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
    geometry: dict | list | None = Field(
        default=None,
        description="Mode 2: a user-drawn AOI — a GeoJSON Polygon geometry OR a [minLon,minLat,maxLon,maxLat] "
                    "bbox, in EPSG:4326. When set, it's used as the area instead of resolving a place from text.")
    hazard: str | None = Field(
        default=None,
        description="Optional explicit hazard (e.g. 'flood' or 'hazard_flood'), e.g. from a UI button; "
                    "otherwise the hazard is inferred from the text.")


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

    # --- Map-visualization payload (present when the answer is a geo result; ADDITIVE,
    # all optional — existing fields are unchanged). Everything is EPSG:4326. ---
    place: str | None = Field(default=None, description="Resolved place name, or 'drawn area'.")
    hazard: str | None = Field(default=None, description="Hazard layer used, e.g. 'hazard_flood'.")
    layer: str | None = Field(default=None, description="Asset layer: roads | hospitals | schools | buildings.")
    metric: dict | None = Field(default=None, description="value, unit, total, min_severity, by_severity.")
    legend: dict | None = Field(default=None, description="{class: {label, color}} severity scale (server-owned colors).")
    bounds: list[float] | None = Field(default=None, description="[minLon,minLat,maxLon,maxLat] AOI bbox, for fitting the map.")
    aoi: dict | None = Field(default=None, description="AOI boundary as a GeoJSON Feature (drawn / nominatim / radius_box via properties.source).")
    features: dict | None = Field(default=None, description="GeoJSON FeatureCollection of assets, each properties.severity 0-5.")
    hazard_layer: dict | None = Field(default=None, description="Hazard raster: {geojson: polygons by class, crs}. raster_url added in a later step.")

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
