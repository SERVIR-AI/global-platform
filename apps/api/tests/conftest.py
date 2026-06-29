"""Shared fixtures: a hermetic AOI bundle (real geojson + a tiny flood raster) and a
stub OpenAI client, so the graph/store can be tested without network or an API key.
"""
import json
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from app.config import get_settings


@pytest.fixture
def aoi(tmp_path):
    """A 0..0.01° square with the left half (lon < 0.005) at flood severity 3.

    hospitals: 2 (1 in flood) | schools: 3 (1 in flood) | one road crossing both halves.
    """
    def write(name, features):
        path = tmp_path / f"{name}.geojson"
        path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        return str(path)

    def point(lon, lat):
        return {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [lon, lat]}}

    admin = [{"type": "Feature", "properties": {"name": "Testville"}, "geometry": {
        "type": "Polygon", "coordinates": [[[0, 0], [0.01, 0], [0.01, 0.01], [0, 0.01], [0, 0]]]}}]
    roads = [{"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [
        [0.001, 0.005], [0.003, 0.005], [0.0049, 0.005], [0.007, 0.005], [0.009, 0.005]]}}]
    hospitals = [point(0.002, 0.005), point(0.008, 0.005)]            # 1 left (flood), 1 right (dry)
    schools = [point(0.003, 0.003), point(0.007, 0.007), point(0.009, 0.002)]  # 1 left, 2 right
    buildings = [point(0.002, 0.006), point(0.008, 0.006)]            # 1 left (flood), 1 right (dry)

    flood = tmp_path / "flood.tif"
    arr = np.zeros((10, 10), dtype="int16")
    arr[:, :5] = 3
    with rasterio.open(flood, "w", driver="GTiff", height=10, width=10, count=1, dtype="int16",
                       crs="EPSG:4326", transform=from_bounds(0, 0, 0.01, 0.01, 10, 10)) as dst:
        dst.write(arr, 1)

    return {"name": "Testville", "area_km2": 1, "counts": {},
            "admin": write("admin", admin), "roads": write("roads", roads),
            "hospitals": write("hospitals", hospitals), "schools": write("schools", schools),
            "buildings": write("buildings", buildings), "hazard_flood": str(flood)}


class StubClient:
    """Mimics the slice of the OpenAI SDK the nodes use. `route_response` is either
    ("text", content) for a decline or ("tool", name, args) for a tool call; the
    finalize call echoes the tool result so the number is quoted (grounded)."""

    def __init__(self, route_response):
        self.route_response = route_response
        self.calls = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, model, messages, **kwargs):
        self.calls += 1
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        if "tools" in kwargs:  # the route call
            kind = self.route_response[0]
            if kind == "text":
                msg = SimpleNamespace(content=self.route_response[1], tool_calls=None)
            else:
                _, name, args = self.route_response
                tc = SimpleNamespace(id="c1", type="function",
                                     function=SimpleNamespace(name=name, arguments=json.dumps(args)))
                msg = SimpleNamespace(content=None, tool_calls=[tc])
        else:  # the finalize call
            tool = next((m["content"] for m in messages if m.get("role") == "tool"), "")
            msg = SimpleNamespace(content=f"Result: {tool}", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


@pytest.fixture
def make_client():
    return StubClient


@pytest.fixture(autouse=True)
def _redirect_traces(tmp_path, monkeypatch):
    """Keep trace writes out of the repo's cache/ during tests."""
    monkeypatch.setattr(get_settings(), "traces_dir", tmp_path / "traces")


@pytest.fixture
def tif_writer(tmp_path):
    """Factory used across the BYOD tests: write a tiny real GeoTIFF and return its path."""
    def _write(data, *, crs="EPSG:4326", count=1, dtype="int16", nodata=None,
               bounds=(103.0, 13.0, 103.1, 13.1), name="t.tif"):
        h, w = data.shape
        path = tmp_path / name
        with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=count,
                           dtype=dtype, crs=crs, transform=from_bounds(*bounds, w, h),
                           nodata=nodata) as dst:
            for band in range(1, count + 1):
                dst.write(data.astype(dtype), band)
        return str(path)
    return _write


@pytest.fixture
def byod_env(tmp_path, monkeypatch):
    """Isolate BYOD state for a test: a tmp tiff cache + a cleared per-thread registry."""
    from app.graph.geo import byod_registry
    monkeypatch.setattr(get_settings(), "tiffs_dir", tmp_path / "tiffs")
    byod_registry.clear()
    yield tmp_path / "tiffs"
    byod_registry.clear()


@pytest.fixture
def log(request):
    """Narrate a test (visible with `pytest -s`): its purpose, the calls it makes,
    and the outputs it gets. Use log("CALL", ...), log("OUTPUT", ...), log("CHECK", ...)."""
    why = " ".join((request.node.function.__doc__ or "").split())
    print(f"\n{'─' * 74}\nTEST   {request.node.name}\nWHY    {why}")

    def _log(kind, msg=""):
        print(f"  {kind:<8}{msg}")

    return _log
