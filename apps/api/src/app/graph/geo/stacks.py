"""Yearly raster STACKS: one legend, one contract, one file per year.

A land-cover product is not one raster but a series — the same classes mapped
every year. The catalog declares the series once (`kind: stack`) and tiffs.catalog()
expands it into an ordinary row per year, so every raster primitive (entry, legend,
source_raster, hazard_clip, schema_for, verify) works on `landcover_hkh_2022`
unchanged. What is specific to a series lives here: which years exist and why
one is missing, where a year's file is, and the pixel-counting computations.

Areas are summed per pixel at the pixel's own latitude, exact on the WGS84
ellipsoid, never with one constant pixel area: a 30 m cell at 28 N covers 11% less
ground than one at the equator. Counts are never resampled.
"""
import math

import numpy as np
import rasterio
import rasterio.features
import rasterio.warp
from shapely.geometry import shape

from . import tiffs

# The authalic radius: a sphere of this radius has the ellipsoid's total area, but not
# its area between two parallels — at 10 N the sphere overstates a pixel by ~0.41%, at
# the equator ~0.45%, at 30 N ~0.11%. Pixel areas use the authalic LATITUDE instead,
# which is exact on WGS84 (row_pixel_areas_km2).
R_KM = 6371.0072
WGS84_A_KM = 6378.137
WGS84_E2 = 0.00669437999014
_E = math.sqrt(WGS84_E2)


def _q(lat_rad):
    """Snyder's q(φ): the authalic latitude β of φ is asin(q(φ) / q(π/2)), and the area
    of the WGS84 ellipsoid between the equator and φ is a²/2 · q(φ) per radian of
    longitude."""
    s = np.sin(lat_rad)
    return (1 - WGS84_E2) * (s / (1 - WGS84_E2 * s ** 2)
                             - (1 / (2 * _E)) * np.log((1 - _E * s) / (1 + _E * s)))


def rows():
    """{stack_id: stack row} for every `kind: stack` row in the catalog."""
    return {k: v for k, v in tiffs.catalog().items() if v.get("kind") == "stack"}


def stack(stack_id):
    row = rows().get(stack_id)
    if row is None:
        raise ValueError(f"unknown raster stack {stack_id!r} — registered: "
                         + ", ".join(sorted(rows())))
    return row


def years(row):
    return [int(y) for y in row.get("years") or []]


def layer_for(stack_id, year):
    """The per-year layer key, or a ValueError that names the years that exist."""
    row = stack(stack_id)
    ys = years(row)
    if int(year) in ys:
        return str(row["layer_pattern"]).format(year=int(year))
    reason = (row.get("missing_years") or {}).get(int(year)) \
        or (row.get("missing_years") or {}).get(str(year))
    if reason:
        raise ValueError(f"{year}: {reason}. Years available: {_span(ys)}")
    raise ValueError(f"{year} is not in the {stack_id} stack. Years available: {_span(ys)}")


def _span(ys):
    """2000-2011, 2013-2022 — runs, so a 21-year list reads at a glance."""
    out, start, prev = [], None, None
    for y in sorted(ys) + [None]:
        if start is None:
            start = prev = y
            continue
        if y is not None and y == prev + 1:
            prev = y
            continue
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = y
    return ", ".join(out)


def year_path(layer_key, cat=None):
    """Where a stack year's file is, without downloading anything: a built-in year
    carries `path` (a plain file or a /vsizip/ entry); a CONTRIBUTED or staged year
    is a landed row whose `local_path` sits under the tiffs directory. None when the
    row names no file."""
    import os
    from ...config import get_settings
    meta = (cat if cat is not None else tiffs.catalog()).get(layer_key) or {}
    if meta.get("path"):
        return meta["path"]
    if meta.get("local_path"):
        return os.path.join(str(get_settings().tiffs_dir), os.path.basename(str(meta["local_path"])))
    return None


def installed(stack_id):
    """{year: bool} — whether each declared year's file opens. Header-only, cheap."""
    row = stack(stack_id)
    cat = tiffs.catalog()
    out = {}
    for y in years(row):
        path = year_path(str(row["layer_pattern"]).format(year=y), cat)
        try:
            with rasterio.open(path):
                out[y] = True
        except Exception:
            out[y] = False
    return out


def header(path):
    """The facts a passport states about a file, without reading pixels."""
    with rasterio.open(path) as ds:
        b = ds.bounds
        return {"width": ds.width, "height": ds.height, "dtype": ds.dtypes[0],
                "nodata": ds.nodata, "crs": ds.crs.to_string() if ds.crs else None,
                "pixel_size_deg": abs(ds.transform.a),
                "bounds": [round(b.left, 5), round(b.bottom, 5), round(b.right, 5), round(b.top, 5)],
                "overviews": list(ds.overviews(1)), "tiled": bool(ds.profile.get("tiled"))}


def row_pixel_areas_km2(transform, height):
    """Area of one pixel in each raster row, km², exact on the WGS84 ellipsoid:
    R_q² · Δλ · (sin β_north − sin β_south), β the authalic latitude and
    R_q = a·sqrt(q_p / 2) — which is a²/2 · Δλ · |q(φ_north) − q(φ_south)|.
    Vectorised over rows."""
    a, e, f = transform.a, transform.e, transform.f
    i = np.arange(height, dtype="float64")
    lat_n = np.radians(np.clip(f + i * e, -90.0, 90.0))
    lat_s = np.radians(np.clip(f + (i + 1) * e, -90.0, 90.0))
    return (WGS84_A_KM ** 2 / 2) * math.radians(abs(a)) * np.abs(_q(lat_n) - _q(lat_s))


def _inside(boundary, shape_, transform):
    return rasterio.features.geometry_mask([boundary.__geo_interface__], out_shape=shape_,
                                           transform=transform, invert=True)


def _read_clip(path):
    with rasterio.open(path) as ds:
        return ds.read(1), ds.transform, ds.crs


def clip_level(path):
    """The overview level a clip was read at (1 = full resolution), from its tags."""
    try:
        with rasterio.open(path) as ds:
            return int(ds.tags().get("overview_level") or 1)
    except Exception:
        return 1


def category_grid(clip_path, outside_values, cells=56):
    """The clip downsampled to a small digit grid for the panel's inline map:
    legend classes 1-9 stay themselves, the outside/nodata values become 0, so a
    nodata 15 is never painted as a class."""
    try:
        with rasterio.open(clip_path) as src:
            h = max(1, min(cells, src.height))
            w = max(1, min(cells, src.width))
            arr = src.read(1, out_shape=(h, w))
            b = src.bounds
        arr = np.where(np.isin(arr, list(outside_values)), 0, arr)
        arr = np.clip(arr, 0, 9)
        return {"w": w, "h": h, "cells": "".join(str(int(v)) for v in arr.ravel()),
                "bounds": [b.left, b.bottom, b.right, b.top]}
    except Exception:
        return None


def class_areas(clip_path, boundary, legend, outside_values):
    """Area of every legend class inside `boundary`, from the clipped window.

    A pixel is inside when its centre is (geometry_mask); `outside_values` (the
    producer's outside-footprint and nodata codes) are reported as one excluded
    area, never as a class. Returns pixels, km², ha and the share of classified area."""
    arr, transform, _ = _read_clip(clip_path)
    inside = _inside(boundary, arr.shape, transform)
    areas = row_pixel_areas_km2(transform, arr.shape[0])
    # One pass: every pixel inside the boundary, binned by value, weighted by its own
    # row's area. A per-class loop with boolean masks over a 50-Mpx province cost a
    # minute; this is a second.
    vals = arr[inside].astype("int64")
    w = np.broadcast_to(areas[:, None], arr.shape)[inside]
    km2_by_val = np.bincount(vals, weights=w, minlength=256)
    px_by_val = np.bincount(vals, minlength=256)
    outside_vals = [int(v) for v in outside_values]
    km2_inside = float(w.sum())
    km2_outside = float(sum(km2_by_val[v] for v in outside_vals))
    km2_classified = km2_inside - km2_outside
    px_outside = int(sum(px_by_val[v] for v in outside_vals))
    by_class = {}
    for cls, label in sorted(legend.items()):
        km2 = float(km2_by_val[cls])
        by_class[cls] = {"label": label, "pixels": int(px_by_val[cls]), "km2": km2,
                         "ha": km2 * 100.0,
                         "share_pct": (100.0 * km2 / km2_classified) if km2_classified else 0.0}
    unlisted = sorted(int(v) for v in np.nonzero(px_by_val)[0]
                      if int(v) not in legend and int(v) not in outside_vals)
    return {"pixels_inside": int(vals.size), "pixels_classified": int(vals.size - px_outside),
            "km2_inside": km2_inside, "km2_classified": km2_classified,
            "km2_outside": km2_outside, "by_class": by_class,
            "classes_absent": [legend[c] for c in sorted(legend) if by_class[c]["pixels"] == 0],
            "values_not_in_legend": unlisted,
            "shape": list(arr.shape), "pixel_size_deg": abs(transform.a)}


def align(arr_b, transform_b, crs_b, shape_a, transform_a, crs_a, nodata):
    """Put year B's clip on year A's grid (nearest — classes never blend). The
    stack's grids are pixel-aligned, so this is a shift by whole pixels at most."""
    if arr_b.shape == tuple(shape_a) and transform_b.almost_equals(transform_a, precision=1e-9):
        return arr_b
    out = np.full(shape_a, nodata, dtype=arr_b.dtype)
    rasterio.warp.reproject(arr_b, out, src_transform=transform_b, src_crs=crs_b,
                            dst_transform=transform_a, dst_crs=crs_a, dst_nodata=nodata,
                            resampling=rasterio.warp.Resampling.nearest)
    return out


def change(clip_a, clip_b, boundary, legend, outside_values, focus_class=None, out_path=None):
    """Class change between two years on the COMMON classified mask: pixels that
    carry a legend class in both years. Everything else is reported, not dropped.
    With `focus_class` and `out_path`, also writes a three-value change raster for
    that class (1 stable, 2 loss, 3 gain; nodata elsewhere) beside the clips."""
    a, ta, ca = _read_clip(clip_a)
    b, tb, cb = _read_clip(clip_b)
    outside_values = list(outside_values)
    # The fill for cells year B has no pixel for. With no outside value declared
    # (a binary map where 0 is a class), a value no legend uses stands in.
    nod = outside_values[-1] if outside_values else (
        int(np.iinfo(a.dtype).max) if np.issubdtype(a.dtype, np.integer) else float("nan"))
    b = align(b, tb, cb, a.shape, ta, ca, nod)
    if not outside_values:
        outside_values = [nod]        # the fill alone marks "no pixel here"
    inside = _inside(boundary, a.shape, ta)
    if focus_class is not None and out_path:
        common_px = inside & ~np.isin(a, list(outside_values)) & ~np.isin(b, list(outside_values))
        code = np.full(a.shape, 15, dtype="uint8")
        code[common_px & (a == focus_class) & (b == focus_class)] = 1
        code[common_px & (a == focus_class) & (b != focus_class)] = 2
        code[common_px & (a != focus_class) & (b == focus_class)] = 3
        with rasterio.open(out_path, "w", driver="GTiff", width=a.shape[1], height=a.shape[0],
                           count=1, dtype="uint8", crs=ca, transform=ta, nodata=15,
                           compress="lzw") as dst:
            dst.write(code, 1)
            dst.update_tags(overview_level=clip_level(clip_a), focus_class=int(focus_class))
    areas = row_pixel_areas_km2(ta, a.shape[0])
    w_all = np.broadcast_to(areas[:, None], a.shape)
    out_a, out_b = np.isin(a, list(outside_values)), np.isin(b, list(outside_values))
    common = inside & ~out_a & ~out_b
    one_year_only = inside & (out_a ^ out_b)
    # One pass over the common mask: the (from, to) pair of every pixel, binned and
    # weighted by the pixel's own area — the full transition matrix at once.
    pair = a[common].astype("int64") * 256 + b[common].astype("int64")
    w = w_all[common]
    matrix = np.bincount(pair, weights=w, minlength=256 * 256).reshape(256, 256)
    km2_common = float(w.sum())
    km2_unchanged = float(np.trace(matrix))
    by_class = {}
    for cls, label in sorted(legend.items()):
        loss_to = {legend[t]: float(matrix[cls, t]) for t in sorted(legend) if t != cls}
        gain_from = {legend[t]: float(matrix[t, cls]) for t in sorted(legend) if t != cls}
        by_class[cls] = {"label": label, "km2_a": float(matrix[cls, :].sum()),
                         "km2_b": float(matrix[:, cls].sum()),
                         "stable_km2": float(matrix[cls, cls]),
                         "loss_km2": sum(loss_to.values()), "gain_km2": sum(gain_from.values()),
                         "loss_to": {k: v for k, v in loss_to.items() if v > 0},
                         "gain_from": {k: v for k, v in gain_from.items() if v > 0}}
    return {"km2_common": km2_common, "km2_one_year_only": float(w_all[one_year_only].sum()),
            "km2_unchanged": km2_unchanged, "km2_changed": km2_common - km2_unchanged,
            "pixels_common": int(pair.size), "by_class": by_class}
