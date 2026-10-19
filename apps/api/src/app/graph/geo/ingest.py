"""Resolve a place name to real, cached data: OSM boundary + roads + POIs, and a
flood-hazard raster clipped to it. Raises ValueError if the place can't be
resolved or is too large — it never silently falls back to somewhere else.
"""
import contextvars
import hashlib
import json
import math
import os
import re
import time
import unicodedata
import warnings

warnings.filterwarnings("ignore")
import rasterio
import requests
from rasterio.windows import from_bounds
from shapely.geometry import LineString, Point, box, mapping, shape

from ...config import get_settings
from . import drive_tifs, tiffs

HEADERS = {"User-Agent": "grp-mvp/0.1 (disaster-risk research prototype)"}
NOMINATIM = "https://nominatim.openstreetmap.org"
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
# Two different caps, because two different things cost. Raster work over a province
# is trivial — 12,000 km² of 1 km flood grid is ~150x150 cells. Counting OSM assets
# over the same area is not: buildings and road geometry run to hundreds of thousands
# of features and stall Overpass. Capping BOTH at the asset budget is why a provincial
# planner was told their province had zero flood exposure: the analysed 115 km² urban
# core is dry in the JRC layers while the floodplain around it — the part they plan
# for — carries class 5. Measured 2026-09-21: 288/288 cells class 0 over the
# municipality, 4,997 flooded cells including 730 at class 5 over the province.
AREA_CAP_KM2 = 25000.0          # how large an AREA we will analyse at all
ASSET_CAP_KM2 = 1500.0          # above this, heavy OSM layers are declined, not fetched
# Which asset layers are cheap enough to fetch over a large area. Points are small;
# building footprints and full road geometry are not.
LIGHT_ASSET_LAYERS = ("hospitals", "schools")
BUFFER_DEG = 0.01
RADIUS_KM = 12.0          # fallback AOI: box of this radius around the centre point
NEAR_KM = 100.0           # contributed areas offered beside an approximate box lie this close
# Bump when place resolution changes meaning. Cached areas of interest stamped with an
# older value are re-resolved on next use rather than served from a stale boundary.
RESOLVER_VERSION = 8
ASSET_LAYERS = ("roads", "hospitals", "schools", "buildings")
OVERPASS_TIMEOUT = 60          # client HTTP timeout (was 180) — fail over a stalled mirror fast
OVERPASS_SERVER_TIMEOUT = 55   # Overpass server-side [timeout:] budget per query

_ACTIVE_COLLECTOR: contextvars.ContextVar = contextvars.ContextVar("io_events_collector", default=None)


class IOCollector:
    """Accumulates {kind, ...} io events for one fetch() call."""

    def __init__(self):
        self.events: list[dict] = []

    def record(self, event: dict) -> None:
        "Append one io event."
        self.events.append(event)

    def drain(self) -> list[dict]:
        "Return the accumulated events and clear them."
        events, self.events = self.events, []
        return events


def install(collector: IOCollector):
    "Make `collector` active for this context; returns a token for uninstall()."
    return _ACTIVE_COLLECTOR.set(collector)


def uninstall(token) -> None:
    "Undo install(), restoring whatever collector (if any) was active before."
    _ACTIVE_COLLECTOR.reset(token)


def emit(event: dict) -> None:
    "Record one io event on the active collector, if any (else a no-op)."
    collector = _ACTIVE_COLLECTOR.get()
    if collector is not None:
        collector.record(event)


def short_path(path: str) -> str:
    """A cache-relative path for the trace - 'battambang/roads.geojson', not the machine
    layout it happens to sit in.

    Falls back to the basename if the path is outside cache_dir, which relpath would 
    otherwise render as a chain of '..'.
    """
    cache_dir = get_settings().cache_dir
    try:
        relative = os.path.relpath(path, cache_dir)
    except ValueError:                       # different drive on Windows
        return os.path.basename(path)
    return os.path.basename(path) if relative.startswith("..") else relative


def _slug(place):
    return re.sub(r"[^a-z0-9]+", "-", place.lower()).strip("-")


def _est_km2(geom):
    """Planar area estimate in km² for a lon/lat shape, scaled by the cosine of its
    latitude. The old constants (111 x 108) were tuned to ~13 N and overstated a
    Himalayan province by ~9%."""
    lat = geom.centroid.y if not geom.is_empty else 0.0
    return geom.area * 111.32 * 111.32 * max(math.cos(math.radians(lat)), 0.05)


# Boundaries a question can be about. OpenStreetMap tags parks and reserves as their
# own boundary types; a land-cover question about a national park needs the park.
_BOUNDARY_KINDS = {"administrative": "admin boundary",
                   "national_park": "protected-area boundary",
                   "protected_area": "protected-area boundary"}


def _boundary_kind(d):
    gj = d.get("geojson", {})
    if gj.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    if d.get("class") == "boundary":
        return _BOUNDARY_KINDS.get(d.get("type"))
    if d.get("class") == "leisure" and d.get("type") == "nature_reserve":
        return "protected-area boundary"
    return None


# ------------------------------------------------------------------ contributed gazetteer
# A contributed polygon layer whose features carry names is a GAZETTEER: "Humedal
# Nacional Térraba-Sierpe" resolves to the hub's own boundary, not a geocoder's box.
# The mangrove sprint's dry run showed what a loose one does to places. Matching by
# part of a name made "Gulf of Nicoya" a canton, "Bragança, Pará, Brazil" a 3 km²
# district in Heredia and "Térraba-Sierpe" the Sierpe district; merging every feature
# of a name made "Colorado" one area of two districts 300 km apart. So: a feature is
# used only when its WHOLE name is the asked one; features merge only when they are
# parts of one area; different areas sharing a name are listed in forms that resolve,
# never picked; and a parent or country named after a comma must hold the feature.

_GAZETTEER: dict = {}      # layer -> (stamp, {key: [area]}); the stamp moves with the master file
# Properties that MEASURE a feature rather than say which area it is. A shapefile
# repeats them per part (SICAP's AREA_ASP, WDPA's GIS_AREA, the COD's area_sqkm and
# center_lat) and a row id differs per part, so two parts of one park differ only here.
_MEASURE_WORDS = {"area", "are", "shape", "len", "leng", "length", "perim", "perimeter",
                  "perimetro", "sqkm", "km2", "ha", "hectares", "hectareas", "acres",
                  "superficie", "center", "centre", "centroid", "lat", "lon", "latitude",
                  "longitude", "x", "y", "objectid", "fid", "oid", "gid"}
_QUALIFIED = re.compile(r"^(?P<name>[^()]+?)\s*\((?P<inner>[^()]*)\)\s*(?P<rest>,.*)?$")
_SHOWN = 15                # candidates listed in one decline


class AmbiguousPlace(ValueError):
    """A contributed-area name that must be restated — several areas share it, or the
    layer it names does not hold it. `candidates` are forms that resolve."""

    def __init__(self, message, candidates):
        super().__init__(message)
        self.candidates = candidates


def _key(text) -> str:
    """A name as compared: case, accents and punctuation ignored, a hyphen the same as
    a space — 'Térraba-Sierpe', 'terraba sierpe' and 'TERRABA SIERPE.' are one key."""
    t = unicodedata.normalize("NFKD", str(text if text is not None else ""))
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).casefold()
    t = re.sub(r"['’`]", "", t)
    return " ".join(re.sub(r"[\W_]+", " ", t).split())


def _is_measure(field) -> bool:
    words = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(field)).lower()
    return any(w in _MEASURE_WORDS or w.startswith(("area", "shape"))
               for w in re.split(r"[^a-z0-9]+", words) if w)


def _km(a, b) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def _gazetteer_layers() -> dict:
    """{layer: entry} for the visible polygon layers whose features carry names."""
    from . import vectors as _vectors
    return {layer: entry for layer, entry in _vectors.visible().items()
            if _vectors.family(entry) == "polygons" and entry.get("name_field")}


def _gazetteer(layer, entry) -> dict:
    """{key: [area]} for one named polygon layer, built once per master file and rebuilt
    when the file or its name field changes — a place lookup must not parse 40 MB of
    GeoJSON on every question. Features sharing a name AND every attribute but their
    measures are parts of ONE area; the same name with other attributes is another
    area (a Ramsar site over the park of the same name, a district in another canton)."""
    from . import vectors as _vectors
    path = _vectors.master_path(entry)
    try:
        st = os.stat(path)
    except OSError:
        return {}
    field = entry["name_field"]
    stamp = (str(path), st.st_mtime, st.st_size, field)
    cached = _GAZETTEER.get(layer)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        with open(path) as f:
            fc = json.load(f)
    except (OSError, ValueError):
        return {}
    index, by_sig = {}, {}
    for ft in fc.get("features") or []:
        props, g = ft.get("properties") or {}, ft.get("geometry") or {}
        k = _key(props.get(field))
        if not k or g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        attrs = {f: v for f, v in props.items()
                 if f != field and v not in (None, "") and not _is_measure(f)}
        sig = (k, json.dumps(attrs, sort_keys=True, default=str))
        area = by_sig.get(sig)
        if area is None:
            area = by_sig[sig] = {"layer": layer, "key": k, "label": str(props[field]).strip(),
                                  "attrs": attrs, "geoms": [], "bbox": None}
            index.setdefault(k, []).append(area)
        area["geoms"].append(g)
        try:
            b = shape(g).bounds
        except Exception:
            continue
        a = area["bbox"]
        area["bbox"] = b if a is None else (min(a[0], b[0]), min(a[1], b[1]),
                                            max(a[2], b[2]), max(a[3], b[3]))
    _GAZETTEER[layer] = (stamp, index)
    return index


def _area_shape(area):
    """One area's parts as one shapely geometry (repaired, unioned), kept on the area."""
    if "_shape" not in area:
        from shapely import make_valid
        from shapely.ops import unary_union
        parts = []
        for g in area["geoms"]:
            try:
                s = shape(g)
                s = s if s.is_valid else make_valid(s)
            except Exception:
                continue
            parts.extend(p for p in getattr(s, "geoms", [s])
                         if p.geom_type in ("Polygon", "MultiPolygon"))
        u = unary_union(parts) if parts else None
        area["_shape"] = None if u is None or u.is_empty else u
    return area["_shape"]


def _qualifiers(areas, entry) -> dict:
    """id(area) -> the words that tell it from the other areas of its name in its layer:
    the layer's group field when that separates them all (a WDPA designation, a
    district's canton), else the first attribute that does, words before codes; when
    no single one does, as few attributes as do, joined."""
    if len(areas) < 2:
        return {}

    def val(a, f):
        v = a["attrs"].get(f)
        return None if v in (None, "") else str(v).strip()

    fields = []
    for a in areas:
        fields.extend(f for f in a["attrs"] if f not in fields)
    group = entry.get("group_field")
    words = [f for f in fields if not any(ch.isdigit() for a in areas for ch in (val(a, f) or ""))]
    order = (([group] if group in fields else []) + [f for f in words if f != group]
             + [f for f in fields if f != group and f not in words])
    for f in order:
        vals = [val(a, f) for a in areas]
        if all(vals) and len({_key(v) for v in vals}) == len(areas):
            return {id(a): v for a, v in zip(areas, vals)}
    chosen = []
    for f in order:
        seen = len({tuple(_key(val(a, c)) for c in chosen) for a in areas})
        if len({tuple(_key(val(a, c)) for c in chosen + [f]) for a in areas}) > seen:
            chosen.append(f)
            if len({tuple(_key(val(a, c)) for c in chosen) for a in areas}) == len(areas):
                break
    return {id(a): " / ".join(val(a, f) or "-" for f in chosen) for a in areas}


def _forms(areas, layers) -> dict:
    """id(area) -> (form, qualifier): the form that names exactly this area —
    'name (layer)', or 'name (layer: qualifier)' when its layer holds other areas of
    the name. Every decline offers these, and every one of them resolves."""
    out = {}
    for a in areas:
        if id(a) in out:
            continue
        entry = layers[a["layer"]]
        group = _gazetteer(a["layer"], entry).get(a["key"]) or [a]
        quals = _qualifiers(group, entry)
        for b in group:
            q = quals.get(id(b))
            out[id(b)] = (f"{b['label']} ({b['layer']}: {q})" if q else f"{b['label']} ({b['layer']})", q)
    return out


def _areas_named(k, layers) -> list:
    return [a for layer, entry in layers.items() for a in _gazetteer(layer, entry).get(k, [])]


def _parse(place, layers):
    """'Colorado (cr_cod_admin3_districts: Abangares), Costa Rica' -> (name, layer,
    qualifier, [later parts]). A bracket that names no gazetteer layer is a qualifier
    like a comma part: 'Colorado (Abangares)' reads as 'Colorado, Abangares'."""
    s = " ".join(str(place or "").split())
    m = _QUALIFIED.match(s)
    if not m:
        parts = [p.strip() for p in s.split(",")]
        return parts[0], None, None, [p for p in parts[1:] if p]
    head = [p.strip() for p in m.group("name").split(",")]
    rest = [p.strip() for p in (m.group("rest") or "").split(",") if p.strip()]
    inner = m.group("inner").strip()
    lay, colon, qual = inner.partition(":")
    if lay.strip().lower() in layers:
        return head[0], lay.strip().lower(), (qual.strip() or None) if colon else None, head[1:] + rest
    return head[0], None, None, [p for p in head[1:] if p] + ([inner] if inner else []) + rest


def _place_shape(part):
    """(boundary, country name or None) the geocoder gives a place named after a comma;
    (None, None) when it gives no polygon or cannot be reached."""
    try:
        results = _search(part)
    except Exception:
        return None, None
    best = None
    for d in results:
        gj = d.get("geojson") or {}
        if gj.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            g = shape(gj)
        except Exception:
            continue
        if best is None or g.area > best[0].area:
            best = (g, d)
    if best is None:
        return None, None
    g, d = best
    try:
        rank = int(d.get("place_rank") or 99)
    except (TypeError, ValueError):
        rank = 99
    is_country = d.get("addresstype") == "country" or (_boundary_kind(d) == "admin boundary" and rank <= 4)
    return g, (str(d.get("display_name") or part).split(",")[0].strip() if is_country else None)


def _held(areas, later, layers):
    """Split `areas` into those every later comma part of the query holds, and
    [(area, reason)] for the rest. A part holds an area when it is one of the area's own
    attribute values (its canton, its province) or a country its layer declares. When
    no area is held that way, the geocoder's boundary for the part decides: a country
    the layer does not declare, or a boundary the area lies outside, rules it out."""
    from . import vectors as _vectors

    def says(a, part):
        declared = [str(c) for c in (layers[a["layer"]].get("countries") or []) if str(c).strip()]
        k = _key(part)
        return (k in {_key(v) for v in a["attrs"].values()}
                or k in {_key(c) for c in declared}
                or bool(declared and _vectors.country_matches(declared, part)))

    direct = [a for a in areas if all(says(a, p) for p in later)]
    if direct:
        ids = {id(a) for a in direct}
        return direct, [(a, "'" + "', '".join(p for p in later if not says(a, p))
                         + "' is not among its attributes")
                        for a in areas if id(a) not in ids]
    kept, aside, places = [], [], {}
    for a in areas:
        why = None
        for p in later:
            if says(a, p):
                continue
            if p not in places:
                places[p] = _place_shape(p)
            g, country = places[p]
            declared = [str(c) for c in (layers[a["layer"]].get("countries") or []) if str(c).strip()]
            if country and declared:
                if not _vectors.country_matches(declared, country):
                    why = f"its layer covers {', '.join(declared)}, not {p}"
            elif g is None:
                why = f"the geocoder gives no boundary for '{p}' to check it against"
            else:
                s = _area_shape(a)
                if s is None or not g.contains(s.representative_point()):
                    why = f"it does not lie in {p}"
            if why:
                break
        (aside.append((a, why)) if why else kept.append(a))
    return kept, aside


def _similar(k, layers, limit=6):
    """(forms, how many in all) of areas whose name holds the asked one as whole words,
    or is held in it — 'Estero Puntarenas y Manglares Asociados', then 'Puntarenas', for
    'Estero Puntarenas'. Only ever named in a note; never used as the place."""
    if len(k) < 4:
        return [], 0
    longer, shorter = [], []
    for layer, entry in layers.items():
        for n, areas in _gazetteer(layer, entry).items():
            if n == k or len(n) < 4:
                continue
            if f" {k} " in f" {n} ":
                longer.extend(areas)
            elif f" {n} " in f" {k} ":
                shorter.extend(areas)
    picked = (sorted(longer, key=lambda a: (len(a["key"]), a["label"], a["layer"]))
              + sorted(shorter, key=lambda a: (-len(a["key"]), a["label"], a["layer"])))
    forms = _forms(picked[:limit], layers)
    return [forms[id(a)][0] for a in picked[:limit]], len(picked)


def _contributed(place) -> dict:
    """The contributed area `place` names: {area, aside, similar, similar_total}.

    `area` is the one area used ({geometry, label, how, layer, form}) or None. An area
    is used only when its whole name is the query's (the whole query, or its first
    comma part) — compared without case, accents or punctuation — and whatever follows
    a comma or sits in a bracket holds it (see _parse, _held). `aside` lists areas of
    the name ruled out that way, `similar` areas whose name only contains, or is
    contained in, the asked one: both are named in the note, never used. Raises
    AmbiguousPlace when several areas fit and nothing in the query tells them apart."""
    out = {"area": None, "aside": [], "similar": [], "similar_total": 0}
    layers = _gazetteer_layers()
    if not layers or len(_key(place)) < 3:
        return out
    found = _areas_named(_key(place), layers)
    if found:
        name, layer, qual, later = str(place).strip(), None, None, []
    else:
        name, layer, qual, later = _parse(place, layers)
        found = _areas_named(_key(name), layers)
    forms = _forms(found, layers)
    if layer:
        named = [a for a in found if a["layer"] == layer]
        if qual:
            qk = _key(qual)
            named = [a for a in named if qk == _key(forms[id(a)][1])
                     or qk in {_key(v) for v in a["attrs"].values()}]
        if not named:
            raise _not_in_layer(place, name, layer, qual, found, forms, layers)
        found = named
    if later and found:
        found, aside = _held(found, later, layers)
        out["aside"] = [{"name": forms[id(a)][0], "reason": why} for a, why in aside]
    if len(found) > 1:
        shown = [forms[id(a)][0] for a in found]
        ex = next((f"{a['label']}, {forms[id(a)][1]}" for a in found
                   if forms[id(a)][1] and " / " not in forms[id(a)][1]), None)
        raise AmbiguousPlace(
            f"'{place}' names {len(found)} different contributed areas: "
            + "; ".join(shown[:_SHOWN])
            + (f"; and {len(shown) - _SHOWN} more" if len(shown) > _SHOWN else "")
            + ". Pass one of them exactly as written here"
            + (f", or add what tells it apart after a comma ('{ex}')" if ex else "")
            + "; a name no contributed layer holds is resolved by OpenStreetMap instead.",
            shown)
    if found:
        out["area"] = _use(found[0], layers, forms, len(_areas_named(found[0]["key"], layers)) - 1)
        if out["area"] is not None:
            out["aside"] = []          # a comma part chose among them: that is the answer
            return out
        out["aside"].append({"name": forms[id(found[0])][0], "reason": "its polygon is empty or unreadable"})
    out["similar"], out["similar_total"] = _similar(_key(name), layers)
    return out


def _not_in_layer(place, name, layer, qual, found, forms, layers):
    """The decline for a form that names a layer: what that layer does hold."""
    here = [forms[id(a)][0] for a in found if a["layer"] == layer]
    elsewhere = [forms[id(a)][0] for a in found if a["layer"] != layer]
    if here:
        return AmbiguousPlace(
            f"'{place}': no area named '{name}' in {layer} is '{qual}'. Its areas of that name: "
            + "; ".join(here[:_SHOWN]) + " — pass one of them exactly as written here.", here)
    similar = _similar(_key(name), {layer: layers[layer]})[0]
    return AmbiguousPlace(
        f"'{place}': {layer} holds no area named '{name}'"
        + (f"; it does hold {'; '.join(similar)}" if similar else "")
        + (f"; other contributed layers hold the name as {'; '.join(elsewhere[:_SHOWN])}" if elsewhere else "")
        + ". Pass one of these exactly as written here.",
        similar + elsewhere)


def _use(area, layers, forms, others):
    """The place record for one contributed area, or None when its polygon is unusable."""
    geom = _area_shape(area)
    if geom is None:
        return None
    layer, entry = area["layer"], layers[area["layer"]]
    form, qual = forms[id(area)]
    label = f"{area['label']} ({qual})" if qual else area["label"]
    parts = len(area["geoms"])
    how = (f"contributed boundary: {label} from {entry.get('title') or layer} ({layer}; "
           f"{entry.get('source') or 'source unstated'}, vintage {entry.get('vintage') or 'unstated'})"
           + (f"; {parts} parts of this one area were merged" if parts > 1 else "")
           + (f"; {others} other contributed area{'s' if others > 1 else ''} share{'' if others > 1 else 's'} "
              f"the name — this is {form}" if others else ""))
    return {"geometry": geom, "label": label, "how": how, "layer": layer, "form": form}


def _contributed_note(c) -> str:
    """What the gazetteer had for a name the geocoder then resolved: the areas a comma
    part ruled out and the areas of a similar name, each in a form that resolves."""
    note = ""
    if c.get("aside"):
        note += (" — contributed areas of this name were not used: "
                 + "; ".join(f"{x['name']} ({x['reason']})" for x in c["aside"][:_SHOWN])
                 + (f"; and {len(c['aside']) - _SHOWN} more" if len(c["aside"]) > _SHOWN else ""))
    if c.get("similar"):
        more = c.get("similar_total", 0) - len(c["similar"])
        note += (" — contributed areas with a similar name: " + "; ".join(c["similar"])
                 + (f"; and {more} more" if more > 0 else "") + "; name one exactly to use it")
    return note


def nearby_named(lon, lat, limit=6, within_km=NEAR_KM) -> list:
    """[{name, layer, km}]: contributed areas within `within_km` of a point, nearest
    first, each under the form that resolves it. Offered beside an approximate box —
    a gulf the geocoder knows only as a point lies in, or beside, a hub's own areas,
    and those are the names worth trying, not a layer's first few."""
    from shapely.ops import nearest_points
    layers = _gazetteer_layers()
    rough = []
    for layer, entry in layers.items():
        for areas in _gazetteer(layer, entry).values():
            for a in areas:
                b = a["bbox"]
                if b is None:
                    continue
                km = _km((lon, lat), (min(max(lon, b[0]), b[2]), min(max(lat, b[1]), b[3])))
                if km <= within_km:
                    rough.append((km, a))
    rough.sort(key=lambda t: t[0])
    pt = Point(lon, lat)
    near = []
    for _bkm, a in rough[: limit * 4]:
        s = _area_shape(a)
        if s is None:
            continue
        p = pt if s.contains(pt) else nearest_points(s, pt)[0]
        km = _km((lon, lat), (p.x, p.y))
        if km <= within_km:
            near.append((km, a))
    near.sort(key=lambda t: (t[0], t[1]["label"]))
    forms = _forms([a for _k, a in near[:limit]], layers)
    return [{"name": forms[id(a)][0], "layer": a["layer"], "km": round(km, 1)} for km, a in near[:limit]]


def _boundary(place, cap=None):
    """Resolve `place` to (km², name, geometry, how) — `_resolve` for callers that need
    only the area."""
    r = _resolve(place, cap)
    return (r["km2"], r["name"], r["geometry"], r["how"])


def _resolve(place, cap=None, contributed=None):
    """Resolve `place` to {km2, name, geometry, how, approximate, nearby, contributed}.

    A contributed area whose whole name is the asked one comes first (the gazetteer
    above; `contributed` passes a lookup already made). Else the geocoder: the most
    complete admin boundary under the area cap (`cap` overrides AREA_CAP_KM2 for
    callers that can analyse larger areas — raster counting, not OSM fetching); if
    none fits but Nominatim recognised the place, a retry with the canonical name it
    returned (recovers typo'd cities, e.g. 'Batambang' -> Battambang); otherwise a
    radius box around the centre point, marked `approximate`, with the contributed
    areas near that point in `nearby`. `how` records which path was taken and names
    the contributed areas that were similar but not used. Raises only when nothing is
    found, or when several contributed areas share the name (AmbiguousPlace)."""
    c = _contributed(place) if contributed is None else contributed
    if c["area"]:
        a = c["area"]
        return {"km2": _est_km2(a["geometry"]), "name": a["label"], "geometry": a["geometry"],
                "how": a["how"], "approximate": False, "nearby": [], "contributed": c}
    note = _contributed_note(c)
    try:
        km2, name, g, how, approximate = _geocoded(place, cap)
    except ValueError as exc:
        raise ValueError(f"{exc}{note}") from None
    nearby = nearby_named(g.centroid.x, g.centroid.y) if approximate else []
    if nearby:
        note += (" — contributed areas near this point: "
                 + "; ".join(f"{n['name']}, {n['km']:.0f} km" for n in nearby)
                 + " — name one to use its boundary")
    return {"km2": km2, "name": name, "geometry": g, "how": how + note,
            "approximate": approximate, "nearby": nearby, "contributed": c}


def _geocoded(place, cap=None):
    """(km², name, geometry, how, approximate) from the geocoder — see _resolve."""
    results = _search(place)
    area_cap = AREA_CAP_KM2 if cap is None else float(cap)
    if not results:
        raise ValueError(f"could not find '{place}' (try 'City, Country')")

    # A geocoder answers the STRING, not the question. "Battambang Province" returns
    # exactly two hits, both points of interest: a records office and a police station
    # that happen to carry the province's name. Building an area of interest around
    # either produced a confident, fully cited answer about a 12 km box around a
    # building. When every hit is a point of interest, drop the administrative noun
    # and ask again — "Battambang" returns the province boundary at rank 8.
    if all(_is_poi(d) for d in results):
        stripped = _strip_admin_noun(place)
        if stripped and stripped.lower() != place.lower():
            retry = _search(stripped)
            if retry and not all(_is_poi(d) for d in retry):
                results = retry
        if all(_is_poi(d) for d in results):
            raise ValueError(
                f"'{place}' only matches points of interest in OpenStreetMap "
                f"({results[0]['display_name'].split(',')[0]}), not a place. "
                "Name the settlement or district itself, e.g. 'Battambang, Cambodia'")

    # How big an area the caller actually meant. Naming an administrative level
    # ("Battambang Province") asks for that level and should get it. Naming a place
    # bare ("Battambang, Cambodia") means the settlement — so the larger cap that
    # makes province-scale planning possible must NOT quietly promote every city
    # query to its province. The wording decides which cap applies.
    level_cap = area_cap if _names_admin_level(place) else min(ASSET_CAP_KM2, area_cap)
    hit = _under_cap_admin(results, level_cap)
    if hit is None and level_cap != area_cap:
        hit = _under_cap_admin(results, area_cap)   # nothing small enough; take what fits
    if hit:
        how = f"{hit[3]} ~{hit[0]:.0f} km²"
        # A planner asking about a province must not be handed the town of the same
        # name without being told. The cap picks the largest area that FITS, so when
        # a bigger administrative area of the same name also matched, say what was
        # left out, by name and size. Silence here is how city-scale numbers get
        # read as province-scale ones.
        bigger = _admin_over_cap(results)
        if bigger and bigger[0] > hit[0] * 1.5:
            km2, d, _g = bigger
            level = d.get("addresstype") or d.get("type") or "area"
            if km2 <= area_cap:
                # We CAN analyse the larger one — the caller just did not ask for it.
                # Say exactly what to type, because the difference between a town and
                # its province is the difference between two different answers.
                # Do not invent the local word for the level — Nominatim calls a
                # Cambodian province a "state". Give an example of the shape of the
                # request instead; any administrative noun widens the search.
                how += (f" — this is {hit[1]} itself, not the wider administrative "
                        f"area of the same name (~{km2:.0f} km²). Name the level "
                        f"(for example '{hit[1]} Province') to analyse that instead")
            else:
                how += (f" — this is the SMALLER {hit[1]}; the {level} of the same "
                        f"name (~{km2:.0f} km²) is over the "
                        f"{area_cap:.0f} km² analysis cap and was NOT analysed")
        return (hit[0], hit[1], hit[2], how, False)

    center = _best_center(results)
    canonical = center["display_name"].split(",")[0]
    if canonical.strip().lower() != place.strip().lower():   # typo -> retry corrected name
        retry = _search(canonical)
        hit = _under_cap_admin(retry, area_cap)
        if hit:
            return (hit[0], hit[1], hit[2],
                    f"{hit[3]} ~{hit[0]:.0f} km² (corrected '{place}' -> '{canonical}')", False)
        if retry:
            center = _best_center(retry)

    lon, lat = float(center["lon"]), float(center["lat"])    # no usable boundary -> box
    dlat = RADIUS_KM / 111.0
    dlon = RADIUS_KM / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    g = box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
    # Say what was actually cut down, and to what. A named administrative area that
    # exceeded the cap must be reported by NAME and SIZE: the caller asked about a
    # province and is getting a town-sized box at its centre, which is a different
    # question and has to read as one. Either way the box is APPROXIMATE: "Golfo de
    # Guayaquil" came back status ok as a 579 km² box around a ~12,000 km² gulf, and
    # nothing in the answer said the box was not the gulf.
    over = _admin_over_cap(results)
    if over:
        km2, d, _ = over
        admin_name = d["display_name"].split(",")[0]
        name = f"{admin_name}: {RADIUS_KM:.0f} km box at its centre"
        how = (f"{RADIUS_KM:.0f} km radius box at the centre of {admin_name} "
               f"(~{km2:.0f} km², over the {area_cap:.0f} km² cap) — an approximation, "
               "NOT the whole administrative area")
    else:
        found = center["display_name"].split(",")[0]
        name = f"{found} (~{RADIUS_KM:.0f} km radius)"
        how = (f"{RADIUS_KM:.0f} km radius box around {found} — an approximation, NOT the "
               f"boundary of {found}: the geocoder gave a point and no admin or "
               "protected-area boundary for it")
    return (_est_km2(g), name, g, how, True)


def _search(place):
    r = requests.get(f"{NOMINATIM}/search", headers=HEADERS, timeout=40, params={
        "q": place, "format": "json", "polygon_geojson": 1, "limit": 10, "accept-language": "en"})
    r.raise_for_status()
    results = r.json()
    first = results[0] if results else {}
    emit({"kind": "api",
          "api": "Nominatim",
          "op": "geocode",
          "query": place,
          "place_id": first.get("place_id"),
          "retrieved_name": first.get("display_name"),
          "type": first.get("type"),
          "n_results": len(results)})
    return results


_COUNTRY_TRIED: dict = {}      # adir -> when the reverse geocode was last attempted


def _reverse_country(lon, lat):
    """The country at a point, from Nominatim's reverse geocoder (English), or None
    when it cannot be had — a declared-coverage test that cannot run is skipped,
    never guessed."""
    try:
        r = requests.get(f"{NOMINATIM}/reverse", headers=HEADERS, timeout=8, params={
            "lat": lat, "lon": lon, "format": "json", "zoom": 3, "accept-language": "en"})
        r.raise_for_status()
        d = r.json() or {}
        c = ((d.get("address") or {}).get("country")
             or str(d.get("display_name") or "").rsplit(",", 1)[-1].strip())
        emit({"kind": "api", "api": "Nominatim", "op": "reverse", "country": c or None})
        return c or None
    except Exception:
        return None


def _under_cap_admin(results, cap=None):
    """The most complete admin boundary under `cap` (default the analysis cap), or None."""
    cap = AREA_CAP_KM2 if cap is None else cap
    under = []
    for d in results:
        kind = _boundary_kind(d)
        if kind:
            g = shape(d["geojson"])
            km2 = _est_km2(g)
            if km2 <= cap:
                under.append((km2, d["display_name"].split(",")[0], g, kind))
    return max(under, key=lambda c: c[0]) if under else None


# Ranks 26+ are street level and below in Nominatim; these classes are things, not places.
_POI_CLASSES = {"office", "amenity", "shop", "building", "tourism", "leisure", "craft",
                "healthcare", "historic", "man_made", "emergency", "military", "club"}
_ADMIN_NOUNS = ("province", "prefecture", "district", "municipality", "county", "region",
                "governorate", "state", "department", "division", "subdistrict", "commune")


def _names_admin_level(place):
    """Did the caller name an administrative level, rather than just a place?"""
    head = str(place or "").partition(",")[0].lower()
    return any(n in head.split() or n in head.replace(".", " ").split()
               for n in _ADMIN_NOUNS)


def _is_poi(d):
    """A point of interest — a building or facility — rather than a place."""
    if d.get("class") in _POI_CLASSES:
        return True
    try:
        return int(d.get("place_rank", 0)) >= 26
    except (TypeError, ValueError):
        return False


def _strip_admin_noun(place):
    """'Battambang Province, Cambodia' -> 'Battambang, Cambodia'. The administrative
    noun is what dragged the match onto a government office in the first place."""
    head, sep, tail = place.partition(",")
    words = head.split()
    while words and words[-1].lower().strip(".") in _ADMIN_NOUNS:
        words.pop()
    if not words:
        return None
    return " ".join(words) + sep + tail


def _admin_over_cap(results):
    """The largest administrative boundary among the results, whatever its size.

    Needed because the cap rejects a province and then the fallback has to choose a
    centre. Picking Nominatim's first hit put a records office at the centre of a
    province query — "Battambang Province" resolved to "Archives of Battambang
    Province" and answered, with full citations, about a 12 km box around a
    building. An administrative area is always a better centre than a point of
    interest that merely shares its name.
    """
    admins = []
    for d in results:
        if _boundary_kind(d):
            g = shape(d["geojson"])
            admins.append((_est_km2(g), d, g))
    return max(admins, key=lambda c: c[0]) if admins else None


def _best_center(results):
    """Pick the centre point: a populated place first, then an administrative area's
    centroid, and a point of interest only if the results hold nothing better."""
    for t in ("city", "town", "municipality", "village", "suburb"):
        for d in results:
            if d.get("class") == "place" and d.get("type") == t:
                return d
    admin = _admin_over_cap(results)
    if admin:
        km2, d, geom = admin
        c = geom.centroid
        return {**d, "lon": str(c.x), "lat": str(c.y)}
    return results[0]


def _overpass(query, attempts=3):
    """Query OSM, trying mirrors and backing off through load/timeout errors."""
    last = "no response"
    for attempt in range(attempts):
        for url in OVERPASS_MIRRORS:
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=OVERPASS_TIMEOUT)
                if r.status_code in (429, 504):
                    last = f"{r.status_code} from {url}"
                    continue
                r.raise_for_status()
                body = r.json()
                # Overpass answers a TIMED-OUT query with HTTP 200, a partial
                # element list and a `remark`. Reading only `elements` turns a
                # truncated fetch into a confident undercount: a province asked for
                # its schools would be told how many are exposed out of however many
                # happened to arrive before the server gave up. For a brief that
                # allocates emergency resources, a partial count is worse than none,
                # so a remark is a FAILURE here, never a result.
                remark = body.get("remark") or ""
                if remark:
                    last = f"partial result from {url}: {remark.strip()[:200]}"
                    emit({"kind": "api", "api": "Overpass", "mirror_used": url,
                          "attempts": attempt + 1, "truncated": True,
                          "remark": remark.strip()[:200], "api_query": query})
                    continue
                elements = body["elements"]
                emit({"kind": "api", "api": "Overpass", "mirror_used": url,
                      "attempts": attempt + 1, "n_elements": len(elements), "api_query": query})
                return elements
            except requests.RequestException as e:
                last = str(e)
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Overpass unavailable: {last}")


def _drive_id(url):
    """The file id out of a Google Drive share URL (…/d/<id>/… or …?id=<id>)."""
    m = re.search(r"/d/([^/]+)", url) or re.search(r"[?&]id=([^&]+)", url)
    if not m:
        raise ValueError(f"cannot parse a Google Drive id from {url}")
    return m.group(1)


def source_raster(layer="hazard_flood"):
    """The full source raster for `layer`, downloaded once if it isn't present.

    `layer` may be a tiffs.yml key (the 9 chat hazards, which also carry rich metadata)
    or any filename/stem in the Drive catalog — the vulnerability, raw, and risk layers
    the risk pipeline needs. The Drive id is resolved through drive_tifs (the single id
    map), falling back to a tiffs.yml download_url for back-compat.
    """
    settings = get_settings()
    try:
        meta = tiffs.entry(layer)
        if meta.get("kind") == "stack":
            raise ValueError(f"{layer} is a stack of yearly rasters, not one file — "
                             "name a year (stacks.layer_for)")
        if meta.get("path"):           # a stack year: opened where it lives, never copied
            return meta["path"]
        fname = os.path.basename(meta["local_path"])
        fallback_url = meta.get("download_url")
    except ValueError as exc:          # not a tiffs.yml layer -> treat as a catalog filename/stem
        if "is a stack" in str(exc):
            raise
        fname = layer if layer.endswith(".tif") else f"{layer}.tif"
        fallback_url = None
    path = os.path.join(settings.tiffs_dir, fname)
    was_cached = os.path.exists(path)
    if not was_cached:
        fid = drive_tifs.drive_id(fname) or (_drive_id(fallback_url) if fallback_url else None)
        if not fid:
            raise ValueError(f"no Drive id for '{layer}' (looked up '{fname}' in drive_tifs + tiffs.yml)")
        os.makedirs(settings.tiffs_dir, exist_ok=True)
        import gdown
        gdown.download(id=fid, output=path, quiet=True)
        emit({"kind": "download", "what": "source_raster", "api": "Google Drive", "layer": layer,
              "filename": fname, "drive_id": fid, "dest": short_path(path), "was_cached": was_cached})
    else:
        emit({"kind": "download", "what": "source_raster", "api": "Google Drive", "layer": layer,
              "filename": fname, "dest": short_path(path), "was_cached": was_cached})
    return path


def area_of(aoi) -> dict | None:
    """The area a pack actually used, as data: {name, how, area_km2, geometry}. The
    geometry is GeoJSON, not the bundle path — a path points into one machine's cache,
    and a pack must stay meaningful on another host. Any pack that resolved an area
    reports it this way, so platform steps that need the area never ask which pack it
    was."""
    try:
        with open(aoi["admin"]) as f:
            geom = json.load(f)["features"][0]["geometry"]
    except (KeyError, IndexError, OSError, ValueError, TypeError):
        return None
    return {"name": aoi.get("name"), "how": aoi.get("how"),
            "area_km2": aoi.get("area_km2"), "geometry": geom}


def resolve_place(place):
    """Resolve a place name to the area the platform would actually analyse.

    Boundary only, no OSM fetch, so it is cheap enough to answer a resolve call.
    `how` carries the honest account when a named administrative area was too big
    to take whole — a planner should be told their province became a box here,
    not discover it later in the numbers. A box is `approximate`, with the
    contributed areas near it offered by name; a name several contributed areas
    share declines with `candidates`, each a form that resolves.
    """
    try:
        r = _resolve(place)
    except ValueError as e:
        return {"status": "declined", "place": place, "note": str(e),
                **({"candidates": e.candidates} if getattr(e, "candidates", None) else {})}
    km2, name, boundary, how = r["km2"], r["name"], r["geometry"], r["how"]
    c = r["contributed"]
    minx, miny, maxx, maxy = boundary.bounds
    return {"status": "ok", "place": place, "name": name,
            "area_km2": round(km2), "how": how, "approximate": r["approximate"],
            "is_admin_boundary": str(how).startswith("admin boundary"),
            "is_contributed_boundary": str(how).startswith("contributed boundary"),
            "bbox": [round(v, 5) for v in (minx, miny, maxx, maxy)],
            "centroid": [round(boundary.centroid.x, 5), round(boundary.centroid.y, 5)],
            "area_cap_km2": AREA_CAP_KM2,
            **({"nearby_contributed_areas": r["nearby"]} if r["nearby"] else {}),
            **({"similar_contributed_areas": c["similar"]} if c["similar"] else {}),
            **({"contributed_not_used": c["aside"]} if c["aside"] else {})}


def ensure_aoi(place=None, geometry=None, layers=None, name=None, area_cap_km2=None, how=None):
    """Return a cached bundle of file paths for an AOI — resolved from a `place` name, or
    from a user-drawn `geometry` (GeoJSON Polygon or [minLon,minLat,maxLon,maxLat] bbox,
    EPSG:4326).

    `layers` limits which OSM asset layers to fetch (a subset of ASSET_LAYERS); None
    fetches all. Each layer is fetched once and cached, so asking later for a different
    layer only fetches what's still missing. Fetching just the layer a question needs
    (roads, not buildings) is the main lever on drawn-AOI latency: each Overpass
    round-trip can stall for tens of seconds, so doing fewer of them is what matters.
    """
    # None = every OSM asset layer; an empty tuple = none at all (a raster-only pack
    # needs the boundary, not a multi-minute Overpass pull it will never read).
    needed = ASSET_LAYERS if layers is None else tuple(layers)
    # A caller that only counts raster pixels may analyse far larger areas than one
    # that fetches OSM assets. The cap is part of the cache identity: a country
    # resolved to its full polygon under one cap must never be served to a caller
    # whose cap would have refused it, nor the 12 km fallback box to one that
    # could have taken the polygon.
    cap = AREA_CAP_KM2 if area_cap_km2 is None else float(area_cap_km2)
    cache = str(get_settings().cache_dir)
    contributed_how = how if (geometry is not None and how) else None
    contributed = None
    if geometry is None and place:
        contributed = _contributed(place)        # raises when several areas share the name
        named = contributed["area"]
        if named:
            # The hub's own polygon, under its own label: cached by its shape, so a
            # re-contributed layer gets a fresh bundle and a geocoder's box for the
            # same words is never served in its place.
            geometry = mapping(named["geometry"])
            name = named["label"]
            contributed_how = named["how"]
    if geometry is not None:
        boundary = _to_polygon(geometry)
        slug = "draw-" + hashlib.sha1(boundary.wkt.encode()).hexdigest()[:12]
    else:
        slug = _slug(place) or "_"
    if cap != AREA_CAP_KM2:
        slug += f"-cap{int(cap)}"
    adir = os.path.join(cache, slug)
    meta = os.path.join(adir, "meta.json")

    # Resolve the AOI boundary once (or reload it from a prior fetch of this AOI).
    emit({"kind": "cache", "what": "aoi_boundary", "key": slug,
          "dest": short_path(meta), "was_cached": os.path.exists(meta)})
    cached = json.load(open(meta)) if os.path.exists(meta) else None
    # A radius box cached before boxes said they were approximate is re-resolved on
    # its own, so it says so and offers the contributed areas near it; bumping the
    # resolver for that would re-fetch every cached area's OSM layers.
    legacy_box = (cached is not None and "approximate" not in cached
                  and str(cached.get("how") or "").startswith(f"{RADIUS_KM:.0f} km radius box"))
    if cached is not None and place is not None \
            and (int(cached.get("resolver", 0)) < RESOLVER_VERSION or legacy_box):
        # The cache is keyed on the query string, so a resolution made by an older,
        # wronger resolver is served forever. "Battambang Province" kept returning a
        # records office long after the geocoder learned not to pick one. Stamp the
        # resolver and re-resolve anything older; the layer files stay, only the
        # boundary is recomputed.
        print(f"   [ingest: re-resolving '{place}' — cached by an older resolver]")
        # The asset layers go too. They were fetched for the OLD boundary, and a
        # boundary can now change by two orders of magnitude — "Battambang Province"
        # moved from a 115 km² town to the 12,159 km² province. Keeping the old
        # files silently reported a town's 27 schools as the province's.
        from . import vectors as _vectors
        for f in ("meta.json", "admin.geojson",
                  *(f"{ly}.geojson" for ly in ASSET_LAYERS),
                  *(f"{ly}.geojson" for ly in _vectors.visible())):
            try:
                os.remove(os.path.join(adir, f))
            except OSError:
                pass
        cached = None
    if cached is not None:
        info = cached
        if geometry is not None and name and (info.get("name") != name or
                                              (contributed_how and info.get("how") != contributed_how)):
            # The same polygon, now with the caller's label: a district named by
            # the system that holds its boundary must not be cited as "drawn area".
            info["name"], info["how"] = name, (contributed_how or "supplied polygon")
            json.dump(info, open(meta, "w"), indent=2)
        boundary = shape(json.load(open(os.path.join(adir, "admin.geojson")))["features"][0]["geometry"])
    else:
        if geometry is not None:
            km2 = _est_km2(boundary)
            if km2 > cap:
                raise ValueError(f"{'the contributed area' if contributed_how else 'drawn area'} "
                                 f"is too large (~{km2:.0f} km², over the {cap:.0f} km² cap)"
                                 + ("" if contributed_how else " — draw a smaller box"))
            name, how = (name or "drawn area"), (contributed_how or ("drawn" if not name else "supplied polygon"))
            approximate = {}
        else:
            r = _resolve(place, cap, contributed)
            km2, name, boundary, how = r["km2"], r["name"], r["geometry"], r["how"]
            # A box is not the place: said as a field every pack's reader can test,
            # with the contributed areas near it that would give a real boundary.
            approximate = ({"approximate": True, "nearby_contributed": r["nearby"]}
                           if r["approximate"] else {})
        print(f"   [ingest: resolving '{name}' (~{km2:.0f} km²)…]")
        os.makedirs(adir, exist_ok=True)
        _write(adir, "admin", [_feature(boundary, {"name": name})])
        info = {"name": name, "area_km2": round(km2), "how": how, "counts": {},
                "resolver": RESOLVER_VERSION, **approximate}
        json.dump(info, open(meta, "w"), indent=2)

    # Over a large area the heavy OSM layers are declined rather than attempted.
    # Saying so is the point: a count that was never fetched must never read as a
    # count of zero.
    # Above the asset budget the heavy OSM layers are DECLARED, not attempted: a
    # single Overpass query over a province truncates, and a count that was never
    # fetched must never read as a count of zero. (Tiling them works and takes
    # ~43 minutes for a province — parked on uat/local-hardening as prefetch work.)
    area_km2 = float(info.get("area_km2") or 0)
    if area_km2 > ASSET_CAP_KM2:
        declined = [ly for ly in needed if ly not in LIGHT_ASSET_LAYERS]
        needed = tuple(ly for ly in needed if ly in LIGHT_ASSET_LAYERS)
        if declined:
            info["assets_declined"] = {
                "layers": declined, "area_km2": round(area_km2),
                "reason": (f"area is ~{area_km2:.0f} km², over the "
                           f"{ASSET_CAP_KM2:.0f} km² asset budget — "
                           f"{', '.join(declined)} were NOT fetched, so they must "
                           "not be read as zero"),
            }
            json.dump(info, open(meta, "w"), indent=2)

    # Fetch only the requested asset layers that aren't already cached.
    minx, miny, maxx, maxy = boundary.bounds
    bbox = f"{miny - BUFFER_DEG},{minx - BUFFER_DEG},{maxy + BUFFER_DEG},{maxx + BUFFER_DEG}"
    fetched = False
    for layer in needed:
        if layer not in ASSET_LAYERS:
            continue
        dest = os.path.join(adir, f"{layer}.geojson")
        was_cached = os.path.exists(dest)
        emit({"kind": "cache", "what": "osm_layer", "layer": layer,
              "dest": short_path(dest), "was_cached": was_cached})
        if was_cached:
            continue
        features = _fetch_layer(layer, bbox, boundary)
        _write(adir, layer, features)
        info.setdefault("counts", {})[layer] = len(features)
        fetched = True
    # Contributed point layers (a hub's evacuation centres, say) are clipped from
    # their master file into this AOI beside the OSM layers, under their own name,
    # so the same exposure count reaches them. Never declined by area: a hub's own
    # list is small and already local, unlike an Overpass pull.
    # A layer whose points lie wholly outside this area is NOT clipped: an empty
    # clip reads as "0 of 0" downstream, and a Thai list was being cited as a
    # clean zero in every Cambodian district. Decided per call from the entry's
    # observed bbox (never persisted, so a re-contributed layer is re-judged).
    from . import vectors as _vectors
    visible = _vectors.visible()
    # The area's country, asked once (reverse geocode of the centroid) and kept in
    # meta.json. Read by every source that declares its countries — a hub's point
    # layer, a risk-pack feed, a table — so a Thai source is never cited into a
    # Cambodian answer. A failed lookup is retried after ten minutes, not on every
    # question, so a geocoder outage costs one timeout per area, not one per call.
    if not info.get("country") and time.time() - _COUNTRY_TRIED.get(adir, 0.0) > 600:
        _COUNTRY_TRIED[adir] = time.time()
        c = _reverse_country(boundary.centroid.x, boundary.centroid.y)
        if c:
            info["country"] = c
            fetched = True
    not_applicable = {}
    for layer, entry in visible.items():
        dest = os.path.join(adir, f"{layer}.geojson")
        na = _vectors.not_applicable(entry, boundary, info.get("country"))
        if na:
            not_applicable[layer] = na
            emit({"kind": "cache", "what": "contributed_layer", "layer": layer,
                  "not_applicable": na["reason"]})
            continue
        emit({"kind": "cache", "what": "contributed_layer", "layer": layer,
              "dest": short_path(dest), "was_cached": os.path.exists(dest)})
        if os.path.exists(dest):
            continue
        n_pts = _vectors.clip_into(adir, boundary, layer, entry)
        info.setdefault("counts", {})[layer] = n_pts
        fetched = True
    if fetched:
        json.dump(info, open(meta, "w"), indent=2)
    bundle = _bundle(adir, info, skip=not_applicable)
    if not_applicable:
        bundle["vectors_not_applicable"] = not_applicable
    return bundle

def _osm_name(tags: dict) -> str:
    """The name OSM gives a point, under whichever tag it carries it. 8 of 12
    schools in one Thai district had no `name` and came back nameless; some
    carry name:th, name:en, official_name or alt_name instead."""
    for k in ("name", "name:en", "name:th", "official_name", "alt_name", "int_name"):
        v = tags.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _fetch_layer(layer, bbox, boundary):
    """Fetch one OSM asset layer within `bbox`, clipped/filtered to `boundary`."""
    t = OVERPASS_SERVER_TIMEOUT
    if layer == "roads":
        out = []
        for e in _overpass(f'[out:json][timeout:{t}];way["highway"]({bbox});out geom;'):
            g = e.get("geometry") or []
            if len(g) < 2:
                continue
            clipped = LineString([(p["lon"], p["lat"]) for p in g]).intersection(boundary)
            for part in getattr(clipped, "geoms", [clipped]):
                if getattr(part, "geom_type", "") == "LineString" and len(part.coords) >= 2:
                    out.append(_feature(part, {"highway": e.get("tags", {}).get("highway", "")}))
        return out
    if layer in ("hospitals", "schools"):
        amenity = "hospital" if layer == "hospitals" else "school"
        out = []
        for e in _overpass(f'[out:json][timeout:{t}];(node["amenity"="{amenity}"]({bbox});'
                           f'way["amenity"="{amenity}"]({bbox}););out center;'):
            lat = e.get("lat") or (e.get("center") or {}).get("lat")
            lon = e.get("lon") or (e.get("center") or {}).get("lon")
            if lat is not None and boundary.contains(Point(lon, lat)):
                out.append(_feature(Point(lon, lat), {"name": _osm_name(e.get("tags") or {})}))
        return out
    if layer == "buildings":
        out = []
        for e in _overpass(f'[out:json][timeout:{t}];way["building"]({bbox});out center;'):
            c = e.get("center") or {}
            if c and boundary.contains(Point(c["lon"], c["lat"])):
                out.append(_feature(Point(c["lon"], c["lat"]), {}))
        return out
    raise ValueError(f"unknown asset layer: {layer}")


def _bundle(adir, info, skip=()):
    """Rebuild file paths from the AOI dir, so the cache is portable across dirs/machines
    (meta.json holds only metadata — name/area/how/counts — never absolute paths).
    `skip` names contributed layers that do not cover this area: no path, so no reader
    can open an empty clip and call it zero."""
    return {"name": info["name"], "area_km2": info["area_km2"],
            "how": info.get("how"), "counts": info["counts"],
            # Which asset layers were never fetched because the area is too large.
            # Carried so a brief can DECLARE them instead of counting them as zero.
            "assets_declined": info.get("assets_declined"),
            "country": info.get("country"),
            # A radius box is not the place's boundary; a pack that needs one declines
            # and offers these, nearest first, as names that would give it one.
            "approximate": bool(info.get("approximate")),
            "nearby_contributed": info.get("nearby_contributed") or [],
            "admin": os.path.join(adir, "admin.geojson"),
            "roads": os.path.join(adir, "roads.geojson"),
            "hospitals": os.path.join(adir, "hospitals.geojson"),
            "schools": os.path.join(adir, "schools.geojson"),
            "buildings": os.path.join(adir, "buildings.geojson"),
            **{ly: os.path.join(adir, f"{ly}.geojson") for ly in _contributed_layers()
               if ly not in skip}}


def _contributed_layers():
    from . import vectors
    return list(vectors.visible())


def hazard_clip(aoi, layer, max_pixels=None):
    """Clip `layer`'s raster to the AOI bundle; cache per (AOI, layer); return path.

    `max_pixels` is a read budget: a window over it is read through the file's
    overviews at the smallest power-of-two level that fits, the clip's transform is
    scaled to match, and the level is written into the clip's tags so a consumer
    can SAY what it read. Without a budget the window is read at full resolution."""
    adir = os.path.dirname(aoi["admin"])
    clip = os.path.join(adir, f"{layer}.tif")
    was_cached = os.path.exists(clip)
    emit({"kind": "cache", "what": "hazard_clip", "layer": layer,
          "dest": short_path(clip), "was_cached": was_cached})
    if not was_cached:
        boundary = shape(json.load(open(aoi["admin"]))["features"][0]["geometry"])
        minx, miny, maxx, maxy = boundary.bounds
        with rasterio.open(source_raster(layer)) as src:
            win = from_bounds(minx - BUFFER_DEG, miny - BUFFER_DEG,
                              maxx + BUFFER_DEG, maxy + BUFFER_DEG, src.transform)
            # INTERSECT with the dataset before reading. read() silently crops the
            # array to the raster's extent, but window_transform(win) describes the
            # UNCROPPED request — an AOI straddling the extent got a clip whose
            # georeference was shifted by the out-of-extent margin, and every
            # severity lookup after that sampled the wrong pixel: confidently
            # wrong exposure numbers with no error. Found by adversarial review,
            # reproduced empirically before fixing.
            full = rasterio.windows.Window(0, 0, src.width, src.height)
            try:
                win = win.intersection(full)
            except rasterio.errors.WindowError:
                win = None
            if win is None or win.width <= 0 or win.height <= 0:
                raise ValueError(
                    f"{aoi.get('name', 'this area')} lies outside {layer}'s coverage "
                    "— the catalog raster does not extend there, so exposure cannot "
                    "be computed against it")
            level = 1
            n_px = float(win.width) * float(win.height)
            if max_pixels and n_px > float(max_pixels):
                level = int(2 ** math.ceil(math.log2(math.sqrt(n_px / float(max_pixels)))))
            transform = src.window_transform(win)
            if level > 1:
                out_shape = (max(1, math.ceil(win.height / level)),
                             max(1, math.ceil(win.width / level)))
                arr = src.read(1, window=win, out_shape=out_shape)   # nearest: classes never blend
                transform = rasterio.Affine(transform.a * level, transform.b, transform.c,
                                            transform.d, transform.e * level, transform.f)
            else:
                arr = src.read(1, window=win)
            prof = src.profile | {"height": arr.shape[0], "width": arr.shape[1],
                                  "transform": transform, "compress": "lzw"}
            with rasterio.open(clip, "w", **prof) as dst:
                dst.write(arr, 1)
                dst.update_tags(overview_level=level)
    return clip


def _to_polygon(geometry):
    """A drawn AOI -> shapely polygon. Accepts a GeoJSON geometry dict, or a
    [minLon, minLat, maxLon, maxLat] bbox array."""
    if isinstance(geometry, (list, tuple)) and len(geometry) == 4:
        return box(*[float(v) for v in geometry])
    return shape(geometry)


def _feature(geom, props):
    return {"type": "Feature", "properties": props, "geometry": mapping(geom)}


def _write(adir, layer, features):
    json.dump({"type": "FeatureCollection", "features": features},
              open(os.path.join(adir, f"{layer}.geojson"), "w"))
