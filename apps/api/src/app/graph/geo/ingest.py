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
AREA_CAP_KM2 = 1500.0
BUFFER_DEG = 0.01
RADIUS_KM = 12.0          # fallback AOI: box of this radius around the centre point
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

    hit = _under_cap_admin(results)
    if hit:
        return (*hit, f"admin boundary ~{hit[0]:.0f} km²")

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
    name = f"{center['display_name'].split(',')[0]} (~{RADIUS_KM:.0f} km radius)"
    return (g.area * 111.0 * 108.0, name, g,
            f"{RADIUS_KM:.0f} km radius box (no admin boundary under cap)")


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


def _under_cap_admin(results):
    """The most complete admin boundary under the area cap, or None."""
    under = []
    for d in results:
        gj = d.get("geojson", {})
        if d.get("class") == "boundary" and d.get("type") == "administrative" \
                and gj.get("type") in ("Polygon", "MultiPolygon"):
            km2 = shape(gj).area * 111.0 * 108.0
            if km2 <= AREA_CAP_KM2:
                under.append((km2, d["display_name"].split(",")[0], shape(gj)))
    return max(under, key=lambda c: c[0]) if under else None


def _best_center(results):
    """Pick the centre point: prefer a populated-place node over a giant boundary."""
    for t in ("city", "town", "municipality", "village", "suburb"):
        for d in results:
            if d.get("class") == "place" and d.get("type") == t:
                return d
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
                elements = r.json()["elements"]
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
    if os.path.exists(meta):
        info = json.load(open(meta))
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
        info = {"name": name, "area_km2": round(km2), "how": how, "counts": {}}
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
