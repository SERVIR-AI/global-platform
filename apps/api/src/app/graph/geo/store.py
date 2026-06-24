"""The deterministic spatial operations — the only place a number is computed.

Vectors via shapely, the flood hazard via rasterio. No LLM here. Each operation
takes an already-fetched `aoi` bundle (see ingest.ensure_aoi); fetching the data
and computing over it are kept separate so the graph can run them as two nodes.
"""
import json
import math
import warnings

warnings.filterwarnings("ignore")
import rasterio
from shapely.geometry import Point, shape

from . import registry, tiffs


def count_features(aoi, layer):
    """Count hospitals or schools inside `aoi`."""
    if layer not in registry.COUNTABLE:
        raise ValueError(f"unknown layer: {layer}")
    boundary = _boundary(aoi)
    n = sum(1 for ft in _features(aoi[layer])
            if boundary.contains(Point(ft["geometry"]["coordinates"])))
    return {"count": n, "layer": layer, "place": aoi["name"],
            "source": f"OSM {layer}", "method": "count_features"}


def count_in_hazard(aoi, hazard, layer, min_severity=1):
    """Count hospitals or schools by `hazard` severity class (1-5)."""
    if layer not in registry.COUNTABLE:
        raise ValueError(f"unknown layer: {layer}")
    boundary = _boundary(aoi)
    sev = _Severity(aoi[hazard])
    by_severity = {s: 0 for s in range(1, 6)}
    for ft in _features(aoi[layer]):
        coords = ft["geometry"]["coordinates"]
        if boundary.contains(Point(coords)):
            s = sev.severity(*coords)
            if s >= 1:
                by_severity[s] += 1
    count = sum(c for s, c in by_severity.items() if s >= min_severity)
    return {"count": count, "by_severity": by_severity, "legend": tiffs.legend(hazard),
            "hazard": hazard, "layer": layer, "place": aoi["name"], "min_severity": min_severity,
            "source": f"{hazard}.tif × {layer}", "method": "count_in_hazard"}


def roads_in_hazard(aoi, hazard, min_severity=1):
    """Length (km) of road in `aoi` by `hazard` severity class (1-5)."""
    boundary = _boundary(aoi)
    sev = _Severity(aoi[hazard])
    by_severity = {s: 0.0 for s in range(1, 6)}
    total = 0.0
    for ft in _features(aoi["roads"]):
        coords = ft["geometry"]["coordinates"]
        for a, b in zip(coords, coords[1:]):
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            if not boundary.contains(Point(mid)):
                continue
            length = haversine_km(a, b)
            total += length
            s = sev.severity(*mid)
            if s >= 1:
                by_severity[s] += length
    by_severity = {s: round(km, 1) for s, km in by_severity.items()}
    affected = round(sum(km for s, km in by_severity.items() if s >= min_severity), 1)
    return {"length_km": affected, "total_road_km": round(total, 1),
            "by_severity": by_severity, "legend": tiffs.legend(hazard),
            "hazard": hazard, "place": aoi["name"], "min_severity": min_severity,
            "source": f"{hazard}.tif × roads", "method": "roads_in_hazard"}


class _Severity:
    """Hazard severity raster: class 0 (none) .. 5 (extreme) at a coordinate."""
    def __init__(self, path):
        self.src = rasterio.open(path)
        self.arr = self.src.read(1)

    def severity(self, lon, lat):
        try:
            row, col = self.src.index(lon, lat)
        except Exception:
            return 0
        if 0 <= row < self.arr.shape[0] and 0 <= col < self.arr.shape[1]:
            v = self.arr[row, col]
            if v != v:               # NaN — nodata in float rasters (e.g. fire) -> no hazard
                return 0
            return max(int(v), 0)
        return 0


def haversine_km(a, b):
    R = 6371.0088
    lon1, lat1, lon2, lat2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _features(path):
    return json.load(open(path))["features"]


def _boundary(aoi):
    return shape(_features(aoi["admin"])[0]["geometry"])
