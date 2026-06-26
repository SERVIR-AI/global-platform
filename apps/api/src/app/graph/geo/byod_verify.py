"""Verification gate for a user-uploaded GeoTIFF (Bring Your Own Data).

A built-in layer is verified against a curated contract in conf/raster_schema.yml; an
uploaded file has none, so we synthesize a contract from the little the user declares
(the hazard it represents + its severity scale) and verify the rest structurally. The
observation reuses `windowed_stats` (never full-loads); the range/dtype/nodata checks
reuse `verify._check`; on top, `_structural_asserts` covers what those don't (band
count, a present CRS, finite bounds, a pixel-count cap). Registration is gated on
`ok=True` — an unverified or failing file is never usable.

Hard failures (`ok=False`, unrecoverable): not a readable GeoTIFF, more than one band,
no CRS, degenerate/non-finite bounds, values outside the declared severity scale,
oversize. Soft signals warn but pass: many distinct values (looks continuous rather
than a class raster), a CRS other than EPSG:4326, bounds outside the Southeast-Asia
window.
"""
from __future__ import annotations

import math

import rasterio

from .rasterstats import windowed_stats
from .verify import VerifyReport, _check

# The two severity scales the catalog uses; every hazard/risk layer is a 0-5 or 1-5 class raster.
_SCALES = {"0-5": (0, 5), "1-5": (1, 5)}

# A class raster has few distinct values; thousands means it's continuous (the mm-vs-metres trap).
_MAX_CLASS_DISTINCT = 32
# SE-Asia window (matches the flood catalog extent in conf/tiffs.yml): lon[92,141], lat[-11,29].
_SEA_BOUNDS = (92.0, -11.0, 141.0, 29.0)


def _scale_to_contract(severity_scale: str, units: str) -> dict:
    """Synthesize a `verify._check`-style contract from the user's declaration."""
    if severity_scale not in _SCALES:
        raise ValueError(f"unknown severity_scale {severity_scale!r}; expected one of {sorted(_SCALES)}")
    if units != "class":
        raise ValueError("BYOD accepts only severity-class rasters (units='class'); reclass a "
                         "continuous raster to a 1-5 scale first")
    lo, hi = _SCALES[severity_scale]
    return {"valid_min": lo, "valid_max": hi, "units": units}


def _structural_asserts(path: str, obs: dict, *, max_pixels: int) -> tuple[list[str], list[str]]:
    """Checks `windowed_stats` doesn't cover. Returns (hard_failures, soft_warnings)."""
    fails: list[str] = []
    warns: list[str] = []

    # Band count needs its own open — windowed_stats silently reads only band 1.
    try:
        with rasterio.open(path) as src:
            count = src.count
            b = src.bounds
    except Exception as e:  # noqa: BLE001 - any open failure means it isn't a usable raster
        return [f"not a readable GeoTIFF: {e}"], warns

    if count != 1:
        fails.append(f"{count} bands; a severity raster must be single-band")

    epsg = obs.get("crs_epsg")
    if epsg is None:
        fails.append("no CRS / EPSG — the raster is not georeferenced")
    elif epsg != 4326:
        warns.append(f"CRS is EPSG:{epsg}, not EPSG:4326 — will need reprojection before use")

    coords = (b.left, b.bottom, b.right, b.top)
    if not all(math.isfinite(v) for v in coords):
        fails.append("bounds are not finite")
    elif not (b.left < b.right and b.bottom < b.top):
        fails.append(f"degenerate bounds {coords}")
    elif epsg == 4326:
        sl, sb, sr, st = _SEA_BOUNDS
        if b.right < sl or b.left > sr or b.top < sb or b.bottom > st:
            warns.append("bounds fall outside the Southeast-Asia window")

    w, h = obs.get("width") or 0, obs.get("height") or 0
    if w * h > max_pixels:
        fails.append(f"{w}x{h} = {w * h:,} px exceeds the {max_pixels:,} px cap")

    ndist = obs.get("sampled_distinct") or 0
    if ndist > _MAX_CLASS_DISTINCT:
        warns.append(f"{ndist} distinct sampled values — looks continuous, not a class raster")

    return fails, warns


def verify_upload(path: str, *, hazard_label: str, severity_scale: str = "0-5",
                  units: str = "class", max_pixels: int = 3_000_000_000) -> VerifyReport:
    """Verify an uploaded GeoTIFF against a contract synthesized from the user's declaration.

    Hard failures set `ok=False` (never usable); soft signals are recorded as `warnings`
    but still pass. Reuses `windowed_stats` (no full-load) + `verify._check` for the
    range/dtype/nodata subset, then adds the structural asserts.
    """
    try:
        decl = _scale_to_contract(severity_scale, units)
    except ValueError as e:
        return VerifyReport(hazard_label, False, {}, {}, [str(e)])
    decl["hazard_label"] = hazard_label

    try:
        obs = windowed_stats(path)
    except Exception as e:  # noqa: BLE001 - corrupt / non-raster upload
        return VerifyReport(hazard_label, False, decl, {}, [f"not a readable GeoTIFF: {e}"])

    mismatches = _check(decl, obs)                       # range (the trap-catcher), dtype, nodata, crs
    fails, warns = _structural_asserts(path, obs, max_pixels=max_pixels)
    mismatches = mismatches + fails
    return VerifyReport(hazard_label, not mismatches, decl, obs, mismatches, warnings=warns)
