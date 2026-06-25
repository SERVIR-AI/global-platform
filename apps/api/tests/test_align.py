"""S2 — grid alignment. Fast tests exercise the pure resampling on synthetic grids
(no network); the [slow] test aligns a real vulnerability layer onto the real flood
grid for an AOI. Run slow with: GRP_RUN_SLOW=1 pytest tests/test_align.py
"""
import json
import os

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from shapely.geometry import box, mapping

from app.graph.geo import align

slow = pytest.mark.skipif(
    os.getenv("GRP_RUN_SLOW") != "1", reason="slow: live Drive download (set GRP_RUN_SLOW=1)")
CRS84 = CRS.from_epsg(4326)


def _ref(bounds, w, h):
    return {"transform": from_bounds(*bounds, w, h), "crs": CRS84, "width": w, "height": h}


def test_resample_matches_reference_shape():
    """S2.T1 [fast] resampling lands exactly on the reference grid's shape (coarser & finer)."""
    bounds = (0, 0, 1, 1)
    src = (np.arange(100).reshape(10, 10) % 6).astype("int8")
    src_t = from_bounds(*bounds, 10, 10)
    for w, h in [(5, 5), (20, 20)]:
        out = align._resample_onto(src, src_t, CRS84, _ref(bounds, w, h))
        assert out.shape == (h, w)


def test_nearest_invents_no_new_classes():
    """S2.T2 [fast] nearest resampling onto a finer grid never creates a class outside the source."""
    bounds = (0, 0, 1, 1)
    src = (np.arange(100).reshape(10, 10) % 5 + 1).astype("int8")   # values 1..5
    out = align._resample_onto(src, from_bounds(*bounds, 10, 10), CRS84, _ref(bounds, 30, 30))
    assert set(np.unique(out).tolist()).issubset({0, 1, 2, 3, 4, 5})   # 0 = fill only


def _minimal_aoi(tmp_path):
    """An AOI bundle with only an admin boundary — enough for hazard_clip/align (no OSM fetch)."""
    adir = tmp_path / "aoi"
    adir.mkdir()
    boundary = box(103.16, 13.07, 103.24, 13.15)
    (adir / "admin.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": "test"}, "geometry": mapping(boundary)}]}))
    return {"name": "test", "admin": str(adir / "admin.geojson")}


@slow
def test_align_real_layer_onto_hazard_grid(tmp_path):
    """S2.T3/T4 [slow] a real vuln layer aligns onto the flood grid: identical grid, classes 0-5."""
    aoi = _minimal_aoi(tmp_path)
    ref = align.reference_grid(aoi, "hazard_flood")
    out = align.align_to(ref, "vulnerability_reclass_blddensity", aoi)
    with rasterio.open(out) as a, rasterio.open(ref["path"]) as r:
        assert a.width == r.width and a.height == r.height       # same grid
        assert a.transform == r.transform
        assert a.crs == r.crs
        vals = a.read(1)
    uniq = set(np.unique(vals).tolist())
    assert uniq.issubset({0, 1, 2, 3, 4, 5})                     # no invented classes
    assert uniq - {0}                                            # real classes present (not all nodata)
