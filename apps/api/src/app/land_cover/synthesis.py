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
import os

from shapely.geometry import shape

from ..graph.geo import ingest, rasterstats, schema, stacks, tiffs, verify

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
    """{stack_id: row} for every registered stack with role landcover."""
    return {k: v for k, v in stacks.rows().items() if v.get("role") == ROLE}


def _pick_stack(boundary, wanted=None, years=()):
    """(stack_id, row, others): the registered land-cover stack to read for this
    area, and every OTHER registered stack that also covers it. Unless the caller
    names one (`stack`), the first covering stack that HAS the requested years is
    read; the others are declared as a gap, never silently ignored. It used to
    return the first match and say nothing: a hub's national 10 m stack over Nepal
    would have sat unread beside the 30 m regional one, with no answer ever
    mentioning it — and a 1996 question declined because the first stack over the
    Gulf of Nicoya held only 2014 while the second held 1996."""
    rows = landcover_stacks()
    if not rows:
        raise ValueError("no land-cover stack is registered on this platform — a hub "
                         "contributes one as a `kind: stack` catalog row")
    minx, miny, maxx, maxy = boundary.bounds
    covering = []
    for sid, row in rows.items():
        ext = row.get("extent") or {}
        lon, lat = ext.get("lon"), ext.get("lat")
        if not lon or not lat:           # extent undeclared: cannot rule it out
            covering.append((sid, row))
        elif maxx >= lon[0] and minx <= lon[1] and maxy >= lat[0] and miny <= lat[1]:
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
        return w, rows[w], others
    if not covering:
        sid, row = next(iter(rows.items()))
        ext = row.get("extent") or {}
        raise ValueError(
            f"this area lies outside every registered land-cover stack — {row.get('title', sid)} "
            f"covers lon {ext.get('lon')} and lat {ext.get('lat')}; nothing can be counted there")
    want = [int(y) for y in years if y]
    able = [(sid, row) for sid, row in covering
            if all(y in stacks.years(row) for y in want)] if want else covering
    if not able and len(covering) == 1:
        # One stack, missing year: let layer_for say WHY it is missing ("no map
        # published by the producer") rather than a bare list of years.
        able = covering
    if not able:
        raise ValueError(
            f"no registered land-cover stack covering this area has {', '.join(map(str, want))} — "
            + "; ".join(f"{row.get('title', sid)} ({sid}) has {stacks._span(stacks.years(row)) or 'no years'}"
                        for sid, row in covering))
    sid, row = able[0]
    return sid, row, [(s, r) for s, r in covering if s != sid]


_BOUNDARY_OK = ("admin boundary", "protected-area boundary", "contributed boundary",
                "supplied polygon", "drawn")


def _resolve_aoi(place, extras, trace):
    geometry = extras.get("geometry")
    if geometry is not None:
        kw = {"how": extras["within_how"]} if extras.get("within_how") else {}
        aoi = ingest.ensure_aoi(geometry=geometry, name=place or None, layers=(),
                                area_cap_km2=AREA_CAP_KM2, **kw)
    else:
        aoi = ingest.ensure_aoi(place=place, layers=(), area_cap_km2=AREA_CAP_KM2)
    how = str(aoi.get("how") or "")
    if not how.startswith(_BOUNDARY_OK):
        from ..graph.geo import vectors as _vectors
        try:
            named = _vectors.names(limit=6)
        except Exception:
            named = {}
        hint = ""
        if named:
            sample = "; ".join(f"{layer}: {', '.join(n[:4])}" for layer, n in named.items())
            hint = f", or name a contributed area ({sample})"
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
    given = {int(k): str(v) for k, v in (row.get("colors") or {}).items()}
    return {k: given.get(k) or DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
            for i, k in enumerate(sorted(legend))}


def _outside(row):
    vals = row.get("outside_values")
    return [0] if vals is None else [int(v) for v in vals]      # [] = the whole grid is mapped


def _match_classes(wanted, legend):
    """Class names a caller asked about, matched to the legend; unknown names declared."""
    by_name = {v.lower(): k for k, v in legend.items()}
    hits, unknown = [], []
    for w in wanted:
        k = by_name.get(w.lower()) or next((k for k, v in legend.items()
                                            if w.lower() in v.lower()), None)
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
            "the boundary. Area per pixel is its spherical area at its own latitude, summed; "
            "counts are never resampled.")
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
    contract, obs, hdr, notes = None, None, None, []
    try:
        contract = schema.schema_for(layer)
    except Exception as exc:
        notes.append(f"declared contract unreadable ({type(exc).__name__})")
    try:
        obs = rasterstats.windowed_stats(clip, keep_values=tuple(legend))
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
                    "(sampled).")
    if hdr:
        b = hdr["bounds"]
        bits.append(f"Grid: {hdr['width']}×{hdr['height']} px, lon {b[0]}..{b[2]}, "
                    f"lat {b[1]}..{b[3]}; overview levels {hdr['overviews'] or 'none'}.")
    bits.append(f"Read here at {'full resolution' if level <= 1 else f'overview level {level}'}.")
    acc = str(row.get("accuracy") or "").strip()
    if acc and acc.lower() not in ("unstated", "unknown", "none", "n/a"):
        bits.append(f"Accuracy, as declared by the producer (not assessed for this area): {acc}.")
    else:
        bits.append("Accuracy: not documented on this platform — no accuracy figure is cited.")
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
            "authority": row.get("authority"), "residency": "platform-hosted",
            "text": " ".join(bits)}


def _method_citation(n, outside_values, two_years, level, how=""):
    text = (
        "Land-cover figures are pixel counts. Window: the boundary's bounding box in the "
        "year's own grid, intersected with the dataset"
        + (" and read at full resolution. " if level <= 1 else
           f", read through the file's overviews at level {level} because the window exceeds the "
           f"pack's {PIXEL_BUDGET:,}-pixel full-resolution budget; the level is stated on every "
           "figure. ")
        + "Membership: a pixel counts when its centre falls inside the boundary. Area: each "
        "pixel's own spherical area at its latitude (authalic radius 6371.0072 km; the "
        "WGS84-ellipsoid figure is 0.08% smaller), summed per class — no constant pixel area, "
        f"no resampling of counts. Outside or unclassified: the producer's values {sorted(outside_values)} "
        "are reported as one excluded area, never as a class, and shares are of the classified "
        "area only. "
        + ("Change: computed only on pixels classified in both years, after placing the later "
           "year on the earlier year's grid by whole pixels (nearest, no blending); a pixel "
           "mapped one class in the first year and another in the second counts as change, "
           "which includes classifier disagreement. "
           if two_years else "")
        + "Boundary: " + _boundary_phrase(how) + ". No model runs in this pack; the LLM does "
        "no arithmetic.")
    return {"n": n, "kind": "method", "retrieval": "config", "source": "platform method registry",
            "title": "land-cover counting method", "validation": "documented-method", "text": text}


def _standing_gaps(row, pixel_size_deg):
    """The gaps every land-cover answer declares, read from the stack it answers
    from: the accuracy the producer states (or that none is recorded), the cell
    size the map actually has. These were two typed sentences — '30 m', 'no
    accuracy' — that stayed true only for the first stack ever registered."""
    acc = str(row.get("accuracy") or "").strip()
    metres = round(float(pixel_size_deg or 0) * 111_320)
    cell = f"{metres} m" if metres else "one cell"
    out = []
    if not acc or acc.lower() in ("unstated", "unknown", "none", "n/a"):
        out.append("accuracy of the land-cover maps is not documented in this platform's "
                   "registry, so no accuracy figure is cited — the producer's method "
                   "publication has not been archived here")
    else:
        out.append(f"accuracy as declared by the producer, not assessed for this area: {acc}")
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
            "hazard_grid": stacks.category_grid(raster_path, outside)}


# ------------------------------------------------------------------ gather

def gather_land_cover_evidence(target: dict, focus: str, trace: list,
                               extras: dict) -> tuple[list, list, dict]:
    from ..config import get_settings
    place = str(target.get("place") or "").strip()
    year = _year(target.get("year"))
    year_to = _year(target.get("year_to"), "year_to") if target.get("year_to") else None
    if year_to is not None and year_to <= year:
        raise ValueError(f"year_to ({year_to}) must come after year ({year})")
    wanted = _classes(target.get("classes"))

    aoi, boundary, how = _resolve_aoi(place, extras, trace)
    name = aoi.get("name", place)
    sid, row, other_stacks = _pick_stack(boundary, wanted=target.get("stack"),
                                         years=tuple(y for y in (year, year_to) if y))
    legend, outside, colors = _legend(row), _outside(row), None
    colors = _colors(row, legend)
    source = f"{row.get('source', sid)} (platform computation)"
    layer = stacks.layer_for(sid, year)
    layer_to = stacks.layer_for(sid, year_to) if year_to else None
    # A registered year whose file is not on this server declines in words, not a traceback:
    # a second host gets the code and the catalog before (or without) the 14 GB of maps.
    inst = stacks.installed(sid)
    absent = [y for y in (year, year_to) if y and not inst.get(y, False)]
    if absent:
        raise ValueError(f"{row.get('title', sid)}: the map for {', '.join(str(y) for y in absent)} "
                         f"is not installed on this server ({sum(inst.values())} of {len(inst)} "
                         f"declared years are)")
    hits, unknown = _match_classes(wanted, legend)

    clip, level = _clip(aoi, layer, trace)
    st = stacks.class_areas(clip, boundary, legend, outside)
    trace.append(f"class_areas[{layer}] {st['pixels_classified']} px classified")
    if st["pixels_classified"] == 0:
        ext = row.get("extent") or {}
        raise ValueError(
            f"{name} falls inside the bounding box of {row.get('title', sid)} (lon {ext.get('lon')}, "
            f"lat {ext.get('lat')}) but every one of its {st['pixels_inside']:,} pixels is outside "
            "the mapped footprint — this product does not cover it, so no class area exists to report")

    citations, gaps = [], _standing_gaps(row, st["pixel_size_deg"])
    gaps.append(_boundary_gap(how))
    if other_stacks:
        gaps.append(f"{len(other_stacks)} other registered land-cover stack(s) also cover "
                    f"{name}: " + "; ".join(f"{r.get('title', s)} ({s})" for s, r in other_stacks)
                    + f". This answer reads {row.get('title', sid)} ({sid}); pass stack=<id> "
                    "to read another.")
    n = 1
    citations.append(_class_area_citation(n, name, year, row, st, level, source))
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
    detail = []
    if layer_to:
        clip_to, level_to = _clip(aoi, layer_to, trace)
        st_to = stacks.class_areas(clip_to, boundary, legend, outside)
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
        citations.append(_class_area_citation(n, name, year_to, row, st_to, level_to, source))
        n += 1
        citations.append(_change_citation(n, name, year, year_to, ch, source))
        for cls in detail:
            n += 1
            citations.append(_transition_citation(n, name, year, year_to, cls,
                                                  ch["by_class"][cls], source))
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
        # A time series for the classes the caller named, when the span allows one.
        if hits and year_to - year >= 2:
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
            for cls in hits:
                pts = [(y, per_year[y]["by_class"][cls]["km2"]) for y in years_in]
                n += 1
                citations.append(_series_citation(n, name, cls, legend[cls], pts, missing,
                                                  level, st["pixel_size_deg"], source))
            trace.append(f"series[{','.join(legend[c] for c in hits)}] {len(years_in)} years")

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
    corpus, doc_hits = _library_hits(name, focus, labels, trace, gaps)
    for h in doc_hits:
        n += 1
        citations.append(_document_citation(n, h, corpus))
    if doc_hits:
        trace.append(f"documents[{len(doc_hits)}] cited")

    # Contributed tables bound to this pack: a FACTOR table (class, factor) is
    # applied to the areas, losses and gains above; any other is cited as context.
    factor_stats = {}
    _gaps_before_tables = len(gaps)
    for ds, spec, res in _bound_tables(aoi.get("country"), gaps, trace):
        cols = (spec.get("fetch") or {}).get("columns") or {}
        if "class" in cols and "factor" in cols:
            cit, fstats = _factor_citation(n + 1, ds, spec, res, name, year, year_to, legend,
                                           st, st_to if layer_to else None, ch if layer_to else None,
                                           gaps, trace)
            if cit:
                n += 1
                citations.append(cit)
                factor_stats[ds] = fstats
        else:
            n += 1
            citations.append(_context_table_citation(n, ds, spec, res))

    if factor_stats:
        gaps.insert(_gaps_before_tables, (
            "no degradation, canopy or NDVI figures, and no carbon MODELLED inside a class: the "
            "carbon-type figures here are class areas multiplied by contributed per-hectare factors "
            f"({', '.join(sorted(factor_stats))}), stated with their arithmetic — defaults, not "
            "measurements of this site"))
    else:
        gaps.insert(_gaps_before_tables, (
            "no degradation, canopy, biomass, carbon, NDVI or building figures: this pack counts "
            "classes, it does not model inside them; a contributed factor table (class, factor per "
            "hectare) bound to this pack would turn class areas into stocks"))

    for yr, ly, cl, lv in ((year, layer, clip, level),
                           *(((year_to, layer_to, aoi[layer_to], level_to),) if layer_to else ())):
        n += 1
        citations.append(_passport_citation(n, sid, row, ly, yr, cl, lv, legend, gaps))
    n += 1
    citations.append(_method_citation(n, outside, bool(layer_to), level, how))

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
             "_layer_defaults": [
                 {"layer": ly, "why": f"the {y} map these class areas are counted from"}
                 for y, ly in ((year, layer), (year_to, layer_to)) if ly]}
    return citations, gaps, stats


def _library_hits(name, focus, labels, trace, gaps, k=3):
    """Documents in this pack's library that speak to the question: a method paper,
    a national map's technical memo, a guideline. Mirrors the risk pack: an empty or
    unreadable library is a declared gap, never a failure. Returns (corpus, hits)."""
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
    hits, seen = [], set()
    for q in queries:
        try:
            got = corpus.search(q, k=k)
        except (CorpusError, MissingAPIKey) as exc:
            gaps.append(f"document retrieval failed ({exc})")
            return corpus, hits
        trace.append(f"retrieve {q!r} -> {len(got)} hits")
        for h in got:
            if _looks_like_references(h.get("text") or ""):
                trace.append(f"retrieve: skipped a reference-list passage of {((h.get('metadata') or {}).get('title') or 'a document')[:50]}")
                continue
            if h.get("id") not in seen:
                seen.add(h.get("id"))
                hits.append(h)
    if not hits:
        gaps.append(f"the land-cover library holds {n_docs} document(s), none relevant to "
                    f"{name} above the relevance floor — a decline, not a weak match")
    return corpus, hits[:2 * k]


def _looks_like_references(text: str) -> bool:
    """A bibliography page matches many queries and says nothing about the place:
    many author-year citations, few sentences of its own."""
    import re as _re
    years = len(_re.findall(r"\b(?:19|20)\d\d[a-z]?\b", text))
    etal = text.lower().count("et al")
    dois = text.lower().count("doi")
    words = max(1, len(text.split()))
    return (years >= 12 and years / words > 0.04) or etal >= 6 or dois >= 4


def _document_citation(n, h, corpus):
    m = h.get("metadata") or {}
    return {"n": n, "kind": "document", "retrieval": "archived-document",
            "source": m.get("source"), "title": m.get("title"),
            "pub_date": m.get("pub_date"), "validation": m.get("validation"),
            "temporal": m.get("temporal"), "url": m.get("url"), "score": h.get("score"),
            "doc_id": h.get("doc_id"), "chunk_id": h.get("id"),
            **({"review_status": m["review"]} if m.get("review") else {}),
            "archived_copy": (f"/api/rag/{PACK_ID}/document/{h.get('doc_id')}"
                              if corpus and h.get("doc_id") and corpus.raw_path(h["doc_id"]) else None),
            "usage_notes": m.get("usage_notes"),
            **({"staged_by": m["staged_by"], "contribution_id": m.get("contribution_id")}
               if m.get("staged_by") else {}),
            "text": h.get("text")}


def _bound_tables(country, gaps, trace):
    """Every contributed table (and staged one the caller may see) bound to this
    pack, with its rows — the same rule the risk pack applies: a source that
    declares its countries is read only where it speaks."""
    from ..contrib import staging
    from ..graph.geo import vectors as _vectors
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
        if not _vectors.country_matches(spec.get("countries"), country):
            trace.append(f"table[{ds}] declares other countries — not read")
            gaps.append(f"{spec.get('title') or ds} ({ds}) was not read: it declares "
                        f"{', '.join(str(c) for c in spec.get('countries') or [])} and this area is in "
                        f"{country}. Available through feeds_query.")
            continue
        res = feeds.query(ds, {"limit": 10_000})
        if res.get("status") != "ok":
            gaps.append(f"table {ds} bound to this pack did not answer: {res.get('note', 'no reason given')}")
            continue
        trace.append(f"table[{ds}] {res.get('count')} rows")
        out.append((ds, spec, res))
    return out


def _norm_label(text) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", str(text or ""))
    return " ".join("".join(ch for ch in t if not unicodedata.combining(ch)).casefold().split())


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _fmt_t(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.1f}"


def _factor_citation(n, ds, spec, res, name, year, year_to, legend, st, st_to, ch, gaps, trace):
    """One citation applying a factor table to the class areas the pack counted —
    every product stated in the text, the arithmetic visible, the caveat in the
    same sentence. Returns (citation or None, stats)."""
    cols = (spec.get("fetch") or {}).get("columns") or {}
    if "class" not in cols or "factor" not in cols:
        return None, None
    by_label = {_norm_label(v): k for k, v in legend.items()}
    matched, unmatched = [], []
    for rec in res.get("records") or []:
        key = by_label.get(_norm_label(rec.get("class")))
        f = _num(rec.get("factor"))
        if key is None or f is None:
            unmatched.append(str(rec.get("class")))
            continue
        matched.append((key, f, rec))
    if not matched:
        gaps.append(f"{spec.get('title') or ds} ({ds}) names classes "
                    f"{', '.join(sorted(set(unmatched))[:6]) or 'none'}; none is in this stack's legend "
                    f"({', '.join(legend.values())}) — the table was not applied")
        return None, None
    units = (spec.get("fetch") or {}).get("units") or "per hectare"
    parts, stats = [], {}
    for cls, f, rec in matched:
        label = legend[cls]
        unit = str(rec.get("unit") or units)
        pool = str(rec.get("pool") or "").strip()
        basis = str(rec.get("basis") or "").strip()
        ha = float(st["by_class"][cls]["ha"])
        value = ha * f
        line = (f"{label} {year}: {ha:,.1f} ha × {f:g} {unit}"
                + (f" ({pool}" + (f"; {basis}" if basis else "") + ")" if pool or basis else "")
                + f" = {_fmt_t(value)} {unit.split('/')[0].strip() or 'units'}")
        entry = {"class": label, "ha": round(ha, 1), "factor": f, "unit": unit,
                 **({"pool": pool} if pool else {}), **({"basis": basis} if basis else {}),
                 "value": round(value, 1)}
        lo, hi = _num(rec.get("low")), _num(rec.get("high"))
        if lo is not None and hi is not None:
            line += (f" (with the table's low-high range for this factor, {lo:g}-{hi:g}: "
                     f"{_fmt_t(ha * lo)} to {_fmt_t(ha * hi)})")
            entry["value_low"], entry["value_high"] = round(ha * lo, 1), round(ha * hi, 1)
        if ch is not None and year_to:
            cd = ch["by_class"][cls]
            loss_ha, gain_ha = cd["loss_km2"] * 100.0, cd["gain_km2"] * 100.0
            ha_to = float(st_to["by_class"][cls]["ha"]) if st_to else None
            line += (f"; {year_to}: {ha_to:,.1f} ha × {f:g} = {_fmt_t(ha_to * f)}" if ha_to is not None else "")
            line += (f"; {year}->{year_to} loss {loss_ha:,.1f} ha × {f:g} = {_fmt_t(loss_ha * f)} in lost extent, "
                     f"gain {gain_ha:,.1f} ha × {f:g} = {_fmt_t(gain_ha * f)} in gained extent, "
                     f"net {_fmt_t((gain_ha - loss_ha) * f)}")
            entry.update({"loss_ha": round(loss_ha, 1), "gain_ha": round(gain_ha, 1),
                          "value_lost": round(loss_ha * f, 1), "value_gained": round(gain_ha * f, 1),
                          **({"ha_to": round(ha_to, 1), "value_to": round(ha_to * f, 1)} if ha_to is not None else {})})
        parts.append(line)
        stats.setdefault(label, []).append(entry)
    pools = sorted({p["pool"] for v in stats.values() for p in v if p.get("pool")})
    text = (f"Applying {spec.get('title') or ds} ({ds}; {spec.get('source') or 'source unstated'}, "
            f"vintage {spec.get('vintage') or 'unstated'}, validation {spec.get('validation') or 'unstated'}, "
            f"licence {spec.get('license') or 'unstated'}) to the class areas counted in {name}: "
            + "; ".join(parts) + ". "
            "Each figure is area × a per-hectare factor, not a measurement of this site: it holds only "
            "where the mapped class and the factor's domain match, and a change figure is the stock "
            "associated with lost or gained extent, not an emission estimate."
            + (f" Pools stated: {', '.join(pools)}." if pools else "")
            + (f" {len(set(unmatched))} row(s) named classes not in this legend and were skipped." if unmatched else "")
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


def _context_table_citation(n, ds, spec, res):
    recs = res.get("records") or []
    sample = "; ".join(", ".join(f"{k}={v}" for k, v in r.items() if v not in (None, ""))
                       for r in recs[:3])
    text = (f"{spec.get('title') or ds} ({ds}; {spec.get('source') or 'source unstated'}, vintage "
            f"{spec.get('vintage') or 'unstated'}, validation {spec.get('validation') or 'unstated'}): "
            f"{res.get('count')} row(s) bound to this pack, first rows: {sample}."
            + (f" Contributor's note: {spec['usage_notes']}" if spec.get("usage_notes") else "")
            + (f" Review: {spec['review']}." if spec.get("review") else ""))
    cit = {"n": n, "kind": "table", "retrieval": "pulled-at-pack-time",
           "source": spec.get("source") or "contributed table (source unstated)",
           "title": spec.get("title") or ds, "pub_date": spec.get("vintage"),
           "validation": f"contributed-table ({spec.get('validation') or 'unstated'})",
           "dataset": ds, "text": text}
    if spec.get("staged_by"):
        cit["staged_by"] = spec["staged_by"]
        cit["contribution_id"] = spec.get("contribution_id")
    return cit


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
                   "year": "a mapped year", "year_to": "optional later year, for change",
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
