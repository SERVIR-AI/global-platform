"""The deterministic spatial operations — the only place a number is computed.

Vectors via shapely, the flood hazard via rasterio. No LLM here.
"""
import json
import math
import warnings

warnings.filterwarnings("ignore")
import rasterio
from shapely.geometry import Point, shape

from . import registry
from .ingest import ensure_aoi


def count_features(place, layer):
    """Count hospitals or schools inside `place`."""
    if layer not in registry.COUNTABLE:
        raise ValueError(f"unknown layer: {layer}")
    aoi = ensure_aoi(place)
    boundary = _boundary(aoi)
    n = sum(1 for ft in _features(aoi[layer])
            if boundary.contains(Point(ft["geometry"]["coordinates"])))
    return {"count": n, "layer": layer, "place": aoi["name"],
            "source": aoi[layer], "method": "count_features"}


def count_in_flood(place, layer, min_severity=1):
    """Count hospitals or schools sitting in flood severity >= min_severity."""
    if layer not in registry.COUNTABLE:
        raise ValueError(f"unknown layer: {layer}")
    aoi = ensure_aoi(place)
    boundary = _boundary(aoi)
    flood = _Flood(aoi["flood"])
    hits = [ft for ft in _features(aoi[layer])
            if boundary.contains(Point(ft["geometry"]["coordinates"]))
            and flood.severity(*ft["geometry"]["coordinates"]) >= min_severity]
    return {"count": len(hits), "layer": layer, "place": aoi["name"],
            "min_severity": min_severity, "source": f"{aoi['flood']} × {aoi[layer]}",
            "method": "count_in_flood"}


def roads_in_flood(place, min_severity=1):
    """Length (km) of road in `place` sitting in flood severity >= min_severity."""
    aoi = ensure_aoi(place)
    boundary = _boundary(aoi)
    flood = _Flood(aoi["flood"])
    flooded = total = 0.0
    for ft in _features(aoi["roads"]):
        coords = ft["geometry"]["coordinates"]
        for a, b in zip(coords, coords[1:]):
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            if not boundary.contains(Point(mid)):
                continue
            length = haversine_km(a, b)
            total += length
            if flood.severity(*mid) >= min_severity:
                flooded += length
    return {"length_km": round(flooded, 1), "total_road_km": round(total, 1),
            "place": aoi["name"], "min_severity": min_severity,
            "source": f"{aoi['flood']} × {aoi['roads']}", "method": "roads_in_flood"}


class _Flood:
    """Flood hazard raster: severity 0 (dry) .. 5 (extreme) at a coordinate."""
    def __init__(self, path):
        self.src = rasterio.open(path)
        self.arr = self.src.read(1)

    def severity(self, lon, lat):
        try:
            row, col = self.src.index(lon, lat)
        except Exception:
            return 0
        if 0 <= row < self.arr.shape[0] and 0 <= col < self.arr.shape[1]:
            return max(int(self.arr[row, col]), 0)
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
