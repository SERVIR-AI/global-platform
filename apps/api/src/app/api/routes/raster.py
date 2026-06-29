"""Serve the per-AOI clipped hazard rasters (small GeoTIFFs) so the frontend can use
an `ol/source/GeoTIFF` layer. Files live at <cache_dir>/<slug>/<layer>.tif, produced
by the geo pipeline when a place is queried. The URL is handed back in the chat
response as `hazard_layer.raster_url`.
"""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ...config import get_settings

router = APIRouter()

_SLUG = re.compile(r"[a-z0-9-]+")
_LAYER = re.compile(r"[a-z0-9_]+")


@router.get("/raster/{slug}/{layer}.tif")
def raster(slug: str, layer: str) -> FileResponse:
    """Stream a clipped hazard raster (EPSG:4326). 404 if the place wasn't queried yet."""
    if not _SLUG.fullmatch(slug) or not _LAYER.fullmatch(layer):
        raise HTTPException(status_code=400, detail="invalid raster id")
    cache = os.path.realpath(str(get_settings().cache_dir))
    path = os.path.realpath(os.path.join(cache, slug, f"{layer}.tif"))
    if not path.startswith(cache + os.sep) or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="raster not found — query that place/hazard first")
    return FileResponse(path, media_type="image/tiff", filename=f"{slug}_{layer}.tif")
