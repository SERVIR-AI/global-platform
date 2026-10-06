"""The LAND-COVER domain pack: pixel counts over a yearly land-cover stack, as
citable evidence.

Nothing here runs a model. The pack resolves an area, clips the chosen year(s)
of a registered stack to it, counts pixels per class at each pixel's own
latitude, and writes every figure a brief may quote into a citation's text —
with the mask, the boundary's source and the stack's undocumented accuracy
stated beside the numbers, not under them. Change between two years is
computed on the pixels classified in BOTH years, because the producer's own
whole-map tables disagree on coverage and a naive difference reads that as
change.
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


def landcover_stacks():
    """{stack_id: row} for every registered stack with role landcover."""
    return {k: v for k, v in stacks.rows().items() if v.get("role") == ROLE}


def _pick_stack(boundary):
    """The registered land-cover stack whose footprint holds the area."""
    rows = landcover_stacks()
    if not rows:
        raise ValueError("no land-cover stack is registered on this platform — a hub "
                         "contributes one as a `kind: stack` catalog row")
    minx, miny, maxx, maxy = boundary.bounds
    for sid, row in rows.items():
        ext = row.get("extent") or {}
        lon, lat = ext.get("lon"), ext.get("lat")
        if not lon or not lat:
            return sid, row
        if maxx >= lon[0] and minx <= lon[1] and maxy >= lat[0] and miny <= lat[1]:
            return sid, row
    sid, row = next(iter(rows.items()))
    ext = row.get("extent") or {}
    raise ValueError(
        f"this area lies outside every registered land-cover stack — {row.get('title', sid)} "
        f"covers lon {ext.get('lon')} and lat {ext.get('lat')}; nothing can be counted there")


def _resolve_aoi(place, extras, trace):
    geometry = extras.get("geometry")
    if geometry is not None:
        aoi = ingest.ensure_aoi(geometry=geometry, name=place or None, layers=())
    else:
        aoi = ingest.ensure_aoi(place=place, layers=())
    how = str(aoi.get("how") or "")
    if not (how.startswith("admin boundary") or how in ("supplied polygon", "drawn")):
        raise ValueError(
            f"{place!r} did not resolve to a boundary but to {how}. Land-cover figures "
            "are only computed for a real boundary: name a district, province or park "
            f"under {ingest.AREA_CAP_KM2:,.0f} km², or pass `geometry` with the boundary you hold")
    trace.append(f"aoi[{aoi.get('name', place)}] {aoi.get('area_km2')} km2 via {how}")
    boundary = shape(json.load(open(aoi["admin"]))["features"][0]["geometry"])
    return aoi, boundary, how


def _legend(row):
    return {int(k): str(v).strip() for k, v in (row.get("legend") or {}).items()}


def _outside(row):
    return [int(v) for v in (row.get("outside_values") or [0])]


def _match_classes(wanted, legend):
    """Class names a caller asked about, matched to the legend; unknown names declared."""
    by_name = {v.lower(): k for k, v in legend.items()}
    hits, unknown = [], []
    for w in wanted:
        k = by_name.get(w.lower()) or next((k for k, v in legend.items()
                                            if w.lower() in v.lower()), None)
        (hits if k is not None else unknown).append(k if k is not None else w)
    return hits, unknown


# ---------------------------------------------------------------- citations

def _class_area_citation(n, name, year, row, st, source):
    ordered = sorted(st["by_class"].items(), key=lambda kv: -kv[1]["km2"])
    parts = [f"{d['label']} {_km2(d['km2'])} km² ({_pct(d['share_pct'])}%)"
             for _, d in ordered if d["pixels"] > 0]
    absent = st["classes_absent"]
    cell_m = st["pixel_size_deg"] * 111_320
    text = (f"Land cover of {name} in {year} ({row.get('title', '')}; read at full resolution, "
            f"{cell_m:.0f} m cells at the equator): {_km2(st['km2_classified'])} km² "
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


def _change_citation(n, name, y1, y2, ch, legend, source):
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


def _passport_citation(n, sid, row, layer, year, clip, legend, gaps):
    """Declared contract vs the clip actually read — 'checked' means compared."""
    meta = tiffs.entry(layer)
    path = meta.get("path") or ""
    contract, obs, hdr, notes = None, None, None, []
    try:
        contract = schema.schema_for(layer)
    except Exception as exc:
        notes.append(f"declared contract unreadable ({type(exc).__name__})")
    try:
        obs = rasterstats.windowed_stats(clip)
    except Exception as exc:
        notes.append(f"clip statistics unreadable ({type(exc).__name__})")
    try:
        hdr = stacks.header(path)
    except Exception as exc:
        notes.append(f"file header unreadable ({type(exc).__name__})")
    bits = [f"Land-cover stack {sid}, year {year}: {row.get('title', sid)}, file "
            f"{os.path.basename(path)}.",
            f"Source: {row.get('source', 'unattributed')} — licence {row.get('license', 'unstated')}, "
            f"published {row.get('pub_date') or row.get('vintage') or 'unrecorded'}.",
            f"Authority: {row.get('authority', 'unstated')}.",
            "Residency: platform-hosted copy, read in place from "
            + ("a stored zip archive." if "/vsizip/" in path else "local disk.")]
    if contract:
        bits.append(f"Declared contract: dtype {contract.get('dtype')}, classes "
                    f"{contract.get('valid_min')}-{contract.get('valid_max')}, nodata "
                    f"{contract.get('nodata')}, {contract.get('crs')}, "
                    f"{contract.get('pixel_size_deg')}° per pixel.")
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
    bits.append(f"Accuracy: {row.get('accuracy') or 'not documented in this platform'} — "
                "no accuracy figure is cited.")
    validation = "unvalidated"
    if contract and obs and obs.get("sampled_min") is not None:
        mism = verify._check(contract, obs)
        if mism:
            validation = "structural-contract-FAILED"
            bits.append("CONTRACT MISMATCH: " + "; ".join(mism) + ".")
            gaps.append(f"{layer} failed its structural contract: " + "; ".join(mism))
        else:
            validation = "structural-contract-checked"
    if notes:
        bits.append("Verification notes: " + "; ".join(notes) + ".")
    bits.append("Legend: " + "; ".join(f"{k}={v}" for k, v in sorted(legend.items())) + ".")
    return {"n": n, "kind": "landcover_layer", "retrieval": "computed-at-pack-time",
            "source": row.get("source", "catalog (unattributed)"),
            "title": f"{row.get('title', sid)}, {year}", "validation": validation,
            **({"url": row["url"]} if row.get("url") else {}),
            **({"pub_date": row["pub_date"]} if row.get("pub_date") else {}),
            "authority": row.get("authority"), "residency": "platform-hosted",
            "text": " ".join(bits)}


def _method_citation(n, outside_values, two_years):
    text = (
        "Land-cover figures are pixel counts. Window: the boundary's bounding box in the "
        "year's own grid, intersected with the dataset, read at full resolution. Membership: "
        "a pixel counts when its centre falls inside the boundary. Area: each pixel's own "
        "spherical area at its latitude (authalic radius 6371.0072 km; the WGS84-ellipsoid "
        "figure is 0.08% smaller), summed per class — no constant pixel area, no resampling "
        f"of counts. Outside or unclassified: the producer's values {sorted(outside_values)} are "
        "reported as one excluded area, never as a class, and shares are of the classified "
        "area only. "
        + ("Change: computed only on pixels classified in both years, after placing the later "
           "year on the earlier year's grid by whole pixels (nearest, no blending); a pixel "
           "mapped one class in the first year and another in the second counts as change, "
           "which includes classifier disagreement. "
           if two_years else "")
        + "The boundary is OpenStreetMap's administrative boundary at pack time, or the "
        "caller's own polygon when one was supplied. No model runs in this pack; the LLM "
        "does no arithmetic.")
    return {"n": n, "kind": "method", "retrieval": "config", "source": "platform method registry",
            "title": "land-cover counting method", "validation": "documented-method", "text": text}


STANDING_GAPS = (
    "accuracy of the land-cover maps is not documented in this platform's registry, so no "
    "accuracy figure is cited — the producer's method publication has not been archived here",
    "a land-cover class is the producer's label for a 30 m pixel; features narrower than one "
    "cell are not resolved",
    "the boundary comes from OpenStreetMap at pack time (or the caller's polygon); the hub's "
    "official boundary may differ",
    "no degradation, canopy, biomass, carbon, NDVI or building figures: this pack counts "
    "classes, it does not model inside them",
    "fire detections are not yet registered for this pack",
)


# ------------------------------------------------------------------ gather

def gather_land_cover_evidence(target: dict, focus: str, trace: list,
                               extras: dict) -> tuple[list, list, dict]:
    place = str(target.get("place") or "").strip()
    year = _year(target.get("year"))
    year_to = _year(target.get("year_to"), "year_to") if target.get("year_to") else None
    if year_to is not None and year_to <= year:
        raise ValueError(f"year_to ({year_to}) must come after year ({year})")
    wanted = _classes(target.get("classes"))

    aoi, boundary, how = _resolve_aoi(place, extras, trace)
    name = aoi.get("name", place)
    sid, row = _pick_stack(boundary)
    legend, outside = _legend(row), _outside(row)
    source = f"{row.get('source', sid)} (platform computation)"
    layer = stacks.layer_for(sid, year)
    layer_to = stacks.layer_for(sid, year_to) if year_to else None
    hits, unknown = _match_classes(wanted, legend)

    aoi[layer] = ingest.hazard_clip(aoi, layer)
    trace.append(f"clip[{layer}]")
    st = stacks.class_areas(aoi[layer], boundary, legend, outside)
    trace.append(f"class_areas[{layer}] {st['pixels_classified']} px classified")
    if st["pixels_classified"] == 0:
        ext = row.get("extent") or {}
        raise ValueError(
            f"{name} falls inside the bounding box of {row.get('title', sid)} (lon {ext.get('lon')}, "
            f"lat {ext.get('lat')}) but every one of its {st['pixels_inside']:,} pixels is outside "
            "the mapped footprint — this product does not cover it, so no class area exists to report")

    citations, gaps = [], list(STANDING_GAPS)
    n = 1
    citations.append(_class_area_citation(n, name, year, row, st, source))
    change_stats = None
    if layer_to:
        aoi[layer_to] = ingest.hazard_clip(aoi, layer_to)
        trace.append(f"clip[{layer_to}]")
        ch = stacks.change(aoi[layer], aoi[layer_to], boundary, legend, outside)
        trace.append(f"change[{layer}->{layer_to}] {ch['pixels_common']} px common")
        st_to = stacks.class_areas(aoi[layer_to], boundary, legend, outside)
        n += 1
        citations.append(_class_area_citation(n, name, year_to, row, st_to, source))
        n += 1
        citations.append(_change_citation(n, name, year, year_to, ch, legend, source))
        detail = hits or _default_detail(ch, legend)
        for cls in detail:
            n += 1
            citations.append(_transition_citation(n, name, year, year_to, cls,
                                                  ch["by_class"][cls], source))
        change_stats = {"km2_common": ch["km2_common"], "km2_changed": ch["km2_changed"],
                        "km2_one_year_only": ch["km2_one_year_only"],
                        "by_class": {d["label"]: {"km2_from": d["km2_a"], "km2_to": d["km2_b"],
                                                  "loss_km2": d["loss_km2"], "gain_km2": d["gain_km2"]}
                                     for d in ch["by_class"].values()}}
    for yr, ly in ((year, layer), *(((year_to, layer_to),) if layer_to else ())):
        n += 1
        citations.append(_passport_citation(n, sid, row, ly, yr, aoi[ly], legend, gaps))
    n += 1
    citations.append(_method_citation(n, outside, bool(layer_to)))

    if unknown:
        gaps.append(f"classes not in this stack's legend and not detailed: {', '.join(unknown)} "
                    f"(legend: {', '.join(legend.values())})")
    if st["km2_outside"] > 0:
        gaps.append(f"{_km2(st['km2_outside'])} km² of the boundary is outside the product "
                    "footprint or unclassified and is excluded from every share")
    missing = row.get("missing_years") or {}
    if missing:
        gaps.append("years with no map: " + "; ".join(f"{y} ({r})" for y, r in missing.items()))
    if how.startswith("admin boundary"):
        gaps.append("boundary: " + how)

    stats = {"queries": None,
             "aoi": {"requested": place, "name": name, "area_km2": aoi.get("area_km2"),
                     "how": how, "is_admin_boundary": how.startswith("admin boundary")},
             "stack": sid, "year": year, "year_to": year_to,
             "classes_km2": {d["label"]: round(d["km2"], 1) for d in st["by_class"].values()},
             "km2_classified": round(st["km2_classified"], 1),
             "km2_outside": round(st["km2_outside"], 1),
             **({"change": change_stats} if change_stats else {})}
    return citations, gaps, stats


def _default_detail(ch, legend):
    """Which classes get a transition citation when none were asked for: Forest when
    it exists, plus the two other classes that moved most."""
    forest = next((k for k, v in legend.items() if v.lower() == "forest"), None)
    moved = sorted(((abs(d["km2_b"] - d["km2_a"]), k) for k, d in ch["by_class"].items()
                    if k != forest and (d["km2_a"] or d["km2_b"])), reverse=True)
    return ([forest] if forest in ch["by_class"] else []) + [k for _, k in moved[:2]]


# ---------------------------------------------------------------- manifest

def manifest() -> dict:
    """What the land-cover pack ships and what it does not — real state where it
    can be read, declared gaps in one place."""
    from ..mcp.packs import corpus_state
    sources = {}
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
    return {
        "id": PACK_ID, "display_name": "Land Cover Platform", "version": "v0",
        "profile": "v0 — class areas for one year, change and transitions between two "
                   "years, over a registered yearly land-cover stack",
        "built_for": "how much of an area is each land-cover class, and how that changed "
                     "between two mapped years — replayable, with the mask and the "
                     "boundary's source declared",
        "output_contract": {"required_sections": list(SECTIONS),
                            "gate": "verify_groundedness — same blocking rules as every pack",
                            "receipt": "record_receipt / publish_answer"},
        "sources": {"stacks": sources, "corpus": corpus_state(PACK_ID)},
        "target": {"place": "district, province or park name inside a stack's footprint "
                            "(or pass geometry and use place as its label)",
                   "year": "a mapped year", "year_to": "optional later year, for change",
                   "classes": "optional class names to detail, e.g. [\"Forest\"]"},
        "gaps": list(STANDING_GAPS) + [
            "no time series across all years yet; no map embed yet",
            "a Southeast Asia land-cover layer is listed in the raster inventory but not "
            "registered as a stack",
        ],
        "compositions": {},
    }
