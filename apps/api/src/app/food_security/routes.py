"""Food-security routes: module health + the thin crop-conditions query.

The conditions endpoint reports GEOGLAM Crop Monitor expert assessments verbatim
(per-region rating + driver) as the grounded input to the synthesis brief — it
computes nothing. Coverage gaps return an honest 404 naming the nearest
alternative, mirroring the platform's decline-don't-guess grounding policy.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import get_settings
from . import cropmonitor

router = APIRouter(prefix="/food-security")


@router.get("/health")
def health() -> dict:
    """Liveness for the food-security module + the upstream source it wraps."""
    return {"status": "ok", "module": "food-security",
            "source": get_settings().cropmonitor_url}


@router.get("/conditions")
def conditions(crop: str | None = None, month: str | None = None,
               place: str | None = None, geometry: bool = False) -> dict:
    """Per-region crop-condition assessments for a month (default: latest published)."""
    try:
        return cropmonitor.conditions(month=month, crop=crop, place=place,
                                      geometry=geometry)
    except ValueError as exc:                       # malformed month, not a coverage gap
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except cropmonitor.MonthNotAvailable as exc:
        raise HTTPException(status_code=404, detail={
            "error": "period_missing", "message": str(exc),
            "nearest_available": exc.nearest,
            # A span, not a promise of every month in it (December is skipped).
            "available": ({"from": exc.available[0], "to": exc.available[-1],
                           "note": "not every month in this span is published "
                                   "(the service skips December)"}
                          if exc.available else None)}) from exc
    except cropmonitor.CropNotFound as exc:
        raise HTTPException(status_code=404, detail={
            "error": "crop_not_found", "message": str(exc),
            "available": exc.available}) from exc
    except cropmonitor.CropMonitorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
