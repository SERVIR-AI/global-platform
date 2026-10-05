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
    if layer not in registry.countable():
        raise ValueError(f"unknown layer: {layer}")
    boundary = _boundary(aoi)
    n = sum(1 for ft in _features(aoi[layer])
            if boundary.contains(Point(ft["geometry"]["coordinates"])))
    return {"count": n, "layer": layer, "place": aoi["name"],
            "source": f"OSM {layer}", "method": "count_features"}


def count_in_hazard(aoi, hazard, layer, min_severity=1):
    """Count hospitals or schools by `hazard` severity class (1-5)."""
    if layer not in registry.countable():
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



def people_in_hazard(aoi, hazard, pop_layer, min_severity=1):
    """Sum a population-COUNT raster inside `aoi` by `hazard` severity class.

    The counts are never resampled: moving people between cells invents or loses
    them (GDAL's sum resampling once reported "0 of 0 people" over a town). The
    hazard CLASSES are reprojected onto the count grid instead — nearest, so a
    class never blends — and the counts are summed where they already are. The
    method text declares the hazard grid's resolution and its offset from the
    count grid, because at the scale of one cell the headcount is only as sharp
    as the coarser grid.
    """
    import numpy as np
    import rasterio.features
    import rasterio.warp
    from rasterio.windows import from_bounds
    from . import ingest
    boundary = _boundary(aoi)
    with rasterio.open(aoi[hazard]) as hz:
        haz = hz.read(1)
        haz = np.where(np.isfinite(haz), haz, 0).astype("int16")
        hz_T, hz_crs, hz_res = hz.transform, hz.crs, abs(hz.res[0])
        h, w = haz.shape
        left, top = hz_T * (0, 0)
        right, bottom = hz_T * (w, h)
    with rasterio.open(ingest.source_raster(pop_layer)) as pop:
        win = from_bounds(left, bottom, right, top, pop.transform).round_offsets().round_lengths()
        full = rasterio.windows.Window(0, 0, pop.width, pop.height)
        try:
            win = win.intersection(full)
        except rasterio.errors.WindowError:
            win = None
        if win is None or win.width <= 0 or win.height <= 0:
            raise ValueError(f"{aoi.get('name', 'this area')} lies outside {pop_layer}'s coverage")
        counts = pop.read(1, window=win).astype("float64")
        pop_T, pop_crs, pop_res = pop.window_transform(win), pop.crs, abs(pop.res[0])
    counts[~np.isfinite(counts)] = 0
    counts[counts < 0] = 0
    classes = np.zeros(counts.shape, dtype="int16")
    rasterio.warp.reproject(haz, classes, src_transform=hz_T, src_crs=hz_crs,
                            dst_transform=pop_T, dst_crs=pop_crs, dst_nodata=0,
                            resampling=rasterio.warp.Resampling.nearest)
    inside = rasterio.features.geometry_mask([boundary.__geo_interface__],
                                             out_shape=counts.shape, transform=pop_T, invert=True)
    total = float(counts[inside].sum())
    by_severity = {s: int(round(float(counts[inside & (classes == s)].sum()))) for s in range(1, 6)}
    count = sum(c for s, c in by_severity.items() if s >= min_severity)
    # Honesty about the grids: resolution ratio and sub-cell offset.
    ratio = hz_res / pop_res if pop_res else 1.0
    off_x = abs(((hz_T.c - pop_T.c) / pop_T.a) % 1.0)
    off_y = abs(((hz_T.f - pop_T.f) / pop_T.e) % 1.0)
    off = max(min(off_x, 1 - off_x), min(off_y, 1 - off_y))
    note = (f"hazard classes reprojected (nearest) onto the {pop_res * 111000:.0f} m count grid; "
            f"hazard grid is {hz_res * 111000:.0f} m")
    if ratio > 1.5:
        note += (f" — {ratio:.0f}x coarser than the counts, so class membership is "
                 f"decided at the hazard grid's scale")
    if off > 0.1 and ratio < 1.5:
        note += (f"; the two grids are offset by {off:.1f} of a cell, so people in cells on "
                 "a class boundary can move by one cell")
    return {"count": int(count), "total": int(round(total)), "by_severity": by_severity,
            "legend": tiffs.legend(hazard), "hazard": hazard, "layer": pop_layer,
            "place": aoi["name"], "min_severity": min_severity,
            "source": f"{hazard}.tif × {pop_layer}", "method": f"people_in_hazard; {note}"}

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
