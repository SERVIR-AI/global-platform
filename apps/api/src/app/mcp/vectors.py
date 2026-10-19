"""The vector-summary step of the assemble seam — the same for every pack.

After any gatherer has run, the contributed vector layers the caller named
(`vectors=[...]`) — or the pack's own `default_vectors` — are measured inside the
area that pack used: how many points, how many km of line, how many km² of
polygon, by the field the contributor named, with the largest named lines and
polygons (or, for points, the names that recur most). The area comes from the
gatherer's reserved `_area` stats key (see mcp/layers.py; this step reads it, the
layer-values step pops it). Nothing here names a pack.

Each layer becomes one numbered citation whose text carries every number a brief
may state, plus a `features_sha256` the receipt records; the records ride the
pack's top-level `vectors` block, never `stats` (the gate scans stats for numbers).
"""

from __future__ import annotations

from ..graph.geo import vecio as _vecio
from ..graph.geo import vectors as _vectors

MAX_LAYERS = 8


def registered() -> dict:
    return _vectors.visible()


def check(names) -> str | None:
    """A decline note for an explicit `vectors` request, before the gather runs."""
    if names is None:
        return None
    if isinstance(names, str) or not isinstance(names, (list, tuple)):
        return "vectors must be a list of contributed vector layer names, e.g. [\"cr_rivers\"]"
    wanted = [str(n).strip() for n in names if str(n).strip()]
    if len(wanted) > MAX_LAYERS:
        return f"at most {MAX_LAYERS} vector layers per pack; {len(wanted)} were named"
    reg = registered()
    unknown = [n for n in wanted if n not in reg]
    if unknown:
        have = ", ".join(sorted(reg)) or "none yet"
        return (f"unknown vector layer(s): {', '.join(repr(u) for u in unknown)}. Contributed "
                f"vector layers: {have}")
    return None


def _wanted(spec: dict, names) -> list[str]:
    if names is not None:
        return [n for n in dict.fromkeys(str(x).strip() for x in names) if n]
    rows = spec.get("default_vectors")
    if callable(rows):
        try:
            rows = rows()
        except Exception:
            rows = None
    return [str(r) for r in (rows or []) if str(r) in registered()]


def _fmt(v, unit: str) -> str:
    if unit == "features":
        return f"{int(v):,}"
    if unit == "km2":
        return f"{v:,.2f} km² ({v * 100:,.1f} ha)"
    return f"{v:,.2f} km"


def restrict_within(layer: str, area_geom, area_label: str) -> tuple:
    """(geometry, label, how) of `area_geom` restricted to the inside of a contributed
    POLYGON layer, dissolved — or raises ValueError in words."""
    reg = registered()
    entry = reg.get(layer)
    if not entry:
        have = ", ".join(sorted(k for k, e in reg.items() if _vectors.family(e) == "polygons")) or "none yet"
        raise ValueError(f"within={layer!r} is not a contributed polygon layer — contributed polygon "
                         f"layers: {have}")
    if _vectors.family(entry) != "polygons":
        raise ValueError(f"within={layer!r} is a {_vectors.family(entry)} layer; `within` needs a "
                         "POLYGON layer (protected areas, a watershed, a map's class polygons)")
    inside = _vectors.union_inside(entry, area_geom)
    km2_in = 0.0 if inside.is_empty else _vectors.area_km2(inside)
    if km2_in <= 0:
        raise ValueError(f"no part of {area_label} lies inside {entry.get('title') or layer} ({layer}) — "
                         "nothing can be measured there")
    km2_all = _vectors.area_km2(area_geom) if area_geom.geom_type in ("Polygon", "MultiPolygon") else None
    title = entry.get("title") or layer
    label = f"{area_label} — inside {title}"
    how = (f"contributed boundary: {area_label} restricted to the inside of {title} ({layer}; "
           f"{entry.get('source') or 'source unstated'}), its features merged so overlaps count once: "
           f"{km2_in:,.1f} km²" + (f" of {km2_all:,.1f} km²" if km2_all else ""))
    return inside, label, how


def citation_text(layer: str, entry: dict, rec: dict, area_name: str) -> str:
    unit = rec["unit"]
    what = {"points": "features", "lines": "of line", "polygons": "of mapped area"}[rec["geometry"]]
    head = (f"{entry.get('title') or layer} ({layer}; a contributed {rec['geometry'][:-1]} layer, "
            f"source {entry.get('source') or 'unstated'}, vintage {entry.get('vintage') or 'unstated'}, "
            f"licence {entry.get('license') or 'unstated'}) inside {area_name}: ")
    if rec["geometry"] == "points":
        body = f"{rec['features_inside']:,} features fall inside"
    else:
        body = (f"{_fmt(rec['total'], unit)} {what}, from {rec['features_inside']:,} features "
                f"clipped to the boundary (the part inside, never the whole feature)")
    bits = [head + body + "."]
    if rec.get("by_group"):
        parts = "; ".join(f"{k}: {_fmt(v, unit)}" for k, v in rec["by_group"].items())
        tail = (f"; {rec['by_group_rest']['groups']} other value(s): "
                f"{_fmt(rec['by_group_rest']['total'], unit)}" if rec.get("by_group_rest") else "")
        bits.append(f"By {rec['group_field']}: {parts}{tail}.")
    if rec.get("largest_named"):
        parts = "; ".join(f"{d['name']} ({_fmt(d['measure'], unit)})" for d in rec["largest_named"][:6])
        more = rec["features_inside"] - (rec.get("unnamed") or 0) - min(len(rec["largest_named"]), 6)
        bits.append(f"Largest named ({rec['name_field']}): {parts}"
                    + (f"; {more:,} more named" if more > 0 else "")
                    + (f"; {rec['unnamed']:,} unnamed" if rec.get("unnamed") else "") + ".")
    if rec.get("most_frequent_named"):
        # A point has no size: what its names say is how often each one recurs.
        parts = ", ".join(f"{d['name']} ({d['count']:,})" for d in rec["most_frequent_named"][:6])
        more = (rec.get("distinct_names") or 0) - min(len(rec["most_frequent_named"]), 6)
        bits.append(f"Most frequent {rec['name_field']}: {parts}"
                    + (f"; {more:,} other name(s)" if more > 0 else "")
                    + (f"; {rec['unnamed']:,} unnamed" if rec.get("unnamed") else "") + ".")
    if rec.get("footprint_km2") is not None:
        bits.append(f"Its features overlap inside {area_name}: counting shared ground once, they "
                    f"cover {_fmt(rec['footprint_km2'], 'km2')} (the per-{rec.get('group_field') or 'feature'} "
                    "figures above each count overlapping ground, so they do not add up to this).")
    if rec.get("truncated"):
        bits.append("The layer was read up to its feature cap; the figures are a lower bound.")
    if rec.get("skipped_unreadable"):
        bits.append(f"{rec['skipped_unreadable']:,} feature(s) could not be clipped to the boundary "
                    "and are not counted; the figures are a lower bound.")
    if rec.get("unreadable_in_layer"):
        bits.append(f"{rec['unreadable_in_layer']:,} feature(s) of the layer could not be read or "
                    "repaired and are not counted anywhere; the figures are a lower bound.")
    lost = _vecio.losses(entry.get("prepared"))
    if lost:
        bits.append("At ingest, " + "; ".join(lost) + ".")
    how = {"km2": "areas exact on the WGS 84 ellipsoid (through the authalic latitude, as the "
                  "platform's raster areas)",
           "km": "lengths on the WGS 84 ellipsoid",
           "features": "points counted where they fall"}[unit]
    bits.append(f"Measured as {how}, over {rec['features_in_layer']:,} features in the layer; "
                "replayable by the features hash on the receipt.")
    if entry.get("usage_notes"):
        bits.append(f"Contributor's note: {entry['usage_notes']}")
    if entry.get("review"):
        bits.append(f"Review: {entry['review']}.")
    return " ".join(bits)


def attach(spec: dict, target: dict, stats: dict, citations: list, gaps: list,
           names, extras: dict) -> tuple[dict | None, dict | None]:
    """Measure the wanted layers inside the pack's area; append their citations and
    gaps. Returns (the `vectors` block, the area they were measured in) — (None, None)
    when there is nothing to measure. The area is persisted with the pack so a
    receipt can recompute every features hash from the stored boundary."""
    wanted = _wanted(spec, names)
    if not wanted:
        return None, None
    area = stats.get("_area") if isinstance(stats, dict) else None
    if not (isinstance(area, dict) and area.get("geometry")):
        from . import layers as _layers
        area = _layers._caller_area(target, extras)
    if not area or not area.get("geometry"):
        gaps.append("vector layers were requested, but this pack resolves no area to measure "
                    "them in — pass `geometry` (a GeoJSON polygon or a bbox) with the request. "
                    "Nothing was measured, which is not a zero.")
        return None, None
    from shapely.geometry import shape
    from ..graph.geo import layer_values
    name = area.get("name") or target.get("place") or "this area"
    try:
        boundary = shape(area["geometry"])
    except Exception as exc:
        gaps.append(f"vector layers could not be measured for {name}: {type(exc).__name__}: {exc}")
        return None, None
    a_sha = layer_values.area_sha256(boundary)
    reg = registered()
    n = max((int(c.get("n") or 0) for c in citations), default=0)
    records = {}
    for layer in wanted:
        entry = reg.get(layer)
        if not entry:
            gaps.append(f"vector layer {layer!r} is not registered; nothing was measured for it")
            continue
        na = _vectors.not_applicable(entry, boundary, area.get("country"))
        if na:
            gaps.append(f"{entry.get('title') or layer} ({layer}) was not measured for {name}: "
                        f"{na['reason']}. This is not a zero: the layer does not cover the place.")
            continue
        try:
            rec = _vectors.summarise(entry, boundary)
        except Exception as exc:
            gaps.append(f"{layer} could not be measured for {name}: {type(exc).__name__}: {exc}. "
                        "Not a zero: the layer was not read here.")
            continue
        rec["area_sha256"] = a_sha
        rec["title"] = entry.get("title")
        rec["source"] = entry.get("source")
        rec["vintage"] = entry.get("vintage")
        rec["license"] = entry.get("license")
        if entry.get("review"):
            rec["review"] = entry["review"]
        n += 1
        rec["n"] = n
        cit = {"n": n, "kind": "vector_summary", "retrieval": "computed-at-pack-time",
               "source": entry.get("source") or "contributed layer (source unstated)",
               "title": f"{entry.get('title') or layer} inside {name}",
               "pub_date": entry.get("vintage"),
               "validation": "contributed-vector-layer, deterministic measure inside the boundary",
               "layer": layer, "features_sha256": rec["features_sha256"],
               "values_in": f"vectors.{layer}",
               "text": citation_text(layer, entry, rec, name)}
        if entry.get("usage_notes"):
            cit["usage_notes"] = entry["usage_notes"]
        if entry.get("review"):
            cit["review_status"] = entry["review"]
        if entry.get("staged_by"):
            cit["staged_by"] = entry["staged_by"]
            cit["contribution_id"] = entry.get("contribution_id")
        citations.append(cit)
        records[layer] = rec
        if rec["features_inside"] == 0:
            gaps.append(f"{entry.get('title') or layer} ({layer}) has no feature inside {name}; "
                        "its figure is a real zero inside this boundary, not a missing layer")
    if not records:
        return None, None
    where = {"name": name, "how": area.get("how"), "area_km2": area.get("area_km2"),
             "area_sha256": a_sha, "geometry": area["geometry"]}
    return records, where


def verify(pack: dict) -> dict | None:
    """{layer: True|False|'<why not>'} — each stored features hash recomputed from the
    stored boundary and the layer as it is NOW; a removed or changed layer says so."""
    recs = pack.get("vectors") or {}
    where = pack.get("vectors_area") or {}
    if not recs:
        return None
    if not where.get("geometry"):
        return {k: "the pack stored no boundary for its vector measures" for k in recs}
    from shapely.geometry import shape
    try:
        boundary = shape(where["geometry"])
    except Exception:
        return {k: "the stored boundary could not be read" for k in recs}
    reg = registered()
    out = {}
    for layer, rec in recs.items():
        entry = reg.get(layer)
        if not entry:
            out[layer] = "the layer is no longer registered on this platform"
            continue
        try:
            now = _vectors.summarise(entry, boundary)
        except Exception as exc:
            out[layer] = f"could not be re-measured: {type(exc).__name__}"
            continue
        out[layer] = now.get("features_sha256") == rec.get("features_sha256")
    return out
