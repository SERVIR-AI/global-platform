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

from . import registry


def count_features(aoi, layer):
    """Count POIs of the given layer that fall inside the AOI's admin boundary.

    Args:
        aoi (dict): AOI bundle from ingest.ensure_aoi
        layer (str): POI layer to count — must be one of registry.COUNTABLE ('hospitals', 'schools')

    Returns:
        dict: {'count': int, 'layer': str, 'place': str, 'source': str, 'method': str}

    Raises:
        ValueError: if `layer` is not in registry.COUNTABLE
    """
    if layer not in registry.COUNTABLE:
        raise ValueError(f"unknown layer: {layer}")
    boundary = _boundary(aoi)
    n = sum(1 for ft in _features(aoi[layer])
            if boundary.contains(Point(ft["geometry"]["coordinates"])))
    return {"count": n, "layer": layer, "place": aoi["name"],
            "source": aoi[layer], "method": "count_features"}


def count_in_flood(aoi, layer, min_severity=1):
    """Count POIs inside the admin boundary that also sit in flood zones at or above a threshold.

    Args:
        aoi (dict): AOI bundle from ingest.ensure_aoi
        layer (str): POI layer — must be one of registry.COUNTABLE ('hospitals', 'schools')
        min_severity (int): minimum flood severity to count (1–5, default 1)

    Returns:
        dict: {'count': int, 'layer': str, 'place': str, 'min_severity': int,
               'source': str, 'method': str}

    Raises:
        ValueError: if `layer` is not in registry.COUNTABLE
    """
    if layer not in registry.COUNTABLE:
        raise ValueError(f"unknown layer: {layer}")
    boundary = _boundary(aoi)
    flood = _Flood(aoi["flood"])
    hits = [ft for ft in _features(aoi[layer])
            if boundary.contains(Point(ft["geometry"]["coordinates"]))
            and flood.severity(*ft["geometry"]["coordinates"]) >= min_severity]
    return {"count": len(hits), "layer": layer, "place": aoi["name"],
            "min_severity": min_severity, "source": f"{aoi['flood']} × {aoi[layer]}",
            "method": "count_in_flood"}


def roads_in_flood(aoi, min_severity=1):
    """Compute the length of road within the admin boundary that sits in flood zones.

    Uses haversine distances over road segment midpoints to decide flood exposure.

    Args:
        aoi (dict): AOI bundle from ingest.ensure_aoi
        min_severity (int): minimum flood severity to count (1–5, default 1)

    Returns:
        dict: {'length_km': float, 'total_road_km': float, 'place': str,
               'min_severity': int, 'source': str, 'method': str}
    """
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
        """Open the raster and cache band 1 in memory for repeated point lookups.

        Args:
            path (str): path to a GeoTIFF flood hazard raster
        """
        self.src = rasterio.open(path)
        self.arr = self.src.read(1)

    def severity(self, lon, lat):
        """Look up flood severity (0–5) at a longitude/latitude point.

        Args:
            lon (float): longitude in decimal degrees
            lat (float): latitude in decimal degrees

        Returns:
            int: severity value 0 (dry or out of bounds) through 5 (extreme flood)
        """
        try:
            row, col = self.src.index(lon, lat)
        except Exception:
            return 0
        if 0 <= row < self.arr.shape[0] and 0 <= col < self.arr.shape[1]:
            return max(int(self.arr[row, col]), 0)
        return 0


def haversine_km(a, b):
    """Return the great-circle distance in kilometres between two (lon, lat) points.

    Args:
        a (tuple[float, float]): (longitude, latitude) of the first point in decimal degrees
        b (tuple[float, float]): (longitude, latitude) of the second point in decimal degrees

    Returns:
        float: distance in kilometres
    """
    R = 6371.0088
    lon1, lat1, lon2, lat2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _features(path):
    """Load the features list from a GeoJSON file.

    Args:
        path (str): path to a GeoJSON FeatureCollection file

    Returns:
        list[dict]: list of GeoJSON Feature objects
    """
    return json.load(open(path))["features"]


def _boundary(aoi):
    """Return the admin boundary polygon from an AOI bundle as a Shapely geometry.

    Args:
        aoi (dict): AOI bundle from ingest.ensure_aoi (must have an 'admin' key)

    Returns:
        shapely.geometry.base.BaseGeometry: the admin boundary polygon
    """
    return shape(_features(aoi["admin"])[0]["geometry"])
