"""Resolve a place name to real, cached data: OSM boundary + roads + POIs, and a
flood-hazard raster clipped to it. Raises ValueError if the place can't be
resolved or is too large — it never silently falls back to somewhere else.
"""
import json
import os
import re
import time
import warnings

warnings.filterwarnings("ignore")
import rasterio
import requests
from rasterio.windows import from_bounds
from shapely.geometry import LineString, Point, mapping, shape

from ...config import get_settings
from . import tiffs

HEADERS = {"User-Agent": "grp-mvp/0.1 (disaster-risk research prototype)"}
NOMINATIM = "https://nominatim.openstreetmap.org"
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
AREA_CAP_KM2 = 1500.0
BUFFER_DEG = 0.01


def _slug(place):
    return re.sub(r"[^a-z0-9]+", "-", place.lower()).strip("-")


def _boundary(place):
    """Nominatim -> the most complete admin boundary under the area cap."""
    r = requests.get(f"{NOMINATIM}/search", headers=HEADERS, timeout=40, params={
        "q": place, "format": "json", "polygon_geojson": 1, "limit": 10, "accept-language": "en"})
    r.raise_for_status()
    cands = []
    for d in r.json():
        gj = d.get("geojson", {})
        if d.get("class") == "boundary" and d.get("type") == "administrative" \
                and gj.get("type") in ("Polygon", "MultiPolygon"):
            g = shape(gj)
            cands.append((g.area * 111.0 * 108.0, d["display_name"].split(",")[0], g))
    if not cands:
        raise ValueError(f"no administrative boundary for '{place}' (try 'City, Country')")
    under = [c for c in cands if c[0] <= AREA_CAP_KM2]
    if not under:
        raise ValueError(f"'{place}' is too large (>{AREA_CAP_KM2:.0f} km²) — name a city or district")
    return max(under, key=lambda c: c[0])


def _overpass(query, attempts=3):
    """Query OSM, trying mirrors and backing off through load/timeout errors."""
    last = "no response"
    for attempt in range(attempts):
        for url in OVERPASS_MIRRORS:
            try:
                r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=180)
                if r.status_code in (429, 504):
                    last = f"{r.status_code} from {url}"
                    continue
                r.raise_for_status()
                return r.json()["elements"]
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
    """The full hazard raster for `layer`, downloaded once if it isn't present."""
    settings = get_settings()
    meta = tiffs.entry(layer)
    path = os.path.join(settings.tiffs_dir, os.path.basename(meta["local_path"]))
    if not os.path.exists(path):
        url = meta.get("download_url")
        if not url:
            raise ValueError(f"raster for '{layer}' not at {path} and no download_url in tiffs.yml")
        os.makedirs(settings.tiffs_dir, exist_ok=True)
        import gdown
        gdown.download(id=_drive_id(url), output=path, quiet=True)
    return path


def ensure_aoi(place):
    """Return a cached bundle of file paths for `place`, fetching it if needed."""
    cache = str(get_settings().cache_dir)
    adir = os.path.join(cache, _slug(place) or "_")
    meta = os.path.join(adir, "meta.json")
    if os.path.exists(meta):
        return json.load(open(meta))

    km2, name, boundary = _boundary(place)
    print(f"   [ingest: fetching '{name}' (~{km2:.0f} km²)…]")
    os.makedirs(adir, exist_ok=True)
    minx, miny, maxx, maxy = boundary.bounds
    bbox = f"{miny - BUFFER_DEG},{minx - BUFFER_DEG},{maxy + BUFFER_DEG},{maxx + BUFFER_DEG}"
    _write(adir, "admin", [_feature(boundary, {"name": name})])

    roads = []
    for e in _overpass(f'[out:json][timeout:170];way["highway"]({bbox});out geom;'):
        g = e.get("geometry") or []
        if len(g) < 2:
            continue
        clipped = LineString([(p["lon"], p["lat"]) for p in g]).intersection(boundary)
        for part in getattr(clipped, "geoms", [clipped]):
            if getattr(part, "geom_type", "") == "LineString" and len(part.coords) >= 2:
                roads.append(_feature(part, {"highway": e.get("tags", {}).get("highway", "")}))
    _write(adir, "roads", roads)

    counts = {"roads": len(roads)}
    for amenity in ("hospital", "school"):
        pts = []
        for e in _overpass(f'[out:json][timeout:150];(node["amenity"="{amenity}"]({bbox});'
                           f'way["amenity"="{amenity}"]({bbox}););out center;'):
            lat = e.get("lat") or (e.get("center") or {}).get("lat")
            lon = e.get("lon") or (e.get("center") or {}).get("lon")
            if lat is not None and boundary.contains(Point(lon, lat)):
                pts.append(_feature(Point(lon, lat), {"name": e.get("tags", {}).get("name", "")}))
        layer = amenity + "s"
        _write(adir, layer, pts)
        counts[layer] = len(pts)

    flood_path = os.path.join(adir, "flood.tif")
    with rasterio.open(source_raster("hazard_flood")) as src:
        win = from_bounds(minx - BUFFER_DEG, miny - BUFFER_DEG,
                          maxx + BUFFER_DEG, maxy + BUFFER_DEG, src.transform)
        arr = src.read(1, window=win)
        prof = src.profile | {"height": arr.shape[0], "width": arr.shape[1],
                              "transform": src.window_transform(win), "compress": "lzw"}
        with rasterio.open(flood_path, "w", **prof) as dst:
            dst.write(arr, 1)

    bundle = {"name": name, "area_km2": round(km2), "counts": counts,
              "admin": os.path.join(adir, "admin.geojson"),
              "roads": os.path.join(adir, "roads.geojson"),
              "hospitals": os.path.join(adir, "hospitals.geojson"),
              "schools": os.path.join(adir, "schools.geojson"),
              "flood": flood_path}
    json.dump(bundle, open(meta, "w"), indent=2)
    return bundle


def _feature(geom, props):
    return {"type": "Feature", "properties": props, "geometry": mapping(geom)}


def _write(adir, layer, features):
    json.dump({"type": "FeatureCollection", "features": features},
              open(os.path.join(adir, f"{layer}.geojson"), "w"))
