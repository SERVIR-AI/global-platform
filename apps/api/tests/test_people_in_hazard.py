"""A population-count raster summed by hazard class, on grids we can add up by hand."""
import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.graph.geo import store


def _write(path, arr, dtype):
    t = from_origin(100.0, 14.0, 0.01, 0.01)          # 10 x 10 cells, 0.01 deg
    with rasterio.open(path, "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
                       count=1, dtype=dtype, crs="EPSG:4326", transform=t) as d:
        d.write(arr.astype(dtype), 1)


@pytest.fixture
def aoi(tmp_path, monkeypatch):
    # hazard: left half class 0 (dry), columns 5-6 class 2, columns 7-9 class 4
    haz = np.zeros((10, 10), dtype="int8")
    haz[:, 5:7] = 2
    haz[:, 7:10] = 4
    _write(tmp_path / "hazard_flood.tif", haz, "int8")
    # people: 10 per cell everywhere -> 1000 in the box
    pop = np.full((10, 10), 10.0, dtype="float32")
    _write(tmp_path / "population_test.tif", pop, "float32")
    # boundary = the whole box
    poly = {"type": "Polygon", "coordinates": [[[100.0, 13.9], [100.1, 13.9],
                                                 [100.1, 14.0], [100.0, 14.0], [100.0, 13.9]]]}
    json.dump({"type": "FeatureCollection",
               "features": [{"type": "Feature", "geometry": poly, "properties": {"name": "box"}}]},
              open(tmp_path / "admin.geojson", "w"))
    from app.graph.geo import ingest
    monkeypatch.setattr(ingest, "source_raster", lambda layer: str(tmp_path / f"{layer}.tif"))
    monkeypatch.setattr(store.tiffs, "legend", lambda layer: {})
    return {"name": "box", "admin": str(tmp_path / "admin.geojson"),
            "hazard_flood": str(tmp_path / "hazard_flood.tif")}


def test_people_are_summed_by_hazard_class(aoi):
    r = store.people_in_hazard(aoi, "hazard_flood", "population_test", min_severity=1)
    assert r["total"] == 1000
    assert r["by_severity"] == {1: 0, 2: 200, 3: 0, 4: 300, 5: 0}
    assert r["count"] == 500


def test_min_severity_cuts_the_low_classes(aoi):
    r = store.people_in_hazard(aoi, "hazard_flood", "population_test", min_severity=3)
    assert r["count"] == 300
    assert r["method"].startswith("people_in_hazard")


def test_the_polygon_mask_is_honoured(aoi, tmp_path):
    # shrink the boundary to the eastern (hazard class 4) three columns only
    poly = {"type": "Polygon", "coordinates": [[[100.07, 13.9], [100.1, 13.9],
                                                 [100.1, 14.0], [100.07, 14.0], [100.07, 13.9]]]}
    json.dump({"type": "FeatureCollection",
               "features": [{"type": "Feature", "geometry": poly, "properties": {"name": "east"}}]},
              open(tmp_path / "admin.geojson", "w"))
    r = store.people_in_hazard(aoi, "hazard_flood", "population_test")
    assert r["total"] == 300
    assert r["by_severity"][4] == 300 and r["by_severity"][2] == 0
