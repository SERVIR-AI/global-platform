"""Assemble the map-visualization payload added to the chat response.

ADDITIVE: the text answer and the computed numbers are untouched. This packages the
spatial data the frontend needs — the AOI polygon, the assets distributed in space
(each tagged with its severity), the hazard raster vectorized by class, and a
server-owned colored legend. Kept out of the LLM's result dict so it doesn't bloat
the model context.
"""
import json
import os
import warnings

warnings.filterwarnings("ignore")
import rasterio
from rasterio import features as rfeatures
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from . import store, tiffs

# A generic low->high severity ramp (1..5). One source of truth for feature colors,
# the raster ramp, and the legend swatches.
SEVERITY_COLORS = {1: "#ffffb2", 2: "#fecc5c", 3: "#fd8d3c", 4: "#f03b20", 5: "#bd0026"}
_DEFAULT_LABELS = {1: "Very low", 2: "Low", 3: "Moderate", 4: "High", 5: "Very high"}


def legend_colored(hazard):
    """{class: {label, color}} for `hazard` — server owns the colors. Falls back to a generic
    severity ramp when the layer carries no catalog legend (e.g. a user-uploaded BYOD layer)."""
    labels = tiffs.legend(hazard) if hazard else {}
    return {s: {"label": labels.get(s, _DEFAULT_LABELS[s]), "color": SEVERITY_COLORS[s]} for s in range(1, 6)}


def _features(path):
    return json.load(open(path))["features"]


def _aoi_feature(aoi):
    """The boundary as a GeoJSON Feature + its [minLon, minLat, maxLon, maxLat]."""
    feat = _features(aoi["admin"])[0]
    bounds = list(shape(feat["geometry"]).bounds)
    name = aoi.get("name", "")
    how = (aoi.get("how") or "").lower()
    source = "drawn" if "drawn" in how else "radius_box" if "radius" in name else "nominatim"
    return {**feat, "properties": {**feat.get("properties", {}), "source": source}}, bounds


def _asset_features(aoi, method, layer, sev):
    """Each asset as a GeoJSON Feature tagged with properties.severity (0 = none).
    Roads keep their geometry, tagged by the max severity they cross."""
    out = []
    if method == "roads_in_hazard":
        for ft in _features(aoi["roads"]):
            coords = ft["geometry"]["coordinates"]
            s = 0
            for a, b in zip(coords, coords[1:]):
                mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                s = max(s, sev.severity(*mid) if sev else 0)
            out.append({"type": "Feature", "geometry": ft["geometry"],
                        "properties": {**ft.get("properties", {}), "severity": s}})
    else:
        for ft in _features(aoi[layer]):
            x, y = ft["geometry"]["coordinates"]
            s = sev.severity(x, y) if sev else 0
            out.append({"type": "Feature", "geometry": ft["geometry"],
                        "properties": {**ft.get("properties", {}), "severity": s}})
    return {"type": "FeatureCollection", "features": out}


def _vectorized_hazard(clip_path):
    """Clipped severity raster -> GeoJSON polygons, one merged (multi)polygon per class."""
    with rasterio.open(clip_path) as src:
        arr = src.read(1)
        feats = []
        for cls in range(1, 6):
            m = (arr == cls).astype("uint8")
            if not m.any():
                continue
            geoms = [shape(g) for g, _ in rfeatures.shapes(m, mask=m == 1, transform=src.transform)]
            if geoms:
                feats.append({"type": "Feature", "properties": {"severity": cls},
                              "geometry": mapping(unary_union(geoms))})
    return {"type": "FeatureCollection", "features": feats}


def build_payload(aoi, result):
    """The visualization block merged into the chat response (additive)."""
    hazard = result.get("hazard")
    method = result.get("method")
    is_roads = method == "roads_in_hazard"
    layer = "roads" if is_roads else result.get("layer")

    aoi_feature, bounds = _aoi_feature(aoi)
    sev = store._Severity(aoi[hazard]) if hazard and hazard in aoi else None
    features = _asset_features(aoi, method, layer, sev)

    metric = {
        "value": result.get("length_km", result.get("count")),
        "unit": "km" if is_roads else "count",
        "total": result.get("total_road_km") if is_roads else len(features["features"]),
        "min_severity": result.get("min_severity", 1),
        "by_severity": result.get("by_severity"),
    }
    hazard_layer = None
    if hazard and hazard in aoi:
        slug = os.path.basename(os.path.dirname(aoi["admin"]))
        hazard_layer = {
            "raster_url": f"/api/raster/{slug}/{hazard}.tif",   # option A: ol/source/GeoTIFF
            "geojson": _vectorized_hazard(aoi[hazard]),         # option B: vectorized polygons
            "crs": "EPSG:4326",
        }

    return {
        "place": result.get("place"),
        "hazard": hazard,
        "layer": layer,
        "metric": metric,
        "legend": legend_colored(hazard) if hazard else None,
        "bounds": bounds,
        "aoi": aoi_feature,
        "features": features,
        "hazard_layer": hazard_layer,
    }
