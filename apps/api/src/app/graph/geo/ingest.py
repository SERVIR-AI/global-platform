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


def _boundary(place):
    """Resolve `place` to (km², name, geometry, how): the most complete admin boundary
    under the area cap; if none fits but Nominatim recognised the place, retry with the
    canonical name it returned (recovers typo'd cities, e.g. 'Batambang' -> Battambang);
    otherwise a radius box around the centre point. `how` records which path was taken.
    Raises only when nothing is found."""
    results = _search(place)
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
    cap = AREA_CAP_KM2 if _names_admin_level(place) else ASSET_CAP_KM2
    hit = _under_cap_admin(results, cap)
    if hit is None and cap != AREA_CAP_KM2:
        hit = _under_cap_admin(results, AREA_CAP_KM2)   # nothing small enough; take what fits
    if hit:
        how = f"admin boundary ~{hit[0]:.0f} km²"
        # A planner asking about a province must not be handed the town of the same
        # name without being told. The cap picks the largest area that FITS, so when
        # a bigger administrative area of the same name also matched, say what was
        # left out, by name and size. Silence here is how city-scale numbers get
        # read as province-scale ones.
        bigger = _admin_over_cap(results)
        if bigger and bigger[0] > hit[0] * 1.5:
            km2, d, _g = bigger
            level = d.get("addresstype") or d.get("type") or "area"
            if km2 <= AREA_CAP_KM2:
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
                        f"{AREA_CAP_KM2:.0f} km² analysis cap and was NOT analysed")
        return (*hit, how)

    center = _best_center(results)
    canonical = center["display_name"].split(",")[0]
    if canonical.strip().lower() != place.strip().lower():   # typo -> retry corrected name
        retry = _search(canonical)
        hit = _under_cap_admin(retry)
        if hit:
            return (*hit, f"admin boundary ~{hit[0]:.0f} km² (corrected '{place}' -> '{canonical}')")
        if retry:
            center = _best_center(retry)

    lon, lat = float(center["lon"]), float(center["lat"])    # no usable boundary -> box
    dlat = RADIUS_KM / 111.0
    dlon = RADIUS_KM / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    g = box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
    # Say what was actually cut down, and to what. A named administrative area that
    # exceeded the cap must be reported by NAME and SIZE: the caller asked about a
    # province and is getting a town-sized box at its centre, which is a different
    # question and has to read as one.
    over = _admin_over_cap(results)
    if over:
        km2, d, _ = over
        admin_name = d["display_name"].split(",")[0]
        name = f"{admin_name}: {RADIUS_KM:.0f} km box at its centre"
        how = (f"{RADIUS_KM:.0f} km radius box at the centre of {admin_name} "
               f"(~{km2:.0f} km², over the {AREA_CAP_KM2:.0f} km² cap) — "
               "NOT the whole administrative area")
    else:
        name = f"{center['display_name'].split(',')[0]} (~{RADIUS_KM:.0f} km radius)"
        how = (f"{RADIUS_KM:.0f} km radius box around "
               f"{center['display_name'].split(',')[0]} (no admin boundary found)")
    return (g.area * 111.0 * 108.0, name, g, how)


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


def _under_cap_admin(results, cap=None):
    """The most complete admin boundary under `cap` (default the analysis cap), or None."""
    cap = AREA_CAP_KM2 if cap is None else cap
    under = []
    for d in results:
        gj = d.get("geojson", {})
        if d.get("class") == "boundary" and d.get("type") == "administrative" \
                and gj.get("type") in ("Polygon", "MultiPolygon"):
            km2 = shape(gj).area * 111.0 * 108.0
            if km2 <= cap:
                under.append((km2, d["display_name"].split(",")[0], shape(gj)))
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
        gj = d.get("geojson", {})
        if d.get("class") == "boundary" and d.get("type") == "administrative" \
                and gj.get("type") in ("Polygon", "MultiPolygon"):
            admins.append((shape(gj).area * 111.0 * 108.0, d, shape(gj)))
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
        fname = os.path.basename(meta["local_path"])
        fallback_url = meta.get("download_url")
    except ValueError:                 # not a tiffs.yml layer -> treat as a catalog filename/stem
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


def resolve_place(place):
    """Resolve a place name to the area the platform would actually analyse.

    Boundary only, no OSM fetch, so it is cheap enough to answer a resolve call.
    `how` carries the honest account when a named administrative area was too big
    to take whole — a planner should be told their province became a box here,
    not discover it later in the numbers.
    """
    try:
        km2, name, boundary, how = _boundary(place)
    except ValueError as e:
        return {"status": "declined", "place": place, "note": str(e)}
    minx, miny, maxx, maxy = boundary.bounds
    return {"status": "ok", "place": place, "name": name,
            "area_km2": round(km2), "how": how,
            "is_admin_boundary": str(how).startswith("admin boundary"),
            "bbox": [round(v, 5) for v in (minx, miny, maxx, maxy)],
            "centroid": [round(boundary.centroid.x, 5), round(boundary.centroid.y, 5)],
            "area_cap_km2": AREA_CAP_KM2}


def ensure_aoi(place=None, geometry=None, layers=None):
    """Return a cached bundle of file paths for an AOI — resolved from a `place` name, or
    from a user-drawn `geometry` (GeoJSON Polygon or [minLon,minLat,maxLon,maxLat] bbox,
    EPSG:4326).

    `layers` limits which OSM asset layers to fetch (a subset of ASSET_LAYERS); None
    fetches all. Each layer is fetched once and cached, so asking later for a different
    layer only fetches what's still missing. Fetching just the layer a question needs
    (roads, not buildings) is the main lever on drawn-AOI latency: each Overpass
    round-trip can stall for tens of seconds, so doing fewer of them is what matters.
    """
    needed = tuple(layers) if layers else ASSET_LAYERS
    cache = str(get_settings().cache_dir)
    if geometry is not None:
        boundary = _to_polygon(geometry)
        slug = "draw-" + hashlib.sha1(boundary.wkt.encode()).hexdigest()[:12]
    else:
        slug = _slug(place) or "_"
    adir = os.path.join(cache, slug)
    meta = os.path.join(adir, "meta.json")

    # Resolve the AOI boundary once (or reload it from a prior fetch of this AOI).
    emit({"kind": "cache", "what": "aoi_boundary", "key": slug,
          "dest": short_path(meta), "was_cached": os.path.exists(meta)})
    cached = json.load(open(meta)) if os.path.exists(meta) else None
    if cached is not None and place is not None \
            and int(cached.get("resolver", 0)) < RESOLVER_VERSION:
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
        for f in ("meta.json", "admin.geojson",
                  *(f"{ly}.geojson" for ly in ASSET_LAYERS)):
            try:
                os.remove(os.path.join(adir, f))
            except OSError:
                pass
        cached = None
    if cached is not None:
        info = cached
        boundary = shape(json.load(open(os.path.join(adir, "admin.geojson")))["features"][0]["geometry"])
    else:
        if geometry is not None:
            km2 = boundary.area * 111.0 * 108.0
            if km2 > AREA_CAP_KM2:
                raise ValueError(f"drawn area is too large (>{AREA_CAP_KM2:.0f} km²) — draw a smaller box")
            name, how = "drawn area", "drawn"
        else:
            km2, name, boundary, how = _boundary(place)
        print(f"   [ingest: resolving '{name}' (~{km2:.0f} km²)…]")
        os.makedirs(adir, exist_ok=True)
        _write(adir, "admin", [_feature(boundary, {"name": name})])
        info = {"name": name, "area_km2": round(km2), "how": how, "counts": {},
                "resolver": RESOLVER_VERSION}
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
    if fetched:
        json.dump(info, open(meta, "w"), indent=2)
    return _bundle(adir, info)

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
                out.append(_feature(Point(lon, lat), {"name": e.get("tags", {}).get("name", "")}))
        return out
    if layer == "buildings":
        out = []
        for e in _overpass(f'[out:json][timeout:{t}];way["building"]({bbox});out center;'):
            c = e.get("center") or {}
            if c and boundary.contains(Point(c["lon"], c["lat"])):
                out.append(_feature(Point(c["lon"], c["lat"]), {}))
        return out
    raise ValueError(f"unknown asset layer: {layer}")


def _bundle(adir, info):
    """Rebuild file paths from the AOI dir, so the cache is portable across dirs/machines
    (meta.json holds only metadata — name/area/how/counts — never absolute paths)."""
    return {"name": info["name"], "area_km2": info["area_km2"],
            "how": info.get("how"), "counts": info["counts"],
            # Which asset layers were never fetched because the area is too large.
            # Carried so a brief can DECLARE them instead of counting them as zero.
            "assets_declined": info.get("assets_declined"),
            "admin": os.path.join(adir, "admin.geojson"),
            "roads": os.path.join(adir, "roads.geojson"),
            "hospitals": os.path.join(adir, "hospitals.geojson"),
            "schools": os.path.join(adir, "schools.geojson"),
            "buildings": os.path.join(adir, "buildings.geojson")}


def hazard_clip(aoi, layer):
    """Clip `layer`'s severity raster to the AOI bundle; cache per (AOI, layer); return path."""
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
            arr = src.read(1, window=win)
            prof = src.profile | {"height": arr.shape[0], "width": arr.shape[1],
                                  "transform": src.window_transform(win), "compress": "lzw"}
            with rasterio.open(clip, "w", **prof) as dst:
                dst.write(arr, 1)
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
