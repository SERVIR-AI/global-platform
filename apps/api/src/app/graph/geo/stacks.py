"""Yearly raster STACKS: one legend, one contract, one file per year.

A land-cover product is not one raster but a series — the same classes mapped
every year. The catalog declares the series once (`kind: stack`) and tiffs.catalog()
expands it into an ordinary row per year, so every raster primitive (entry, legend,
source_raster, hazard_clip, schema_for, verify) works on `landcover_hkh_2022`
unchanged. What is specific to a series lives here: which years exist and why
one is missing, where a year's file is, and the pixel-counting computations.

Areas are summed per pixel at the pixel's own latitude on the authalic sphere,
never with one constant pixel area: a 30 m cell at 28 N covers 11% less ground
than one at the equator. Counts are never resampled.
"""
import math

import numpy as np
import rasterio
import rasterio.features
import rasterio.warp
from shapely.geometry import shape

from . import tiffs

R_KM = 6371.0072          # authalic Earth radius; the WGS84-exact area is 0.08% smaller


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


def installed(stack_id):
    """{year: bool} — whether each declared year's file opens. Header-only, cheap."""
    row = stack(stack_id)
    out = {}
    for y in years(row):
        path = tiffs.catalog()[str(row["layer_pattern"]).format(year=y)]["path"]
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
    """Area of one pixel in each raster row, km², on the authalic sphere:
    R² · Δλ · (sin φ_north − sin φ_south). Vectorised over rows."""
    a, e, f = transform.a, transform.e, transform.f
    i = np.arange(height, dtype="float64")
    lat_n = np.radians(f + i * e)
    lat_s = np.radians(f + (i + 1) * e)
    return (R_KM ** 2) * math.radians(abs(a)) * np.abs(np.sin(lat_n) - np.sin(lat_s))


def _inside(boundary, shape_, transform):
    return rasterio.features.geometry_mask([boundary.__geo_interface__], out_shape=shape_,
                                           transform=transform, invert=True)


def _read_clip(path):
    with rasterio.open(path) as ds:
        return ds.read(1), ds.transform, ds.crs


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


def change(clip_a, clip_b, boundary, legend, outside_values):
    """Class change between two years on the COMMON classified mask: pixels that
    carry a legend class in both years. Everything else is reported, not dropped."""
    a, ta, ca = _read_clip(clip_a)
    b, tb, cb = _read_clip(clip_b)
    nod = list(outside_values)[-1]
    b = align(b, tb, cb, a.shape, ta, ca, nod)
    inside = _inside(boundary, a.shape, ta)
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
