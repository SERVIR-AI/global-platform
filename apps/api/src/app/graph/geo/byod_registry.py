"""In-process registry of user-uploaded (BYOD) hazard rasters, keyed by thread_id.

Scope is per-thread and in-memory: a layer uploaded in one conversation is invisible to
others and is forgotten on restart, exactly like the chat checkpointer. The verified tif
is written into the tiff cache under its layer name, so `ingest.source_raster` resolves it
locally (its `os.path.exists` short-circuit) with no Drive lookup — and the curated catalog
(conf/tiffs.yml) is never mutated.

Registration is downstream of the gate: `register` runs `verify_upload` and stores the file
ONLY on a PASS, so an unverified or failing tif can never be registered.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid

from ...config import get_settings
from .byod_verify import verify_upload
from .verify import VerifyReport

# {thread_id: {layer_name: entry}}
_REGISTRY: dict[str, dict[str, dict]] = {}


def _layer_name(hazard_label: str) -> str:
    """A unique, URL-safe layer key, e.g. 'byod_flood_1a2b3c4d'."""
    slug = re.sub(r"[^a-z0-9]+", "_", hazard_label.lower()).strip("_") or "layer"
    return f"byod_{slug}_{uuid.uuid4().hex[:8]}"


def register(thread_id: str, *, hazard_label: str, severity_scale: str, src_path: str,
             filename: str | None = None,
             max_pixels: int = 3_000_000_000) -> tuple[str | None, VerifyReport]:
    """Verify `src_path` and, ONLY on a PASS, move it into the tiff cache and register it
    for `thread_id`. Returns (layer_name | None, report); on failure nothing is stored."""
    report = verify_upload(src_path, hazard_label=hazard_label, severity_scale=severity_scale,
                           max_pixels=max_pixels)
    if not report.ok:
        return None, report

    settings = get_settings()
    layer = _layer_name(hazard_label)
    os.makedirs(settings.tiffs_dir, exist_ok=True)
    dest = os.path.join(str(settings.tiffs_dir), f"{layer}.tif")
    shutil.move(src_path, dest)

    _REGISTRY.setdefault(thread_id, {})[layer] = {
        "layer": layer,
        "hazard_label": hazard_label,
        "severity_scale": severity_scale,
        "filename": filename or os.path.basename(dest),
        "local_path": dest,
        "description": (f"User-uploaded {hazard_label} layer (severity classes {severity_scale}); "
                        "the user's own raster, not from the built-in catalog."),
    }
    return layer, report


def entries_for(thread_id: str) -> dict[str, dict]:
    """All BYOD layer entries registered for `thread_id` (empty if none)."""
    return dict(_REGISTRY.get(thread_id, {}))


def descriptions_for(thread_id: str) -> dict[str, str]:
    """{layer: description} to merge into the route menu for `thread_id`."""
    return {layer: e["description"] for layer, e in _REGISTRY.get(thread_id, {}).items()}


def clear(thread_id: str | None = None) -> None:
    """Drop one thread's BYOD layers, or all of them (cleanup / test helper)."""
    if thread_id is None:
        _REGISTRY.clear()
    else:
        _REGISTRY.pop(thread_id, None)
