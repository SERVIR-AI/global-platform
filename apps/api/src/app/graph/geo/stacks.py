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
import functools
import math

import numpy as np
import rasterio
import rasterio.features
import rasterio.warp
from shapely.geometry import shape

from . import tiffs
from ...mcp.budget import once

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


@once
def rows():
    """{stack_id: stack row} for every `kind: stack` row in the catalog (once per snapshot)."""
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


# Cells within 1% of each other are one resolution: reprojecting the same 30 m grid
# moves its cell by hundredths of a percent, which is no reason to prefer a map.
CELL_TIE = 0.01


def cell_tiers(cells):
    """{cell: tier} — 0 for the finest, counting up; a cell within CELL_TIE of its
    tier's finest shares the tier. Two copies of one 30 m grid reprojected from
    different UTM extents differ by 0.02%."""
    tier, start, t = {}, None, -1
    for c in sorted(set(cells)):
        if start is None or c > start * (1 + CELL_TIE):
            start, t = c, t + 1
        tier[c] = t
    return tier


def rank(candidates, facts):
    """`candidates` [(stack_id, row)] best first, by `facts` {stack_id: (classes, share of
    the area its extent covers, cell size in degrees or None, ...)}: two or more classes
    first (a one-class map cannot show loss or gain), then the extent covering most of
    the area, then the finest cells — cells within CELL_TIE of the finest of their group
    count as equal — then the most years, then registration order (the sort is stable).
    A one-year change map whose cell came out 0.02% finer from reprojection won the
    default over the three-year series it was drawn from. The one ranking rule: the
    land-cover pack's default and every listing read it from here."""
    tier = cell_tiers(f[2] for f in facts.values() if f[2])
    return sorted(candidates, key=lambda sr: (
        facts[sr[0]][0] < 2, -round(facts[sr[0]][1], 2),
        tier.get(facts[sr[0]][2], len(tier) + 1), -len(years(sr[1]))))


RANK_RULE = ("two or more classes first, then the extent covering most of the area, then the "
             f"finest cells (within {CELL_TIE:.0%} counts as equal), then the most years, then "
             "the first registered")


def bounds(layer_key, row=None, cat=None):
    """A layer's [w, s, e, n]: its row's declared extent or bbox, else its file's own header
    when the file is on this server (read once per file version); None when neither."""
    import os
    cat = cat if cat is not None else tiffs.catalog()
    row = row if row is not None else cat.get(layer_key) or {}
    ext, bbox = row.get("extent") or {}, row.get("bbox")
    if ext.get("lon") and ext.get("lat"):
        return [float(ext["lon"][0]), float(ext["lat"][0]), float(ext["lon"][1]), float(ext["lat"][1])]
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        return [float(v) for v in bbox]
    path = year_path(layer_key, cat)
    if not path:
        return None
    try:
        stamp = None if str(path).startswith("/vsi") else os.stat(path).st_mtime_ns
    except OSError:
        return None                                    # not on this server (yet)
    return _file_bounds(str(path), stamp)


@functools.lru_cache(maxsize=1024)
def _file_bounds(path, stamp):
    try:
        return list(header(path)["bounds"])
    except Exception:
        return None


def header(path, driver=None):
    """The facts a passport states about a file, without reading pixels (`driver`: the
    one GDAL driver to open with — the contribution gate names it)."""
    with rasterio.open(path, driver=driver) as ds:
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


def category_grid(clip_path, outside_values, legend, cells=56):
    """The clip downsampled to a small grid for the panel's inline map. Each cell is the
    1-based position of its class in `legend` order ({code: {label, color}}); 0 is outside
    the map, no data, or a code the legend does not name. The legend rides with the grid as
    [{code, label, color}], so a code of any size draws in its own colour — the cells used
    to be the code itself as one digit, clipped to 9, and every map coded 10..220 (ESA
    WorldCover, MapBiomas, a change map) drew as one colour. One character per cell
    (layer_values.PALETTE_SYMBOLS) up to 61 classes, else a list of positions."""
    from .layer_values import PALETTE_SYMBOLS
    try:
        with rasterio.open(clip_path) as src:
            h = max(1, min(cells, src.height))
            w = max(1, min(cells, src.width))
            arr = src.read(1, out_shape=(h, w))
            b = src.bounds
        entries = []
        for code, item in (legend or {}).items():
            item = item if isinstance(item, dict) else {"label": item}
            entries.append({"code": _plain(code), "label": item.get("label"),
                            "color": item.get("color")})
        pos = np.zeros(arr.shape, dtype="int32")
        out = _outside_mask(arr, list(outside_values or []))
        for i, e in enumerate(entries, 1):
            pos[(arr == e["code"]) & ~out] = i
        flat = pos.ravel().tolist()
        grid = ("".join(PALETTE_SYMBOLS[i] for i in flat) if len(entries) < len(PALETTE_SYMBOLS)
                else flat)
        return {"w": w, "h": h, "encoding": "legend", "legend": entries, "cells": grid,
                "bounds": [b.left, b.bottom, b.right, b.top]}
    except Exception:
        return None


def dense_codes(vals):
    """(codes, index): the distinct values of `vals` and each cell's index into them, so
    a count bins the index, never the raw value — a code may be negative (an int32 fill
    of -2147483648, an int8 -128) or far above 255 (a change code 220, a 2**31-1 fill).
    A narrow range is binned in one pass; a wide one is sorted."""
    vals = np.asarray(vals).ravel()
    if not vals.size:
        return vals[:0], np.zeros(0, dtype=np.intp)
    if np.issubdtype(vals.dtype, np.integer):
        lo, hi = int(vals.min()), int(vals.max())
        if hi - lo < (1 << 20):
            off = vals.astype("int64") - lo
            present = np.bincount(off, minlength=hi - lo + 1) > 0
            return (np.nonzero(present)[0] + lo).astype(vals.dtype), (np.cumsum(present) - 1)[off]
    return np.unique(vals, return_inverse=True)


def _outside_mask(vals, outside_values):
    """Cells holding an outside value, or no number at all (NaN) — masked before any
    count, so a fill of any size or sign is never binned as a class."""
    ov = [float(v) for v in outside_values]
    out = np.isin(vals, np.array(ov, dtype="float64")) if ov else np.zeros(vals.shape, dtype=bool)
    if np.issubdtype(vals.dtype, np.floating):
        out |= ~np.isfinite(vals)
    return out


def duplicate_labels(legend):
    """[(label, [codes])] for every label the legend gives to more than one code. Change
    compares codes, so one class under two codes reads as total change where nothing moved."""
    by = {}
    for code, label in sorted(legend.items()):
        by.setdefault(" ".join(str(label).lower().split()), []).append((code, str(label).strip()))
    return [(pairs[0][1], [c for c, _ in pairs]) for pairs in by.values() if len(pairs) > 1]


def duplicate_labels_text(legend):
    """The refusal, in words, for a legend naming one class under several codes; else None."""
    dups = duplicate_labels(legend)
    if not dups:
        return None
    said = "; ".join(f"codes {', '.join(str(c) for c in codes[:-1])} and {codes[-1]} are "
                     f"{'both' if len(codes) == 2 else 'all'} {label!r}" for label, codes in dups)
    return f"labels must be unique: {said} — recode the file so one code means one class"


def _count_classes(arr, transform, inside, legend, outside_values):
    areas = row_pixel_areas_km2(transform, arr.shape[0])
    # One pass: every pixel inside the boundary, binned by value, weighted by its own
    # row's area. A per-class loop with boolean masks over a 50-Mpx province cost a
    # minute; this is a second. Outside values are masked first, and the codes left are
    # binned by a dense index, never by their raw value (an int32 fill is -2**31).
    vals = arr[inside]
    w = np.broadcast_to(areas[:, None], arr.shape)[inside]
    out = _outside_mask(vals, list(outside_values))
    codes, idx = dense_codes(vals[~out])
    codes = codes.tolist()
    km2_of = dict(zip(codes, np.bincount(idx, weights=w[~out], minlength=len(codes)).tolist()))
    px_of = dict(zip(codes, np.bincount(idx, minlength=len(codes)).tolist()))
    km2_inside = float(w.sum())
    km2_outside = float(w[out].sum())
    km2_classified = km2_inside - km2_outside
    px_outside = int(out.sum())
    by_class = {}
    for cls, label in sorted(legend.items()):
        km2 = float(km2_of.get(cls, 0.0))
        by_class[cls] = {"label": label, "pixels": int(px_of.get(cls, 0)), "km2": km2,
                         "ha": km2 * 100.0,
                         "share_pct": (100.0 * km2 / km2_classified) if km2_classified else 0.0}
    unlisted = sorted(_plain(v) for v in px_of if v not in legend)
    return {"pixels_inside": int(vals.size), "pixels_classified": int(vals.size - px_outside),
            "km2_inside": km2_inside, "km2_classified": km2_classified,
            "km2_outside": km2_outside, "by_class": by_class,
            "classes_absent": [legend[c] for c in sorted(legend) if by_class[c]["pixels"] == 0],
            "values_not_in_legend": unlisted,
            "shape": list(arr.shape), "pixel_size_deg": abs(transform.a)}


def _plain(v):
    v = float(v)
    return int(v) if v.is_integer() else v


def class_areas(clip_path, boundary, legend, outside_values):
    """Area of every legend class inside `boundary`, from the clipped window.

    A pixel is inside when its centre is (geometry_mask); `outside_values` (the
    producer's outside-footprint and nodata codes) are reported as one excluded
    area, never as a class. Codes may be any integers. Returns pixels, km², ha and
    the share of classified area."""
    arr, transform, _ = _read_clip(clip_path)
    return _count_classes(arr, transform, _inside(boundary, arr.shape, transform),
                          legend, outside_values)


def read_check(path, legend, outside_values, cells=1024, driver=None):
    """None when a class map's file reads the way every pack reads it — counted by
    class over its whole extent, on a decimated grid — else what fails, in words. The
    gate approved a file whose int32 fill crashed every read; this runs the readers'
    own count on a file before it is approved (`driver`: the one GDAL driver to open with)."""
    try:
        with rasterio.open(path, driver=driver) as src:
            h, w = min(cells, src.height), min(cells, src.width)
            arr = src.read(1, out_shape=(h, w))
            b = src.bounds
        transform = rasterio.transform.from_bounds(b.left, b.bottom, b.right, b.top, w, h)
        st = _count_classes(arr, transform, np.ones(arr.shape, dtype=bool), legend, outside_values)
    except Exception as exc:
        return (f"the file could not be counted by class the way every pack reads it "
                f"({type(exc).__name__}: {exc})")
    if not st["pixels_classified"]:
        return (f"every cell read is outside the map (outside values "
                f"{[_plain(v) for v in outside_values]}): nothing would be counted — check "
                "outside_values and declared.nodata against the file")
    return None


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
    dups = duplicate_labels(legend)
    if dups:
        # Change compares codes: one class under two codes reads as total change.
        raise ValueError(
            "change between years is not computed on this stack: its legend gives one label "
            "to several codes ("
            + "; ".join(f"codes {', '.join(str(c) for c in codes)} are all {label!r}"
                        for label, codes in dups)
            + "), and change compares codes, so it would report change where nothing moved. "
            "Each year's class areas are still available (ask without year_to); the stack's "
            "contributor can recode the files so one code means one class")
    a, ta, ca = _read_clip(clip_a)
    b, tb, cb = _read_clip(clip_b)
    outside_values = list(outside_values)
    # The fill for cells year B has no pixel for: an outside value B's type can hold
    # (years may differ in type: an int8 year beside a uint8 one). With none (a binary
    # map where 0 is a class), a value no legend uses stands in, and marks "no pixel".
    nod = _fill_for(b.dtype, outside_values, legend)
    b = align(b, tb, cb, a.shape, ta, ca, nod)
    if not any(float(nod) == float(v) for v in outside_values):
        outside_values = [*outside_values, nod]
    inside = _inside(boundary, a.shape, ta)
    if focus_class is not None and out_path:
        common_px = inside & ~_outside_mask(a, outside_values) & ~_outside_mask(b, outside_values)
        code = np.full(a.shape, 15, dtype="uint8")
        code[common_px & (a == focus_class) & (b == focus_class)] = 1
        code[common_px & (a == focus_class) & (b != focus_class)] = 2
        code[common_px & (a != focus_class) & (b == focus_class)] = 3
        # into the area bundle beside the clips: whole (temp + rename), one writer at a time
        from . import ingest
        with ingest._lock(out_path), ingest.atomic_path(out_path) as tmp:
            with rasterio.open(tmp, "w", driver="GTiff", width=a.shape[1], height=a.shape[0],
                               count=1, dtype="uint8", crs=ca, transform=ta, nodata=15,
                               compress="lzw") as dst:
                dst.write(code, 1)
                dst.update_tags(overview_level=clip_level(clip_a), focus_class=int(focus_class))
    areas = row_pixel_areas_km2(ta, a.shape[0])
    w_all = np.broadcast_to(areas[:, None], a.shape)
    out_a, out_b = _outside_mask(a, outside_values), _outside_mask(b, outside_values)
    common = inside & ~out_a & ~out_b
    one_year_only = inside & (out_a ^ out_b)
    # One pass over the common mask: the (from, to) pair of every pixel, binned and
    # weighted by the pixel's own area — the full transition matrix at once, indexed by
    # each code's place among the codes present (any integer), never by the code itself.
    av, bv = a[common], b[common]
    if np.issubdtype(av.dtype, np.integer) and np.issubdtype(bv.dtype, np.integer):
        av, bv = av.astype("int64"), bv.astype("int64")
    codes, idx = dense_codes(np.concatenate([av, bv]))
    k = int(codes.size)
    pair = idx[:av.size] * k + idx[av.size:]
    w = w_all[common]
    matrix = np.bincount(pair, weights=w, minlength=k * k).reshape(k, k)
    at = {c: i for i, c in enumerate(codes.tolist())}

    def km2(frm, to):
        """km² from code `frm` (year A) to code `to` (year B); None is any code."""
        i = slice(None) if frm is None else at.get(frm)
        j = slice(None) if to is None else at.get(to)
        return 0.0 if i is None or j is None else float(matrix[i, j].sum())
    km2_common = float(w.sum())
    km2_unchanged = float(np.trace(matrix))
    by_class = {}
    for cls, label in sorted(legend.items()):
        loss_to = {legend[t]: km2(cls, t) for t in sorted(legend) if t != cls}
        gain_from = {legend[t]: km2(t, cls) for t in sorted(legend) if t != cls}
        by_class[cls] = {"label": label, "km2_a": km2(cls, None), "km2_b": km2(None, cls),
                         "stable_km2": km2(cls, cls),
                         "loss_km2": sum(loss_to.values()), "gain_km2": sum(gain_from.values()),
                         "loss_to": {k: v for k, v in loss_to.items() if v > 0},
                         "gain_from": {k: v for k, v in gain_from.items() if v > 0}}
    return {"km2_common": km2_common, "km2_one_year_only": float(w_all[one_year_only].sum()),
            "km2_unchanged": km2_unchanged, "km2_changed": km2_common - km2_unchanged,
            "pixels_common": int(pair.size), "by_class": by_class}


def _fill_for(dtype, outside_values, legend):
    """A value marking "no pixel here" in an array of `dtype`: the last declared outside
    value the type can hold, else the type's largest value no legend class uses."""
    dtype = np.dtype(dtype)
    if not np.issubdtype(dtype, np.integer):
        return float(outside_values[-1]) if outside_values else float("nan")
    info = np.iinfo(dtype)
    for v in reversed(outside_values):
        if float(v).is_integer() and info.min <= int(float(v)) <= info.max:
            return int(float(v))
    used = {int(c) for c in legend}
    return next(v for v in (int(info.max), int(info.min), int(info.max) - 1) if v not in used)
