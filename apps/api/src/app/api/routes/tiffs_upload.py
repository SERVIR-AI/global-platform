"""POST /api/tiffs — upload a GeoTIFF as a per-thread hazard layer (Bring Your Own Data).

The upload is written to a quarantine temp file, verified, and registered for the thread
ONLY on a PASS (`byod_registry.register` runs the gate). The `VerifyReport` is returned
either way — `ok:true` with the new layer name, or `ok:false` with the mismatches so the UI
can explain the rejection. A failing upload is deleted, never registered. This is the first
multipart endpoint in the API.
"""
from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ...graph.geo import byod_registry

router = APIRouter()

_ALLOWED_EXT = {".tif", ".tiff"}
_MAX_BYTES = 200 * 1024 * 1024   # 200 MB cap on the raw upload


def _report_json(report, layer: str | None) -> dict:
    o = report.observed or {}
    return {
        "ok": report.ok,
        "layer": layer,                       # the registered layer name, or null on failure
        "hazard_label": report.name,
        "mismatches": report.mismatches,      # why it failed (empty on pass)
        "warnings": report.warnings,          # soft signals that didn't fail the gate
        "observed": {k: o.get(k) for k in
                     ("dtype", "crs_epsg", "sampled_min", "sampled_max",
                      "sampled_distinct", "width", "height")},
    }


@router.post("/tiffs")
async def upload_tiff(file: UploadFile = File(...), thread_id: str = Form(...),
                      hazard_label: str = Form(...), severity_scale: str = Form("0-5")) -> dict:
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in _ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"only .tif/.tiff accepted (got {ext or 'no extension'})")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=400, detail=f"file too large (> {_MAX_BYTES // (1024 * 1024)} MB)")

    # Quarantine: write to a temp file, then verify+register. register moves it into the tiff
    # cache on PASS; on FAIL it stays put and we delete it here — an unverified file never lingers.
    fd, quarantine = tempfile.mkstemp(suffix=".tif")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        layer, report = byod_registry.register(
            thread_id, hazard_label=hazard_label, severity_scale=severity_scale,
            src_path=quarantine, filename=file.filename)
    finally:
        if os.path.exists(quarantine):
            os.remove(quarantine)
    return _report_json(report, layer)
