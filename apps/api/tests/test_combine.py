"""S3 — the Layer-2 combine engine. Fast tests exercise the pure numpy core
(Risk = clip(round(Hazard * weighted-avg(Vulnerability) / 5), 1, 5)) on hand-computed
grids; the [slow] test combines real reclassed layers into a risk grid on the flood
grid. Run slow with: GRP_RUN_SLOW=1 pytest tests/test_combine.py
"""
import json
import os

import numpy as np
import pytest
import rasterio
from shapely.geometry import box, mapping

from app.graph.geo import combine

slow = pytest.mark.skipif(
    os.getenv("GRP_RUN_SLOW") != "1", reason="slow: live Drive download (set GRP_RUN_SLOW=1)")


def test_combine_matches_hand_computation():
    """S3.T1 [fast] Risk = clip(round(H * weighted-avg(V) / 5), 1, 5), exact on known grids."""
    h = np.full((3, 3), 4, dtype="uint8")
    va = np.full((3, 3), 3, dtype="uint8")
    vb = np.full((3, 3), 5, dtype="uint8")
    risk = combine._combine(h, [va, vb], [0.5, 0.5])
    assert (risk == 3).all()                       # V=4 -> round(4*4/5)=round(3.2)=3


def test_nodata_vuln_cell_is_dropped_and_renormalized():
    """S3.T2 [fast] a nodata vulnerability cell is dropped and its weight renormalized (R6)."""
    h = np.full((1, 2), 4, dtype="uint8")
    va = np.array([[0, 3]], dtype="uint8")         # left cell nodata
    vb = np.array([[5, 5]], dtype="uint8")
    risk = combine._combine(h, [va, vb], [0.5, 0.5])
    assert risk[0, 0] == 4                          # only vb valid -> V=5 -> round(4*5/5)=4
    assert risk[0, 1] == 3                          # V=4 -> 3


def test_hazard_nodata_yields_nodata():
    """S3.T3 [fast] a cell with no hazard (0) is nodata (0) in the risk grid."""
    h = np.array([[0, 4]], dtype="uint8")
    v = np.array([[5, 5]], dtype="uint8")
    risk = combine._combine(h, [v], [1.0])
    assert risk[0, 0] == 0 and risk[0, 1] == 4


def test_higher_weight_shifts_risk_up():
    """S3.T6 [fast] shifting weight toward the higher-class layer raises the risk (monotone)."""
    h = np.full((2, 2), 5, dtype="uint8")
    lo = np.full((2, 2), 1, dtype="uint8")
    hi = np.full((2, 2), 5, dtype="uint8")
    r_lo = combine._combine(h, [lo, hi], [0.9, 0.1])
    r_hi = combine._combine(h, [lo, hi], [0.1, 0.9])
    assert (r_hi >= r_lo).all() and (r_hi > r_lo).any()


def _minimal_aoi(tmp_path):
    adir = tmp_path / "aoi"
    adir.mkdir()
    b = box(103.16, 13.07, 103.24, 13.15)
    (adir / "admin.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": "t"}, "geometry": mapping(b)}]}))
    return {"name": "t", "admin": str(adir / "admin.geojson")}


# weights over the 3 layers cached in S1 (avoids extra downloads), summing to 1.0
_CACHED = {"vulnerability_pop_all_total": 0.4,
           "vulnerability_reclass_blddensity": 0.35,
           "vulnerability_reclass_road": 0.25}


@slow
def test_combine_l2_real_aoi(tmp_path):
    """S3.T4/T5 [slow] real AOI -> a 1-5 risk grid we computed, on the flood grid, not all-zero."""
    from app.graph.geo import align as al
    aoi = _minimal_aoi(tmp_path)
    ref = al.reference_grid(aoi, "hazard_flood")
    out = combine.combine_l2(aoi, "hazard_flood", vuln_weights=_CACHED)
    with rasterio.open(out) as r, rasterio.open(ref["path"]) as f:
        assert r.width == f.width and r.height == f.height and r.transform == f.transform
        vals = r.read(1)
    uniq = set(np.unique(vals).tolist())
    assert uniq.issubset({0, 1, 2, 3, 4, 5})       # valid 1-5 classes (0=nodata)
    assert uniq - {0}                              # real risk present, not all nodata
