"""S7 — the L2 engine is hazard-agnostic. Per-hazard recipes (conf/risk_l2.yml) drive
combine_l2 / resolver / validate for any hazard; here we compute a real L2 grid for
two more hazards and diff each against its own risk_<x>.tif (handling the
landslides/landslide naming). Run slow with: GRP_RUN_SLOW=1 pytest tests/test_l2_multi_hazard.py
"""
import json
import os

import numpy as np
import pytest
import rasterio
from shapely.geometry import box, mapping

from app.graph.geo import combine, validate, verify

slow = pytest.mark.skipif(
    os.getenv("GRP_RUN_SLOW") != "1", reason="slow: live Drive download (set GRP_RUN_SLOW=1)")


def _minimal_aoi(tmp_path):
    adir = tmp_path / "aoi"
    adir.mkdir()
    (adir / "admin.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": "t"},
             "geometry": mapping(box(103.16, 13.07, 103.24, 13.15))}]}))
    return {"name": "t", "admin": str(adir / "admin.geojson")}


@slow
@pytest.mark.parametrize("hazard", ["hazard_landslides", "hazard_earthquake"])
def test_l2_runs_and_validates_per_hazard(hazard, tmp_path):
    """S7.T1/T2 [slow] each hazard's recipe -> a 1-5 grid on its grid, agreeing with its risk_<x>.tif."""
    aoi = _minimal_aoi(tmp_path)
    grid = combine.combine_l2(aoi, hazard)                # per-hazard conf recipe
    with rasterio.open(grid) as r:
        uniq = set(np.unique(r.read(1)).tolist())
    assert uniq.issubset({0, 1, 2, 3, 4, 5}) and (uniq - {0})   # valid 1-5, not all nodata
    d = validate.diff_against_risk(grid, hazard, aoi)            # resolves risk_landslide (singular) etc.
    assert d["n_cells"] > 0
    assert d["within1_pct"] >= 70                               # reality ~100 on these AOIs


@slow
@pytest.mark.parametrize("layer", ["vulnerability_pop_all_total",
                                   "vulnerability_reclass_blddensity",
                                   "vulnerability_reclass_road"])
def test_recipe_inputs_verify(layer):
    """S7.T3 [slow] every vulnerability input the recipes use passes verification."""
    assert verify.verify_raster(layer).ok
