"""Contributed VECTOR layers — points (evacuation centres, shelters), lines (rivers,
coastlines, drainage channels) and polygons (protected areas, an official mangrove
map, watersheds) a hub knows about.

The built-in assets (hospitals, schools, buildings) come from OSM and are hard-wired.
A hub's own layer lands once, as a master GeoJSON, and is clipped into each AOI
bundle beside the OSM layers under the same name — so the exposure count that
crosses hospitals with a hazard crosses a point layer with no new code, and any
pack with an area can SUMMARISE a line or polygon layer inside it (`summarise`):
how many, how many km, how many km², by a field the contributor names. A polygon
layer whose features carry names is also a GAZETTEER (`named_area`): "Humedal
Nacional Térraba-Sierpe" resolves to the hub's own boundary, not a geocoder's box.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import unicodedata
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.ops import unary_union

R_KM = 6371.0072          # authalic Earth radius, as stacks.py

FAMILIES = {"Point": "points", "MultiPoint": "points",
            "LineString": "lines", "MultiLineString": "lines",
            "Polygon": "polygons", "MultiPolygon": "polygons"}

REGISTRY = "vectors/registry.json"          # under cache_dir: {layer: entry}
NAME_RE = r"^[a-z][a-z0-9_]{2,40}$"


def _dir() -> Path:
    from ...config import get_settings
    d = Path(get_settings().cache_dir) / "vectors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def landed() -> dict:
    """{layer: entry} for layers that passed review. Entry carries `local_path`."""
    p = _dir() / "registry.json"
    if not p.is_file():
        return {}
    try:
        return json.load(open(p)) or {}
    except (OSError, ValueError):
        return {}


def _save(reg: dict) -> None:
    json.dump(reg, open(_dir() / "registry.json", "w"), indent=2)


def register(layer: str, entry: dict) -> None:
    reg = landed()
    reg[layer] = entry
    _save(reg)


def unregister(layer: str) -> None:
    reg = landed()
    if reg.pop(layer, None) is not None:
        _save(reg)


def visible() -> dict:
    """Landed layers plus the staged ones THIS caller may see. Same visibility rule
    as staged rasters: contributor and reviewers, nobody else."""
    out = dict(landed())
    try:
        from ...contrib import staging
        out.update(staging.visible_staged_vectors())
        for k in staging.visible_staged_amends():
            if k in out:
                out[k] = staging.amended_row(k, out[k])
    except Exception:
        pass
    return out


def master_path(entry: dict) -> Path:
    from ...config import get_settings
    return Path(get_settings().cache_dir) / entry["local_path"]


_COUNTRY_ALIASES = {"vietnam": "viet nam", "laos": "lao pdr", "lao people's democratic republic": "lao pdr",
                    "burma": "myanmar", "brunei darussalam": "brunei", "timor-leste": "east timor",
                    "kingdom of cambodia": "cambodia", "kingdom of thailand": "thailand",
                    "republic of the philippines": "philippines", "republic of indonesia": "indonesia"}


def _country_key(name) -> str:
    k = " ".join(str(name or "").lower().replace("_", " ").split())
    return _COUNTRY_ALIASES.get(k, k)


def country_matches(declared, country) -> bool:
    """Does a source that declares `declared` countries speak to `country`? Either
    side unknown -> yes (global as far as the platform knows). Names are matched
    forgivingly (Viet Nam / Vietnam, Lao PDR / Laos, "Kingdom of ...")."""
    names = [str(c) for c in (declared or []) if str(c).strip()]
    if not country or not names:
        return True
    return _country_key(country) in {_country_key(c) for c in names}


def not_applicable(entry: dict, boundary, country: str | None = None) -> dict | None:
    """Why a layer must NOT be clipped into this area, or None when it covers it.

    Two tests, cheapest first. (1) The layer's points lie wholly outside the area's
    bounding box (the observed bbox the gate records). (2) The contributor DECLARED
    the layer's countries and this area's country is not among them. The second is
    the one that matters: Thailand's bounding box contains most of Cambodia, so a
    Thai shelter list passes the bbox test for Battambang — and it was being clipped
    into an empty file there and cited as "0 of 0 shelters", a real-looking zero for
    a layer that never covered the place. The gate collected `countries` and `bbox`
    for exactly this and nothing read them. With neither a bbox nor a declaration
    we cannot tell, so the layer clips as before."""
    bbox = entry.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        minx, miny, maxx, maxy = boundary.bounds
        if bbox[2] < minx or bbox[0] > maxx or bbox[3] < miny or bbox[1] > maxy:
            where = (", ".join(str(c) for c in entry["countries"]) if entry.get("countries")
                     else f"lon {bbox[0]:.2f} to {bbox[2]:.2f}, lat {bbox[1]:.2f} to {bbox[3]:.2f}")
            return {"reason": f"its points lie wholly outside this area (the layer covers {where})",
                    "covers": where, "bbox": list(bbox), "countries": entry.get("countries")}
    declared = [str(c) for c in (entry.get("countries") or []) if str(c).strip()]
    if not country_matches(declared, country):
        where = ", ".join(declared)
        return {"reason": f"it declares {where} and this area is in {country}",
                "covers": where, "bbox": list(bbox) if bbox else None, "countries": declared,
                "area_country": country}
    return None


def clip_into(adir: str, boundary, layer: str, entry: dict) -> int:
    """Write {adir}/{layer}.geojson holding the master's features inside `boundary`:
    points that fall in it; lines and polygons CLIPPED to it (the part inside, never
    the whole feature). Returns how many. A layer with nothing inside still writes
    an empty file, so a count of zero is a real zero, not a missing layer."""
    from shapely.geometry import mapping
    from shapely.prepared import prep
    src = master_path(entry)
    try:
        fc = json.load(open(src))
    except (OSError, ValueError):
        return 0
    keep = []
    minx, miny, maxx, maxy = boundary.bounds
    ready = prep(boundary)
    for ft in fc.get("features") or []:
        g = ft.get("geometry") or {}
        fam = FAMILIES.get(g.get("type"))
        if fam == "points" and g.get("type") == "Point":
            if boundary.contains(Point(g["coordinates"])):
                keep.append({"type": "Feature", "geometry": g,
                             "properties": ft.get("properties") or {}})
            continue
        if fam is None:
            continue
        try:
            geom = _valid(shape(g))
        except Exception:
            continue
        bx = geom.bounds
        if bx[2] < minx or bx[0] > maxx or bx[3] < miny or bx[1] > maxy:
            continue
        if not ready.intersects(geom):
            continue
        try:
            part = geom if ready.contains(geom) else geom.intersection(boundary)
        except Exception:
            continue
        if part.is_empty:
            continue
        keep.append({"type": "Feature", "geometry": mapping(part),
                     "properties": ft.get("properties") or {}})
    json.dump({"type": "FeatureCollection", "features": keep},
              open(os.path.join(adir, f"{layer}.geojson"), "w"))
    return len(keep)


def purge_clips(layer: str) -> int:
    """Per-AOI clips are cached by layer NAME; a layer that changes identity must not
    be served from an old clip."""
    from ...config import get_settings
    root = Path(get_settings().cache_dir)
    n = 0
    for clip in root.glob(f"*/{layer}.geojson"):
        if clip.parent.name == "vectors":
            continue
        try:
            clip.unlink()
            n += 1
        except OSError:
            pass
    return n


def _walk(coords, out):
    if not coords:
        return
    if isinstance(coords[0], (int, float)):
        out.append(coords)
        return
    for c in coords:
        _walk(c, out)


def inspect(raw: bytes) -> dict:
    """Parse a candidate GeoJSON and say what it is: how many features, of which
    geometry family (points | lines | polygons — one family per layer), their bbox
    and property names. Raises ValueError with the reason when it is not usable."""
    try:
        fc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"not valid JSON: {exc}") from None
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
        raise ValueError("must be a GeoJSON FeatureCollection")
    feats = fc.get("features")
    if not isinstance(feats, list) or not feats:
        raise ValueError("FeatureCollection has no features")
    crs = (fc.get("crs") or {}).get("properties", {}).get("name", "") if isinstance(fc.get("crs"), dict) else ""
    if crs and not any(tag in str(crs).upper() for tag in ("4326", "CRS84", "WGS84", "WGS 84")):
        raise ValueError(f"the file declares crs {crs!r}; GeoJSON must be EPSG:4326 (lon, lat) — "
                         "export it in WGS 84 and resubmit")
    types: dict = {}
    families: dict = {}
    pts: list = []
    for ft in feats:
        g = (ft or {}).get("geometry") or {}
        t = g.get("type")
        types[t] = types.get(t, 0) + 1
        fam = FAMILIES.get(t)
        if fam is None:
            raise ValueError(f"unsupported geometry {t!r}: features must be Point, LineString or "
                             f"Polygon (or their Multi forms); found {types}")
        families[fam] = families.get(fam, 0) + 1
        c = g.get("coordinates")
        if not isinstance(c, (list, tuple)) or not c:
            raise ValueError(f"a {t} has no coordinates")
        _walk(c, pts)
    if len(families) > 1:
        raise ValueError(f"one geometry family per layer — this file mixes {families}; split it "
                         "into one layer per family")
    for c in pts:
        if not (len(c) >= 2 and -180 <= float(c[0]) <= 180 and -90 <= float(c[1]) <= 90):
            raise ValueError("coordinates fall outside lon/lat range — the layer must be "
                             "EPSG:4326 (lon, lat); a projected file (UTM, CRTM05) must be "
                             "reprojected before contributing")
    xs = [float(c[0]) for c in pts]
    ys = [float(c[1]) for c in pts]
    props = sorted({k for ft in feats for k in ((ft.get("properties") or {}).keys())})
    return {"features": len(feats), "geometry": next(iter(families)),
            "types": types, "bbox": [min(xs), min(ys), max(xs), max(ys)],
            "properties": props[:30]}


# ------------------------------------------------------------------ measures
# Geodesic measures on the authalic sphere, no projection library needed: the
# same sphere stacks.py counts pixels on, so a polygon layer and a raster class
# measured over one boundary are comparable.

def _hav_km(a, b) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * R_KM * math.asin(math.sqrt(h))


def length_km(geom) -> float:
    """Length of a (Multi)LineString in km along the sphere."""
    total = 0.0
    parts = getattr(geom, "geoms", None) or [geom]
    for part in parts:
        if part.geom_type == "LineString":
            c = list(part.coords)
            total += sum(_hav_km(c[i], c[i + 1]) for i in range(len(c) - 1))
        elif part.geom_type == "Polygon":
            total += length_km(part.exterior)
        elif hasattr(part, "geoms"):
            total += length_km(part)
    return total


def _ring_km2(coords) -> float:
    """Spherical area of a lon/lat ring (Chamberlain & Duquette); sign dropped."""
    n = len(coords)
    if n < 3:
        return 0.0
    acc = 0.0
    for i in range(n):
        lon1, lat1 = math.radians(coords[i][0]), math.radians(coords[i][1])
        lon2, lat2 = math.radians(coords[(i + 1) % n][0]), math.radians(coords[(i + 1) % n][1])
        acc += (lon2 - lon1) * (2 + math.sin(lat1) + math.sin(lat2))
    return abs(acc) * R_KM ** 2 / 2


def area_km2(geom) -> float:
    """Area of a (Multi)Polygon in km² on the sphere, holes subtracted."""
    total = 0.0
    parts = getattr(geom, "geoms", None) or [geom]
    for part in parts:
        if part.geom_type == "Polygon":
            total += _ring_km2(list(part.exterior.coords))
            total -= sum(_ring_km2(list(r.coords)) for r in part.interiors)
        elif hasattr(part, "geoms"):
            total += area_km2(part)
    return max(total, 0.0)


def union_inside(entry: dict, area):
    """The dissolved union of a polygon layer's features inside `area` (shapely), or an
    empty geometry. Overlapping features count once — what 'inside the protected
    areas' means, as opposed to a sum over designations."""
    from shapely.geometry import GeometryCollection
    from shapely.prepared import prep
    fc = json.load(open(master_path(entry)))
    minx, miny, maxx, maxy = area.bounds
    ready = prep(area)
    parts = []
    for ft in fc.get("features") or []:
        g = ft.get("geometry") or {}
        if FAMILIES.get(g.get("type")) != "polygons":
            continue
        try:
            geom = _valid(shape(g))
        except Exception:
            continue
        bx = geom.bounds
        if bx[2] < minx or bx[0] > maxx or bx[3] < miny or bx[1] > maxy:
            continue
        if not ready.intersects(geom):
            continue
        try:
            parts.append(geom.intersection(area))
        except Exception:
            continue
    if not parts:
        return GeometryCollection()
    u = unary_union(parts)
    if u.geom_type == "GeometryCollection":
        polys = [g for g in u.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        u = unary_union(polys) if polys else GeometryCollection()
    return u


def _valid(geom):
    """A polygon a hub's GIS left invalid (a self-touching ring, nested shells) is
    repaired before it is clipped or measured — GEOS refuses to intersect it otherwise,
    and a layer would silently lose those features. Lines and points pass through."""
    if geom.geom_type in ("Polygon", "MultiPolygon") and not geom.is_valid:
        from shapely import make_valid
        fixed = make_valid(geom)
        if fixed.geom_type == "GeometryCollection":
            polys = [g for g in fixed.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
            fixed = unary_union(polys) if polys else fixed
        return fixed
    return geom


def family(entry: dict) -> str:
    return str(entry.get("geometry") or "points")


_TOP = 12          # groups and names stated per layer


def summarise(entry: dict, boundary, cap_features: int = 250_000) -> dict:
    """What a layer amounts to inside `boundary`: a count for points, km for lines,
    km² (and ha) for polygons — measured on the part of each feature INSIDE the
    boundary — with a breakdown by the contributor's `group_field` and the largest
    named features by `name_field`. Deterministic: the same master file over the
    same boundary gives the same `features_sha256`."""
    from shapely.prepared import prep
    fam = family(entry)
    src = master_path(entry)
    fc = json.load(open(src))
    feats = fc.get("features") or []
    minx, miny, maxx, maxy = boundary.bounds
    ready = prep(boundary)
    gkey, nkey = entry.get("group_field"), entry.get("name_field")
    groups: dict = {}
    named: list = []
    total = 0.0
    n_in = 0
    parts_for_union: list = []
    digest = hashlib.sha256()
    truncated = False
    skipped = 0
    for i, ft in enumerate(feats):
        if i >= cap_features:
            truncated = True
            break
        g = ft.get("geometry") or {}
        if FAMILIES.get(g.get("type")) != fam:
            continue
        try:
            geom = _valid(shape(g))
        except Exception:
            continue
        bx = geom.bounds
        if bx[2] < minx or bx[0] > maxx or bx[3] < miny or bx[1] > maxy:
            continue
        if fam == "points":
            if not boundary.contains(geom if geom.geom_type == "Point" else geom.representative_point()):
                continue
            measure = 1.0
        else:
            if not ready.intersects(geom):
                continue
            try:
                part = geom if ready.contains(geom) else geom.intersection(boundary)
            except Exception:
                skipped += 1
                continue
            if part.is_empty:
                continue
            measure = length_km(part) if fam == "lines" else area_km2(part)
            if measure <= 0:
                continue
            if fam == "polygons":
                parts_for_union.append(part)
        n_in += 1
        total += measure
        props = ft.get("properties") or {}
        if gkey:
            key = str(props.get(gkey)) if props.get(gkey) not in (None, "") else "(blank)"
            groups[key] = groups.get(key, 0.0) + measure
        if nkey and props.get(nkey) not in (None, ""):
            named.append((measure, str(props.get(nkey)).strip()))
        digest.update(f"{ft.get('id', i)}:{measure:.6f};".encode())
    unit = {"points": "features", "lines": "km", "polygons": "km2"}[fam]
    by_group = sorted(groups.items(), key=lambda kv: -kv[1])
    footprint = None
    if fam == "polygons" and len(parts_for_union) > 1:
        # Overlapping polygons (a Ramsar site over a national park) are each counted in
        # `total`; the footprint counts their shared ground once.
        try:
            footprint = area_km2(unary_union(parts_for_union))
        except Exception:
            footprint = None
    out = {"geometry": fam, "unit": unit, "features_inside": n_in,
           "total": round(total, 3) if fam != "points" else n_in,
           **({"total_ha": round(total * 100, 1)} if fam == "polygons" else {}),
           **({"footprint_km2": round(footprint, 3)} if footprint is not None
              and total - footprint > max(0.005 * total, 1e-6) else {}),
           "features_in_layer": len(feats), "features_sha256": digest.hexdigest(),
           "truncated": truncated, **({"skipped_unreadable": skipped} if skipped else {})}
    if gkey:
        out["group_field"] = gkey
        out["by_group"] = {k: (round(v, 3) if fam != "points" else int(v)) for k, v in by_group[:_TOP]}
        rest = sum(v for _, v in by_group[_TOP:])
        if rest:
            out["by_group_rest"] = {"groups": len(by_group) - _TOP,
                                    "total": round(rest, 3) if fam != "points" else int(rest)}
    if nkey:
        out["name_field"] = nkey
        named.sort(key=lambda t: (-t[0], t[1]))
        out["largest_named"] = [{"name": n, "measure": (round(m, 3) if fam != "points" else 1)}
                                for m, n in named[:_TOP]]
        out["unnamed"] = n_in - len(named)
    return out


# ------------------------------------------------------------------ gazetteer
def _norm(text) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return " ".join(t.casefold().replace("-", " ").replace("_", " ").split())


_NAME_INDEX: dict = {}       # layer -> (mtime, size, {normalised name: [(label, geometry dict)]})


def _name_index(layer: str, entry: dict) -> dict:
    """{normalised name: [(label, geometry)]} for one polygon layer, built once per
    master file and rebuilt when the file changes — a place lookup must not parse
    40 MB of GeoJSON on every question."""
    path = master_path(entry)
    try:
        st = os.stat(path)
    except OSError:
        return {}
    cached = _NAME_INDEX.get(layer)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    index: dict = {}
    key = entry["name_field"]
    try:
        fc = json.load(open(path))
    except (OSError, ValueError):
        return {}
    for ft in fc.get("features") or []:
        props = ft.get("properties") or {}
        label = props.get(key)
        if label in (None, ""):
            continue
        index.setdefault(_norm(label), []).append((str(label).strip(), ft.get("geometry")))
    _NAME_INDEX[layer] = (st.st_mtime, st.st_size, index)
    return index


def named_area(name: str) -> dict | None:
    """A contributed polygon feature (or all features sharing a name) whose name
    matches `name`: {"geometry": shapely, "label", "layer", "entry", "how", "matches"}.
    Exact matches win; else one feature whose name contains the query (or the query
    contains it) — several such are refused as ambiguous, in words. None when no
    contributed polygon layer knows the name."""
    q = _norm(name)
    if not q or len(q) < 3:
        return None
    exact, partial = [], []
    for layer, entry in visible().items():
        if family(entry) != "polygons" or not entry.get("name_field"):
            continue
        for n, items in _name_index(layer, entry).items():
            if n == q:
                exact.extend((layer, entry, label, {"geometry": g}) for label, g in items)
            elif len(q) >= 5 and (q in n or n in q):
                partial.extend((layer, entry, label, {"geometry": g}) for label, g in items)
    hits = exact or partial
    if not hits:
        return None
    labels = sorted({(h[0], h[2]) for h in hits})
    if not exact and len(labels) > 1:
        return {"ambiguous": True, "matches": [f"{lab} ({lay})" for lay, lab in labels[:8]]}
    if len(labels) > 1:                       # the same exact name in two layers
        first_layer = hits[0][0]
        hits = [h for h in hits if h[0] == first_layer]
        labels = sorted({(h[0], h[2]) for h in hits})
    layer, entry, label, _ = hits[0]
    try:
        geom = unary_union([shape(h[3]["geometry"]) for h in hits])
    except Exception:
        return None
    if geom.is_empty:
        return None
    how = (f"contributed boundary: {label} from {entry.get('title') or layer} ({layer}; "
           f"{entry.get('source') or 'source unstated'}, vintage {entry.get('vintage') or 'unstated'})"
           + (f"; {len(hits)} features share this name and were merged" if len(hits) > 1 else "")
           + ("" if exact else f" — matched '{name}' by part of the name"))
    return {"geometry": geom, "label": label, "layer": layer, "entry": entry, "how": how,
            "matches": len(hits), "ambiguous": False}


def names(limit: int = 20) -> dict:
    """{layer: [some names]} for the polygon layers that can serve as places."""
    out = {}
    for layer, entry in visible().items():
        if family(entry) != "polygons" or not entry.get("name_field"):
            continue
        seen = []
        for items in _name_index(layer, entry).values():
            for label, _g in items:
                if label not in seen:
                    seen.append(label)
                if len(seen) >= limit:
                    break
            if len(seen) >= limit:
                break
        out[layer] = seen
    return out
