"""The layer-values step of the assemble seam — the same for every pack.

After any gatherer has run, the values of registered raster layers are read over the
area that pack used: the layers the caller named (`layers=[...]`), else the pack's
own defaults. A gatherer reports its area and its defaults as data — the reserved
stats keys `_area` ({name, how, area_km2, geometry}) and `_layer_defaults`
([{layer, why}]); a pack row or extension SPEC may instead declare
`default_layers` (a list, or a callable of the target). Nothing here names a pack.
A gatherer whose answer rests on a class map may also hand it over as `_class_grid`
({path, legend, outside_values, year, stack, label}): every layer read is then also
summarised per class of that map — mean and total biomass inside mangrove, mangrove
area per elevation band — for any pack, from the same key.
Every path that persists gatherer stats strips the reserved keys (`strip_reserved`):
an area's coordinates left in `stats` would count as evidence to the gate.

Each layer read becomes one numbered citation whose text carries every number a
brief may state (the groundedness gate reads citation text), plus a `values_sha256`
the receipt records. The grids themselves ride the pack's top-level `layers` block,
never `stats`: the gate scans `str(stats)` for numbers, and thousands of cell values
there would let a made-up figure pass.
"""

from __future__ import annotations

from ..graph.geo import layer_values


def check(layers) -> str | None:
    """A decline note for an explicit `layers` request, before the gather runs."""
    if layers is None:
        return None
    if isinstance(layers, str) or not isinstance(layers, (list, tuple)):
        return "layers must be a list of registered raster layer names, e.g. [\"population_worldpop_sea\"]"
    return layer_values.check_names(layers)


RESERVED = ("_area", "_layer_defaults", "_class_grid")


def strip_reserved(stats) -> tuple:
    """(area, defaults) popped from a gatherer's stats — call it on every path that
    persists them, used or not. The class map (`_class_grid`) goes too: its file path
    is not evidence."""
    if not isinstance(stats, dict):
        return None, None
    area = stats.pop("_area", None)
    defaults = stats.pop("_layer_defaults", None)
    stats.pop("_class_grid", None)
    if not (isinstance(area, dict) and area.get("geometry")):
        area = None
    return area, defaults


def _defaults(spec: dict, target: dict, rows) -> list[dict]:
    if rows is None:
        rows = spec.get("default_layers")
        if callable(rows):
            try:
                rows = rows(target)
            except Exception:
                rows = None
    out = []
    for item in rows or []:
        if isinstance(item, str):
            out.append({"layer": item, "why": "a default of this pack"})
        elif isinstance(item, dict) and item.get("layer"):
            out.append({"layer": str(item["layer"]), "why": item.get("why")})
    return out


def _caller_area(target: dict, extras: dict) -> dict | None:
    geometry = (extras or {}).get("geometry")
    if geometry is None:
        return None
    from shapely.geometry import mapping

    from ..graph.geo import ingest
    try:
        geom = ingest._to_polygon(geometry)
    except Exception:
        return None
    return {"name": (target.get("place") or "the area you supplied"),
            "how": "the boundary passed as `geometry` (this pack resolves no area of its own)",
            "geometry": mapping(geom)}


def attach(spec: dict, target: dict, stats: dict, citations: list, gaps: list,
           layers, extras: dict) -> tuple[dict | None, dict | None]:
    """Read the wanted layers over the pack's area; append their citations (numbered
    after the last) and gaps. Returns (the `layers` block, the area they were read
    over) — (None, None) when there is nothing to read. Always removes the reserved
    keys from `stats`."""
    class_grid = stats.get("_class_grid") if isinstance(stats, dict) else None
    if class_grid is not None and not (
            isinstance(class_grid, dict) and (class_grid.get("path") or class_grid.get("layer"))
            and class_grid.get("legend")):
        gaps.append("this pack handed over a class map without a file or a legend, so the "
                    "layer values are not split by its classes")
        class_grid = None
    area, rows = strip_reserved(stats)
    defaults = _defaults(spec, target, rows)
    if layers is not None:
        names = [n for n in dict.fromkeys(str(x).strip() for x in layers) if n]
        wanted = [{"layer": n, "why": "requested by name"} for n in names]
    else:
        wanted = defaults
    if not wanted:
        return None, None
    area = area or _caller_area(target, extras)
    if not area or not area.get("geometry"):
        if layers is not None:
            gaps.append("layer values were requested, but this pack resolves no area to read "
                        "them over — pass `geometry` (a GeoJSON polygon or a bbox) with the "
                        "request. No values were read, which is not a zero.")
        return None, None
    from shapely.geometry import shape
    name = area.get("name") or target.get("place") or "this area"
    try:
        boundary = shape(area["geometry"])
        records, lgaps = layer_values.for_area(boundary, wanted, name, class_grid=class_grid)
    except Exception as exc:
        gaps.append(f"layer values could not be read for {name}: {type(exc).__name__}: {exc}")
        return None, None
    n = max((int(c.get("n") or 0) for c in citations), default=0)
    for layer, rec in records.items():
        n += 1
        rec["n"] = n
        cit = {"n": n, "kind": "layer_values", "retrieval": "computed-at-pack-time",
               "source": rec.get("source") or "platform raster registry (source unrecorded)",
               "title": f"values of {rec['title']} in {name}",
               "pub_date": rec.get("vintage"), "validation": rec["validation"],
               "layer": layer, "values_sha256": rec["values_sha256"],
               "values_in": f"layers.{layer}",
               "text": layer_values.citation_text(layer, rec, name)}
        if rec.get("usage_notes"):
            cit["usage_notes"] = rec["usage_notes"]
        for k in ("prepared", "fetched"):     # what ingest did, and where a clip was cut from
            if rec.get(k):
                cit[k] = rec[k]
        if rec.get("review"):
            cit["review_status"] = rec["review"]
        if rec.get("staged_by"):
            cit["staged_by"] = rec["staged_by"]
            cit["contribution_id"] = rec.get("contribution_id")
        citations.append(cit)
    gaps.extend(lgaps)
    if not records:
        return None, None
    # The boundary the values were clipped to, kept with the pack (not returned
    # inline): with it, anyone holding the pack can recompute each values_sha256.
    where = {"name": name, "how": area.get("how"), "area_km2": area.get("area_km2"),
             "area_sha256": layer_values.area_sha256(boundary),
             "geometry": area["geometry"]}
    return records, where
