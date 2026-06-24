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
    """Normalize a place name to a filesystem-safe slug (lowercase alphanumeric + hyphens).

    Args:
        place (str): raw place name, e.g. 'Siem Reap, Cambodia'

    Returns:
        str: slugified name, e.g. 'siem-reap-cambodia'
    """
    return re.sub(r"[^a-z0-9]+", "-", place.lower()).strip("-")


def _boundary(place):
    """Query Nominatim for the largest administrative boundary under the area cap.

    Args:
        place (str): free-text place name, e.g. 'Battambang' or 'Siem Reap, Cambodia'

    Returns:
        tuple[float, str, shapely.geometry.base.BaseGeometry]:
            (area_km2, display_name, boundary_polygon)

    Raises:
        ValueError: if no administrative boundary is found, or all results exceed AREA_CAP_KM2
    """
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
    """Run an Overpass QL query, rotating mirrors and backing off on rate-limit/timeout errors.

    Args:
        query (str): Overpass QL query string
        attempts (int): number of full mirror-rotation rounds before giving up (default 3)

    Returns:
        list[dict]: list of OSM element dicts from the 'elements' key of the Overpass JSON response

    Raises:
        RuntimeError: if all mirrors fail across all attempts
    """
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
    """Extract the file ID from a Google Drive share URL.

    Handles both '/d/<id>/' and '?id=<id>' URL forms.

    Args:
        url (str): Google Drive share URL

    Returns:
        str: the Drive file ID

    Raises:
        ValueError: if neither URL pattern is found
    """
    m = re.search(r"/d/([^/]+)", url) or re.search(r"[?&]id=([^&]+)", url)
    if not m:
        raise ValueError(f"cannot parse a Google Drive id from {url}")
    return m.group(1)


def source_raster(layer="hazard_flood"):
    """Return the local path to the full hazard raster, downloading it on first use.

    Args:
        layer (str): tiff catalog key (default 'hazard_flood'); must have a download_url
                     in conf/tiffs.yml if the file is not already cached

    Returns:
        str: absolute path to the raster file on disk

    Raises:
        ValueError: if the raster is missing and tiffs.yml has no download_url for it
    """
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
    """Return the AOI bundle for a place, fetching OSM data and clipping the raster if not cached.

    The bundle is a dict with keys: 'name', 'area_km2', 'counts', 'admin', 'roads',
    'hospitals', 'schools', 'flood' — each a path to the corresponding file on disk.
    Subsequent calls for the same place return instantly from the on-disk cache.

    Args:
        place (str): free-text place name, e.g. 'Battambang' or 'Siem Reap, Cambodia'

    Returns:
        dict: AOI bundle mapping layer names to absolute file paths

    Raises:
        ValueError: if the place has no administrative boundary or exceeds AREA_CAP_KM2
        RuntimeError: if the Overpass API is unavailable
    """
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
    """Wrap a Shapely geometry and property dict into a GeoJSON Feature dict.

    Args:
        geom: Shapely geometry (Point, LineString, Polygon, etc.)
        props (dict): GeoJSON properties to embed in the feature

    Returns:
        dict: GeoJSON Feature object
    """
    return {"type": "Feature", "properties": props, "geometry": mapping(geom)}


def _write(adir, layer, features):
    """Serialize a list of GeoJSON Feature dicts to {adir}/{layer}.geojson.

    Args:
        adir (str): directory path where the file will be written
        layer (str): base filename (without extension), e.g. 'roads' or 'hospitals'
        features (list[dict]): list of GeoJSON Feature objects
    """
    json.dump({"type": "FeatureCollection", "features": features},
              open(os.path.join(adir, f"{layer}.geojson"), "w"))
