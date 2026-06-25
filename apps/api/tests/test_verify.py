"""S1 — the verification pass. Fast tests exercise the windowed-stats helper on a
synthetic raster (no network). The [slow] tests open the REAL rasters (live Drive
pull, then cached) and assert the schema contract — including the proven traps:
global_pc_h100glob is millimetres (not metres), and a wrong 'metres' declaration must FAIL.
Run the slow ones with: GRP_RUN_SLOW=1 pytest tests/test_verify.py
"""
import os

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from app.graph.geo import verify
from app.graph.geo.rasterstats import windowed_stats

slow = pytest.mark.skipif(
    os.getenv("GRP_RUN_SLOW") != "1", reason="slow: live Drive download (set GRP_RUN_SLOW=1)")


def _make_raster(path, arr, nodata=None, crs="EPSG:4326"):
    h, w = arr.shape
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype=arr.dtype.name, crs=crs, nodata=nodata,
                       transform=from_bounds(0, 0, 0.01, 0.01, w, h)) as dst:
        dst.write(arr, 1)


def test_windowed_stats_reads_dtype_range_nodata(tmp_path):
    """S1.T1 [fast] windowed_stats reports dtype/min/max/distinct/crs from a synthetic 1-5 grid."""
    arr = (np.arange(100).reshape(10, 10) % 6).astype("int8")     # values 0..5
    p = tmp_path / "synthetic.tif"
    _make_raster(str(p), arr)
    s = windowed_stats(str(p))
    assert s["dtype"] == "int8"
    assert s["sampled_min"] == 0 and s["sampled_max"] == 5
    assert s["sampled_distinct"] == 6
    assert s["nodata"] is None and s["crs_epsg"] == 4326


def test_windowed_stats_excludes_nodata(tmp_path):
    """S1.T1 [fast] nodata cells are excluded from the sampled range."""
    arr = (np.arange(100).reshape(10, 10) % 6).astype("int8")
    p = tmp_path / "nd.tif"
    _make_raster(str(p), arr, nodata=0)
    s = windowed_stats(str(p))
    assert s["sampled_min"] == 1            # 0 is nodata -> excluded
    assert s["nodata"] == 0


@slow
def test_verify_hazard_flood_passes():
    """S1.T2 [slow] hazard_flood is int8 0-5 and PASSes its declared schema."""
    r = verify.verify_raster("hazard_flood")
    assert r.ok, r.mismatches
    assert r.observed["dtype"] == "int8"
    assert r.observed["sampled_min"] >= 0 and r.observed["sampled_max"] <= 5


@slow
def test_verify_pop_passes_and_reports_range():
    """S1.T3 [slow] population reclass passes; the report exposes its sampled 1-5 range."""
    r = verify.verify_raster("vulnerability_pop_all_total")
    assert r.ok, r.mismatches
    assert r.observed["dtype"] == "uint8"
    assert r.observed["sampled_max"] <= 5


@slow
def test_verify_global_depth_is_millimetres():
    """S1.T4 [slow] the raw flood layer is uint32 with a thousands-scale max -> millimetres, not metres."""
    r = verify.verify_raster("global_pc_h100glob")
    assert r.ok, r.mismatches
    assert r.observed["dtype"] == "uint32"
    assert r.observed["sampled_max"] > 100          # thousands of mm — never <= 5 metres


@slow
def test_verify_fails_on_wrong_units_declaration():
    """S1.T5 [slow] declaring the mm raw file as metres/1-5 must FAIL verify with a max mismatch."""
    wrong = {"dtype": "uint32", "valid_min": 0, "valid_max": 5, "units": "metres", "nodata": 0.0}
    r = verify.verify_raster("global_pc_h100glob", schema=wrong)
    assert not r.ok
    assert any("valid_max" in m for m in r.mismatches)
