"""Land-cover answers after the round-3 re-test (2026-10-19), on the shapes it found:

- a layer the gate approved is never called 'structural-contract-FAILED' at answer time
  for the outside values the gate excluded (GWL_FCS30D: classes 0-187, 255 over the sea;
  MapBiomas: classes from 3, 0 outside) — one verdict per layer, in the passport and in
  the layer-values block alike; the risk pack's hazard passport reads the same way;
- each year of a stack cites its own map (WorldCover 2020 v100, 2021 v200), and a change
  between them names both;
- a stack that maps only part of the asked boundary says how much, in the class-area
  citation and as a gap, whether or not layers= is passed.

Every fixture is synthetic and built here; layers arrive through the gate a moment
before they are read."""

import json

import numpy as np
import pytest
import rasterio
from shapely.geometry import box, mapping, shape

from app.graph.geo import ingest, vectors
from app.mcp import assemble

from test_sprint_fix2_stacks import _submit, env, lc_env  # noqa: F401  (fixture reuse)
from test_sprint_fix_landcover import H, LEFT, POLY, PX, TOP, W
from test_sprint_fix_rasters import _submit as _submit_raster, _tif, gate  # noqa: F401
from test_layer_values import POLY as RISK_POLY, world  # noqa: F401

GWL_LEGEND = {0: "Non-wetland", 185: "Mangrove forest", 187: "Tidal flat"}
MB_LEGEND = {3: "Forest Formation", 5: "Mangrove", 33: "River, Lake and Ocean"}
WC_LEGEND = {10: "Tree cover", 80: "Permanent water bodies", 95: "Mangroves"}
V100 = "ESA WorldCover 10 m 2020 v100, doi:10.5281/zenodo.5571936 (synthetic)"
V200 = "ESA WorldCover 10 m 2021 v200, doi:10.5281/zenodo.7254221 (synthetic)"


def _gwl(year):
    """GWL_FCS30D's shape: Non-wetland 0 is a class, 185/187 wetland classes, the sea 255."""
    a = np.zeros((H, W), "uint8")
    a[:, :3] = 185
    a[:, 7] = 187
    a[8:, :] = 255
    if year == 2022:
        a[:, 3] = 185
    return a


def _mapbiomas(year):
    """MapBiomas' shape: classes from 3 up, 0 outside the country."""
    a = np.full((H, W), 3, "uint8")
    a[:, :4] = 5
    a[:, 8] = 33
    a[:2, :] = 0
    if year == 2025:
        a[:, 4] = 5
    return a


def _land(env, sid, legend, declared, outside, maker, years, per_year=None):
    per_year = per_year or {}
    for y in years:
        out = _submit(f"{sid}_{y}", env["serve"](f"{sid}-{y}.tif", maker(y)), stack=sid, year=y,
                      stack_title=f"{sid} (synthetic)", legend=legend, outside_values=outside,
                      declared=declared, **per_year.get(y, {}))
        assert out["status"] == "approved", out


def _verdicts(p):
    """{layer: {verdicts}} over the passports and the layer-values block of one pack."""
    out = {}
    for c in p["citations"]:
        if c["kind"] == "landcover_layer":
            layer = c["text"].split(" file ", 1)[1].split(".tif", 1)[0]
            out.setdefault(layer, set()).add(c["validation"])
        elif c["kind"] == "layer_values":
            out.setdefault(c["layer"], set()).add(c["validation"])
    return out


# ------------------------------------------------- A. one contract verdict per layer
@pytest.mark.parametrize("sid, legend, declared, outside, maker, years, cls, rng", [
    ("landcover_wet", GWL_LEGEND, {"dtype": "uint8", "valid_min": 0, "valid_max": 187, "units": "class"},
     [255], _gwl, (2020, 2022), "Mangrove forest", "range 0-187"),
    ("landcover_mbz", MB_LEGEND, {"dtype": "uint8", "valid_min": 3, "valid_max": 75, "units": "class"},
     [0], _mapbiomas, (2017, 2025), "Mangrove", "range 3-33"),
])
def test_a_gate_approved_layer_is_contract_checked_in_the_pack(env, log, sid, legend, declared,
                                                               outside, maker, years, cls, rng):
    """MGV-018: the passport read the outside values (255, 0) as readings and failed a
    layer the gate approved, beside a layer-values block that said 'checked'."""
    _land(env, sid, legend, declared, outside, maker, years)
    p = assemble.assemble(pack="land-cover", place="Test bay", geometry=POLY, year=str(years[0]),
                          year_to=str(years[1]), stack=sid, classes=[cls])
    assert p["status"] == "ok", p.get("note")
    passports = [c for c in p["citations"] if c["kind"] == "landcover_layer"]
    log("OUTPUT", " | ".join(c["validation"] + ": " + c["text"][c["text"].find("Observed"):][:90]
                             for c in passports))
    assert len(passports) == 2
    assert all(c["validation"] == "structural-contract-checked" for c in passports)
    assert all(rng in c["text"] and "CONTRACT MISMATCH" not in c["text"] for c in passports)
    # the no-data the passport left out is the one the layer-values block names
    assert all(f"no-data, not counted as readings: {outside[0]} — outside the producer's footprint"
               in c["text"] for c in passports)
    assert not [g for g in p["gaps"] if "failed its structural contract" in g]
    verdicts = _verdicts(p)
    assert set(verdicts) == {f"{sid}_{y}" for y in years}
    assert all(v == {"structural-contract-checked"} for v in verdicts.values()), verdicts


def test_a_value_outside_the_declared_range_still_fails_the_passport(env, log):
    """The control: a class the contract does not allow, and that is not declared
    outside, is still a mismatch."""
    def bad(year):
        a = _gwl(year)
        a[0, 9] = 200
        return a
    sid = "landcover_wetbad"
    declared = {"dtype": "uint8", "valid_min": 0, "valid_max": 187, "units": "class"}
    # the gate would decline the file, so land a clean one and change it on disk after
    _land(env, sid, GWL_LEGEND, declared, [255], _gwl, (2020, 2022))
    for y in (2020, 2022):
        with rasterio.open(ingest.source_raster(f"{sid}_{y}"), "r+") as ds:
            ds.write(bad(y), 1)
    p = assemble.assemble(pack="land-cover", place="Test bay", geometry=POLY, year="2020",
                          year_to="2022", stack=sid)
    passports = [c for c in p["citations"] if c["kind"] == "landcover_layer"]
    log("OUTPUT", [c["validation"] for c in passports])
    assert all(c["validation"] == "structural-contract-FAILED" for c in passports)
    assert any("max 200.0 > declared valid_max 187" in g for g in p["gaps"])


def test_the_risk_pack_reads_a_hazard_outside_value_the_way_the_gate_does(gate, log):
    """Beyond the asking pack: a hazard contributed with 255 outside its 1-5 classes is
    checked on its readings in the risk pack's passport, as the gate checked it."""
    a = np.full((20, 20), 3, "uint8")
    a[:, :10] = 1
    a[8:12, :] = 255                                   # inside the polygon: no reading
    url = gate["serve"]("toysea.tif", _tif(gate["tmp"] / "toysea.tif", a, "uint8"))
    out = _submit_raster("hazard_toysea", url, legend={1: "Low", 3: "Moderate", 5: "High"},
                         outside_values=[255],
                         declared={"dtype": "uint8", "valid_min": 1, "valid_max": 5, "units": "class"})
    assert out["status"] == "approved", out
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toysea", geometry=RISK_POLY)
    assert p["status"] == "ok", p.get("note")
    hz = next(c for c in p["citations"] if c["kind"] == "hazard_layer")
    log("OUTPUT", hz["validation"] + ": " + hz["text"][hz["text"].find("Observed"):][:120])
    assert hz["validation"] == "structural-contract-checked"
    assert "range 1-3" in hz["text"] and "CONTRACT MISMATCH" not in hz["text"]
    assert not [g for g in p["gaps"] if "failed its structural contract" in g]
