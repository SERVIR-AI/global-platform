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

    The platform's class layers say how dense a cell is; this says how many people
    are in it. The count grid is resampled onto the hazard clip's grid with a
    sum-preserving method where the toolchain allows it, masked to the AOI polygon,
    and summed per class. Returns whole people, rounded.
    """
    import numpy as np
    import rasterio.features
    import rasterio.warp
    from . import ingest
    boundary = _boundary(aoi)
    with rasterio.open(aoi[hazard]) as hz:
        haz = hz.read(1).astype("float64")
        transform, crs, shape_ = hz.transform, hz.crs, hz.shape
    haz[~np.isfinite(haz)] = 0
    pop_on_grid = np.zeros(shape_, dtype="float64")
    with rasterio.open(ingest.source_raster(pop_layer)) as src:
        same_grid = (src.crs == crs
                     and abs(abs(src.res[0]) - abs(transform.a)) < 1e-7
                     and abs(abs(src.res[1]) - abs(transform.e)) < 1e-7)
        if same_grid:
            # Same CRS and pixel size: read the source window that covers the
            # hazard clip and lay it on directly. No resampling, so no mass is
            # lost — GDAL's sum resampling dropped a third of a town's residents
            # between two equal-resolution grids, and put none on the flooded cells.
            from rasterio.windows import from_bounds
            h, w = shape_
            left, top = transform * (0, 0)
            right, bottom = transform * (w, h)
            win = from_bounds(left, bottom, right, top, src.transform)
            r0, c0 = int(round(win.row_off)), int(round(win.col_off))
            full = rasterio.windows.Window(0, 0, src.width, src.height)
            req = rasterio.windows.Window(c0, r0, w, h)
            try:
                got = req.intersection(full)
            except rasterio.errors.WindowError:
                got = None
            if got is not None and got.width > 0 and got.height > 0:
                arr = src.read(1, window=got).astype("float64")
                rr = int(got.row_off) - r0
                cc = int(got.col_off) - c0
                pop_on_grid[rr:rr + arr.shape[0], cc:cc + arr.shape[1]] = arr
            method = "aligned window read (same grid; no resampling)"
        else:
            resampling = getattr(rasterio.warp.Resampling, "sum",
                                 rasterio.warp.Resampling.nearest)
            method = ("resample:sum (count-preserving)" if resampling.name == "sum"
                      else "resample:nearest (approximate)")
            rasterio.warp.reproject(
                source=rasterio.band(src, 1), destination=pop_on_grid,
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=transform, dst_crs=crs,
                src_nodata=src.nodata, dst_nodata=0.0, resampling=resampling)
    pop_on_grid[~np.isfinite(pop_on_grid)] = 0
    pop_on_grid[pop_on_grid < 0] = 0
    inside = rasterio.features.geometry_mask([boundary.__geo_interface__], out_shape=shape_,
                                             transform=transform, invert=True)
    total = float(pop_on_grid[inside].sum())
    by_severity = {}
    for s in range(1, 6):
        by_severity[s] = int(round(float(pop_on_grid[inside & (haz == s)].sum())))
    count = sum(c for s, c in by_severity.items() if s >= min_severity)
    return {"count": int(count), "total": int(round(total)), "by_severity": by_severity,
            "legend": tiffs.legend(hazard), "hazard": hazard, "layer": pop_layer,
            "place": aoi["name"], "min_severity": min_severity,
            "source": f"{hazard}.tif × {pop_layer}", "method": f"people_in_hazard; {method}"}

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
