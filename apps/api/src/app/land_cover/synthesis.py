"""The LAND-COVER domain pack: pixel counts over a yearly land-cover stack, as
citable evidence.

Nothing here runs a model at pack time. The pack resolves an area, clips the
chosen year(s) of a registered stack to it, counts pixels per class at each
pixel's own latitude, and writes every figure a brief may quote into a
citation's text — with the resolution read, the mask, the boundary's source and
the stack's undocumented accuracy stated beside the numbers, not under them.
Change between two years is computed on the pixels classified in BOTH years,
because the producer's own whole-map tables disagree on coverage and a naive
difference reads that as change. The map payload, the per-class time series and
the fire detections ride the same pack; the composition at the bottom is the
accompanied path for consumers with no model of their own.
"""
from __future__ import annotations

import json
import math
import os
import re

from shapely.geometry import shape

from ..graph.geo import ingest, layer_values, rasterstats, schema, stacks, tiffs, verify
from ..rag import filters as _rag_filters

SECTIONS = (
    "## What the maps show",
    "## Method, resolution and masks",
    "## What's missing and how to weigh it",
    "## Reading land-cover change",
)
ROLE = "landcover"
PACK_ID = "land-cover"
# Raster counting is cheap, so a country is a fair question here (it is not for OSM
# asset fetching, which is what the resolver's own 25,000 km² cap protects).
AREA_CAP_KM2 = 250_000.0
# Full resolution up to about a province; above it, the overviews — and the pack says so.
PIXEL_BUDGET = 60_000_000
DEFAULT_COLORS = ["#4f8fd6", "#cfe3f5", "#2e7d32", "#c9b27c", "#d64545", "#e6c84a",
                  "#b07b4f", "#8c8c8c", "#9bcb6b", "#7a5c99", "#e08b3c", "#3cb4b4"]
CHANGE_COLORS = {1: "#2e7d32", 2: "#d64545", 3: "#9bcb6b"}


# ----------------------------------------------------------------- helpers

def _year(v, what="year"):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a map year such as 2022, got {v!r}") from None


def _classes(v):
    if not v:
        return []
    if isinstance(v, str):
        v = v.split(",")
    return [str(c).strip() for c in v if str(c).strip()]


def _km2(x):
    return f"{x:,.1f}"


def _ha(km2):
    return f"{round(km2 * 100):,}"


def _pct(x):
    return f"{x:.1f}"


def _signed(x, unit=""):
    return f"{x:+,.1f}{unit}"


def _res_phrase(level, px_deg):
    m = px_deg * 111_320
    if level <= 1:
        return f"read at full resolution, {m:.0f} m cells at the equator"
    return (f"read at overview level {level}, {m:.0f} m cells at the equator — above the "
            f"pack's full-resolution budget, so the boundary mask is applied at that cell size")


def landcover_stacks():
    """{stack_id: row} for every registered stack whose role is land cover, in
    registration order. The role is the stack's namespace as the gate assigned it
    (contrib.rasters), so a stack of another role — a change map, say — is never
    counted, chosen or listed as land cover, whatever its id looks like."""
    return {k: v for k, v in stacks.rows().items() if v.get("role") == ROLE}


class NoStackHere(ValueError):
    """No registered stack can count classes for this area (and year). A class
    question declines with it; a request that only measures vectors or layers gets
    an area-only answer that carries `brief` as a gap. `outside`: no stack's map
    reaches the area at all."""

    def __init__(self, message, brief=None, outside=False):
        super().__init__(message)
        self.brief = brief or message
        self.outside = outside


def _extent_box(row):
    """(w, s, e, n) of a stack's declared extent, or None when it declares none."""
    ext = row.get("extent") or {}
    lon, lat = ext.get("lon"), ext.get("lat")
    if not lon or not lat:
        return None
    return float(lon[0]), float(lat[0]), float(lon[1]), float(lat[1])


def _box_phrase(b):
    return f"lon {b[0]:.2f} to {b[2]:.2f}, lat {b[1]:.2f} to {b[3]:.2f}"


def _km_to_box(boundary, b):
    """Great-circle km from the area's centre to the nearest point of an extent box."""
    c = boundary.centroid
    lon, lat = min(max(c.x, b[0]), b[2]), min(max(c.y, b[1]), b[3])
    p1, p2 = math.radians(c.y), math.radians(lat)
    h = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon - c.x) / 2) ** 2)
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _stacks_by_distance(boundary, rows, limit=None):
    """'title (id): lon .. lat .., ~N km away' for registered stacks, nearest first."""
    placed = sorted(((_km_to_box(boundary, b), sid, row) for sid, row in rows.items()
                     if (b := _extent_box(row))), key=lambda t: t[0])
    out = [f"{row.get('title', sid)} ({sid}): {_box_phrase(_extent_box(row))}, "
           f"~{km:,.0f} km away" for km, sid, row in placed[:limit]]
    return "; ".join(out)


def _cover_share(boundary, row):
    """How much of the area the stack's declared extent covers, 0-1 (0 when undeclared)."""
    from shapely.geometry import box
    b = _extent_box(row)
    if not b or boundary.area <= 0:
        return 0.0
    return boundary.intersection(box(*b)).area / boundary.area


def stack_cell_deg(sid, row):
    """The stack's cell size in degrees: its first year's file header (cheap, and
    true), else its declared contract; None when neither can be read."""
    try:
        ys = stacks.years(row)
        path = stacks.year_path(str(row["layer_pattern"]).format(year=ys[0])) if ys else None
        if path:
            return float(stacks.header(path)["pixel_size_deg"])
    except Exception:
        pass
    try:
        px = (schema._doc().get("stacks") or {}).get(sid, {}).get("pixel_size_deg")
        return float(px) if px else None
    except Exception:
        return None


def _pick_stack(boundary, wanted=None, years=(), name="this area"):
    """(stack_id, row, others, why): the registered land-cover stack to read for this
    area, every OTHER registered stack that also covers it, and why this one was
    read. Unless the caller names one (`stack`), the covering stacks that HAVE the
    requested years are ranked — two or more classes first (a one-class map cannot
    show loss or gain), then the one whose extent covers most of the area, then the
    finest cells (within 1% counts as equal), then the most years, then the first
    registered (stacks.rank, the one rule) — and the others are declared as a gap, never
    silently ignored.
    The first registered match used to win: a one-class mangrove extent map was read
    over a Costa Rica bay while an eleven-class map of the same years sat beside it.
    Then a raw cell size 0.02% finer, from reprojection alone, let a contributed copy
    beat the map it was made from."""
    rows = landcover_stacks()
    if not rows:
        raise NoStackHere("no land-cover stack is registered on this platform — a hub "
                          "contributes one as a `kind: stack` catalog row")
    minx, miny, maxx, maxy = boundary.bounds
    covering = []
    for sid, row in rows.items():
        b = _extent_box(row)
        if b is None:                    # extent undeclared: cannot rule it out
            covering.append((sid, row))
        elif maxx >= b[0] and minx <= b[2] and maxy >= b[1] and miny <= b[3]:
            covering.append((sid, row))
    if wanted:
        w = str(wanted).strip()
        if w not in rows:
            raise ValueError(f"no land-cover stack named {w!r} is registered — one of: "
                             + ", ".join(sorted(rows)))
        if w not in {sid for sid, _ in covering}:
            raise ValueError(f"{rows[w].get('title', w)} ({w}) does not cover this area; "
                             "covering stacks: "
                             + (", ".join(sid for sid, _ in covering) or "none"))
        others = [(sid, row) for sid, row in covering if sid != w]
        return w, rows[w], others, "named by the caller"
    if not covering:
        raise NoStackHere(
            f"{name} lies outside every registered land-cover stack, so no class can be "
            f"counted there. The {len(rows)} registered stack(s), nearest first: "
            + _stacks_by_distance(boundary, rows)
            + ". To measure contributed layers there instead, pass vectors=[...] or layers=[...]",
            brief="it lies outside every registered land-cover stack", outside=True)
    want = [int(y) for y in years if y]
    able = [(sid, row) for sid, row in covering
            if all(y in stacks.years(row) for y in want)] if want else covering
    if not able and len(covering) == 1:
        # One stack, missing year: let layer_for say WHY it is missing ("no map
        # published by the producer") rather than a bare list of years.
        able = covering
    if not able:
        raise NoStackHere(
            f"no registered land-cover stack covering {name} has {', '.join(map(str, want))} — "
            + "; ".join(f"{row.get('title', sid)} ({sid}) has {stacks._span(stacks.years(row)) or 'no years'}"
                        for sid, row in covering))
    facts = {sid: (len(_legend(row)), _cover_share(boundary, row), stack_cell_deg(sid, row))
             for sid, row in able}
    able = stacks.rank(able, facts)        # stable: registration order breaks a tie
    sid, row = able[0]
    n_cls, share, cell = facts[sid]
    why = (f"chosen among the {len(able)} stack(s) holding the years asked — {stacks.RANK_RULE}: "
           f"it maps {n_cls} class(es), its extent covers {share * 100:.0f}% of {name}"
           + (f", its cells are {cell * 111_320:.0f} m" if cell else "")
           + f" and it holds {len(stacks.years(row))} year(s)"
           if len(able) > 1 else "the only covering stack holding the years asked")
    return sid, row, [(s, r) for s, r in covering if s != sid], why


_BOUNDARY_OK = ("admin boundary", "protected-area boundary", "contributed boundary",
                "supplied polygon", "drawn")


def _resolve_aoi(place, extras, trace):
    geometry = extras.get("geometry")
    if geometry is not None:
        kw = {"how": extras["within_how"]} if extras.get("within_how") else {}
        # A geometry with no place is still the caller's own area, and is labelled so.
        aoi = ingest.ensure_aoi(geometry=geometry, name=place or "the area you supplied",
                                layers=(), area_cap_km2=AREA_CAP_KM2, **kw)
    else:
        aoi = ingest.ensure_aoi(place=place, layers=(), area_cap_km2=AREA_CAP_KM2)
    how = str(aoi.get("how") or "")
    if aoi.get("approximate") or not how.startswith(_BOUNDARY_OK):
        # the contributed areas NEAR the asked point, nearest first, in forms that resolve
        near = aoi.get("nearby_contributed") or []
        hint = ""
        if near:
            sample = "; ".join(f"{d['name']} ({d['layer']}, {d['km']:.0f} km)" if isinstance(d, dict)
                               else str(d) for d in near[:6])
            hint = f", or name a contributed area near it ({sample})"
        raise ValueError(
            f"{place!r} did not resolve to a boundary but to {how}. Land-cover figures "
            "are only computed for a real boundary: name a district, province, park or "
            f"country under {AREA_CAP_KM2:,.0f} km², pass `geometry` with the boundary you hold"
            f"{hint}")
    trace.append(f"aoi[{aoi.get('name', place)}] {aoi.get('area_km2')} km2 via {how}")
    boundary = shape(json.load(open(aoi["admin"]))["features"][0]["geometry"])
    return aoi, boundary, how


def _legend(row):
    return {int(k): str(v).strip() for k, v in (row.get("legend") or {}).items()}


def _colors(row, legend):
    """{code: colour}: the row's own colours, keyed by class code or by its label (the
    form asks for {class: '#rrggbb'}, and a label key crashed every read), else defaults."""
    by_label = {str(v).strip().lower(): k for k, v in legend.items()}
    given = {}
    for k, v in (row.get("colors") or {}).items():
        try:
            code = int(k)
        except (TypeError, ValueError):
            code = by_label.get(str(k).strip().lower())
        if code is not None:
            given[code] = str(v)
    return {k: given.get(k) or DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
            for i, k in enumerate(sorted(legend))}


def _outside(row):
    vals = row.get("outside_values")
    return [0] if vals is None else [int(v) for v in vals]      # [] = the whole grid is mapped


def _match_classes(wanted, legend):
    """Class names a caller asked about, matched to the legend; unknown names declared."""
    by_name = {_fold(v): k for k, v in legend.items()}
    hits, unknown = [], []
    for w in wanted:
        k = by_name.get(_fold(w))
        if k is None:
            k = next((k for k, v in legend.items() if w.lower() in v.lower()), None)
        (hits if k is not None else unknown).append(k if k is not None else w)
    return hits, unknown


def _clip(aoi, layer, trace):
    aoi[layer] = ingest.hazard_clip(aoi, layer, max_pixels=PIXEL_BUDGET)
    level = stacks.clip_level(aoi[layer])
    trace.append(f"clip[{layer}]" + (f" level {level}" if level > 1 else ""))
    return aoi[layer], level


# ---------------------------------------------------------------- citations

def _class_area_citation(n, name, year, row, st, level, source):
    ordered = sorted(st["by_class"].items(), key=lambda kv: -kv[1]["km2"])
    parts = [f"{d['label']} {_km2(d['km2'])} km² ({_pct(d['share_pct'])}%)"
             for _, d in ordered if d["pixels"] > 0]
    absent = st["classes_absent"]
    text = (f"Land cover of {name} in {year} ({row.get('title', '')}; "
            f"{_res_phrase(level, st['pixel_size_deg'])}): {_km2(st['km2_classified'])} km² "
            f"({_ha(st['km2_classified'])} ha) classified of {_km2(st['km2_inside'])} km² inside the "
            f"boundary; {_km2(st['km2_outside'])} km² is outside the product footprint or "
            f"unclassified and is excluded from every share below. By class: "
            + "; ".join(parts) + "."
            + (f" Classes absent from this area: {', '.join(absent)}." if absent else "")
            + f" Pixels: {st['pixels_classified']:,} classified of {st['pixels_inside']:,} inside "
            "the boundary. Area per pixel is its area on the WGS84 ellipsoid at its own "
            "latitude, summed; counts are never resampled.")
    if st["values_not_in_legend"]:
        text += (f" Values present but not in the legend: {st['values_not_in_legend']} "
                 "(counted as classified, reported here, not attributed to a class).")
    return {"n": n, "kind": "class_area", "retrieval": "computed-at-pack-time",
            "source": source, "title": f"land cover by class, {name}, {year}",
            "validation": "deterministic-computation", "text": text,
            "series": {"id": f"lc_{year}_by_class", "unit": "km2", "categorical": True,
                       "points": [{"t": d["label"], "v": round(d["km2"], 1), "c": f"class {k}"}
                                  for k, d in sorted(st["by_class"].items())]}}


def _change_citation(n, name, y1, y2, ch, source):
    pct_same = 100.0 * ch["km2_unchanged"] / ch["km2_common"] if ch["km2_common"] else 0.0
    pct_changed = 100.0 * ch["km2_changed"] / ch["km2_common"] if ch["km2_common"] else 0.0
    net = []
    for k, d in sorted(ch["by_class"].items()):
        delta = d["km2_b"] - d["km2_a"]
        # a percentage of a near-zero base says nothing (0.0 -> 0.5 km² is "+5,800%")
        dpct = (f", {_signed(100.0 * delta / d['km2_a'])}%" if d["km2_a"] >= 1.0
                else ", from under 1 km²" if delta > 0 else "")
        net.append(f"{d['label']} {_km2(d['km2_a'])} -> {_km2(d['km2_b'])} km² "
                   f"({_signed(delta)} km²{dpct})")
    text = (f"Between {y1} and {y2}, on the {_km2(ch['km2_common'])} km² classified in both years "
            f"in {name} ({_km2(ch['km2_one_year_only'])} km² classified in only one year, excluded): "
            f"unchanged {_km2(ch['km2_unchanged'])} km² ({_pct(pct_same)}%), changed "
            f"{_km2(ch['km2_changed'])} km² ({_pct(pct_changed)}%). Net by class: "
            + "; ".join(net) + ". Single-year maps disagree on "
            f"{_pct(pct_changed)}% of these pixels; part of that is classifier noise, not "
            "change — read differences over several years, not between neighbours.")
    return {"n": n, "kind": "class_change", "retrieval": "computed-at-pack-time",
            "source": source, "title": f"net land-cover change, {name}, {y1}-{y2}",
            "validation": "deterministic-computation", "text": text,
            "series": {"id": f"lc_change_{y1}_{y2}_by_class", "unit": "km2",
                       "categorical": True,
                       "points": [{"t": d["label"], "v": round(d["km2_b"] - d["km2_a"], 1),
                                   "c": f"class {k}"} for k, d in sorted(ch["by_class"].items())]}}


def _transition_citation(n, name, y1, y2, cls, d, source):
    def flows(m):
        return ", ".join(f"{k} {_km2(v)} km²" for k, v in
                         sorted(m.items(), key=lambda kv: -kv[1])) or "none"
    net = d["km2_b"] - d["km2_a"]
    net_pct = (f"; {_signed(100.0 * net / d['km2_a'])}% of {y1} {d['label'].lower()}"
               if d["km2_a"] >= 1.0 else "")
    net_ha = f"{round(net * 100):+,}"
    text = (f"{d['label']} in {name}, {y1} -> {y2}: stable {_km2(d['stable_km2'])} km²; "
            f"loss {_km2(d['loss_km2'])} km² ({_ha(d['loss_km2'])} ha) to {flows(d['loss_to'])}; "
            f"gain {_km2(d['gain_km2'])} km² ({_ha(d['gain_km2'])} ha) from {flows(d['gain_from'])}; "
            f"net {_signed(net)} km² ({net_ha} ha{net_pct}). Loss here means a "
            f"pixel mapped {d['label']} in {y1} and another class in {y2}; it is not degradation "
            f"inside standing {d['label'].lower()}, and gain is not planting.")
    return {"n": n, "kind": "class_transitions", "retrieval": "computed-at-pack-time",
            "source": source, "title": f"{d['label']} transitions, {name}, {y1}-{y2}",
            "validation": "deterministic-computation", "text": text,
            "series": {"id": f"lc_{cls}_{y1}_{y2}_flows", "unit": "km2", "categorical": True,
                       "points": [{"t": "stable", "v": round(d["stable_km2"], 1)},
                                  {"t": "loss", "v": round(d["loss_km2"], 1)},
                                  {"t": "gain", "v": round(d["gain_km2"], 1)}]}}


def _series_citation(n, name, cls, label, pts, missing, level, px_deg, source):
    y0, y1 = pts[0][0], pts[-1][0]
    vals = [v for _, v in pts]
    lo, hi = min(pts, key=lambda p: p[1]), max(pts, key=lambda p: p[1])
    delta = vals[-1] - vals[0]
    dpct = f" ({_signed(100.0 * delta / vals[0])}%)" if vals[0] >= 1.0 else ""
    gap = [y for y in missing if y0 < y < y1]
    text = (f"{label} area in {name}, {y0}-{y1} ({_res_phrase(level, px_deg)}), km² on each "
            "year's classified mask: " + "; ".join(f"{y}: {_km2(v)}" for y, v in pts) + "."
            + (f" No map was published for {', '.join(str(y) for y in gap)}." if gap else "")
            + f" Lowest {_km2(lo[1])} km² in {lo[0]}; highest {_km2(hi[1])} km² in {hi[0]}; "
            f"{y1} vs {y0}: {_signed(delta)} km²{dpct}. Each year is an independent annual "
            "classification: swings of a few percent between consecutive years are within the "
            "maps' own disagreement and should be read as a multi-year trend.")
    return {"n": n, "kind": "class_series", "retrieval": "computed-at-pack-time",
            "source": source, "title": f"{label} area by year, {name}, {y0}-{y1}",
            "validation": "deterministic-computation", "text": text,
            "series": {"id": f"lc_{cls}_{y0}_{y1}_km2", "unit": "km2", "period": "year",
                       "points": [{"t": str(y), "v": round(v, 1)} for y, v in pts]}}


def _fire_citation(n, name, spec, q, passport, source):
    codes = passport.get("land_cover_codes") or {}
    if not q.get("n"):
        text = (f"{spec.get('title', 'Fire detections')}: no detections fall inside {name} "
                f"between {passport.get('date_range', ['?', '?'])[0]} and "
                f"{passport.get('date_range', ['?', '?'])[1]} ({passport.get('records', 0):,} "
                "detections in the whole layer). An absence of detections is an absence of "
                "satellite-observed thermal anomalies at MODIS's ~1 km scale, not proof of no fire.")
        series = None
    else:
        by_year = "; ".join(f"{y}: {c:,}" for y, c in sorted(q["by_year"].items()))
        by_class = "; ".join(f"{k} {v:,}" for k, v in sorted(q["by_class"].items(), key=lambda kv: -kv[1]))
        nrt_pct = 100.0 * q["nrt"] / q["n"]
        text = (f"{spec.get('title', 'Fire detections')} inside {name}: {q['n']:,} detections from "
                f"{q['first']} to {q['last']}. By year: {by_year}. By the land-cover class stamped at "
                f"the detection point: {by_class}. Day {q['n'] - q['night']:,} / night {q['night']:,}. "
                f"{q['nrt']:,} ({_pct(nrt_pct)}%) are near-real-time records not yet replaced by "
                f"standard processing. Median fire radiative power {q['median_frp']:.1f} MW; median "
                f"confidence {q['confidence_median']:.0f} of 100. A detection is the centre of a "
                "~1 km MODIS pixel attributed to the 30 m land-cover class at that point: a count of "
                "detections, not burned area, and the layer holds only detections stamped with "
                f"classes {sorted(int(k) for k in codes)} by its producer.")
        series = {"id": "fire_detections_by_year", "unit": "detections", "period": "year",
                  "points": [{"t": str(y), "v": c} for y, c in sorted(q["by_year"].items())]}
    return {"n": n, "kind": "fire_detections", "retrieval": "computed-at-pack-time",
            "source": spec.get("source", source), "title": f"{spec.get('title', 'fire detections')}, {name}",
            "validation": "deterministic-computation", "text": text,
            **({"series": series} if series else {})}


def _passport_citation(n, sid, row, layer, year, clip, level, legend, gaps):
    """Declared contract vs the clip actually read — 'checked' means compared."""
    meta = tiffs.entry(layer)
    path = meta.get("path") or ""
    contract, obs, hdr, notes, skip, skip_why = None, None, None, [], [], ""
    try:
        contract = schema.schema_for(layer)
    except Exception as exc:
        notes.append(f"declared contract unreadable ({type(exc).__name__})")
    try:
        # The range is of READINGS, by the rule the gate and the layer-values block use:
        # the outside values and the declared nodata (255 over open sea) are skipped.
        role = (contract or {}).get("role") or meta.get("role") or schema.role_for(layer)
        skip, skip_why = layer_values._no_value(
            {**(contract or {}), **({"role": role} if role else {})}, None, meta)
        obs = rasterstats.windowed_stats(clip, keep_values=tuple(legend), skip_values=tuple(skip))
    except Exception as exc:
        notes.append(f"clip statistics unreadable ({type(exc).__name__})")
    try:
        hdr = stacks.header(path or ingest.source_raster(layer))
    except Exception as exc:
        notes.append(f"file header unreadable ({type(exc).__name__})")
    bits = [f"Land-cover stack {sid}, year {year}: {row.get('title', sid)}, file "
            f"{os.path.basename(path) or layer + '.tif'}.",
            f"Source: {row.get('source', 'unattributed')} — licence {row.get('license', 'unstated')}, "
            f"published {row.get('pub_date') or row.get('vintage') or 'unrecorded'}.",
            f"Authority: {row.get('authority', 'unstated')}.",
            "Residency: platform-hosted copy, read in place from "
            + ("a stored zip archive." if "/vsizip/" in path else "local disk.")]
    if contract:
        extra = [str(contract["crs"])] if contract.get("crs") else []
        if contract.get("pixel_size_deg") is not None:
            extra.append(f"{contract['pixel_size_deg']}° per pixel")
        bits.append(f"Declared contract: dtype {contract.get('dtype')}, classes "
                    f"{contract.get('valid_min')}-{contract.get('valid_max')}, nodata "
                    f"{contract.get('nodata')}" + (", " + ", ".join(extra) if extra else "") + ".")
    else:
        gaps.append(f"{layer}: no declared contract — layer unvalidated")
    if obs and obs.get("sampled_min") is not None:
        bits.append(f"Observed on this clip: dtype {obs['dtype']}, range {obs['sampled_min']:g}-"
                    f"{obs['sampled_max']:g} across {obs.get('sampled_distinct')} distinct values "
                    "(sampled" + (f"; no-data, not counted as readings: {skip_why}" if skip else "")
                    + ").")
    if hdr:
        b = hdr["bounds"]
        bits.append(f"Grid: {hdr['width']}×{hdr['height']} px, lon {b[0]}..{b[2]}, "
                    f"lat {b[1]}..{b[3]}; overview levels {hdr['overviews'] or 'none'}.")
    bits.append(f"Read here at {'full resolution' if level <= 1 else f'overview level {level}'}.")
    # what ingest did to the contributed file (reprojected, recoded, band 1 of 4)
    prepared = [str(p).rstrip(".") for p in (meta.get("prepared") or []) if str(p).strip()]
    if prepared:
        bits.append("At ingest: " + "; ".join(prepared) + ".")
    acc = declared_accuracy(layer, row)
    if acc:
        bits.append(f"Accuracy, as declared by the producer for this year's map (not assessed "
                    f"for this area): {acc}.")
    else:
        bits.append("Accuracy: not documented on this platform — no accuracy figure was declared "
                    "with this layer, so none is cited.")
    validation = "unvalidated"
    if contract and obs and obs.get("sampled_min") is not None:
        # The clip was read at an overview: its cells are `level` source cells wide.
        # The contract describes the FILE, so compare the file's pixel size — an
        # overview read is not a contract failure.
        if level > 1 and obs.get("pixel_size_deg"):
            obs = {**obs, "pixel_size_deg": obs["pixel_size_deg"] / level}
        mism = verify._check(contract, obs)
        if mism:
            validation = "structural-contract-FAILED"
            bits.append("CONTRACT MISMATCH: " + "; ".join(mism) + ".")
            gaps.append(f"{layer} failed its structural contract: " + "; ".join(mism))
        else:
            validation = "structural-contract-checked"
    if notes:
        bits.append("Verification notes: " + "; ".join(notes) + ".")
    review = meta.get("review") or row.get("review")
    if review:
        bits.append(f"Review: {review}.")
    bits.append("Legend: " + "; ".join(f"{k}={v}" for k, v in sorted(legend.items())) + ".")
    return {"n": n, "kind": "landcover_layer", "retrieval": "computed-at-pack-time",
            "source": row.get("source", "catalog (unattributed)"),
            "title": f"{row.get('title', sid)}, {year}", "validation": validation,
            **({"url": row["url"]} if row.get("url") else {}),
            **({"pub_date": row["pub_date"]} if row.get("pub_date") else {}),
            **({"staged_by": meta["staged_by"], "contribution_id": meta.get("contribution_id")}
               if meta.get("staged_by") else {}),
            **({"review_status": review} if review else {}),
            **({"prepared": prepared} if prepared else {}),
            **({"fetched": meta["fetched"]} if meta.get("fetched") else {}),
            "authority": row.get("authority"), "residency": "platform-hosted",
            "text": " ".join(bits)}


def _method_citation(n, outside_values, two_years, level, how="", one_class=False):
    text = (
        "Land-cover figures are pixel counts. Window: the boundary's bounding box in the "
        "year's own grid, intersected with the dataset"
        + (" and read at full resolution. " if level <= 1 else
           f", read through the file's overviews at level {level} because the window exceeds the "
           f"pack's {PIXEL_BUDGET:,}-pixel full-resolution budget; the level is stated on every "
           "figure. ")
        + "Membership: a pixel counts when its centre falls inside the boundary. Area: computed "
        "on the WGS84 ellipsoid — each pixel's area is exact at its own latitude (via the "
        "authalic latitude), summed per class — no constant pixel area, no resampling of "
        f"counts. Outside or unclassified: the producer's values {sorted(outside_values)} "
        "are reported as one excluded area, never as a class, and shares are of the classified "
        "area only. "
        + ("Change: computed only on pixels classified in both years, after placing the later "
           "year on the earlier year's grid by whole pixels (nearest, no blending); a pixel "
           "mapped one class in the first year and another in the second counts as change, "
           "which includes classifier disagreement. "
           if two_years and not one_class else "")
        + ("Change: not computed — the map holds one class and everything else is outside it, "
           "so a pixel of that class in one year and outside the map in the other cannot be "
           "told from a pixel with no data; the years are compared by their areas only. "
           if two_years and one_class else "")
        + "Boundary: " + _boundary_phrase(how) + ". No model runs in this pack; the LLM does "
        "no arithmetic.")
    return {"n": n, "kind": "method", "retrieval": "config", "source": "platform method registry",
            "title": "land-cover counting method", "validation": "documented-method", "text": text}


_UNSTATED = ("unstated", "unknown", "none", "n/a")


def declared_accuracy(layer, row):
    """The accuracy declared with one year's map — its own row first, then the stack's —
    or None. A stack whose years carry different maps (WorldCover v100 in 2020, v200
    in 2021) declares a figure per year, and only the first year's was ever cited."""
    try:
        meta = tiffs.entry(layer) if layer else {}
    except Exception:
        meta = {}
    for v in (meta.get("accuracy"), row.get("accuracy")):
        s = str(v or "").strip()
        if s and s.lower() not in _UNSTATED:
            return s
    return None


_YEAR_PASSPORT = ("source", "license", "vintage", "pub_date", "url", "authority")


def _year_row(row, layer):
    """The stack row with one year's own passport over it (source, licence, vintage, ...),
    read from that year's catalog row. A stack whose years are different maps (WorldCover
    2020 v100, 2021 v200) cited the first year's map for every year."""
    try:
        meta = tiffs.entry(layer) if layer else {}
    except Exception:
        meta = {}
    own = {k: meta[k] for k in _YEAR_PASSPORT if meta.get(k)}
    if own.get("source", row.get("source")) != row.get("source"):
        own = {"url": None, "pub_date": None, **own}     # never the other map's link or date
    return {**row, **own}


def _sources(sid, *rows):
    """'<source> (platform computation)' for a figure read from these years' maps: each
    distinct source once, in year order."""
    seen = []
    for r in rows:
        s = str(r.get("source") or sid)
        if s not in seen:
            seen.append(s)
    return "; ".join(seen) + " (platform computation)"


def _standing_gaps(row, pixel_size_deg, accuracy=None):
    """The gaps every land-cover answer declares, read from the stack it answers
    from: the accuracy the producer states for each year read (or that none is
    declared), the cell size the map actually has. These were two typed sentences —
    '30 m', 'no accuracy' — that stayed true only for the first stack ever registered."""
    accuracy = accuracy if accuracy is not None else {None: declared_accuracy(None, row)}
    metres = round(float(pixel_size_deg or 0) * 111_320)
    cell = f"{metres} m" if metres else "one cell"
    out = []
    stated = {y: a for y, a in accuracy.items() if a}
    if not stated:
        out.append("accuracy of these land-cover maps is not documented: no accuracy figure was "
                   "declared with the layer, so none is cited")
    elif len(set(accuracy.values())) == 1:
        out.append("accuracy as declared by the producer, not assessed for this area: "
                   + next(iter(stated.values())))
    else:
        out.append("accuracy as declared by the producer, not assessed for this area, per year — "
                   + "; ".join(f"{y}: {a or 'none declared'}" for y, a in accuracy.items()))
    out.append(f"a land-cover class is the producer's label for a {cell} pixel at the equator; "
               "features narrower than one cell are not resolved")
    out.extend(STANDING_GAPS)
    return out


def _boundary_phrase(how: str) -> str:
    h = str(how or "")
    if h.startswith("contributed boundary"):
        return f"a contributed polygon — {h.split(': ', 1)[-1]}"
    if h.startswith(("supplied polygon", "drawn")):
        return "the caller's own polygon (no administrative boundary was resolved)"
    if h:
        return f"OpenStreetMap at pack time ({h})"
    return "OpenStreetMap at pack time"


def _boundary_gap(how: str) -> str:
    """One sentence about where the boundary came from — the one actually used."""
    h = str(how or "")
    if h.startswith("contributed boundary"):
        return (f"the boundary is {_boundary_phrase(h)}; the hub's official boundary may differ "
                "from it and from OpenStreetMap's")
    if h.startswith(("supplied polygon", "drawn")):
        return "the boundary is the caller's own polygon; no administrative boundary was resolved"
    return (f"the boundary comes from {_boundary_phrase(h)}; the hub's official boundary may differ")


STANDING_GAPS = (
)


# ---------------------------------------------------------------------- viz

def _viz(aoi, name, layer_key, raster_path, legend_entries, label, caption, metric, outside):
    from ..graph.geo import viz as geoviz
    aoi_feature, bounds = geoviz._aoi_feature(aoi)
    slug = os.path.basename(os.path.dirname(aoi["admin"]))
    return {"place": name, "hazard": layer_key, "layer": None, "layer_kind": "categorical",
            "layer_label": label, "caption": caption, "metric": metric,
            "legend": legend_entries, "bounds": bounds, "aoi": aoi_feature,
            "features": {"type": "FeatureCollection", "features": []},
            "hazard_layer": {"raster_url": f"/api/raster/{slug}/{os.path.basename(raster_path)}",
                             "geojson": None, "crs": "EPSG:4326"},
            "hazard_grid": stacks.category_grid(raster_path, outside, legend_entries)}


# ------------------------------------------------------------------ gather

def _measures_asked(extras) -> bool:
    """Did the call name contributed vectors or raster layers to measure? The seam
    passes the names it was given; only they make an answer without classes worth
    returning."""
    extras = extras or {}
    return bool(extras.get("vectors") or extras.get("layers"))


def _one_class_gap(row, sid, legend):
    label = next(iter(legend.values()), "the class")
    return (f"{row.get('title', sid)} ({sid}) maps one class ({label}) and nothing else: a pixel "
            f"that is {label} in one year and outside the map in the other cannot be told from a "
            "pixel with no data, so loss, gain and transitions are not computed — compare the "
            "per-year areas (net change only). Contribute the map with its background as a class "
            f"(for example 0 = not {label.lower()}, outside_values: []) to get them")


def _area_only(aoi, boundary, how, name, place, why, trace):
    """An answer for the area alone: its boundary as a citation and a gap saying why no
    class is counted; the seam then measures the vectors and layers the caller named
    inside it. Outside every stack the pack used to refuse everything, so a hub's
    polygons in Kenya or Guyana could not be measured by any pack at all."""
    km2 = aoi.get("area_km2")
    gap = f"no land-cover class is counted in {name}: {why.brief}"
    if why.outside:
        near = _stacks_by_distance(boundary, landcover_stacks(), limit=3)
        gap += f". The registered stacks nearest it: {near}" if near else ""
    gaps = [gap, _boundary_gap(how)]
    trace.append("area-only: no classes counted; the seam measures the layers asked")
    # The tables and feeds bound to the pack are read here exactly as beside a class
    # count — by their rows inside this boundary.
    tables, _f, _n = _table_citations(1, _area_countries(aoi), boundary, name, gaps, trace)
    text = (f"The area measured is {name}" + (f", ~{km2:,} km²" if km2 else "")
            + f"; boundary: {_boundary_phrase(how)}. No land-cover class is counted in this "
            "answer; each contributed vector or raster layer named in the request is measured "
            "inside this boundary in a citation of its own"
            + (", and each table or feed bound to this pack that has rows inside it is cited by "
               "those rows." if tables else "."))
    stats = {"queries": None,
             "aoi": {"requested": place, "name": name, "area_km2": km2, "how": how,
                     "is_admin_boundary": how.startswith("admin boundary")},
             "stack": None, "classes_counted": False,
             "_area": ingest.area_of(aoi), "_layer_defaults": []}
    return ([{"n": 1, "kind": "area", "retrieval": "computed-at-pack-time",
              "source": "platform boundary resolution", "title": f"the area measured, {name}",
              "validation": "boundary as resolved at pack time", "text": text}, *tables],
            gaps, stats)


def gather_land_cover_evidence(target: dict, focus: str, trace: list,
                               extras: dict) -> tuple[list, list, dict]:
    from ..config import get_settings
    place = str(target.get("place") or "").strip()
    measures = _measures_asked(extras)
    year = _year(target.get("year")) if target.get("year") not in (None, "") else None
    year_to = _year(target.get("year_to"), "year_to") if target.get("year_to") else None
    if year is None and not measures:
        raise ValueError("pack 'land-cover' needs year: the map year to count classes in (e.g. "
                         "2020). Only a call that measures contributed vectors=[...] or "
                         "layers=[...] may leave it out")
    if year is None and year_to is not None:
        raise ValueError(f"year_to ({year_to}) needs year, the earlier map year to compare it with")
    if year_to is not None and year_to <= year:
        raise ValueError(f"year_to ({year_to}) must come after year ({year})")
    if not place and (extras or {}).get("geometry") is None:
        raise ValueError("pack 'land-cover' needs place (a district, province, park or country) "
                         "or geometry (a GeoJSON polygon or a [w, s, e, n] bbox; place is then its "
                         "label)")
    wanted = _classes(target.get("classes"))

    aoi, boundary, how = _resolve_aoi(place, extras, trace)
    name = aoi.get("name", place)
    try:
        if year is None:
            sid, row, other_stacks, _ = _pick_stack(boundary, wanted=target.get("stack"), name=name)
            raise NoStackHere(
                f"no map year was asked; the stack(s) covering {name}: "
                + "; ".join(f"{r.get('title', s)} ({s}) has {stacks._span(stacks.years(r)) or 'no years'}"
                            for s, r in [(sid, row), *other_stacks])
                + " — pass year= to count their classes")
        sid, row, other_stacks, why = _pick_stack(boundary, wanted=target.get("stack"),
                                                  years=tuple(y for y in (year, year_to) if y),
                                                  name=name)
        try:
            layer = stacks.layer_for(sid, year)
            layer_to = stacks.layer_for(sid, year_to) if year_to else None
        except ValueError as exc:
            raise NoStackHere(f"{row.get('title', sid)}: {exc}") from None
        # A registered year whose file is not on this server declines in words, not a
        # traceback: a second host gets the code and the catalog before (or without) the
        # 14 GB of maps.
        inst = stacks.installed(sid)
        absent = [y for y in (year, year_to) if y and not inst.get(y, False)]
        if absent:
            raise NoStackHere(f"{row.get('title', sid)}: the map for "
                              f"{', '.join(str(y) for y in absent)} is not installed on this server "
                              f"({sum(inst.values())} of {len(inst)} declared years are)")
        legend, outside = _legend(row), _outside(row)
        clip, level = _clip(aoi, layer, trace)
        st = stacks.class_areas(clip, boundary, legend, outside)
        trace.append(f"class_areas[{layer}] {st['pixels_classified']} px classified")
        if st["pixels_classified"] == 0:
            raise NoStackHere(
                f"{name} falls inside the bounding box of {row.get('title', sid)} "
                f"({_box_phrase(_extent_box(row)) if _extent_box(row) else 'extent undeclared'}) but "
                f"every one of its {st['pixels_inside']:,} pixels is outside the mapped footprint — "
                "this product does not cover it, so no class area exists to report",
                outside=True)
    except NoStackHere as exc:
        if not measures:
            raise
        return _area_only(aoi, boundary, how, name, place, exc, trace)

    colors = _colors(row, legend)
    source = f"{row.get('source', sid)} (platform computation)"
    yrow = {y: _year_row(row, ly) for y, ly in ((year, layer), (year_to, layer_to)) if ly}
    hits, unknown = _match_classes(wanted, legend)
    # A one-class map (mangrove, with everything else outside it) shows each year's
    # area; it cannot show loss or gain, and its own values hold one number.
    one_class = len(legend) < 2
    accuracy = {y: declared_accuracy(ly, row) for y, ly in ((year, layer), (year_to, layer_to)) if ly}
    countries = _area_countries(aoi, row)

    citations, gaps = [], _standing_gaps(row, st["pixel_size_deg"], accuracy)
    gaps.append(_boundary_gap(how))
    if other_stacks:
        gaps.append(f"{len(other_stacks)} other registered land-cover stack(s) also cover "
                    f"{name}: " + "; ".join(f"{r.get('title', s)} ({s})" for s, r in other_stacks)
                    + f". This answer reads {row.get('title', sid)} ({sid})"
                    + (f", {why}" if why != "named by the caller" else "")
                    + "; pass stack=<id> to read another.")
    n = 1
    citations.append(_class_area_citation(n, name, year, row, st, level, _sources(sid, yrow[year])))
    legend_entries = {str(k): {"label": v, "color": colors[k]} for k, v in legend.items()}
    headline = next((d for d in sorted(st["by_class"].values(), key=lambda d: -d["km2"])), None)
    viz = _viz(aoi, name, layer, clip, legend_entries,
               f"Land cover {year} ({row.get('title', sid)})",
               f"Classes are the producer's land-cover classes, {_res_phrase(level, st['pixel_size_deg'])}; "
               "rendered from the receipt's recorded evidence pack",
               {"value": round(headline["km2"], 1) if headline else None, "unit": "km2",
                "label": headline["label"] if headline else None,
                "total": round(st["km2_classified"], 1),
                "by_class": {d["label"]: round(d["km2"], 1) for d in st["by_class"].values()}},
               outside)
    change_stats = None
    detail, st_to, ch, level_to = [], None, None, None
    if layer_to:
        clip_to, level_to = _clip(aoi, layer_to, trace)
        st_to = stacks.class_areas(clip_to, boundary, legend, outside)
        if one_class:
            n += 1
            citations.append(_class_area_citation(n, name, year_to, row, st_to, level_to,
                                                  _sources(sid, yrow[year_to])))
            gaps.append(_one_class_gap(row, sid, legend))
            trace.append(f"change[{layer}->{layer_to}] not computed: one-class map")
        else:
            detail = hits or _default_detail_from_areas(st, st_to, legend)
            focus_cls = detail[0] if detail else None
            change_path = None
            if focus_cls is not None:
                change_path = os.path.join(os.path.dirname(aoi["admin"]),
                                           f"landcover_change_{focus_cls}_{year}_{year_to}.tif")
            ch = stacks.change(clip, clip_to, boundary, legend, outside,
                               focus_class=focus_cls, out_path=change_path)
            trace.append(f"change[{layer}->{layer_to}] {ch['pixels_common']} px common")
            n += 1
            citations.append(_class_area_citation(n, name, year_to, row, st_to, level_to,
                                                  _sources(sid, yrow[year_to])))
            both = _sources(sid, yrow[year], yrow[year_to])
            n += 1
            citations.append(_change_citation(n, name, year, year_to, ch, both))
            for cls in detail:
                n += 1
                citations.append(_transition_citation(n, name, year, year_to, cls,
                                                      ch["by_class"][cls], both))
            change_stats = {"km2_common": ch["km2_common"], "km2_changed": ch["km2_changed"],
                            "km2_one_year_only": ch["km2_one_year_only"],
                            "by_class": {d["label"]: {"km2_from": d["km2_a"], "km2_to": d["km2_b"],
                                                      "loss_km2": d["loss_km2"], "gain_km2": d["gain_km2"]}
                                         for d in ch["by_class"].values()}}
            if change_path and os.path.exists(change_path):
                fl = legend[focus_cls]
                fd = ch["by_class"][focus_cls]
                viz = _viz(aoi, name, f"landcover_change_{focus_cls}_{year}_{year_to}", change_path,
                           {"1": {"label": f"stable {fl.lower()}", "color": CHANGE_COLORS[1]},
                            "2": {"label": f"{fl.lower()} loss", "color": CHANGE_COLORS[2]},
                            "3": {"label": f"{fl.lower()} gain", "color": CHANGE_COLORS[3]}},
                           f"{fl} change {year} -> {year_to} ({row.get('title', sid)})",
                           f"Pixels mapped {fl} in both years, in {year} only (loss) or in {year_to} only "
                           f"(gain), on the common classified mask; {_res_phrase(level, st['pixel_size_deg'])}; "
                           "rendered from the receipt's recorded evidence pack",
                           {"value": round(fd["km2_b"] - fd["km2_a"], 1), "unit": "km2",
                            "label": f"net {fl.lower()} change", "total": round(ch["km2_common"], 1),
                            "loss_km2": round(fd["loss_km2"], 1), "gain_km2": round(fd["gain_km2"], 1)},
                           [15])
        # A time series for the classes the caller named, when the span allows one — and
        # for the class of a one-class map, whose areas are the only comparison it has.
        series_cls = hits or (list(legend) if one_class else [])
        if series_cls and (year_to - year >= 2 or one_class):
            years_in = [y for y in stacks.years(row) if year <= y <= year_to and inst.get(y, False)]
            skipped = [y for y in stacks.years(row) if year <= y <= year_to and not inst.get(y, False)]
            if skipped:
                trace.append(f"series: years not installed on this server, skipped: {skipped}")
            per_year = {year: st, year_to: st_to}
            for y in years_in:
                if y in per_year:
                    continue
                ly = stacks.layer_for(sid, y)
                c_y, _ = _clip(aoi, ly, trace)
                per_year[y] = stacks.class_areas(c_y, boundary, legend, outside)
            missing = sorted(int(y) for y in (row.get("missing_years") or {}))
            series_src = _sources(sid, *(_year_row(row, stacks.layer_for(sid, y))
                                         for y in years_in))
            for cls in series_cls:
                pts = [(y, per_year[y]["by_class"][cls]["km2"]) for y in years_in]
                n += 1
                citations.append(_series_citation(n, name, cls, legend[cls], pts, missing,
                                                  level, st["pixel_size_deg"], series_src))
            trace.append(f"series[{','.join(legend[c] for c in series_cls)}] {len(years_in)} years")

    # Fire detections registered on this stack, when the derived file has been built.
    fire_stats = None
    for pid, spec in (row.get("points") or {}).items():
        npz = os.path.join(str(get_settings().cache_dir), "vectors", str(spec.get("derived") or ""))
        if not spec.get("derived") or not os.path.exists(npz):
            gaps.append(f"{spec.get('title', pid)} is registered for this stack but not built on "
                        "this server (python -m app.land_cover.fire build)")
            continue
        from . import fire
        q = fire.query(npz, boundary, legend)
        try:
            passport = json.load(open(npz + ".passport.json"))
        except Exception:
            passport = {}
        n += 1
        citations.append(_fire_citation(n, name, spec, q, passport, source))
        fire_stats = {k: v for k, v in q.items() if k in ("n", "by_year", "by_class", "first", "last")}
        trace.append(f"fire[{pid}] {q.get('n', 0)} detections")

    # What the library holds about this question: a method paper, the national map's
    # memo, a guideline — cited so a brief can lean on more than pixel counts.
    labels = [legend[c] for c in (detail or hits or [])] or list(legend.values())
    corpus, doc_hits = _library_hits(name, focus, labels, trace, gaps, countries=countries)
    for h in doc_hits:
        n += 1
        citations.append(_document_citation(n, h, corpus))
    if doc_hits:
        trace.append(f"documents[{len(doc_hits)}] cited")

    _gaps_before_tables = len(gaps)
    cits, factor_stats, factor_tables = _table_citations(
        n, countries, boundary, name, gaps, trace,
        {"year": year, "year_to": year_to, "legend": legend, "st": st, "st_to": st_to, "ch": ch})
    citations.extend(cits)
    n += len(cits)

    if factor_stats:
        gaps.insert(_gaps_before_tables, (
            "no degradation, canopy or NDVI figures, and no carbon MODELLED inside a class: the "
            "carbon-type figures here are class areas multiplied by contributed per-hectare factors "
            f"({', '.join(sorted(factor_stats))}), stated with their arithmetic — defaults, not "
            "measurements of this site"))
    else:
        gaps.insert(_gaps_before_tables, (
            "no degradation, canopy, biomass, carbon, NDVI or building figures: this pack counts "
            "classes, it does not model inside them; "
            + (f"none of the {factor_tables} factor table(s) bound to it applies to this map's "
               "classes here" if factor_tables else
               "a contributed factor table (class, factor per hectare) bound to this pack would "
               "turn class areas into stocks")))

    for yr, ly, cl, lv in ((year, layer, clip, level),
                           *(((year_to, layer_to, aoi[layer_to], level_to),) if layer_to else ())):
        n += 1
        citations.append(_passport_citation(n, sid, yrow[yr], ly, yr, cl, lv, legend, gaps))
    n += 1
    citations.append(_method_citation(n, outside, bool(layer_to), level, how, one_class=one_class))

    if unknown:
        gaps.append(f"classes not in this stack's legend and not detailed: {', '.join(unknown)} "
                    f"(legend: {', '.join(legend.values())})")
    if st["km2_outside"] > 0:
        gaps.append(f"{_km2(st['km2_outside'])} km² of the boundary is outside the product "
                    "footprint or unclassified and is excluded from every share")
    if level > 1:
        gaps.append(f"figures were read at overview level {level}; thin classes (riverbeds, "
                    "water, built-up strips) move most at coarser cells")
    missing_years = row.get("missing_years") or {}
    if missing_years:
        gaps.append("years with no map: " + "; ".join(f"{y} ({r})" for y, r in missing_years.items()))

    if one_class:
        # Its values are the class area already counted — one value where the class is,
        # none elsewhere — so they are not read again as a layer.
        trace.append("layer defaults: none — a one-class map's values are its class area")
    end_year, end_layer, end_clip, end_level = ((year_to, layer_to, aoi[layer_to], level_to)
                                                if layer_to else (year, layer, clip, level))
    stats = {"queries": [t for t in trace if t.startswith("retrieve ")] or None,
             "aoi": {"requested": place, "name": name, "area_km2": aoi.get("area_km2"),
                     "how": how, "is_admin_boundary": how.startswith("admin boundary")},
             "stack": sid, "year": year, "year_to": year_to, "overview_level": level,
             "classes_km2": {d["label"]: round(d["km2"], 1) for d in st["by_class"].values()},
             "km2_classified": round(st["km2_classified"], 1),
             "km2_outside": round(st["km2_outside"], 1),
             **({"change": change_stats} if change_stats else {}),
             **({"fire": fire_stats} if fire_stats else {}),
             **({"factors": factor_stats} if factor_stats else {}),
             "viz": viz,
             # for the assemble seam's layer-values step: the area this pack read and
             # the map(s) its class areas are counted from
             "_area": ingest.area_of(aoi),
             "_layer_defaults": [] if one_class else [
                 {"layer": ly, "why": f"the {y} map these class areas are counted from"}
                 for y, ly in ((year, layer), (year_to, layer_to)) if ly],
             # for the layer step's cross-tabulation (a measure layer's values by class):
             # the end year's class map as counted here. Reserved: the step pops it.
             "_class_grid": {"path": end_clip, "legend": dict(legend),
                             "outside_values": list(outside), "year": str(end_year),
                             "stack": sid, "label": row.get("title", sid),
                             "layer": end_layer, "overview_level": end_level}}
    return citations, gaps, stats


def _table_citations(n, countries, boundary, name, gaps, trace, counted=None):
    """(citations numbered after `n`, factor stats, factor tables seen) for the
    contributed tables and feeds bound to this pack, each read only where it speaks: a
    FACTOR table (class, factor) is applied to the class areas, losses and gains in
    `counted` ({year, year_to, legend, st, st_to, ch}); any other is cited by its rows
    about this area. An answer that counts no class (`counted` None: the area alone)
    still cites the rows inside it — a Kenya mangrove answer cited none while the soil
    cores bound to the pack sat inside its boundary. A source that is simply about
    somewhere else is left to the trace: a mangrove table has nothing to say to a
    Himalayan valley, and saying so in every answer there is noise, not honesty."""
    out, factor_stats, unplaced, factor_tables = [], {}, [], 0
    legend = (counted or {}).get("legend") or {}
    for ds, spec, res, miss in _bound_tables(countries, boundary, gaps, trace):
        if _is_factor_table(spec):
            factor_tables += 1
            if counted is None:
                trace.append(f"factors[{ds}] not applied: no class area is counted in this answer")
                continue
            if miss:
                if _names_legend_class(res, legend):
                    gaps.append(f"{spec.get('title') or ds} ({ds}) is scoped to {miss[0]}; not "
                                f"applied here — {miss[1]}. Available through feeds_query.")
                continue
            cit, fstats = _factor_citation(n + 1, ds, spec, res, name, counted["year"],
                                           counted["year_to"], legend, counted["st"],
                                           counted["st_to"], counted["ch"], gaps, trace)
            if cit:
                n += 1
                out.append(cit)
                factor_stats[ds] = fstats
        else:
            cit, note, placeable = _context_citation(n + 1, ds, spec, res, name, countries, boundary)
            trace.append(f"table[{ds}] " + (note if cit else f"not cited: {note}"))
            if cit:
                n += 1
                out.append(cit)
            elif not placeable:
                unplaced.append(f"{spec.get('title') or ds} ({ds})")
    if unplaced:
        gaps.append(f"{len(unplaced)} table(s) or feed(s) bound to this pack were not cited: "
                    + "; ".join(unplaced) + " — they have no coordinate or country column and declare "
                    "no country, so no row can be placed in an area. Available through feeds_query.")
    return out, factor_stats, factor_tables


def _area_countries(aoi, row=None):
    """The countries this area is in: the one its boundary resolved to, else the ones
    the stack read here declares (a drawn polygon carries none of its own)."""
    if aoi.get("country"):
        return [str(aoi["country"])]
    return [str(c) for c in ((row or {}).get("countries") or []) if str(c).strip()]


def _speaks_to(declared, countries) -> bool:
    return _rag_filters.speaks_to(declared, countries)


def _library_hits(name, focus, labels, trace, gaps, k=3, countries=()):
    """Documents in this pack's library that speak to the question: a method paper,
    a national map's technical memo, a guideline. Mirrors the risk pack: an empty or
    unreadable library is a declared gap, never a failure. A document that declares
    its countries is cited only for an area in one of them — a Gulf of Nicoya report
    was being cited in Gulf of Guayaquil answers. Returns (corpus, hits)."""
    from ..llm import MissingAPIKey
    from ..rag.store import Corpus, CorpusError
    try:
        corpus = Corpus(PACK_ID)
    except (CorpusError, MissingAPIKey) as exc:
        gaps.append(f"the land-cover document library could not be read ({exc}) — nothing "
                    "published is cited alongside these numbers")
        return None, []
    try:
        n_docs = len(corpus.documents())
    except Exception:
        n_docs = 0
    if n_docs == 0:
        gaps.append("the land-cover document library is empty — no method paper, national map "
                    "memo or guideline is cited alongside these numbers. It accepts "
                    "contributions: a document lands the same way it does for any pack")
        return corpus, []
    queries = [f"{focus} {name}".strip(),
               f"{' '.join(labels[:3])} map method classes accuracy {name}".strip()]
    hits, seen, skipped = [], set(), {}
    for q in queries:
        try:
            # The area's own countries first, so its documents are not crowded out by a
            # better-scored library about somewhere else; then the rest. Land cover
            # opts in to passing over reference lists (a bibliography page outscored
            # every finding on its corpus) as well as documents about other countries
            # (rag.filters), said in `skipped`; sifted again here, whatever the
            # library did, so the rule holds for any corpus.
            got = (corpus.search(q, k=k, countries=list(countries), skipped=skipped,
                                 skip_references=True) if countries else []) \
                + corpus.search(q, k=3 * k, where=list(countries), skipped=skipped,
                                skip_references=True)
        except (CorpusError, MissingAPIKey) as exc:
            gaps.append(f"document retrieval failed ({exc})")
            return corpus, hits
        trace.append(f"retrieve {q!r} -> {len(got)} hits")
        fresh = []
        for h in sorted(got, key=lambda h: -(h.get("score") or 0)):
            if h.get("id") not in seen:
                seen.add(h.get("id"))
                fresh.append(h)
        hits.extend(_rag_filters.sift(fresh, where=countries, skipped=skipped,
                                      skip_references=True)[:k])
    for title in sorted(set(skipped.get("references") or [])):
        trace.append(f"retrieve: skipped a reference-list passage of {title[:50]}")
    elsewhere = {where: set(t) for where, t in (skipped.get("elsewhere") or {}).items()}
    if elsewhere:
        trace.append(f"retrieve: skipped documents about other countries: {sorted(elsewhere)}")
    if not hits and elsewhere:
        gaps.append(f"the land-cover library holds no document about {name} ({', '.join(countries)}) "
                    "above the relevance floor; its closest matches are about other countries and are "
                    "not cited — " + "; ".join(f"{where}: {', '.join(sorted(t)[:3])}"
                                               for where, t in elsewhere.items()))
    elif not hits:
        gaps.append(f"the land-cover library holds {n_docs} document(s), none relevant to "
                    f"{name} above the relevance floor — a decline, not a weak match")
    return corpus, hits[:2 * k]


# The reference-list test is every pack's (rag.filters); the name stays for callers.
_looks_like_references = _rag_filters.looks_like_references


def _document_citation(n, h, corpus):
    from ..contrib import staging
    m = h.get("metadata") or {}
    return {"n": n, "kind": "document", "retrieval": "archived-document",
            "source": m.get("source"), "title": m.get("title"),
            "pub_date": m.get("pub_date"), "validation": m.get("validation"),
            "temporal": m.get("temporal"), "url": m.get("url"), "score": h.get("score"),
            "doc_id": h.get("doc_id"), "chunk_id": h.get("id"),
            **({"countries": m["countries"]} if m.get("countries") else {}),
            **({"review_status": staging.cited_review(m)} if staging.cited_review(m) else {}),
            "archived_copy": (f"/api/rag/{PACK_ID}/document/{h.get('doc_id')}"
                              if corpus and h.get("doc_id") and corpus.raw_path(h["doc_id"]) else None),
            "usage_notes": m.get("usage_notes"),
            **({"staged_by": m["staged_by"], "contribution_id": m.get("contribution_id")}
               if m.get("staged_by") else {}),
            "text": (h.get("text") or "") + (f" Review: {staging.cited_review(m)}."
                                             if staging.cited_review(m) else "")}


def _scope_miss(spec, countries, boundary):
    """None when a table or feed may be read for this area, else (where it is scoped,
    why this area is not in it). One that declares countries or a bbox [w, s, e, n]
    is read only for an area in one of those countries and inside that box; one that
    declares neither is global. The rule is every pack's (mcp.feeds.scope_miss)."""
    from ..mcp import feeds
    return feeds.scope_miss(spec, countries, boundary.bounds if boundary is not None else None)


TABLE_ROWS = 100_000      # rows read per bound table or feed; more is said, never hidden


def _is_factor_table(spec) -> bool:
    return {"class", "factor"} <= set((spec.get("fetch") or {}).get("columns") or {})


def _bound_tables(countries, boundary, gaps, trace):
    """Every contributed table and feed (and staged one the caller may see) bound to
    this pack, with its rows and, for one scoped elsewhere, (where, why) — the rule
    every pack applies: a source that declares its countries or its box is read only
    where it speaks. A Gulf of Nicoya soil-carbon factor was being multiplied into
    Ecuador and Brazil. A factor table scoped elsewhere is still read, so the caller
    can say so where its classes are this legend's."""
    from ..contrib import staging
    from ..mcp import feeds, registry
    rows = {k: v for k, v in registry.FEEDS.items()
            if v.get("pack") == PACK_ID and v.get("status") == "available"}
    try:
        rows.update({k: v for k, v in staging.visible_staged_feeds_for_pack(PACK_ID).items()
                     if k not in rows})
    except Exception:
        pass
    out = []
    for ds in sorted(rows):
        spec = rows[ds]
        miss = _scope_miss(spec, countries, boundary)
        if miss:
            trace.append(f"table[{ds}] scoped to {miss[0]}; not read here: {miss[1]}")
            if not _is_factor_table(spec):
                continue
        res = feeds.query(ds, {"limit": TABLE_ROWS})
        if res.get("status") != "ok":
            if not miss:
                gaps.append(f"table {ds} bound to this pack did not answer: {res.get('note', 'no reason given')}")
            continue
        if not miss:
            trace.append(f"table[{ds}] {len(res.get('records') or [])} rows read")
        out.append((ds, spec, res, miss))
    return out


def _names_legend_class(res, legend) -> bool:
    """Does a factor table name a class of this legend, or one plausibly the same
    (a shared word: 'Mangrove' and 'Mangrove forest')?"""
    labels = [_words(v) for v in legend.values()]
    folded = {_fold(v) for v in legend.values()}
    for rec in res.get("records") or []:
        for a in str(rec.get("class") or "").split("|"):
            if _fold(a) in folded or any(_words(a) & w for w in labels):
                return True
    return False


def _norm_label(text) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text or ""))
    return " ".join("".join(ch for ch in t if not unicodedata.combining(ch)).casefold().split())


def _singular(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _fold(text) -> str:
    """One class name however it is written: case, accents, punctuation and simple
    plurals folded ('Mangroves' is 'mangrove'). Whole names only — 'Non-mangrove'
    stays 'non mangrove', never 'mangrove'."""
    return " ".join(_singular(w) for w in re.sub(r"[^0-9a-z]+", " ", _norm_label(text)).split())


_GENERIC_WORDS = {"and", "or", "of", "the", "de", "del", "la", "el", "y", "e", "non", "not", "no",
                  "other", "others", "area", "cover", "land", "class", "with", "without"}


def _words(text) -> set:
    """The words that make a class name what it is — none for a negated one ('Non-
    mangrove' is never plausibly 'Mangrove')."""
    words = _fold(text).split()
    if words and words[0] in ("non", "not", "no", "sin", "without"):
        return set()
    return {w for w in words if w not in _GENERIC_WORDS and not w.isdigit()}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _fmt_t(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}"


_ALT_COLUMNS = ("climate", "region", "zone", "scope", "ecoregion", "basis")


def _distinguishing(recs):
    """The column that tells rows of the same class and pool apart, preferring the
    ones that name where a factor applies ('climate', 'region', 'scope', 'basis')."""
    keys = [k for k in recs[0] if k not in ("class", "factor", "low", "high", "unit", "pool")]
    differ = [k for k in keys if len({str(r.get(k) or "").strip() for r in recs}) > 1]
    return next((k for k in _ALT_COLUMNS if k in differ), differ[0] if differ else None)


def _factor_citation(n, ds, spec, res, name, year, year_to, legend, st, st_to, ch, gaps, trace):
    """One citation applying a factor table to the class areas the pack counted —
    every product stated in the text, the arithmetic visible, the caveat in the
    same sentence. A row's class may list aliases ('Mangrove|Mangroves|Mangrove
    forest|Manglar'), matched as whole names. Rows of the same class and pool are
    ALTERNATIVES (tropical wet vs dry), stated side by side, never as one answer.
    Returns (citation or None, stats)."""
    cols = (spec.get("fetch") or {}).get("columns") or {}
    if "class" not in cols or "factor" not in cols:
        return None, None
    by_label = {}
    for k, v in legend.items():
        by_label.setdefault(_fold(v), []).append(k)
    matched, unmatched = [], []
    for rec in res.get("records") or []:
        f = _num(rec.get("factor"))
        aliases = [a.strip() for a in str(rec.get("class") or "").split("|") if a.strip()]
        keys = sorted({k for a in aliases for k in by_label.get(_fold(a), [])})
        if not keys or f is None:
            unmatched.append(aliases[0] if aliases else str(rec.get("class")))
            continue
        matched.extend((k, f, rec) for k in keys)
    if not matched:
        # Only a legend that plausibly names the same thing earns the gap; a table of
        # mangrove factors says nothing to a Himalayan legend of snow and rock.
        near = sorted({lab for lab in legend.values()
                       if any(_words(lab) & _words(u) for u in set(unmatched))})
        if near:
            gaps.append(f"{spec.get('title') or ds} ({ds}) names classes "
                        f"{', '.join(sorted(set(unmatched))[:6]) or 'none'}; none is a class of this "
                        f"stack's legend, though {', '.join(near[:4])} may be the same thing — the table "
                        "was not applied. A factor row's class may list aliases separated by '|' "
                        f"(for example '{sorted(set(unmatched))[0]}|{near[0]}')")
        trace.append(f"factors[{ds}] no class of this legend")
        return None, None
    units = (spec.get("fetch") or {}).get("units") or "per hectare"
    groups = {}
    for cls, f, rec in matched:
        groups.setdefault((cls, str(rec.get("pool") or "").strip()), []).append((f, rec))
    parts, stats = [], {}
    for (cls, pool), rows in groups.items():
        label = legend[cls]
        by = _distinguishing([r for _, r in rows]) if len(rows) > 1 else None
        lines = []
        for f, rec in rows:
            unit = str(rec.get("unit") or units)
            basis = str(rec.get("basis") or "").strip()
            alt = str(rec.get(by) or "").strip() if by else ""
            ha = float(st["by_class"][cls]["ha"])
            value = ha * f
            note = [p for p in ((pool if not by else ""), (basis if by != "basis" else "")) if p]
            line = ((f"[{by}: {alt}] " if by else "")
                    + f"{label} {year}: {ha:,.1f} ha × {f:g} {unit}"
                    + (f" ({'; '.join(note)})" if note else "")
                    + f" = {_fmt_t(value)} {unit.split('/')[0].strip() or 'units'}")
            entry = {"class": label, "ha": round(ha, 1), "factor": f, "unit": unit,
                     **({"pool": pool} if pool else {}), **({"basis": basis} if basis else {}),
                     **({"alternatives_by": by, by: alt} if by else {}),
                     "value": round(value, 1)}
            lo, hi = _num(rec.get("low")), _num(rec.get("high"))
            if lo is not None and hi is not None:
                line += (f" (with the table's low-high range for this factor, {lo:g}-{hi:g}: "
                         f"{_fmt_t(ha * lo)} to {_fmt_t(ha * hi)})")
                entry["value_low"], entry["value_high"] = round(ha * lo, 1), round(ha * hi, 1)
            if st_to is not None and year_to:
                ha_to = float(st_to["by_class"][cls]["ha"])
                line += f"; {year_to}: {ha_to:,.1f} ha × {f:g} = {_fmt_t(ha_to * f)}"
                entry.update({"ha_to": round(ha_to, 1), "value_to": round(ha_to * f, 1)})
            if ch is not None and year_to:
                # Loss and gain exist only where change was computed — never on a
                # one-class map, where both read as zero beside a falling series.
                cd = ch["by_class"][cls]
                loss_ha, gain_ha = cd["loss_km2"] * 100.0, cd["gain_km2"] * 100.0
                line += (f"; {year}->{year_to} loss {loss_ha:,.1f} ha × {f:g} = {_fmt_t(loss_ha * f)} in lost extent, "
                         f"gain {gain_ha:,.1f} ha × {f:g} = {_fmt_t(gain_ha * f)} in gained extent, "
                         f"net {_fmt_t((gain_ha - loss_ha) * f)}")
                entry.update({"loss_ha": round(loss_ha, 1), "gain_ha": round(gain_ha, 1),
                              "value_lost": round(loss_ha * f, 1), "value_gained": round(gain_ha * f, 1)})
            lines.append(line)
            stats.setdefault(label, []).append(entry)
        if by:
            parts.append(f"{label} ({pool or 'the same pool'}) — the table gives one row per {by}; use "
                         "the one that applies, never their sum: " + " | ".join(lines))
        else:
            parts.extend(lines)
    pools = sorted({p["pool"] for v in stats.values() for p in v if p.get("pool")})
    skipped = sorted(set(unmatched))
    text = (f"Applying {spec.get('title') or ds} ({ds}; {spec.get('source') or 'source unstated'}, "
            f"vintage {spec.get('vintage') or 'unstated'}, validation {spec.get('validation') or 'unstated'}, "
            f"licence {spec.get('license') or 'unstated'}) to the class areas counted in {name}: "
            + "; ".join(parts) + ". "
            "Each figure is area × a per-hectare factor, not a measurement of this site: it holds only "
            "where the mapped class and the factor's domain match"
            + (", and a change figure is the stock associated with lost or gained extent, not an "
               "emission estimate." if ch is not None and year_to else ".")
            + (f" Pools stated: {', '.join(pools)}." if pools else "")
            + (f" {len(skipped)} row(s) named classes not in this legend and were skipped." if skipped else "")
            + (f" Contributor's note: {spec['usage_notes']}" if spec.get("usage_notes") else "")
            + (f" Review: {spec['review']}." if spec.get("review") else ""))
    cit = {"n": n, "kind": "class_factor", "retrieval": "computed-at-pack-time",
           "source": spec.get("source") or "contributed table (source unstated)",
           "title": f"{spec.get('title') or ds} applied to {name}",
           "pub_date": spec.get("vintage"),
           "validation": f"contributed-table ({spec.get('validation') or 'unstated'}), deterministic arithmetic",
           "dataset": ds, "values_in": f"stats.factors.{ds}", "text": text}
    if spec.get("usage_notes"):
        cit["usage_notes"] = spec["usage_notes"]
    if spec.get("review"):
        cit["review_status"] = spec["review"]
    if spec.get("staged_by"):
        cit["staged_by"] = spec["staged_by"]
        cit["contribution_id"] = spec.get("contribution_id")
    trace.append(f"factors[{ds}] {len(matched)} rows applied")
    return cit, stats


_LAT_KEYS = ("lat", "latitude", "decimallatitude")
_LON_KEYS = ("lon", "lng", "long", "longitude", "decimallongitude")
_COUNTRY_KEYS = ("country", "country_name", "countryname", "pais", "país", "nation")


def _key_of(recs, names):
    keys = {str(k).lower(): k for r in recs[:50] for k in r}
    return next((keys[n] for n in names if n in keys), None)


def _fmt_v(v: float) -> str:
    if v == int(v) and abs(v) < 1e12:
        return f"{int(v):,}" if abs(v) >= 10_000 else str(int(v))      # a year stays 2014
    return f"{v:,.1f}" if abs(v) >= 10_000 else f"{v:.1f}" if abs(v) >= 100 else f"{v:.4g}"


def _is_identifier(key) -> bool:
    k = str(key).lower()
    return k in ("id", "key", "code", "fid", "objectid") or k.endswith(("_id", "_key", "_code"))


def _true_count(res) -> int:
    """The source's own row count: the adapter's summary states it ("16,715 rows from
    ..."); the records are only what was read."""
    m = re.match(r"\s*([\d,]+)\s+(?:rows|records)\b", str(res.get("summary") or ""))
    read = len(res.get("records") or [])
    return max(read, int(m.group(1).replace(",", ""))) if m else read


def _context_citation(n, ds, spec, res, name, countries, boundary):
    """(citation, note, placeable) for the rows of a table or feed that are about this
    area — by their coordinates, by a country column, or all of them when the source
    declares this area's country — stated as a count and a summary per column; (None,
    why, placeable) when no row is about it or none can be placed at all. It used to
    quote the first three rows of whatever was bound, so a Gulf of Nicoya answer cited
    a UK salt-marsh core."""
    from shapely.geometry import Point
    from shapely.prepared import prep
    recs = res.get("records") or []
    total = _true_count(res)
    lat_k, lon_k = _key_of(recs, _LAT_KEYS), _key_of(recs, _LON_KEYS)
    cty_k = _key_of(recs, _COUNTRY_KEYS)
    declared = [str(c) for c in (spec.get("countries") or []) if str(c).strip()]
    if lat_k and lon_k:
        inside_area = prep(boundary)
        rows = [r for r in recs if _num(r.get(lat_k)) is not None and _num(r.get(lon_k)) is not None
                and inside_area.intersects(Point(_num(r.get(lon_k)), _num(r.get(lat_k))))]
        how, placed_by = f"their {lat_k}/{lon_k} fall inside the boundary", f"its {lat_k}/{lon_k}"
    elif cty_k and countries:
        rows = [r for r in recs if str(r.get(cty_k) or "").strip()
                and _speaks_to([str(r[cty_k])], countries)]
        how, placed_by = f"their {cty_k} is {', '.join(countries)}", f"its {cty_k} column"
    elif declared and countries and _speaks_to(declared, countries):
        rows = list(recs)
        how, placed_by = f"the source declares {', '.join(declared)}, where this area is", "its countries"
    else:
        return None, "it has no coordinate or country column and declares no country", False
    if not rows:
        return None, f"none of its {total:,} row(s) is about {name} (placed by {placed_by})", True
    # A summary per column: min/mean/max of a numeric one, the counts of a text one
    # whose values repeat. Identifiers and one-off values (dates, names) say nothing.
    nums, texts = [], []
    for k in [k for k in rows[0] if k not in (lat_k, lon_k)]:
        vals = [r.get(k) for r in rows if r.get(k) not in (None, "", "NA")]
        fl = [_num(v) for v in vals]
        if vals and all(v is not None for v in fl):
            if _is_identifier(k):
                continue
            nums.append(f"{k} min {_fmt_v(min(fl))}, mean {_fmt_v(sum(fl) / len(fl))}, "
                        f"max {_fmt_v(max(fl))}" + (f" ({len(fl):,} with a value)" if len(fl) < len(rows) else ""))
        elif vals:
            counts = {}
            for v in vals:
                counts[str(v)[:60]] = counts.get(str(v)[:60], 0) + 1
            if len(counts) <= 8 and (len(counts) < len(vals) or len(vals) == 1) and len(texts) < 6:
                texts.append(f"{k}: " + ", ".join(f"{v} ({c:,})" for v, c in
                                                   sorted(counts.items(), key=lambda kv: -kv[1])))
    is_feed = not (spec.get("fetch") or {}).get("path")      # a landed table names its copy
    kind = "feed" if is_feed else "table"
    read = len(recs)
    text = (f"{spec.get('title') or ds} ({ds}; a {'live ' if is_feed else ''}{kind}, "
            f"{spec.get('source') or 'source unstated'}, vintage {spec.get('vintage') or 'unstated'}, "
            f"validation {spec.get('validation') or 'unstated'}): {len(rows):,} of its {total:,} row(s) "
            f"are about {name} — {how}."
            + (f" Only the last {read:,} of the {total:,} rows were read; the figures are over those."
               if read < total else "")
            + (" Over those rows: " + "; ".join(nums) + "." if nums else "")
            + (" Values: " + "; ".join(texts) + "." if texts else "")
            + (f" Contributor's note: {spec['usage_notes']}" if spec.get("usage_notes") else "")
            + (f" Review: {spec['review']}." if spec.get("review") else ""))
    pp = res.get("passport") or {}
    contributed = "contributed-" if spec.get("contributed") or spec.get("staged_by") else ""
    cit = {"n": n, "kind": kind,
           "retrieval": "pulled-at-pack-time" if is_feed else "computed-at-pack-time",
           "source": spec.get("source") or f"contributed {kind} (source unstated)",
           "title": spec.get("title") or ds,
           "pub_date": (res.get("as_of") if is_feed else None) or spec.get("vintage"),
           "validation": f"{contributed}{kind} ({spec.get('validation') or 'unstated'})",
           "dataset": ds, "rows_about_area": len(rows), "rows_total": total, "text": text}
    if is_feed and pp.get("url"):
        cit["url"] = pp["url"]
    if is_feed and pp.get("stale_data"):
        cit["stale_data"] = pp["stale_data"]
    if spec.get("usage_notes"):
        cit["usage_notes"] = spec["usage_notes"]
    if spec.get("review"):
        cit["review_status"] = spec["review"]
    if spec.get("staged_by"):
        cit["staged_by"] = spec["staged_by"]
        cit["contribution_id"] = spec.get("contribution_id")
    return cit, f"{len(rows)} of {total} rows about the area", True


def _default_detail_from_areas(st_a, st_b, legend):
    """Which classes get a transition citation when none were asked for: Forest when
    it exists, plus the two other classes whose area moved most."""
    forest = next((k for k, v in legend.items() if v.lower() == "forest"), None)
    moved = sorted(((abs(st_b["by_class"][k]["km2"] - st_a["by_class"][k]["km2"]), k)
                    for k in legend if k != forest
                    and (st_a["by_class"][k]["pixels"] or st_b["by_class"][k]["pixels"])),
                   reverse=True)
    return ([forest] if forest is not None else []) + [k for _, k in moved[:2]]


# ---------------------------------------------------------------- manifest

def manifest() -> dict:
    """What the land-cover pack ships and what it does not — real state where it
    can be read, declared gaps in one place."""
    from ..config import get_settings
    from ..mcp.packs import corpus_state
    sources, points = {}, {}
    for sid, row in landcover_stacks().items():
        inst = stacks.installed(sid)
        sources[sid] = {
            "title": row.get("title"), "source": row.get("source"), "url": row.get("url"),
            "licence": row.get("license"), "authority": row.get("authority"),
            "accuracy": row.get("accuracy") or "unstated",
            "legend": _legend(row), "extent": row.get("extent"),
            "years_available": [y for y, ok in inst.items() if ok],
            "years_not_installed": [y for y, ok in inst.items() if not ok],
            "missing_years": row.get("missing_years") or {},
            "residency": "platform-hosted copy, read in place",
        }
        for pid, spec in (row.get("points") or {}).items():
            npz = os.path.join(str(get_settings().cache_dir), "vectors", str(spec.get("derived") or ""))
            built = os.path.exists(npz)
            entry = {"stack": sid, "title": spec.get("title"), "source": spec.get("source"),
                     "status": "available" if built else "registered, not built"}
            if built:
                try:
                    pp = json.load(open(npz + ".passport.json"))
                    entry.update(records=pp.get("records"), date_range=pp.get("date_range"),
                                 built_at=pp.get("built_at"))
                except Exception:
                    pass
            points[pid] = entry
    return {
        "id": PACK_ID, "display_name": "Land Cover Platform", "version": "v0",
        "profile": "v0 — class areas for a year; change and transitions between two years; a "
                   "per-class series across the years; fire detections by class; a map embed",
        "built_for": "how much of an area is each land-cover class, how that changed "
                     "between mapped years, and what burned — replayable, with the mask, the "
                     "resolution read and the boundary's source declared",
        "output_contract": {"required_sections": list(SECTIONS),
                            "gate": "verify_groundedness — same blocking rules as every pack",
                            "receipt": "record_receipt / publish_answer — pack carries the map viz"},
        "sources": {"stacks": sources, "points": points, "corpus": corpus_state(PACK_ID)},
        "target": {"place": "district, province, park or country inside a stack's footprint "
                            f"(up to {AREA_CAP_KM2:,.0f} km²; or pass geometry and use place as its label)",
                   "year": "a mapped year — may be left out when the call only measures vectors=[...] "
                           "or layers=[...]; where no stack covers the area such a call answers for "
                           "the area alone",
                   "year_to": "optional later year, for change",
                   "classes": "optional class names to detail, e.g. [\"Forest\"] — with year_to, "
                              "also a per-year series"},
        "gaps": [
            "accuracy is stated per stack where its producer declared one (see each stack's "
            "accuracy above); a stack that declares none is said to have none in every answer",
            "a class is the producer's label for one cell of the stack's own resolution; features "
            "narrower than one cell are not resolved",
            "the boundary comes from OpenStreetMap at pack time, a contributed polygon layer, or "
            "the caller's polygon — every answer says which",
            "carbon and other per-hectare figures come only from contributed factor tables bound "
            "to this pack (class x factor), never from a model inside a class",
            "a Southeast Asia land-cover layer is listed in the raster inventory but not "
            "registered as a stack",
        ],
        "compositions": {"landcover.brief": "available via compose_run — the platform LLM "
                                            "parses the question, drafts and gates server-side; "
                                            "or run assemble_pack -> publish_answer with your own model"},
    }


# -------------------------------------------------------- the composition

_PARSE_SYSTEM = (
    "You extract the target of a land-cover question for a counting service. Call "
    "set_landcover_target with the place (a district, province, park or country), the year(s) "
    "and the land-cover classes it names (for example Forest, Cropland, Grassland, Built-up "
    "area). A single map year goes in `year`. When the question compares two dates or asks "
    "about change since a year, put the EARLIER year in `year` and the LATER year in `year_to` "
    "whatever order the question names them ('forest in 2022, and how it changed since 2000' is "
    "year 2000, year_to 2022). Do not answer the question. If it is not about land cover in a "
    "place, reply in words without calling the tool.")

_PARSE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "set_landcover_target",
        "description": "Record the place, year(s) and classes the question asks about.",
        "parameters": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "named area"},
                "year": {"type": "integer", "description": "the only year, or the EARLIER of two"},
                "year_to": {"type": "integer", "description": "the LATER year, for change"},
                "classes": {"type": "array", "items": {"type": "string"}},
                "focus": {"type": "string"},
            },
            "required": ["place", "year"],
        },
    },
}]

_SYNTH_SYSTEM_TMPL = """You write land-cover briefs for planners, foresters and hub analysts.

Non-negotiable rules:
- Use ONLY the numbered evidence provided. Every paragraph must carry at least one citation marker like [3]. Never use a citation number that is not in the evidence list.
- Never state a number that does not appear in the evidence text, and quote each with its year.
- The figures are pixel counts of a mapped product, not measurements or model predictions — attribute them to the named stack and method, never to yourself. If the evidence states the producer's declared accuracy, quote it as declared (not assessed for this area); if it states none, say so in the what's-missing section and cite the declared-gaps entry.
- Change between two years is on the common classified mask and includes classifier disagreement; say so in the reading section. Loss is not degradation; gain is not planting.
- If the evidence cannot support an answer, write no sections; reply with one paragraph starting "DECLINE:" naming exactly what is missing.

Write exactly these sections, in this order, with these exact headers:
{sections}
"""


def synthesize(question: str, provider: str | None = None, model: str | None = None) -> dict:
    """The accompanied pipeline for one land-cover question: LLM parse -> deterministic
    gather -> platform-LLM draft -> groundedness gate (retry x1). Same shape as the
    risk composition, so compose.run persists it generically."""
    import json as _json

    from ..config import get_settings
    from ..food_security import synthesis as fs
    from ..llm import build_client, default_model
    from ..mcp import packs as mcp_packs

    settings = get_settings()
    provider = provider or settings.default_provider
    model = model or default_model(provider)
    client = build_client(provider)
    trace, usage = [f'question: "{question}"'], []
    base = {"provider": provider, "model": model,
            "required_sections": list(SECTIONS), "pack": PACK_ID}

    resp = client.chat.completions.create(
        model=model, max_tokens=300, tools=_PARSE_TOOLS,
        messages=[{"role": "system", "content": _PARSE_SYSTEM},
                  {"role": "user", "content": question}])
    usage.append(fs._usage(resp))
    msg = resp.choices[0].message
    if not msg.tool_calls:
        trace.append("parse -> out of scope (no tool call)")
        return fs._declined(msg.content or "This question is outside the land-cover "
                            "brief's scope.", trace=trace, usage=usage) | base
    try:
        args = _json.loads(msg.tool_calls[0].function.arguments)
        if not isinstance(args, dict):
            raise ValueError("tool arguments are not an object")
    except (ValueError, TypeError) as exc:
        trace.append(f"parse -> malformed tool arguments ({exc})")
        return fs._declined("The question could not be parsed reliably — please "
                            "rephrase it.", trace=trace, usage=usage) | base
    target = {"place": str(args.get("place") or "").strip(), "year": args.get("year")}
    if args.get("year_to"):
        target["year_to"] = args["year_to"]
        # "in 2022 ... since 2000": the model may put the first-named year in `year`. The
        # pack wants the earlier year first; reorder here and say so in the trace.
        if isinstance(target["year"], int) and isinstance(target["year_to"], int) \
                and target["year_to"] < target["year"]:
            target["year"], target["year_to"] = target["year_to"], target["year"]
            trace.append("parse -> years reordered, earlier first")
    if args.get("classes"):
        target["classes"] = list(args["classes"])
    focus = (args.get("focus") or question).strip()
    trace.append(f"parse -> {target}  [the model extracts the target; it fetches nothing]")

    try:
        citations, gaps, stats = gather_land_cover_evidence(target, focus, trace, {})
    except ValueError as exc:
        return fs._declined(str(exc), trace=trace, usage=usage) | base
    except (OSError, RuntimeError) as exc:
        return fs._declined(
            f"evidence gathering failed: {type(exc).__name__}: {exc}. This is an "
            "upstream/infrastructure failure, not a coverage gap — retrying later "
            "may succeed.", trace=trace, usage=usage) | base
    citations = [*citations, mcp_packs.gaps_citation(citations, gaps)]

    system = _SYNTH_SYSTEM_TMPL.format(sections="\n".join(SECTIONS))
    user_msg = (f"Question: {question}\nParsed target: {target}\n\n"
                "Numbered evidence (the ONLY permissible sources):\n\n"
                + fs._render_pack(citations))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user_msg}]
    check = None
    for attempt in (1, 2):
        resp = client.chat.completions.create(model=model, max_tokens=3000, messages=messages)
        usage.append(fs._usage(resp))
        draft = (resp.choices[0].message.content or "").strip()
        if getattr(resp.choices[0], "finish_reason", None) == "length":
            trace.append(f"synthesis attempt {attempt} -> TRUNCATED at max_tokens; "
                         "the draft is incomplete, not ungrounded")
        if draft.startswith("DECLINE:"):
            trace.append(f"synthesis attempt {attempt} -> model declined")
            return fs._declined(draft, trace=trace, usage=usage,
                                citations=citations, stats=stats) | base
        check = fs.check_grounded(draft, citations, sections=SECTIONS)
        check["attempts"] = attempt
        trace.append(f"groundedness attempt {attempt} -> "
                     + ("PASS" if check["passed"]
                        else "FAIL: " + "; ".join(check["failures"])))
        if check["passed"]:
            brief = draft + "\n\n## Sources\n" + fs._sources_md(citations)
            return {"declined": False, "brief": brief, "citations": citations,
                    "parsed": target, "target": target, "evidence": stats,
                    "gaps": gaps, "grounded": check, "trace": trace,
                    "usage": usage} | base
        messages += [{"role": "assistant", "content": draft},
                     {"role": "user", "content":
                      "Your draft failed the groundedness check — "
                      + "; ".join(check["failures"])
                      + ". Rewrite following the rules exactly."}]
    return fs._declined(
        "The draft could not be grounded in the evidence after a retry — refusing "
        "to ship an uncited brief.", trace=trace, usage=usage, citations=citations,
        check=check, stats=stats) | base
