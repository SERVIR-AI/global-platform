"""Values of registered raster layers over an area — the primitive behind
`assemble_pack(layers=[...])`, for every pack that has an area.

Domain-free on purpose: a pack hands over the boundary it used and the layers its
own method rests on, and gets back each layer's values clipped to that boundary,
with the layer's provenance and a check of its declared contract. Which layers can
be named is derived from the catalog and the schema on every call, so a layer
approved a minute ago is readable now, with no code change.

Reads are in memory, never through ingest.hazard_clip: that cache is keyed by
(area, layer) alone, and a budgeted overview read written there would be handed to
the risk engine as if it were full resolution.

A grid is {w, h, bounds, crs, encoding, cells}, row-major from the north-west
corner, cells outside the area or without a value empty:
  encoding "palette": `cells` is a string, one character per cell, "." for empty;
      character i of PALETTE_SYMBOLS stands for `palette[i]` — lossless, and compact
      for class layers and layers with few distinct values;
  encoding "values": `cells` is a list of numbers, null for empty — used when the
      area holds more distinct values than there are symbols.

What a layer's cells hold decides its summary (schema.values_kind): a layer with
classes (a legend, or a classes role) gets the area of each class, counted on every
full-resolution cell with each cell's own area on the WGS84 ellipsoid; an amount (a
measure_* layer: elevation, biomass, carbon) gets its mean and percentiles, and — when
its units are a density per area — a total over the area, the arithmetic stated. A
gatherer may hand over a class map (the reserved stats key `_class_grid`, see
mcp/layers.py); every layer is then also summarised per class of that map.
"""

from __future__ import annotations

import math
import os
import re

import numpy as np

GRID_SIDE = 48                 # longest side of one layer's returned grid, in cells
GRID_TOTAL = 4 * GRID_SIDE ** 2  # all layers in one response share this many cells
READ_BUDGET = 4_000_000        # source pixels read per layer before an overview is used
FULL_COUNT_BUDGET = 64_000_000  # cells a class layer is counted on at full resolution
MAX_LAYERS = 8                 # layers one pack may carry
FEW_CELLS = 100                # an amount over fewer cells than this is said to be coarse
_PATCH_LIMIT = 250_000         # cells above which equal-value patches are not traced
INLINE_CHARS = 16_000          # all grids in one response, serialised — a float grid costs
                               # ~7 characters a cell, and pixels cost a model tokens
PALETTE_SYMBOLS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def registered() -> dict:
    """{layer: catalog row} for every single-file raster a caller may name: catalog
    rows (hand-authored, contributed, and the caller's own staged previews) plus
    schema-declared layers with a registered source. A stack's root row is not a
    file; its per-year rows are."""
    from ...config import get_settings
    from . import drive_tifs, schema, tiffs
    out = {k: v for k, v in tiffs.catalog().items()
           if v.get("kind") != "stack" and not v.get("computed")}
    tdir = get_settings().tiffs_dir
    for k in schema.names():
        if k not in out and (drive_tifs.drive_id(f"{k}.tif")
                             or os.path.exists(os.path.join(tdir, f"{k}.tif"))):
            out[k] = {}
    return out


def check_names(names) -> str | None:
    """A decline note for a `layers` request that cannot be honoured, else None."""
    names = [str(n).strip() for n in names or [] if str(n).strip()]
    if len(names) > MAX_LAYERS:
        return (f"{len(names)} layers requested; one pack carries at most {MAX_LAYERS}. "
                "Ask for fewer, or split them across packs.")
    known = registered()
    unknown = [n for n in names if n not in known]
    if unknown:
        return (f"unknown layer(s): {', '.join(map(repr, unknown))}. Registered raster "
                f"layers: {', '.join(sorted(known))}")
    return None


def _role_rule(contract: dict) -> dict:
    """What this layer's role says about its values — the schema's `roles` table
    (data, merged with contributed roles), so a role states its own rule."""
    from . import schema
    return dict(schema.roles().get(str(contract.get("role") or "")) or {})


def _isnan(v) -> bool:
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def _no_value(contract: dict, file_nodata, row: dict) -> tuple[list, str]:
    """Every value that means 'no value' here, and why — all of them, never the first
    rule that matches: a stack's contract says 15 while its older years mark the
    outside of the footprint 0, and both are in the row's `outside_values`."""
    vals, why = [], []

    def add(v, reason):
        if v is None or _isnan(v):
            return
        v = float(v)
        if v not in vals:
            vals.append(v)
            why.append(reason)
    add(contract.get("nodata"), "declared in the contract")
    add(file_nodata, "the file's nodata tag")
    for v in row.get("outside_values") or []:
        add(v, "outside the producer's footprint")
    # The role's zero rule is the default; a row that DECLARES its outside values
    # (even none: a binary map where 0 is the class "not mangrove") replaces it.
    if row.get("outside_values") is None and \
            str(_role_rule(contract).get("zero", "")).strip().lower() == "nodata":
        add(0, f"0 means no value for a {contract.get('role')} layer")
    # A value the row's LEGEND names is a class, never no-data: a binary extent map
    # tags 0 as nodata and calls it "Not mangrove" in the same breath; the legend
    # wins, or every answer reports the map as one uniform value.
    legend_keys = set()
    for k in (row.get("legend") or {}):
        try:
            legend_keys.add(float(k))
        except (TypeError, ValueError):
            pass
    if legend_keys:
        kept = [(v, r) for v, r in zip(vals, why) if float(v) not in legend_keys]
        dropped = [v for v in vals if float(v) in legend_keys]
        vals, why = [v for v, _ in kept], [r for _, r in kept]
        if dropped:
            why = why + [f"{', '.join(str(int(d) if float(d).is_integer() else d) for d in dropped)} "
                         "is a legend class here, so it counts as a value"]
    # a float fill (-3.4e38) stays a float: as an integer it prints 39 digits
    vals = [int(v) if float(v).is_integer() and abs(float(v)) < 1e15 else v for v in vals]
    rule = ("; ".join(f"{v} — {r}" for v, r in zip(vals, why[:len(vals)]))
            + ("; " + "; ".join(why[len(vals):]) if len(why) > len(vals) else "")
            if (vals or len(why) > len(vals))
            else "no no-data value is declared, so every value read is counted")
    if _isnan(contract.get("nodata")) or _isnan(file_nodata):
        rule += "; NaN marks no value"
    return vals, rule


def _cell_km2(dx: float, dy: float, lat: float, geographic: bool) -> float:
    """One cell's area at `lat`: exact on the WGS84 ellipsoid for a geographic grid,
    planar in the file's own units (metres) for a projected one."""
    if not geographic:
        return abs(dx * dy) / 1e6
    from rasterio import Affine

    from .stacks import row_pixel_areas_km2
    return float(row_pixel_areas_km2(Affine(abs(dx), 0, 0, 0, -abs(dy), lat + abs(dy) / 2), 1)[0])


def _row_km2(transform, height: int, geographic: bool):
    """Each row's cell area, km² — per row on the ellipsoid, constant when projected."""
    if not geographic:
        return np.full(height, abs(transform.a * transform.e) / 1e6)
    from .stacks import row_pixel_areas_km2
    return row_pixel_areas_km2(transform, height)


def _num(v, integer: bool):
    if v is None:
        return None
    return int(v) if integer else float(f"{float(v):.4g}")


def _r4(x) -> float:
    """A figure as it is stated: whole above 1,000, else four significant digits."""
    x = float(x)
    return float(round(x)) if abs(x) >= 1000 else float(f"{x:.4g}")


def _fig(x) -> str:
    x = float(x)
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    return f"{x:g}"


def _nth(n: int) -> str:
    """2nd, 4th, 8th — a read level as words say it."""
    n = int(n)
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ------------------------------------------------------------------ amounts per area
_AREA_UNIT_KM2 = {"ha": 0.01, "km2": 1.0, "m2": 1e-6, "ac": 0.00404685642}
_PER = re.compile(r"^(?P<num>.*?)\s*(?:/|\bper\s+)\s*(?P<den>hectares?|ha|km2|km²|km\^2|"
                  r"sq\.?\s*km|square\s+kilomet(?:re|er)s?|m2|m²|m\^2|square\s+met(?:re|er)s?|"
                  r"acres?|ac)\.?$", re.I)
_PER_EXP = re.compile(r"^(?P<num>.*?)\s+(?P<den>ha|km|m)\s*\^?\s*(?P<exp>-\s*[12]|⁻[¹²])$", re.I)


def density(units) -> dict | None:
    """{numerator, per, km2_per_unit} when `units` is an amount per area — 'Mg/ha',
    't C/ha', 'Mg C ha-1', 'people per km2', 'kg/m2' — else None ('m', 'degC', 'mm',
    'people per pixel' are not totalled over an area)."""
    u = " ".join(str(units or "").split())
    m = _PER.match(u)
    if m:
        den = re.sub(r"[\s.]", "", m["den"].lower())
        den = ("ha" if den.startswith("hectare") or den == "ha" else
               "km2" if "km" in den or "kilomet" in den else
               "ac" if den.startswith("ac") else "m2")
    else:
        m = _PER_EXP.match(u)
        if not m:
            return None
        exp = 1 if m["exp"].replace(" ", "") in ("-1", "⁻¹") else 2
        den = {("ha", 1): "ha", ("km", 2): "km2", ("m", 2): "m2"}.get((m["den"].lower(), exp))
        if not den:
            return None
    num = m["num"].strip() or "units"
    return {"numerator": num, "per": den, "km2_per_unit": _AREA_UNIT_KM2[den]}


def _scaling(contract: dict):
    """(scale_factor, offset) when the contract says the file stores scaled values."""
    sf, off = contract.get("scale_factor"), contract.get("offset")
    if sf is None and off is None:
        return None
    return float(1 if sf is None else sf), float(0 if off is None else off)


def _kind(contract: dict, legend: dict, nominal: bool) -> str:
    """'classes' when the cells name classes (a legend, or a role whose values are
    classes), else what the role says — 'count' or 'amount' (the default)."""
    from . import schema
    if legend or nominal:
        return "classes"
    return schema.values_kind(contract.get("role")) or "amount"


def _amount_summary(v, w_km2, units) -> dict:
    """Percentiles of an amount over the cells with a value, and — for a density per
    area — the total: Σ value × cell area, in the numerator's unit."""
    out = {"p10": _r4(np.percentile(v, 10)), "p50": _r4(np.percentile(v, 50)),
           "p90": _r4(np.percentile(v, 90))}
    d = density(units)
    if d and v.size:
        area_u = float(w_km2.sum()) / d["km2_per_unit"]
        total = float((v * w_km2).sum()) / d["km2_per_unit"]
        out["total"] = {"value": _r4(total), "unit": d["numerator"], "area": _r4(area_u),
                        "area_unit": d["per"],
                        "mean_area_weighted": _r4(total / area_u) if area_u else None,
                        "cells": int(v.size)}
    return out


def _class_counts(values, w_km2, legend: dict) -> dict:
    """{code: {label, cells, km2, share_pct}} over the cells with a value — shares are of
    the area that holds a value, the way the land-cover pack states class shares."""
    if not values.size:
        return {}
    u, inv = np.unique(values, return_inverse=True)
    km2 = np.bincount(inv, weights=w_km2)
    n = np.bincount(inv)
    total = float(km2.sum())
    out = {}
    for val, k, c in zip(u.tolist(), km2.tolist(), n.tolist()):
        code = _num(val, float(val).is_integer())
        out[str(code)] = {"label": legend.get(str(code)), "cells": int(c), "km2": _r4(k),
                          "share_pct": round(100.0 * k / total, 1) if total else 0.0}
    return out


def _class_counts_full(path, area, win, nod_vals, geographic: bool, legend: dict) -> dict:
    """The area of every value inside `area`, read block by block over the window at
    full resolution — never a decimated read: a thin class is exactly what decimation
    loses."""
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.windows import Window
    acc_km2, acc_n = {}, {}
    rows_per = max(1, int(1_048_576 // max(1, int(win.width))))
    with rasterio.open(path, OVERVIEW_LEVEL="NONE") as src:
        for r0 in range(0, int(win.height), rows_per):
            w = Window(win.col_off, win.row_off + r0, win.width, min(rows_per, int(win.height) - r0))
            blk = src.read(1, window=w).astype("float64")
            t = src.window_transform(w)
            ok = geometry_mask([area.__geo_interface__], out_shape=blk.shape, transform=t,
                               invert=True) & np.isfinite(blk)
            if nod_vals:
                ok &= ~np.isin(blk, np.array(nod_vals, dtype="float64"))
            if not ok.any():
                continue
            wk = np.broadcast_to(_row_km2(t, blk.shape[0], geographic)[:, None], blk.shape)[ok]
            u, inv = np.unique(blk[ok], return_inverse=True)
            for val, k, c in zip(u.tolist(), np.bincount(inv, weights=wk).tolist(),
                                 np.bincount(inv).tolist()):
                acc_km2[val] = acc_km2.get(val, 0.0) + k
                acc_n[val] = acc_n.get(val, 0) + c
    total = sum(acc_km2.values())
    out = {}
    for val in sorted(acc_km2):
        code = _num(val, float(val).is_integer())
        out[str(code)] = {"label": legend.get(str(code)), "cells": int(acc_n[val]),
                          "km2": _r4(acc_km2[val]),
                          "share_pct": round(100.0 * acc_km2[val] / total, 1) if total else 0.0}
    return out


def _patches(arr, valid) -> int | None:
    """Connected regions of equal value inside the area (4-connected)."""
    if not valid.any() or int(valid.sum()) > _PATCH_LIMIT:
        return None
    from rasterio.features import shapes
    _, inv = np.unique(arr[valid], return_inverse=True)
    labels = np.zeros(arr.shape, dtype="int32")
    labels[valid] = inv.astype("int32") + 1
    return sum(1 for _ in shapes(labels, mask=valid, connectivity=4))


_PARTS = (("north-west", "north", "north-east"), ("west", "centre", "east"),
          ("south-west", "south", "south-east"))


def _by_part(a, valid, integer: bool, nominal: bool, legend: dict) -> dict | None:
    """Each ninth of the area's window, north-west to south-east — the mean of its
    cells with a value, or for a nominal layer (class codes that are names, not
    amounts) its most common class and that class's share. A sentence about WHERE
    something is has a number to cite; an average of class codes never is one."""
    h, w = a.shape
    if h < 3 or w < 3 or not valid.any():
        return None
    rows = np.array_split(np.arange(h), 3)
    cols = np.array_split(np.arange(w), 3)
    out = {}
    for i, rr in enumerate(rows):
        for j, cc in enumerate(cols):
            block = a[np.ix_(rr, cc)][valid[np.ix_(rr, cc)]]
            if not block.size:
                out[_PARTS[i][j]] = None
            elif nominal:
                vals, counts = np.unique(block, return_counts=True)
                k = int(np.argmax(counts))
                cls = _num(vals[k], integer)
                out[_PARTS[i][j]] = {"class": cls, "label": legend.get(str(cls)),
                                     "share": round(float(counts[k]) / block.size, 3)}
            else:
                out[_PARTS[i][j]] = float(f"{block.mean():.3g}")
    return out


def _sample(arr, valid, side: int):
    """Nearest-centre decimation to at most `side` cells on the longest edge."""
    h, w = arr.shape
    f = max(1, math.ceil(max(h, w) / max(1, side)))
    if f == 1:
        return arr, valid, 1
    rows = np.minimum(np.arange(math.ceil(h / f)) * f + f // 2, h - 1)
    cols = np.minimum(np.arange(math.ceil(w / f)) * f + f // 2, w - 1)
    return arr[np.ix_(rows, cols)], valid[np.ix_(rows, cols)], f


def _window(src, boundary):
    """(window clipped to the file, the same window unclipped) covering `boundary`,
    for north-up and south-up files alike."""
    from rasterio.windows import Window
    inv = ~src.transform
    x0, y0, x1, y1 = boundary.bounds
    cr = [inv * (x, y) for x in (x0, x1) for y in (y0, y1)]
    c0, c1 = min(c for c, _ in cr), max(c for c, _ in cr)
    r0, r1 = min(r for _, r in cr), max(r for _, r in cr)
    whole = Window(math.floor(c0), math.floor(r0),
                   math.ceil(c1) - math.floor(c0) + 1, math.ceil(r1) - math.floor(r0) + 1)
    try:
        clipped = whole.intersection(Window(0, 0, src.width, src.height))
    except Exception:
        clipped = None
    if clipped is not None and (clipped.width <= 0 or clipped.height <= 0):
        clipped = None
    return clipped, whole


def _in_crs(boundary, crs):
    """`boundary` (EPSG:4326) in a file's CRS."""
    if crs.to_epsg() == 4326:
        return boundary
    from rasterio.warp import transform_geom
    from shapely.geometry import shape as _shape
    return _shape(transform_geom("EPSG:4326", crs, boundary.__geo_interface__))


def _north_up(arr, transform):
    """A south-up read flipped to north-up rows, its transform with it."""
    from rasterio import Affine
    if transform.e <= 0:
        return arr, transform
    return arr[::-1], Affine(transform.a, transform.b, transform.c, transform.d,
                             -transform.e, transform.f + transform.e * arr.shape[0])


def _class_map(cg: dict, boundary, read_budget: int):
    """The class map a gatherer handed over (`_class_grid`), read over the area like a
    layer — budgeted, nearest, never the file's own overviews. Returns (values as
    float64, transform, crs, read level)."""
    import rasterio
    from rasterio import Affine
    path = cg.get("path")
    if not path and cg.get("layer"):
        from . import ingest
        path = ingest.source_raster(str(cg["layer"]))
    with rasterio.open(path, OVERVIEW_LEVEL="NONE") as src:
        if src.crs is None:
            raise ValueError("the class map declares no coordinate reference system")
        crs = src.crs
        win, _ = _window(src, _in_crs(boundary, crs))
        if win is None:
            raise ValueError("the class map does not extend over this area")
        level = 1
        n_px = float(win.width) * float(win.height)
        if n_px > read_budget:
            level = int(2 ** math.ceil(math.log2(math.sqrt(n_px / read_budget))))
        shape_ = (max(1, math.ceil(win.height / level)), max(1, math.ceil(win.width / level)))
        arr = src.read(1, window=win, out_shape=shape_) if level > 1 else src.read(1, window=win)
        transform = src.window_transform(win) * Affine.scale(win.width / arr.shape[1],
                                                             win.height / arr.shape[0])
    arr, transform = _north_up(arr.astype("float64"), transform)
    return arr, transform, crs, level


def _same_map(cg: dict, layer: str, path: str, row: dict | None = None) -> bool:
    """Is the class map this layer's own file, or a year of the same stack? A year map
    read as a layer is not cross-tabulated with itself, and two years of one stack are
    the pack's own change figures, not a second table of them."""
    if cg.get("layer") == layer or (cg.get("stack") and (row or {}).get("stack") == cg["stack"]):
        return True
    try:
        return bool(cg.get("path")) and os.path.realpath(str(cg["path"])) == os.path.realpath(str(path))
    except (TypeError, ValueError):
        return False


def _cross(cg: dict, boundary, vals, transform, crs, mode: str, units, legend: dict,
           read_budget: int, layer_level: int) -> dict:
    """`vals` (the layer's values over its read window, NaN where it has none) per class
    of the class map, inside `boundary`. The coarser grid is placed on the finer by
    nearest neighbour, and the method says which. mode 'amount': area, mean, median and
    — for a density — total per class; mode 'classes': the area of each of the layer's
    classes within each class of the map (a cross-tab)."""
    from rasterio.features import geometry_mask
    from rasterio.warp import Resampling, reproject
    c_arr, c_tr, c_crs, c_level = _class_map(cg, boundary, read_budget)
    lat = boundary.centroid.y
    m_cell = _cell_km2(transform.a, transform.e, lat, bool(crs.is_geographic))
    c_cell = _cell_km2(c_tr.a, c_tr.e, lat, bool(c_crs.is_geographic))
    m_m, c_m = round(math.sqrt(m_cell) * 1000), round(math.sqrt(c_cell) * 1000)
    if c_cell < m_cell * 0.999:            # the class map is finer: the layer goes onto it
        L = np.full(c_arr.shape, np.nan)
        reproject(vals, L, src_transform=transform, src_crs=crs, dst_transform=c_tr,
                  dst_crs=c_crs, src_nodata=np.nan, dst_nodata=np.nan,
                  resampling=Resampling.nearest)
        C, tr, tcrs = c_arr, c_tr, c_crs
        method = (f"the layer's cells (about {m_m} m) were placed on the class map's finer grid "
                  f"(about {c_m} m) by nearest neighbour: each class-map cell takes the value of "
                  "the layer cell it falls in, and is counted with its own area")
    else:                                  # the layer is finer (or the same): the map goes onto it
        C = np.full(vals.shape, np.nan)
        reproject(c_arr, C, src_transform=c_tr, src_crs=c_crs, dst_transform=transform,
                  dst_crs=crs, src_nodata=np.nan, dst_nodata=np.nan,
                  resampling=Resampling.nearest)
        L, tr, tcrs = vals, transform, crs
        method = (f"the class map (about {c_m} m) was placed on the layer's grid (about {m_m} m) "
                  "by nearest neighbour: each layer cell takes the class at its centre")
    if c_level > 1 or layer_level > 1:
        method += ("; read at every " + " / ".join(
            f"{_nth(lv)} cell of the {what}" for lv, what in ((layer_level, "layer"), (c_level, "class map"))
            if lv > 1) + " because the area exceeds the read budget")
    inside = geometry_mask([_in_crs(boundary, tcrs).__geo_interface__], out_shape=C.shape,
                           transform=tr, invert=True)
    W = np.broadcast_to(_row_km2(tr, C.shape[0], bool(tcrs.is_geographic))[:, None], C.shape)
    leg = {}
    for k, v in (cg.get("legend") or {}).items():
        try:
            leg[int(k)] = str(v).strip()
        except (TypeError, ValueError):
            continue
    outside = {float(v) for v in cg.get("outside_values") or []}
    d = density(units) if mode == "amount" else None
    classes = {}
    for code, label in sorted(leg.items()):
        if float(code) in outside:
            continue
        sel = inside & (C == code)
        km2 = float(W[sel].sum())
        cov = sel & np.isfinite(L)
        row = {"km2": _r4(km2), "km2_with_value": _r4(W[cov].sum())}
        if mode == "amount":
            if cov.any():
                lv, lw = L[cov], W[cov]
                row.update({"mean": _r4((lv * lw).sum() / lw.sum()), "p50": _r4(np.median(lv))})
                if d:
                    row["total"] = _r4(float((lv * lw).sum()) / d["km2_per_unit"])
        else:
            u, inv = np.unique(L[cov], return_inverse=True) if cov.any() else (np.array([]), None)
            row["by_value"] = ({} if inv is None else {
                legend.get(str(_num(val, True)), str(_num(val, True))): _r4(k)
                for val, k in zip(u.tolist(), np.bincount(inv, weights=W[cov]).tolist())})
        classes[label] = row
    known = np.isin(C, np.array([c for c in leg if float(c) not in outside], dtype="float64"))
    return {"map": cg.get("label") or cg.get("stack") or "the class map",
            "stack": cg.get("stack"), "year": cg.get("year"), "method": method,
            **({"total_unit": d["numerator"]} if d else {}),
            "classes": classes, "km2_outside_map": _r4(W[inside & ~known].sum())}


def read(layer: str, boundary, *, side: int | None = None,
         read_budget: int | None = None, class_grid: dict | None = None) -> dict:
    """One layer's values inside `boundary` (a shapely geometry, EPSG:4326); with a
    `class_grid` ({path, legend, outside_values, year, stack, label}), also per class of
    that map.

    Raises ValueError when the layer does not reach the area or cannot be placed; the
    caller turns that into a declared gap, never a zero."""
    import rasterio
    from rasterio import Affine
    from rasterio.features import geometry_mask

    from . import ingest, schema, tiffs
    from .verify import _check

    side = side or GRID_SIDE                   # read at call time: deployments tune them
    read_budget = read_budget or READ_BUDGET
    row = registered().get(layer, {})
    contract = schema.schema_for(layer) or {}
    rule = _role_rule(contract)
    nominal = str(rule.get("scale", "")).strip().lower() == "nominal"
    path = ingest.source_raster(layer)
    # OVERVIEW_LEVEL=NONE: a budgeted read decimates the full-resolution file. A file's
    # own overviews may be averaged or cubic, and would hand back values (a class 3
    # between a 1 and a 5) that the layer never holds.
    with rasterio.open(path, OVERVIEW_LEVEL="NONE") as src:
        if src.crs is None:
            raise ValueError(f"{layer}'s file declares no coordinate reference system, so "
                             "its cells cannot be placed on the area")
        crs_text = src.crs.to_string()
        area = boundary
        if src.crs.to_epsg() != 4326:
            from rasterio.warp import transform_geom
            from shapely.geometry import shape as _shape
            area = _shape(transform_geom("EPSG:4326", src.crs, boundary.__geo_interface__))
        win, whole = _window(src, area)
        if win is None:
            raise ValueError(f"{layer} does not extend over this area")
        level = 1
        n_px = float(win.width) * float(win.height)
        if n_px > read_budget:
            level = int(2 ** math.ceil(math.log2(math.sqrt(n_px / read_budget))))
        shape_ = (max(1, math.ceil(win.height / level)), max(1, math.ceil(win.width / level)))
        arr = src.read(1, window=win, out_shape=shape_) if level > 1 else src.read(1, window=win)
        # the true ratio of source cells to output cells, not the nominal level
        transform = src.window_transform(win) * Affine.scale(win.width / arr.shape[1],
                                                             win.height / arr.shape[0])
        wtrans = src.window_transform(whole) * Affine.scale(win.width / arr.shape[1],
                                                            win.height / arr.shape[0])
        wshape = (max(1, math.ceil(whole.height * arr.shape[0] / win.height)),
                  max(1, math.ceil(whole.width * arr.shape[1] / win.width)))
        file_nodata, dtype = src.nodata, src.dtypes[0]
        native = abs(src.transform.a)
        geographic = bool(src.crs.is_geographic)
        crs_epsg = src.crs.to_epsg()
        crs_obj = src.crs
    if transform.e > 0:                        # south-up: flip to north-up rows
        arr = arr[::-1]
        transform = Affine(transform.a, transform.b, transform.c, transform.d,
                           -transform.e, transform.f + transform.e * arr.shape[0])
        wtrans = Affine(wtrans.a, wtrans.b, wtrans.c, wtrans.d,
                        -wtrans.e, wtrans.f + wtrans.e * wshape[0])

    inside = geometry_mask([area.__geo_interface__], out_shape=arr.shape,
                           transform=transform, invert=True)
    in_total = int(geometry_mask([area.__geo_interface__], out_shape=wshape,
                                 transform=wtrans, invert=True).sum())
    touched = False
    if not inside.any():               # an area smaller than one cell
        inside = geometry_mask([area.__geo_interface__], out_shape=arr.shape,
                               transform=transform, invert=True, all_touched=True)
        touched = True
        in_total = max(in_total, int(inside.sum()))
    if not inside.any():
        raise ValueError(f"{layer} does not extend over this area")
    beyond = max(0, in_total - int(inside.sum()))
    nod_vals, nod_rule = _no_value(contract, file_nodata, row)
    stored = arr.astype("float64")
    finite = np.isfinite(stored)
    n_nan = int((inside & ~finite).sum())
    if n_nan:
        nod_rule += f"; {n_nan} cell(s) here are NaN"
    has_value = finite
    if nod_vals:
        has_value = finite & ~np.isin(stored, np.array(nod_vals, dtype="float64"))
    valid = inside & has_value
    # A file storing scaled integers (biomass x10 in int16) is read in its units; the
    # contract's range stays a range of what is stored.
    scaling = _scaling(contract)
    a = stored * scaling[0] + scaling[1] if scaling else stored
    integer = (np.issubdtype(arr.dtype, np.integer) and not scaling) or bool(
        valid.any() and np.all(np.mod(a[valid], 1) == 0))
    v = a[valid]
    lat = boundary.centroid.y
    cell_km2 = _cell_km2(transform.a, transform.e, lat, geographic)
    legend = {str(k): val for k, val in sorted(tiffs.legend(layer).items())}
    units = contract.get("units") or row.get("units") or row.get("unit")
    kind = _kind(contract, legend, nominal)
    classed = kind == "classes" and integer
    w_all = np.broadcast_to(_row_km2(transform, arr.shape[0], geographic)[:, None], arr.shape)

    summary = {"cells_in_area": int(inside.sum()), "cells_with_value": int(v.size),
               "cells_without_value": int(inside.sum() - v.size),
               "cells_beyond_layer": beyond,
               "cell_km2": round(cell_km2, 5),
               "km2_in_area": _r4(w_all[inside].sum()),
               "km2_with_value": _r4(w_all[valid].sum()),
               "coverage_pct": round(100.0 * v.size / max(1, int(inside.sum())), 1)}
    if v.size:
        vals, counts = np.unique(v, return_counts=True)
        top = int(np.argmax(counts))
        patches = _patches(a, valid)
        summary.update({
            "min": _num(v.min(), integer), "max": _num(v.max(), integer),
            **({} if nominal else {"mean": float(f"{v.mean():.4g}")}),
            "distinct": int(vals.size),
            "dominant": {"value": _num(vals[top], integer),
                         "share": round(float(counts[top]) / v.size, 3)},
            "uniform": bool(vals.size == 1),
            "by_part": (_by_part(a, valid, integer, nominal, legend) if vals.size > 1 else None),
            "patches": patches,
            "mean_patch_km2": (round(v.size * cell_km2 / patches, 3) if patches else None)})
        if classed:
            # The area of each class on EVERY full-resolution cell, each with its own
            # area — a decimated read loses exactly the thin classes a question is about.
            full = level == 1
            if not full and float(win.width) * float(win.height) <= FULL_COUNT_BUDGET:
                summary["classes"] = _class_counts_full(path, area, win, nod_vals, geographic,
                                                        legend)
                full = True
            else:
                summary["classes"] = _class_counts(v, w_all[valid], legend)
            summary["classes_counted"] = (
                "on every cell at full resolution, each with its own area" if full else
                f"on every {_nth(level)} cell (the area holds more than {FULL_COUNT_BUDGET:,} "
                "cells), each standing for the cells around it")
        else:
            summary.update(_amount_summary(v, w_all[valid], units))

    obs = {"dtype": dtype, "nodata": file_nodata, "crs_epsg": crs_epsg,
           "pixel_size_deg": native,           # the FILE's grid, not the read level's
           "sampled_min": float(stored[valid].min()) if v.size else None,
           "sampled_max": float(stored[valid].max()) if v.size else None}
    if contract:
        mism = _check(contract, obs)
        validation = "structural-contract-FAILED" if mism else "structural-contract-checked"
        vnote = ("; ".join(mism) if mism else
                 "dtype, nodata, CRS, pixel size and this area's value range match the declared contract")
    else:
        validation, vnote = "unvalidated", "no contract is declared for this layer"

    g_arr, g_valid, f = _sample(a, valid, side)
    gh, gw = g_arr.shape
    flat = [(_num(x, integer) if ok else None)
            for x, ok in zip(g_arr.ravel().tolist(), g_valid.ravel().tolist())]
    palette = sorted({x for x in flat if x is not None})
    if len(palette) <= len(PALETTE_SYMBOLS):
        sym = {val: PALETTE_SYMBOLS[i] for i, val in enumerate(palette)}
        grid_cells = {"encoding": "palette", "palette": palette,
                      "cells": "".join("." if x is None else sym[x] for x in flat)}
    else:
        grid_cells = {"encoding": "values", "cells": flat}
    left, top_ = transform.c, transform.f
    right = left + transform.a * arr.shape[1]
    bottom = top_ + transform.e * arr.shape[0]
    rng = ([contract.get("valid_min"), contract.get("valid_max")]
           if contract.get("valid_min") is not None or contract.get("valid_max") is not None
           else None)
    unit_word = "deg" if geographic else "m"
    by_class_of = None
    if class_grid and v.size and not _same_map(class_grid, layer, path, row):
        try:
            by_class_of = _cross(class_grid, boundary, np.where(has_value, a, np.nan), transform,
                                 crs_obj, "classes" if classed else "amount", units, legend,
                                 read_budget, level)
        except Exception as exc:
            by_class_of = {"error": f"{type(exc).__name__}: {exc}",
                           "map": class_grid.get("label") or class_grid.get("stack")}
    if by_class_of is not None:
        summary["by_class_of"] = by_class_of
    rec = {
        "title": row.get("title") or layer,
        "unit": units,
        "unit_note": (str(units) if units else "units not declared")
                     + (f"; declared range {rng[0]}–{rng[1]}"
                        + (" as stored" if scaling else "") if rng else "")
                     + (f"; stored values x {scaling[0]:g}"
                        + (f" + {scaling[1]:g}" if scaling[1] else "")
                        + f" give {units or 'the values'}" if scaling else "")
                     + ("; class codes are names, not amounts" if nominal else ""),
        "declared_range": rng,
        # what the summary was computed as: a classes role holding fractions is an amount
        "values": "classes" if classed else ("amount" if kind == "classes" else kind),
        "scale": "nominal" if nominal else None,
        "role": contract.get("role") or row.get("role"),
        "source": row.get("source"), "licence": row.get("license"),
        "vintage": row.get("vintage"),
        "validation": validation, "validation_note": vnote,
        "nodata": {"value": (nod_vals[0] if nod_vals else None), "values": nod_vals,
                   "rule": nod_rule},
        "resolution": {f"source_cell_{unit_word}": round(native, 9),
                       f"grid_cell_{unit_word}": round(abs(transform.a) * f, 9),
                       "source_cell_m": round(math.sqrt(_cell_km2(native, native, lat, geographic)) * 1000)},
        **({"legend": legend} if legend else {}),
        "summary": summary,
        "grid": {"w": gw, "h": gh,
                 "bounds": [round(left, 6), round(bottom, 6), round(right, 6), round(top_, 6)],
                 "crs": crs_text, **grid_cells},
        "downsampled": (None if f == 1 else {
            "from": [int(arr.shape[1]), int(arr.shape[0])], "to": [gw, gh], "factor": f,
            "method": "nearest: each grid cell shows the source value at its centre; "
                      "the summary is computed on every cell read"}),
        "read_level": (None if level == 1 else {
            "overview_level": level,
            "note": f"the area is larger than the read budget, so every {_nth(level)} cell of the "
                    f"full-resolution file was read (nearest, never the file's own overviews) "
                    "and the summary is computed on those cells"
                    + ("; the class areas are counted on every full-resolution cell"
                       if classed and summary.get("classes_counted", "").startswith("on every cell")
                       else "")}),
        "area_smaller_than_a_cell": touched,
    }
    for k in ("usage_notes", "review", "staged_by", "contribution_id", "description",
              "granularity", "colors", "prepared"):
        if row.get(k):
            rec[k] = row[k]
    if row.get("amended"):
        last = row["amended"][-1]
        rec["amended"] = {"times": len(row["amended"]),
                          "last": {k: last.get(k) for k in ("contribution_id", "by",
                                                            "rationale", "replaced", "review")}}
    return rec


def thin(grid: dict) -> dict:
    """Every other row and column of a grid, either encoding, bounds unchanged."""
    w, h = int(grid["w"]), int(grid["h"])
    cells = grid["cells"]
    rows = range(min(1, h - 1), h, 2)
    cols = range(min(1, w - 1), w, 2)
    if isinstance(cells, str):
        new = "".join(cells[r * w + c] for r in rows for c in cols)
    else:
        new = [cells[r * w + c] for r in rows for c in cols]
    return {**grid, "w": len(cols), "h": len(rows), "cells": new}


def values_sha256(layer: str, rec: dict, area_sha: str) -> str:
    """A fingerprint of exactly what was returned for one layer over one area — the
    receipt records it, so the values a brief rests on can be checked later."""
    import hashlib
    import json
    body = {"layer": layer, "area_sha256": area_sha, "grid": rec["grid"],
            "summary": rec["summary"], "nodata": rec["nodata"]["value"],
            "unit": rec.get("unit"), "downsampled": rec.get("downsampled"),
            "read_level": rec.get("read_level")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def area_sha256(boundary) -> str:
    import hashlib
    import json
    return hashlib.sha256(json.dumps(boundary.__geo_interface__, sort_keys=True,
                                     default=str).encode()).hexdigest()


def _coverage_words(share: float) -> str:
    """'only 99.8%' — one decimal, and never 'only 100%' for a sliver short of it."""
    pct = share * 100
    if pct >= 99.95:
        return "all but a sliver (more than 99.9%)"
    return f"only {pct:.1f}%"


def for_area(boundary, wanted: list[dict], area_name: str,
             class_grid: dict | None = None) -> tuple[dict, list]:
    """Read every wanted layer ({layer, why}) over one area, sharing GRID_TOTAL
    cells between them; with a `class_grid`, each also per class of that map. Returns
    ({layer: record}, gaps)."""
    import json
    gaps, out = [], {}
    a_sha = area_sha256(boundary)
    side = min(GRID_SIDE, max(8, int(math.sqrt(GRID_TOTAL / max(1, len(wanted))))))
    for w in wanted:
        layer = w["layer"]
        try:
            rec = read(layer, boundary, side=side, class_grid=class_grid)
        except Exception as exc:
            gaps.append(f"values of {layer} could not be read for {area_name}: {exc}. "
                        "Not a zero: the layer was not read here.")
            continue
        rec["why"] = w.get("why")
        rec["area_sha256"] = a_sha
        s = rec["summary"]
        if s["cells_beyond_layer"]:
            share = s["cells_in_area"] / (s["cells_in_area"] + s["cells_beyond_layer"])
            gaps.append(f"{layer} covers {_coverage_words(share)} of {area_name}: "
                        f"{s['cells_beyond_layer']:,} of its cells lie beyond the layer's "
                        "extent and are not counted anywhere in its values")
        if (s.get("by_class_of") or {}).get("error"):
            gaps.append(f"values of {layer} per class of {s['by_class_of']['map']} could not be "
                        f"computed for {area_name}: {s['by_class_of']['error']}")
        if rec.get("values") != "classes" and s["cells_with_value"]:
            # An amount says what it covers and how coarse it is next to the place.
            if s["cells_without_value"]:
                gaps.append(f"{layer} has a value on {s['coverage_pct']:.1f}% of the cells of "
                            f"{area_name} ({s['cells_without_value']:,} of "
                            f"{s['cells_in_area']:,} are no-data: {rec['nodata']['rule']}): its "
                            "mean, percentiles and any total are of the covered part only — "
                            "the rest is not a zero")
            if s["cells_in_area"] < FEW_CELLS:
                gaps.append(f"{layer}'s cells are about {round(math.sqrt(s['cell_km2']) * 1000):,} m "
                            f"across ({_fig(_r4(s['cell_km2']))} km² each) and {area_name} holds "
                            f"{s['cells_in_area']:,} of them: each figure averages over cells "
                            "that straddle the boundary and over anything smaller than a cell, "
                            "so read it as indicative of the place, not measured within it")
        if not s["cells_with_value"]:
            gaps.append(f"{layer} has no value anywhere in {area_name}: every cell inside is "
                        f"no-data ({rec['nodata']['rule']})")
        elif s.get("uniform"):
            g = (rec.get("granularity") or {}).get("typical_run_km")
            gaps.append(f"{layer} holds ONE value ({s['min']}) across all "
                        f"{s['cells_with_value']:,} cells of {area_name} it has a value for "
                        f"(cells of about {round(math.sqrt(s['cell_km2']) * 1000)} m): it does not "
                        "vary inside this area, so it cannot show where in it anything differs. "
                        "One area cannot tell a uniform place from a layer whose unit is larger "
                        "than the place"
                        + (f"; at the gate its equal values typically ran {g[0]:.1f} km"
                           if g else "") + ".")
        if rec["validation"] == "structural-contract-FAILED":
            gaps.append(f"{layer} failed its declared contract here: {rec['validation_note']}")
        missing = [k for k in ("source", "vintage", "licence") if not rec.get(k)]
        if missing:
            gaps.append(f"{layer}: no {', '.join(missing)} recorded in the platform's registry")
        out[layer] = rec
    # The response carries the grids inline, so they are budgeted by size, not by cell
    # count: the largest grid is thinned until all of them fit, and says so.
    size = lambda: sum(len(json.dumps(r["grid"])) for r in out.values())
    while out and size() > INLINE_CHARS:
        name = max(out, key=lambda k: len(json.dumps(out[k]["grid"])))
        g = out[name]["grid"]
        if min(g["w"], g["h"]) <= 4:
            break
        out[name]["grid"] = thin(g)
        out[name]["grid_thinned"] = {
            "to": [out[name]["grid"]["w"], out[name]["grid"]["h"]],
            "note": "thinned to keep the response proportionate; the summary is computed on "
                    "every cell read"}
    for layer, rec in out.items():
        rec["values_sha256"] = values_sha256(layer, rec, a_sha)
    return out, gaps


def _class_bits(s: dict, leg: dict) -> list[str]:
    """The area of each class, as counted."""
    cl = s.get("classes") or {}
    if not cl:
        return []

    def name(code, r):
        if r.get("label"):
            return f"{code} ({r['label']})"
        return f"{code} (not in the legend)" if leg else str(code)
    total = sum(r["km2"] for r in cl.values())
    return [f"Area by class, counted {s.get('classes_counted')}: "
            + "; ".join(f"{name(k, r)} {_fig(r['km2'])} km² ({r['share_pct']:.1f}%)"
                        for k, r in cl.items())
            + f" — shares of the {_fig(_r4(total))} km² that carry a value."]


def _amount_bits(s: dict, rec: dict) -> list[str]:
    """Percentiles, coverage and — for a density per area — the total, with its sums."""
    if s.get("p50") is None:
        return []
    u = rec.get("unit") or ""
    us = f" {u}" if u else ""
    out = [f"Over the {s['cells_with_value']:,} cells with a value: p10 {_fig(s['p10'])}{us}, "
           f"median {_fig(s['p50'])}{us}, p90 {_fig(s['p90'])}{us}; a value on "
           f"{s['coverage_pct']:.1f}% of the area's cells ({_fig(s['km2_with_value'])} of "
           f"{_fig(s['km2_in_area'])} km²)."]
    t = s.get("total")
    if t:
        how = ("its own area on the WGS84 ellipsoid" if "source_cell_deg" in rec["resolution"]
               else "its planar area in the file's projection")
        out.append(f"Total over the area: the sum of value × cell area over the {t['cells']:,} "
                   f"cells with a value, each with {how}, is {_fig(t['value'])} {t['unit']} — "
                   f"an area-weighted mean of {_fig(t['mean_area_weighted'])}{us} × "
                   f"{_fig(t['area'])} {t['area_unit']}"
                   + ("; estimated from the cells read" if rec.get("read_level") else "") + ".")
    return out


def _by_class_of_text(b: dict, rec: dict) -> str:
    """The layer per class of the gatherer's class map, every figure stated."""
    u = rec.get("unit") or ""
    us = f" {u}" if u else ""
    parts = []
    for label, r in b["classes"].items():
        if "by_value" in r:
            inner = ", ".join(f"{k} {_fig(v)} km²" for k, v in r["by_value"].items())
            parts.append(f"{label} {_fig(r['km2'])} km² — " + (inner or "no value of the layer"))
        elif not r["km2"]:
            parts.append(f"{label} absent (0 km²)")
        elif r.get("mean") is None:
            parts.append(f"{label} {_fig(r['km2'])} km², no value of the layer there")
        else:
            pct = 100.0 * r["km2_with_value"] / r["km2"] if r["km2"] else 0.0
            parts.append(f"{label} {_fig(r['km2'])} km² ({_fig(_r4(r['km2'] * 100))} ha), "
                         f"{pct:.1f}% with a value, mean {_fig(r['mean'])}{us}, median "
                         f"{_fig(r['p50'])}{us}"
                         + (f", total {_fig(r['total'])} {b['total_unit']}"
                            if r.get("total") is not None else ""))
    what = ("the area of each of the layer's classes" if b["classes"] and all(
        "by_value" in r for r in b["classes"].values()) else
            "area-weighted mean, median" + (" and total (the sum of value × cell area)"
                                            if b.get("total_unit") else ""))
    return (f"Per class of {b['map']}" + (f" in {b['year']}" if b.get("year") else "")
            + f" inside the area, {what}: " + "; ".join(parts)
            + f". Outside that map or unclassified: {_fig(b['km2_outside_map'])} km². "
            f"Method: {b['method']}.")


def citation_text(layer: str, rec: dict, area_name: str) -> str:
    """Every number a brief may state about these values, written literally — the
    groundedness gate reads citation text."""
    s = rec["summary"]
    nominal = rec.get("scale") == "nominal"
    leg = rec.get("legend") or {}
    named = rec["title"] if rec["title"] == layer else f"{rec['title']} ({layer})"
    bits = [f"Values of {named} inside {area_name}"
            + (f", {rec['why']}" if rec.get("why") else "") + "."]
    if s["cells_with_value"]:
        dom = s["dominant"]["value"]
        counted_m = round(math.sqrt(s["cell_km2"]) * 1000)
        size = (f"cells of about {counted_m} m" if not rec.get("read_level") else
                f"counted on cells of about {counted_m} m; the file's cells are about "
                f"{rec['resolution']['source_cell_m']} m")
        bits.append(
            f"{s['cells_with_value']:,} of {s['cells_in_area']:,} cells carry a value and "
            f"{s['cells_without_value']:,} do not ({size}); min {s['min']}, max {s['max']}"
            + ("" if nominal else f", mean {s['mean']}")
            + f"; {s['distinct']} distinct value(s); the most common, {dom}"
            + (f" ({leg[str(dom)]})" if leg.get(str(dom)) else "")
            + f", covers {s['dominant']['share'] * 100:.1f}% of the cells with a value.")
        if s.get("uniform"):
            bits.append(f"The layer holds a single value ({s['min']}) across the whole area, so "
                        "it does not show where inside the area anything varies.")
        else:
            if s.get("mean_patch_km2") is not None:
                bits.append(f"Equal values come in {s['patches']} patch(es) averaging "
                            f"{s['mean_patch_km2']} km² here.")
            parts = {k: v for k, v in (s.get("by_part") or {}).items() if v is not None}
            if parts and nominal:
                bits.append("Most common class by part of the area: " + "; ".join(
                    f"{k} {v['class']}" + (f" ({v['label']})" if v.get("label") else "")
                    + f" {v['share'] * 100:.0f}%" for k, v in parts.items()) + ".")
            elif parts:
                bits.append("Mean by part of the area: "
                            + "; ".join(f"{k} {v}" for k, v in parts.items()) + ".")
        bits += _class_bits(s, leg) if rec.get("values") == "classes" else _amount_bits(s, rec)
        if s.get("by_class_of") and not s["by_class_of"].get("error"):
            bits.append(_by_class_of_text(s["by_class_of"], rec))
    else:
        bits.append("No cell inside the area carries a value.")
    if s.get("cells_beyond_layer"):
        bits.append(f"{s['cells_beyond_layer']:,} cells of the area lie beyond the layer's extent.")
    if leg:
        bits.append("Legend: " + "; ".join(f"{k}={v}" for k, v in leg.items()) + ".")
    nod = rec["nodata"]
    bits.append(f"Unit: {rec['unit_note']}. No-data: "
                + (", ".join(str(v) for v in nod.get("values") or []) or "none")
                + f" ({nod['rule']}). Contract: {rec['validation']} "
                f"({rec['validation_note']}). Source: {rec.get('source') or 'unrecorded'}; "
                f"vintage {rec.get('vintage') or 'unrecorded'}; licence "
                f"{rec.get('licence') or 'unrecorded'}.")
    if rec.get("downsampled"):
        d = rec["downsampled"]
        bits.append(f"The grid in layers.{layer}.grid is downsampled from "
                    f"{d['from'][0]}x{d['from'][1]} to {d['to'][0]}x{d['to'][1]} cells.")
    if rec.get("grid_thinned"):
        t = rec["grid_thinned"]["to"]
        bits.append(f"To keep the response proportionate it is returned at {t[0]}x{t[1]} cells.")
    if rec.get("read_level"):
        bits.append(rec["read_level"]["note"][0].upper() + rec["read_level"]["note"][1:] + ".")
    if rec.get("prepared"):
        bits.append("At ingest: " + "; ".join(str(n).rstrip(".") for n in rec["prepared"]) + ".")
    if rec.get("amended"):
        last = rec["amended"]["last"]
        bits.append(f"This layer's record was corrected ({rec['amended']['times']} time(s); "
                    f"latest by {last.get('by') or 'a contributor'}: {last.get('rationale')}; "
                    f"it replaced {', '.join(sorted(last.get('replaced') or {}))})"
                    + (f" — {last['review']}" if last.get("review") else "") + ".")
    if rec.get("usage_notes"):
        bits.append(f"Contributor guidance: {rec['usage_notes']}")
    if rec.get("review"):
        bits.append(f"Review: {rec['review']}.")
    return " ".join(bits)


def granularity(path: str, contract: dict | None = None, windows: int = 4,
                side: int = 512, row: dict | None = None) -> dict | None:
    """How coarse a raster's values really are — read at full resolution in a few
    windows centred where it has data, measured as the typical run of equal values
    along rows and columns. Advisory, for the contribution preview: a layer described
    as varying by sub-district whose equal values run for tens of kilometres says
    otherwise. Only a layer holding non-integral values gets the warning: a class
    layer stored as floats repeats its classes by design."""
    import rasterio
    from rasterio.windows import Window
    try:
        with rasterio.open(path) as src:
            nod_vals, _ = _no_value(contract or {}, src.nodata, row or {})
            geographic = bool(src.crs and src.crs.is_geographic)
            W, H = src.width, src.height
            s = min(side, W, H)

            def valid(a):
                ok = np.isfinite(a)
                if nod_vals:
                    ok &= ~np.isin(a, np.array(nod_vals, dtype="float64"))
                return ok

            oh, ow = min(256, H), min(256, W)
            ov = src.read(1, out_shape=(oh, ow)).astype("float64")
            cand = np.argwhere(valid(ov))
            if not cand.size:
                return None
            picks = cand[np.linspace(0, len(cand) - 1, num=min(windows * 3, len(cand))).astype(int)]
            runs, distinct, seen, fractional = [], [], set(), False
            for r, c in picks:
                if len(runs) >= windows:
                    break
                row_ = int(max(0, min(H - s, (r + 0.5) * H / oh - s / 2)))
                col = int(max(0, min(W - s, (c + 0.5) * W / ow - s / 2)))
                if (row_ // s, col // s) in seen:
                    continue
                seen.add((row_ // s, col // s))
                win = Window(col, row_, s, s)
                a = src.read(1, window=win).astype("float64")
                ok = valid(a)
                if ok.mean() < 0.05:
                    continue
                px, py = ok[:, 1:] & ok[:, :-1], ok[1:, :] & ok[:-1, :]
                if not px.any() or not py.any():
                    continue
                fractional = fractional or bool(np.any(np.mod(a[ok], 1) != 0))
                fx = ((a[:, 1:] == a[:, :-1]) & px).sum() / px.sum()
                fy = ((a[1:, :] == a[:-1, :]) & py).sum() / py.sum()
                lat = rasterio.windows.bounds(win, src.transform)[1]
                dx_km = _cell_km2(src.transform.a, src.transform.a, lat, geographic) ** 0.5
                runs.append((min(1 / max(1e-9, 1 - fx), s) * dx_km,
                             min(1 / max(1e-9, 1 - fy), s) * dx_km))
                distinct.append(int(np.unique(a[ok]).size))
            if not runs:
                return None
            cell_km = _cell_km2(src.transform.a, src.transform.a,
                                (src.bounds.bottom + src.bounds.top) / 2, geographic) ** 0.5
    except Exception:
        return None
    rx_km = float(np.median([r[0] for r in runs]))
    ry_km = float(np.median([r[1] for r in runs]))
    return {
        "windows_read": len(runs), "window_cells": s, "cell_km": round(cell_km, 3),
        "distinct_values_per_window": distinct,
        "typical_run_km": [round(rx_km, 2), round(ry_km, 2)],
        "continuous": fractional,
        "note": (f"Read at full resolution in {len(runs)} window(s) of {s}x{s} cells where the "
                 f"layer has data: equal values typically run {rx_km:.1f} km east-west and "
                 f"{ry_km:.1f} km north-south (one cell is ~{cell_km * 1000:.0f} m)."
                 + (" The layer holds non-integral values, so a value repeated over many cells "
                    "means its source unit is larger than a cell: if the description says it "
                    "varies at a finer unit than these runs, check it before approval."
                    if fractional and max(rx_km, ry_km) > 3 * cell_km else "")),
    }
