"""S5 — validation against ADPC's precomputed risk_<hazard>.tif. Fast tests check the
agreement metric on synthetic grids; the [slow] tests diff a real computed L2 grid
against the reference and run the weight search. Run slow with:
GRP_RUN_SLOW=1 pytest tests/test_validate.py
"""
import json
import os

import numpy as np
import pytest
from shapely.geometry import box, mapping

from app.graph.geo import combine, validate

slow = pytest.mark.skipif(
    os.getenv("GRP_RUN_SLOW") != "1", reason="slow: live Drive download (set GRP_RUN_SLOW=1)")

_CACHED = {"vulnerability_pop_all_total": 0.4,
           "vulnerability_reclass_blddensity": 0.35,
           "vulnerability_reclass_road": 0.25}


def test_metrics_self_is_perfect_and_discriminates():
    """S5.T1 [fast] grid-vs-self = 100% agreement; a strongly-disagreeing grid scores 0."""
    g = np.array([[1, 2, 3], [4, 5, 0]], dtype="uint8")
    m = validate._metrics(g, g)
    assert m["exact_pct"] == 100.0 and m["within1_pct"] == 100.0 and m["mean_abs_diff"] == 0.0
    d = validate._metrics(np.full((2, 2), 5, "uint8"), np.full((2, 2), 1, "uint8"))
    assert d["exact_pct"] == 0.0 and d["within1_pct"] == 0.0 and d["mean_abs_diff"] == 4.0


def test_confusion_sums_to_n_cells():
    """S5.T2 [fast] the confusion-matrix counts sum to n_cells (the active union)."""
    ours = np.array([[1, 2, 0], [3, 0, 5]], dtype="uint8")
    oracle = np.array([[1, 2, 4], [2, 0, 5]], dtype="uint8")
    m = validate._metrics(ours, oracle)
    assert sum(m["confusion"].values()) == m["n_cells"] == 5   # (0,0) cell excluded (both nodata)


def _minimal_aoi(tmp_path):
    adir = tmp_path / "aoi"
    adir.mkdir()
    (adir / "admin.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": "t"},
             "geometry": mapping(box(103.16, 13.07, 103.24, 13.15))}]}))
    return {"name": "t", "admin": str(adir / "admin.geojson")}


@slow
def test_l2_agrees_with_adpc_reference(tmp_path):
    """S5.T3 [slow] our default-weight L2 risk agrees with ADPC risk_flood.tif above a floor."""
    aoi = _minimal_aoi(tmp_path)
    grid = combine.combine_l2(aoi, "hazard_flood", vuln_weights=_CACHED)
    d = validate.diff_against_risk(grid, "hazard_flood", aoi)
    assert d["n_cells"] > 0
    assert d["within1_pct"] >= 70          # reality ~100 on this AOI
    assert d["exact_pct"] >= 50            # reality ~94


@slow
def test_weight_search_at_least_matches_default(tmp_path):
    """S5.T4 [slow] the weight search returns a weighting at least as good as the default."""
    aoi = _minimal_aoi(tmp_path)
    grid = combine.combine_l2(aoi, "hazard_flood", vuln_weights=_CACHED)
    default = validate.diff_against_risk(grid, "hazard_flood", aoi)
    best = validate.best_weights(aoi, "hazard_flood", layers=list(_CACHED))
    assert (best["within1_pct"] or 0) >= (default["within1_pct"] or 0)
