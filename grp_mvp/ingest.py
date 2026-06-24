"""Resolve a place name to real cached data — OSM boundary + roads + POIs — and
clip any hazard severity raster (downloaded from Drive by id) to it. Raises
ValueError if the place can't be resolved or is too large; never silently falls back.
"""
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

from . import hazards, narrate

HEADERS = {"User-Agent": "grp-mvp/0.1 (disaster-risk research prototype)"}
NOMINATIM = "https://nominatim.openstreetmap.org"
OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
CACHE = os.path.join(os.path.dirname(__file__), "cache")
AREA_CAP_KM2 = 1500.0
BUFFER_DEG = 0.01
RADIUS_KM = 12.0          # fallback AOI: box of this radius around the centre point
_aoi_narrated = set()     # places already narrated this process (ensure_aoi runs twice/query)


def _slug(place):
    return re.sub(r"[^a-z0-9]+", "-", place.lower()).strip("-")


def _boundary(place):
    """Resolve `place` to (km², name, geometry, how). Try the name as given; if no admin
    boundary under the cap turns up but Nominatim recognised the place, retry with the
    canonical name it returned — this recovers the real boundary for typo'd cities
    (e.g. 'Batambang' -> Battambang's 115 km²). Only if that still finds no boundary do
    we box a radius around the centre point. Raises only when the place can't be found.
    `how` is a short plain-text record of the decision, also stored for cached runs."""
    narrate.step(f"resolving the boundary for '{place}'")
    results = _search(place)
    narrate.detail(f'searched OpenStreetMap (Nominatim) for "{place}" → {len(results)} result(s)')
    if not results:
        raise ValueError(f"could not find '{place}' (try 'City, Country')")

    hit = _under_cap_admin(results)
    if hit:
        narrate.detail(f"found an admin boundary under the {AREA_CAP_KM2:.0f} km² cap: "
                       f"{hit[1]} ≈ {hit[0]:.0f} km²")
        narrate.decision(f"use the admin polygon for {hit[1]} ({hit[0]:.0f} km²)")
        return (*hit, f"admin boundary ≈ {hit[0]:.0f} km²")

    center = _best_center(results)
    canonical = center["display_name"].split(",")[0]
    narrate.detail(f"no admin area under {AREA_CAP_KM2:.0f} km² in those results "
                   f"(top hit is the point '{canonical}')")
    if canonical.strip().lower() != place.strip().lower():   # typo -> retry corrected name
        narrate.detail(f'Nominatim recognised it as "{canonical}" → re-querying the corrected name')
        retry = _search(canonical)
        hit = _under_cap_admin(retry)
        if hit:
            narrate.detail(f'"{canonical}" returned an admin boundary {hit[1]} ≈ {hit[0]:.0f} km²')
            narrate.decision(f"corrected '{place}' → '{canonical}', use its admin polygon ({hit[0]:.0f} km²)")
            return (*hit, f"admin boundary ≈ {hit[0]:.0f} km² (corrected '{place}' → '{canonical}')")
        narrate.detail(f'"{canonical}" still has no boundary under the cap')
        if retry:
            center = _best_center(retry)

    lon, lat = float(center["lon"]), float(center["lat"])    # genuinely no boundary -> box
    dlat = RADIUS_KM / 111.0
    dlon = RADIUS_KM / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    g = box(lon - dlon, lat - dlat, lon + dlon, lat + dlat)
    name = f"{center['display_name'].split(',')[0]} (~{RADIUS_KM:.0f} km radius)"
    narrate.decision(f"no boundary small enough → {RADIUS_KM:.0f} km box around the centre "
                     f"({lat:.3f}, {lon:.3f})")
    return (g.area * 111.0 * 108.0, name, g, f"{RADIUS_KM:.0f} km radius box (no admin boundary under cap)")


def _search(place):
    r = requests.get(f"{NOMINATIM}/search", headers=HEADERS, timeout=40, params={
        "q": place, "format": "json", "polygon_geojson": 1, "limit": 10, "accept-language": "en"})
    r.raise_for_status()
    return r.json()


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


def ensure_hazard(hazard):
    """Download the SEA severity raster for `hazard` once (by Drive id); return its path."""
    tif = hazards.HAZARDS[hazard]["tif"]
    os.makedirs(os.path.join(CACHE, "_source"), exist_ok=True)
    legacy = os.path.join(CACHE, "_source", "hazard_flood_sea.tif")   # flood, downloaded earlier
    if tif == "hazard_flood.tif" and os.path.exists(legacy):
        return legacy
    path = os.path.join(CACHE, "_source", tif)
    if not os.path.exists(path):
        narrate.detail(f"source raster {tif}: downloading from Google Drive "
                       f"(id {hazards.DRIVE_TIFS[tif][:10]}…)")
        import gdown
        gdown.download(id=hazards.DRIVE_TIFS[tif], output=path, quiet=True)
    else:
        narrate.detail(f"source raster {tif}: cached")
    return path


def hazard_clip(place, hazard):
    """Clip `hazard`'s severity raster to the AOI; cache per (place, hazard)."""
    aoi = ensure_aoi(place)
    clip = os.path.join(os.path.dirname(aoi["admin"]), f"{hazard}.tif")
    tif = hazards.HAZARDS[hazard]["tif"]
    narrate.step(f"hazard raster — {hazard}")
    if not os.path.exists(clip):
        boundary = shape(json.load(open(aoi["admin"]))["features"][0]["geometry"])
        minx, miny, maxx, maxy = boundary.bounds
        with rasterio.open(ensure_hazard(hazard)) as src:
            win = from_bounds(minx - BUFFER_DEG, miny - BUFFER_DEG,
                              maxx + BUFFER_DEG, maxy + BUFFER_DEG, src.transform)
            arr = src.read(1, window=win)
            prof = src.profile | {"height": arr.shape[0], "width": arr.shape[1],
                                  "transform": src.window_transform(win), "compress": "lzw"}
            with rasterio.open(clip, "w", **prof) as dst:
                dst.write(arr, 1)
        narrate.detail(f"{tif} → clipped to the AOI window ({arr.shape[1]}×{arr.shape[0]} px)")
    else:
        narrate.detail(f"{tif} → AOI clip cached")
    return clip


def ensure_aoi(place):
    """Return a cached bundle of file paths for `place`, fetching it if needed."""
    adir = os.path.join(CACHE, _slug(place) or "_")
    meta = os.path.join(adir, "meta.json")
    if os.path.exists(meta):
        info = json.load(open(meta))
        _narrate_aoi(place, info, cached=True)
        return _bundle(adir, info)

    km2, name, boundary, how = _boundary(place)
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

    buildings = []
    for e in _overpass(f'[out:json][timeout:170];way["building"]({bbox});out center;'):
        c = e.get("center") or {}
        if c and boundary.contains(Point(c["lon"], c["lat"])):
            buildings.append(_feature(Point(c["lon"], c["lat"]), {}))
    _write(adir, "buildings", buildings)
    counts["buildings"] = len(buildings)

    info = {"name": name, "area_km2": round(km2), "how": how, "counts": counts}
    json.dump(info, open(meta, "w"), indent=2)
    _narrate_aoi(place, info, cached=False)
    return _bundle(adir, info)


def _narrate_aoi(place, info, cached):
    """Narrate the boundary outcome (from cache) + the OSM exposure counts, once per place."""
    if not narrate.on() or _slug(place) in _aoi_narrated:
        return
    _aoi_narrated.add(_slug(place))
    if cached:
        how = info.get("how") or (f"{RADIUS_KM:.0f} km radius box" if "radius" in info["name"]
                                   else f"admin boundary ≈ {info['area_km2']} km²")
        narrate.step(f"resolving the boundary for '{place}'")
        narrate.detail(f"served from cache: {info['name']} — {how}")
    c = info["counts"]
    narrate.step("exposure from OpenStreetMap" + (" [cached]" if cached else " [just fetched]"))
    narrate.detail(f"roads {c['roads']} · hospitals {c['hospitals']} · "
                   f"schools {c['schools']} · buildings {c['buildings']}")


def _bundle(adir, info):
    """Rebuild file paths from the AOI dir, so the cache is portable across dirs/machines."""
    return {"name": info["name"], "area_km2": info["area_km2"], "counts": info["counts"],
            "admin": os.path.join(adir, "admin.geojson"),
            "roads": os.path.join(adir, "roads.geojson"),
            "hospitals": os.path.join(adir, "hospitals.geojson"),
            "schools": os.path.join(adir, "schools.geojson"),
            "buildings": os.path.join(adir, "buildings.geojson")}


def _feature(geom, props):
    return {"type": "Feature", "properties": props, "geometry": mapping(geom)}


def _write(adir, layer, features):
    json.dump({"type": "FeatureCollection", "features": features},
              open(os.path.join(adir, f"{layer}.geojson"), "w"))
