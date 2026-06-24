"""The deterministic spatial operations — the only place a number is computed.

Vectors via shapely, hazard severity via rasterio. No LLM here.
"""
import json
import math
import warnings

warnings.filterwarnings("ignore")
import rasterio
from shapely.geometry import Point, shape

from . import hazards, narrate, registry
from .ingest import ensure_aoi, hazard_clip


def count_features(place, layer):
    """Count hospitals or schools inside `place` (no hazard)."""
    if layer not in registry.COUNTABLE:
        raise ValueError(f"unknown layer: {layer}")
    aoi = ensure_aoi(place)
    boundary = _boundary(aoi)
    narrate.step("overlay (deterministic, no LLM)")
    narrate.detail(f"counting {layer} inside the boundary (no hazard)")
    n = sum(1 for ft in _features(aoi[layer])
            if boundary.contains(Point(ft["geometry"]["coordinates"])))
    narrate.detail(f"count_features(place='{aoi['name']}', layer='{layer}') → {n}")
    return {"count": n, "layer": layer, "place": aoi["name"],
            "source": aoi[layer], "method": "count_features"}


def count_in_hazard(place, layer, hazard="flood", min_severity=1):
    """Count hospitals or schools, broken down by `hazard` severity class (1-5)."""
    if layer not in registry.COUNTABLE:
        raise ValueError(f"unknown layer: {layer}")
    aoi = ensure_aoi(place)
    boundary = _boundary(aoi)
    sev = _Severity(hazard_clip(place, hazard))
    narrate.step("overlay (deterministic, no LLM)")
    narrate.detail(f"sampling the {hazard} severity class (1-5) under each {layer[:-1]}")
    by_severity = {s: 0 for s in range(1, 6)}
    for ft in _features(aoi[layer]):
        x, y = ft["geometry"]["coordinates"]
        if boundary.contains(Point(x, y)):
            s = sev.value(x, y)
            if s >= 1:
                by_severity[s] += 1
    count = sum(c for s, c in by_severity.items() if s >= min_severity)
    narrate.detail(f"count_in_hazard(place='{aoi['name']}', layer='{layer}', "
                   f"hazard='{hazard}', min_severity={min_severity}) → {count}")
    return {"count": count, "by_severity": by_severity, "layer": layer, "hazard": hazard,
            "legend": hazards.legend_dict(hazard), "place": aoi["name"], "min_severity": min_severity,
            "source": f"{hazards.HAZARDS[hazard]['tif']} × {layer}", "method": "count_in_hazard"}


def roads_in_hazard(place, hazard="flood", min_severity=1):
    """Road length (km), broken down by `hazard` severity class (1-5)."""
    aoi = ensure_aoi(place)
    boundary = _boundary(aoi)
    sev = _Severity(hazard_clip(place, hazard))
    narrate.step("overlay (deterministic, no LLM)")
    narrate.detail(f"walking each road segment, sampling the {hazard} severity class (1-5) under it")
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
            s = sev.value(*mid)
            if s >= 1:
                by_severity[s] += length
    by_severity = {s: round(km, 1) for s, km in by_severity.items()}
    affected = round(sum(km for s, km in by_severity.items() if s >= min_severity), 1)
    narrate.detail(f"roads_in_hazard(place='{aoi['name']}', hazard='{hazard}', "
                   f"min_severity={min_severity}) → {affected} km of {round(total, 1)} km total")
    return {"length_km": affected, "total_road_km": round(total, 1), "by_severity": by_severity,
            "hazard": hazard, "legend": hazards.legend_dict(hazard), "place": aoi["name"],
            "min_severity": min_severity, "source": f"{hazards.HAZARDS[hazard]['tif']} × roads",
            "method": "roads_in_hazard"}


class _Severity:
    """Hazard severity raster: class 0 (none) .. 5 (very high) at a coordinate."""
    def __init__(self, path):
        self.src = rasterio.open(path)
        self.arr = self.src.read(1)

    def value(self, lon, lat):
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
