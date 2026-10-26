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
import threading
import unicodedata
from collections import Counter, OrderedDict
from pathlib import Path

import numpy as np
from shapely.geometry import shape
from shapely.ops import unary_union

from ...mcp.budget import once

# WGS 84. Areas are computed on (lon, AUTHALIC latitude) with the authalic radius —
# the sphere of the ellipsoid's own area, reached by an equal-area map — so they are
# the ellipsoid's areas, not the sphere's (which run +0.41% at 10° N); stacks.py
# counts pixel areas the same way, so a polygon layer and a raster class measured
# over one boundary are comparable.
A_M = 6378137.0
E2 = 0.00669437999014
_E = math.sqrt(E2)


def _q(phi):
    s = np.sin(phi)
    return (1 - E2) * (s / (1 - E2 * s * s) - (1 / (2 * _E)) * np.log((1 - _E * s) / (1 + _E * s)))


_QP = float(_q(math.pi / 2))
R_Q_KM = A_M * math.sqrt(_QP / 2) / 1000.0       # the authalic radius, 6371.0072 km

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
    from .ingest import write_json
    write_json(_dir() / "registry.json", reg, indent=2)


# register/unregister read, change and rewrite registry.json: two approvals landing at
# once each read the old file, and the second write dropped the first one's layer.
_REGISTRY_LOCK = threading.RLock()


def register(layer: str, entry: dict) -> None:
    with _REGISTRY_LOCK:
        reg = landed()
        reg[layer] = entry
        _save(reg)


def unregister(layer: str) -> None:
    with _REGISTRY_LOCK:
        reg = landed()
        if reg.pop(layer, None) is not None:
            _save(reg)


@once
def visible() -> dict:
    """Landed layers plus the staged ones THIS caller may see. Same visibility rule
    as staged rasters: contributor and reviewers, nobody else. Read once per snapshot."""
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


country_key = _country_key        # one country however it is written (Viet Nam / Vietnam)


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

    Two tests, cheapest first. (1) The layer's features lie wholly outside the area's
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
            return {"reason": f"its features lie wholly outside this area (the layer covers {where})",
                    "covers": where, "bbox": list(bbox), "countries": entry.get("countries")}
    declared = [str(c) for c in (entry.get("countries") or []) if str(c).strip()]
    if not country_matches(declared, country):
        where = ", ".join(declared)
        return {"reason": f"it declares {where} and this area is in {country}",
                "covers": where, "bbox": list(bbox) if bbox else None, "countries": declared,
                "area_country": country}
    return None


_LAYERS: OrderedDict = OrderedDict()     # master path -> (mtime, size, parsed layer)
_LAYERS_COORDS = 6_000_000                # coordinates kept parsed across layers (the latest always is)
_CACHE_LOCK = threading.Lock()            # _LAYERS and _UNIONS, shared by concurrent requests


def _layer(entry: dict) -> dict:
    """A layer's master file parsed once and reused until the file changes: each
    feature's properties and id, its shapely geometry (a polygon repaired, None when
    unreadable) and an STRtree over them. Every measure — summarise, clip_into,
    union_inside, the gazetteer — reads it here, so a national map is not re-parsed
    on every question and only the features near an area are ever looked at. The
    most recently used layers stay parsed, within a budget of coordinates."""
    import shapely
    path = master_path(entry)
    st = os.stat(path)
    key = str(path)
    with _CACHE_LOCK:
        hit = _LAYERS.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            _LAYERS.move_to_end(key)
            return hit[2]
    fc = json.load(open(path))
    feats = fc.get("features") or []
    geoms, props, ids, unreadable = [], [], [], 0
    for i, ft in enumerate(feats):
        ft = ft if isinstance(ft, dict) else {}
        g = ft.get("geometry") or {}
        props.append(ft.get("properties") or {})
        ids.append(ft.get("id", i))
        geom = None
        if FAMILIES.get(g.get("type")) is not None:
            try:
                geom = _valid(shape(g))
                if geom.is_empty or FAMILIES.get(geom.geom_type) != FAMILIES[g["type"]]:
                    geom = None
            except Exception:
                geom = None
            unreadable += geom is None
        geoms.append(geom)
    arr = np.array(geoms, dtype=object)
    lay = {"geoms": arr, "props": props, "ids": ids, "n": len(feats), "unreadable": unreadable,
           "tree": shapely.STRtree(arr),
           "coords": int(shapely.get_num_coordinates(arr).sum()) if len(arr) else 0}
    with _CACHE_LOCK:
        _LAYERS[key] = (st.st_mtime, st.st_size, lay)
        _LAYERS.move_to_end(key)
        while len(_LAYERS) > 1 and sum(v[2]["coords"] for v in _LAYERS.values()) > _LAYERS_COORDS:
            _LAYERS.popitem(last=False)
    return lay


def _near(lay: dict, area, cap: int | None = None) -> list[int]:
    """Indices of the features whose envelope meets `area`'s, in file order."""
    idx = sorted(int(i) for i in lay["tree"].query(area))
    return [i for i in idx if i < cap] if cap is not None else idx


class _Clip:
    """`area` ready for clipping many features: prepared for the predicates, and —
    when it is many polygons (a dissolved protected-area network, a mangrove map) —
    each feature is intersected only with the parts near it, not the whole area
    (a `within=` area over a national map is ~200 parts and 250,000 vertices)."""

    def __init__(self, area):
        from shapely.prepared import prep
        import shapely
        self.area = area
        self.ready = prep(area)
        parts = list(area.geoms) if area.geom_type == "MultiPolygon" else []
        self.parts = np.array(parts, dtype=object) if len(parts) > 8 else None
        self.tree = shapely.STRtree(self.parts) if self.parts is not None else None

    def inside(self, geom):
        """The part of `geom` inside the area (the whole of it when wholly inside)."""
        from shapely.geometry import MultiPolygon
        if self.ready.contains(geom):
            return geom
        near = self.tree.query(geom) if self.tree is not None else None
        if near is None or not len(near):
            return geom.intersection(self.area)
        return geom.intersection(self.parts[near[0]] if len(near) == 1
                                 else MultiPolygon(list(self.parts[near])))


def clip_into(adir: str, boundary, layer: str, entry: dict) -> int:
    """Write {adir}/{layer}.geojson holding the master's features inside `boundary`:
    points that fall in it; lines and polygons CLIPPED to it (the part inside, never
    the whole feature). Returns how many. A layer with nothing inside still writes
    an empty file, so a count of zero is a real zero, not a missing layer."""
    from shapely.geometry import mapping
    try:
        lay = _layer(entry)
    except (OSError, ValueError):
        return 0
    keep = []
    clip = _Clip(boundary)
    for i in _near(lay, boundary):
        geom = lay["geoms"][i]
        if geom is None:
            continue
        if geom.geom_type == "Point":
            if clip.ready.contains(geom):
                keep.append({"type": "Feature", "geometry": mapping(geom), "properties": lay["props"][i]})
            continue
        if not clip.ready.intersects(geom):
            continue
        try:
            part = clip.inside(geom)
        except Exception:
            continue
        if part.is_empty:
            continue
        keep.append({"type": "Feature", "geometry": mapping(part), "properties": lay["props"][i]})
    from .ingest import write_json
    write_json(os.path.join(adir, f"{layer}.geojson"), {"type": "FeatureCollection", "features": keep})
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
    # Counted over the whole file before anything is refused, so a message states the
    # layer as it is, not as far as the first bad feature. A feature without geometry
    # is set aside (the gate's reader drops it and says so), never the whole layer.
    types: dict = {}
    families: dict = {}
    pts: list = []
    located, without = [], 0
    for ft in feats:
        g = ft.get("geometry") if isinstance(ft, dict) else None
        t = g.get("type") if isinstance(g, dict) else None
        if t is None or (t in FAMILIES and not g.get("coordinates")):
            without += 1
            continue
        types[t] = types.get(t, 0) + 1
        fam = FAMILIES.get(t)
        if fam is not None:
            families[fam] = families.get(fam, 0) + 1
            located.append(ft)
    bad = {t: n for t, n in types.items() if FAMILIES.get(t) is None}
    if bad:
        raise ValueError(f"unsupported geometry {', '.join(repr(t) for t in bad)}: features must be "
                         f"Point, LineString or Polygon (or their Multi forms); the file's "
                         f"{len(feats):,} features hold {types}"
                         + (f" and {without:,} without geometry" if without else ""))
    if not located:
        raise ValueError(f"none of the file's {len(feats):,} features has a geometry")
    if len(families) > 1:
        raise ValueError(f"one geometry family per layer — this file mixes {families}; split it "
                         "into one layer per family")
    for ft in located:
        _walk(ft["geometry"]["coordinates"], pts)
    xs = [float(c[0]) for c in pts if len(c) >= 2]
    ys = [float(c[1]) for c in pts if len(c) >= 2]
    if not (xs and -180 <= min(xs) and max(xs) <= 180 and -90 <= min(ys) and max(ys) <= 90):
        span = (f" (x {min(xs):,.0f} to {max(xs):,.0f}, y {min(ys):,.0f} to {max(ys):,.0f})"
                if xs else "")
        raise ValueError(f"coordinates fall outside lon/lat range{span} — a GeoJSON with no crs "
                         "member is EPSG:4326 (lon, lat) by specification; a projected layer (UTM, "
                         "CRTM05) must be exported in WGS 84, or given as a zipped shapefile with its "
                         ".prj, which is reprojected at ingest")
    props = sorted({k for ft in located for k in ((ft.get("properties") or {}).keys())})
    return {"features": len(located), "geometry": next(iter(families)),
            "types": types, "bbox": [min(xs), min(ys), max(xs), max(ys)],
            "properties": props[:30], **({"without_geometry": without} if without else {})}


# ------------------------------------------------------------------ measures
# Measures on the WGS 84 ellipsoid, no projection library needed. Areas: the
# cylindrical equal-area sum on (lon, authalic latitude) with the authalic radius,
# exact for the ellipsoid. Lengths: each segment on the ellipsoid's local radii at
# its mid-latitude (meridional and prime-vertical), exact as segments shorten.

def _segments_km(coords) -> float:
    c = np.asarray(coords, dtype=float)
    if len(c) < 2:
        return 0.0
    lon, lat = np.radians(c[:, 0]), np.radians(c[:, 1])
    mid = (lat[1:] + lat[:-1]) / 2
    w = 1 - E2 * np.sin(mid) ** 2
    n_rad = A_M / np.sqrt(w)                       # prime vertical
    m_rad = A_M * (1 - E2) / w ** 1.5              # meridian
    dx = n_rad * np.cos(mid) * np.diff(lon)
    dy = m_rad * np.diff(lat)
    return float(np.hypot(dx, dy).sum()) / 1000.0


def length_km(geom) -> float:
    """Length of a (Multi)LineString in km on the ellipsoid."""
    total = 0.0
    parts = getattr(geom, "geoms", None) or [geom]
    for part in parts:
        if part.geom_type == "LineString":
            total += _segments_km(part.coords)
        elif part.geom_type == "Polygon":
            total += length_km(part.exterior)
        elif part.geom_type == "LinearRing":
            total += _segments_km(part.coords)
        elif hasattr(part, "geoms"):
            total += length_km(part)
    return total


def _ring_km2(coords) -> float:
    """Ellipsoidal area of a lon/lat ring; sign dropped."""
    c = np.asarray(coords, dtype=float)
    if len(c) < 3:
        return 0.0
    lon = np.radians(c[:, 0])
    sb = _q(np.radians(c[:, 1])) / _QP             # sin(authalic latitude)
    lon2, sb2 = np.roll(lon, -1), np.roll(sb, -1)
    acc = float(np.sum((lon2 - lon) * (2 + sb + sb2)))
    return abs(acc) * R_Q_KM ** 2 / 2


def area_km2(geom) -> float:
    """Area of a (Multi)Polygon in km² on the ellipsoid, holes subtracted."""
    total = 0.0
    parts = getattr(geom, "geoms", None) or [geom]
    for part in parts:
        if part.geom_type == "Polygon":
            total += _ring_km2(part.exterior.coords)
            total -= sum(_ring_km2(r.coords) for r in part.interiors)
        elif hasattr(part, "geoms"):
            total += area_km2(part)
    return max(total, 0.0)


_UNIONS: OrderedDict = OrderedDict()       # (master, mtime, size, area wkb sha) -> union
_UNIONS_MAX = 24


def union_inside(entry: dict, area):
    """The dissolved union of a polygon layer's features inside `area` (shapely), or an
    empty geometry. Overlapping features count once — what 'inside the protected
    areas' means, as opposed to a sum over designations. Only the features near the
    area are clipped; the result is kept per (layer file, area), so asking again over
    the same place does not redo it."""
    from shapely.geometry import GeometryCollection
    path = master_path(entry)
    st = os.stat(path)
    key = (str(path), st.st_mtime, st.st_size, hashlib.sha256(area.wkb).hexdigest())
    with _CACHE_LOCK:
        if key in _UNIONS:
            _UNIONS.move_to_end(key)
            return _UNIONS[key]
    lay = _layer(entry)
    clip = _Clip(area)
    parts = []
    for i in _near(lay, area):
        geom = lay["geoms"][i]
        if geom is None or FAMILIES.get(geom.geom_type) != "polygons":
            continue
        if not clip.ready.intersects(geom):
            continue
        try:
            parts.append(clip.inside(geom))
        except Exception:
            continue
    u = GeometryCollection()
    if parts:
        u = unary_union(parts)
        if u.geom_type == "GeometryCollection":
            polys = [g for g in u.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
            u = unary_union(polys) if polys else GeometryCollection()
    with _CACHE_LOCK:
        _UNIONS[key] = u
        while len(_UNIONS) > _UNIONS_MAX:
            _UNIONS.popitem(last=False)
    return u


def _valid(geom):
    """A polygon a hub's GIS left invalid (a self-touching ring, nested shells) is
    repaired before it is clipped or measured — GEOS refuses to intersect it otherwise,
    and a layer would silently lose those features. The gate repairs at ingest
    (vecio.repair); this catches a layer stored before it did. An empty geometry when
    nothing with area is left. Lines and points pass through."""
    if geom.geom_type in ("Polygon", "MultiPolygon") and not geom.is_valid:
        from shapely.geometry import GeometryCollection
        from . import vecio
        fixed = vecio.repair(geom)
        return fixed if fixed is not None else GeometryCollection()
    return geom


def family(entry: dict) -> str:
    return str(entry.get("geometry") or "points")


_TOP = 12          # groups and names stated per layer
MEASURE = "wgs84-ellipsoid"   # the method every record carries; a replay of an older one says so


def summarise(entry: dict, boundary, cap_features: int = 250_000) -> dict:
    """What a layer amounts to inside `boundary`: a count for points, km for lines,
    km² (and ha) for polygons — measured on the part of each feature INSIDE the
    boundary — with a breakdown by the contributor's `group_field`; by `name_field`,
    the largest named lines and polygons, or for points the names that recur most
    (a point has no size). Deterministic: the same master file over the same
    boundary gives the same `features_sha256`."""
    fam = family(entry)
    lay = _layer(entry)
    clip = _Clip(boundary)
    gkey, nkey = entry.get("group_field"), entry.get("name_field")
    groups: dict = {}
    named: list = []
    total = 0.0
    n_in = 0
    parts_for_union: list = []
    digest = hashlib.sha256()
    truncated = lay["n"] > cap_features
    skipped = 0
    for i in _near(lay, boundary, cap_features):
        geom = lay["geoms"][i]
        if geom is None or FAMILIES.get(geom.geom_type) != fam:
            continue
        if fam == "points":
            if not clip.ready.contains(geom if geom.geom_type == "Point" else geom.representative_point()):
                continue
            measure = 1.0
        else:
            if not clip.ready.intersects(geom):
                continue
            try:
                part = clip.inside(geom)
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
        props = lay["props"][i]
        if gkey:
            key = str(props.get(gkey)) if props.get(gkey) not in (None, "") else "(blank)"
            groups[key] = groups.get(key, 0.0) + measure
        if nkey and props.get(nkey) not in (None, ""):
            named.append((measure, str(props.get(nkey)).strip()))
        digest.update(f"{lay['ids'][i]}:{measure:.6f};".encode())
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
    out = {"geometry": fam, "unit": unit, "measure": MEASURE, "features_inside": n_in,
           "total": round(total, 3) if fam != "points" else n_in,
           **({"total_ha": round(total * 100, 1)} if fam == "polygons" else {}),
           **({"footprint_km2": round(footprint, 3)} if footprint is not None
              and total - footprint > max(0.005 * total, 1e-6) else {}),
           "features_in_layer": lay["n"], "features_sha256": digest.hexdigest(),
           "truncated": truncated, **({"skipped_unreadable": skipped} if skipped else {}),
           **({"unreadable_in_layer": lay["unreadable"]} if lay["unreadable"] else {})}
    if gkey:
        out["group_field"] = gkey
        out["by_group"] = {k: (round(v, 3) if fam != "points" else int(v)) for k, v in by_group[:_TOP]}
        rest = sum(v for _, v in by_group[_TOP:])
        if rest:
            out["by_group_rest"] = {"groups": len(by_group) - _TOP,
                                    "total": round(rest, 3) if fam != "points" else int(rest)}
    if nkey:
        out["name_field"] = nkey
        if fam == "points":
            counts = sorted(Counter(n for _, n in named).items(), key=lambda kv: (-kv[1], kv[0]))
            out["most_frequent_named"] = [{"name": n, "count": c} for n, c in counts[:_TOP]]
            out["distinct_names"] = len(counts)
        else:
            named.sort(key=lambda t: (-t[0], t[1]))
            out["largest_named"] = [{"name": n, "measure": round(m, 3)} for m, n in named[:_TOP]]
        out["unnamed"] = n_in - len(named)
    return out


# ------------------------------------------------------------------ gazetteer
def _norm(text) -> str:
    t = unicodedata.normalize("NFKD", str(text or ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return " ".join(t.casefold().replace("-", " ").replace("_", " ").split())


_NAME_INDEX: dict = {}       # layer -> (mtime, size, {normalised name: [(label, shapely geometry)]})


def _name_index(layer: str, entry: dict) -> dict:
    """{normalised name: [(label, geometry)]} for one polygon layer, built once per
    master file and rebuilt when the file changes — a place lookup must not parse
    40 MB of GeoJSON on every question. The geometries are the parsed layer's own,
    repaired, so a merged boundary is never built from an invalid ring."""
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
        lay = _layer(entry)
    except (OSError, ValueError):
        return {}
    for props, geom in zip(lay["props"], lay["geoms"]):
        label = props.get(key)
        if label in (None, "") or geom is None:
            continue
        index.setdefault(_norm(label), []).append((str(label).strip(), geom))
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
        geom = unary_union([h[3]["geometry"] for h in hits])
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
