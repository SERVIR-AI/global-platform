"""The raster gate and layer values after the mangrove-sprint dry run: continuous
measures (measure_*), class areas on full-resolution cells, per-class summaries
through the seam's `_class_grid`, the stack-legend and outside-value checks at the gate,
band handling, and pixel areas exact on the WGS84 ellipsoid.

Every fixture is synthetic and built here. A measure is read through the risk pack, the
land-cover pack and a contributed extension pack; layers arrive through the gate a
moment before they are read."""

import json
import math

import numpy as np
import pytest
import rasterio
import yaml
from rasterio.enums import ColorInterp
from rasterio.transform import from_bounds, from_origin
from shapely.geometry import box, mapping

from app.config import get_settings
from app.contrib import fetch_policy, identity, rasters, staging
from app.graph.geo import layer_values, stacks, tiffs
from app.mcp import assemble, packs, store

from test_landcover_pack import lc_env  # noqa: F401  (fixture reuse)
from test_layer_values import B, POLY, world  # noqa: F401  (fixture reuse)

OWNER = identity.Caller("user_owner", "analyst@hub.test", False, "bound")


# --------------------------------------------------------------------------- helpers
def _tif(path, arr, dtype, nodata=None, bounds=B, crs="EPSG:4326", transform=None, count=1):
    h, w = arr.shape[-2:]
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=count, dtype=dtype,
                       crs=crs, transform=transform or from_bounds(*bounds, w, h),
                       nodata=nodata) as d:
        if count == 1:
            d.write(arr.astype(dtype), 1)
        else:
            for b in range(count):
                d.write(arr[b].astype(dtype), b + 1)
    return str(path)


def _q(phi):
    """Snyder's q(φ), written out here so the test does not trust stacks.py."""
    e2 = 0.00669437999014
    e, s = math.sqrt(e2), math.sin(phi)
    return (1 - e2) * (s / (1 - e2 * s * s) - (1 / (2 * e)) * math.log((1 - e * s) / (1 + e * s)))


def _cell_km2(lat_top, d_lat, d_lon):
    """A cell's area on WGS84 from the authalic latitude: R_q² · Δλ · (sin β_n − sin β_s)."""
    a, qp = 6378.137, _q(math.pi / 2)
    rq2 = a * a * qp / 2
    return rq2 * math.radians(d_lon) * (_q(math.radians(lat_top)) - _q(math.radians(lat_top - d_lat))) / qp


@pytest.fixture
def gate(world, monkeypatch):
    """The layer-values world, auto-approving, with a fetcher that serves files by URL."""
    files = {}
    monkeypatch.setattr(get_settings(), "grp_auto_approve", True)
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch",
                        lambda url, max_bytes=None: (files[url], url.rsplit("/", 1)[-1]))

    def serve(name, path):
        url = f"https://hub.test/{name}"
        with open(path, "rb") as f:
            files[url] = f.read()
        return url
    return {"tmp": world, "serve": serve, "files": files}


def _submit(layer, url, **over):
    m = {"layer": layer, "url": url, "title": f"{layer} (synthetic)",
         "description": "synthetic test layer", "source": "test fixture",
         "license": "CC0-1.0", "vintage": "2026-10", "countries": ["Testland"], **over}
    tok = identity.bind(OWNER)
    try:
        return staging.submit("raster", m, OWNER)
    finally:
        identity.unbind(tok)


def _agb(gate):
    """Biomass x10 in int16, -9999 over the 'sea' (rows 0-2) and NO nodata tag — the
    shape of the CTrees clip. Column c holds (100 + c) Mg/ha."""
    arr = np.tile((1000 + 10 * np.arange(20)).astype("int16"), (20, 1))
    arr[:3, :] = -9999
    url = gate["serve"]("agb.tif", _tif(gate["tmp"] / "agb.tif", arr, "int16"))
    out = _submit("measure_toy_agb", url, declared={
        "dtype": "int16", "valid_min": 0, "valid_max": 3000, "nodata": -9999,
        "units": "Mg/ha", "scale_factor": 0.1})
    assert out["status"] == "approved", out
    return out


def _toy_pack(monkeypatch, class_grid=None, geometry=POLY):
    """A contributed extension pack: an area of its own and, optionally, a class map."""
    def gather(target, focus, trace, extras):
        stats = {"queries": None,
                 "_area": {"name": "Toy bay", "how": "test polygon", "geometry": geometry}}
        if class_grid:
            stats["_class_grid"] = class_grid
        return ([{"n": 1, "kind": "measurement", "retrieval": "computed-at-pack-time",
                  "source": "toy", "title": "reading", "text": "42 units."}], [], stats)
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy", "version": "v0", "target_keys": ("place",),
        "target_doc": {"place": "site"}, "gather": gather,
        "sections": lambda: ["## Reading"], "corpus": None,
        "default_focus": lambda t: t.get("place", "")})


def _class_map(gate):
    """A finer (0.0005 deg) mangrove map: west of 103.01 Mangrove (1), east Other (2),
    the northernmost row outside the map (0)."""
    arr = np.ones((40, 40), dtype="uint8")
    arr[:, 20:] = 2
    arr[0, :] = 0
    path = _tif(gate["tmp"] / "classmap.tif", arr, "uint8", nodata=0)
    return {"path": path, "legend": {1: "Mangrove", 2: "Other"}, "outside_values": [0],
            "year": "2020", "stack": "landcover_toy_extent", "label": "Toy extent map"}


def _rows_km2(rows, cols, value=lambda c: 1.0):
    """Σ value(c) × cell area over a block of the world's 0.001-degree cells."""
    top = B[3]
    return sum(value(c) * _cell_km2(top - r * 0.001, 0.001, 0.001) for r in rows for c in cols)


# --------------------------------------------------------------- 1. value rasters
def test_a_continuous_measure_lands_and_reads_with_a_stated_total(gate, log):
    """measure_* through the gate: a scaled int16 with an untagged -9999 lands (tagged as
    declared), and the risk pack reads its mean, percentiles and Mg total in Mg/ha."""
    out = _agb(gate)
    log("OUTPUT", out["preview"].get("note"))
    assert "tagged -9999" in out["preview"]["note"]
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["measure_toy_agb"])
    assert p["status"] == "ok", p.get("note")
    rec = p["layers"]["measure_toy_agb"]
    s = rec["summary"]
    vals = np.repeat(100 + np.arange(5, 15), 10).astype(float)        # inside: rows/cols 5..14
    assert rec["values"] == "amount" and rec["unit"] == "Mg/ha"
    assert s["mean"] == pytest.approx(109.5) and s["p50"] == pytest.approx(109.5)
    assert s["p10"] == pytest.approx(np.percentile(vals, 10), rel=1e-3)
    want = _rows_km2(range(5, 15), range(5, 15), lambda c: 100 + c) * 100    # Mg/ha x ha
    assert s["total"]["unit"] == "Mg" and s["total"]["value"] == pytest.approx(want, rel=1e-3)
    assert s["total"]["area_unit"] == "ha"
    cit = next(c for c in p["citations"] if c.get("layer") == "measure_toy_agb")
    log("OUTPUT", cit["text"][:400])
    assert f"is {layer_values._fig(s['total']['value'])} Mg" in cit["text"]
    assert "sum of value × cell area" in cit["text"] and "WGS84 ellipsoid" in cit["text"]
    assert "stored values x 0.1 give Mg/ha" in cit["text"]
    assert "median 109.5 Mg/ha" in cit["text"]


def test_a_measure_per_class_of_a_class_map_handed_over_by_any_pack(gate, monkeypatch, log):
    """The `_class_grid` hook: a contributed extension pack hands over a class map; the
    measure is placed on the finer map grid and summed per class — mean biomass and total
    biomass inside mangrove — and the class map's path never rides stats."""
    _agb(gate)
    cg = _class_map(gate)
    _toy_pack(monkeypatch, class_grid=cg)
    p = assemble.assemble(pack="toy", place="Toy bay", layers=["measure_toy_agb", "vulnerability_toy"])
    assert p["status"] == "ok", p.get("note")
    assert "_class_grid" not in json.dumps(p["stats"]) and cg["path"] not in json.dumps(p["stats"])
    b = p["layers"]["measure_toy_agb"]["summary"]["by_class_of"]
    log("OUTPUT", json.dumps(b)[:300])
    assert "finer grid" in b["method"] and "nearest neighbour" in b["method"]
    mangrove, other = b["classes"]["Mangrove"], b["classes"]["Other"]
    assert mangrove["mean"] == pytest.approx(107.0, rel=1e-4)        # columns 5..9
    assert other["mean"] == pytest.approx(112.0, rel=1e-4)           # columns 10..14
    want = _rows_km2(range(5, 15), range(5, 10), lambda c: 100 + c) * 100
    assert mangrove["total"] == pytest.approx(want, rel=1e-3)
    assert mangrove["km2"] == pytest.approx(_rows_km2(range(5, 15), range(5, 10)), rel=1e-3)
    cit = next(c for c in p["citations"] if c.get("layer") == "measure_toy_agb")
    assert "Per class of Toy extent map in 2020" in cit["text"]
    assert f"total {layer_values._fig(mangrove['total'])} Mg" in cit["text"]
    # a categorical layer gets the cross-tab: area of each of its classes in each map class
    v = p["layers"]["vulnerability_toy"]["summary"]["by_class_of"]["classes"]
    assert set(v["Mangrove"]["by_value"]) == {"2"} and set(v["Other"]["by_value"]) == {"4"}
    assert v["Mangrove"]["by_value"]["2"] == pytest.approx(_rows_km2(range(5, 15), range(5, 10)), rel=1e-3)


def test_the_land_cover_pack_reads_a_measure_by_name(lc_env, monkeypatch, tmp_path, log):
    """The asking pack: land-cover, with a measure landed through the gate a moment ago."""
    from test_landcover_pack import LEFT, PX, TOP, W, H
    dem = np.tile(np.linspace(0.5, 12.0, W, dtype="float32"), (H, 1))
    path = _tif(tmp_path / "dem.tif", dem, "float32", nodata=-9999,
                transform=from_origin(LEFT, TOP, PX, PX))
    out = rasters.add({"layer": "measure_test_dem", "file": path, "title": "Test terrain",
                       "description": "elevation, m", "source": "test fixture", "license": "CC0-1.0",
                       "vintage": "2026-10",
                       "declared": {"dtype": "float32", "valid_min": -10, "valid_max": 100,
                                    "nodata": -9999, "units": "m"}})
    assert out["status"] == "landed", out
    p = assemble.assemble(pack="land-cover", place="Test District", year="2022",
                          layers=["measure_test_dem"])
    assert p["status"] == "ok", p.get("note")
    s = p["layers"]["measure_test_dem"]["summary"]
    log("OUTPUT", json.dumps({k: s[k] for k in ("mean", "p10", "p50", "p90", "coverage_pct")}))
    assert s["coverage_pct"] == 100.0 and "total" not in s           # metres are not per area
    assert s["p50"] == pytest.approx(float(np.median(dem[2:10, 2:10])), rel=1e-3)
    assert any("measure_test_dem's cells are about" in g for g in p["gaps"])   # 64 cells


def test_the_gate_states_what_a_measure_needs(log):
    """A measure needs units, takes no legend; a classed namespace declared continuous is
    pointed at measure_*; the form text and how_to_test say where a DEM goes."""
    base = {"layer": "measure_x", "file": "-", "title": "t", "description": "d", "source": "s",
            "license": "CC-BY-4.0", "vintage": "2026-10",
            "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 3000}}
    fails = rasters.validate_manifest({**base, "legend": {1: "continuous"}})
    log("OUTPUT", fails)
    assert any("drop the legend" in f for f in fails)
    assert any("declared.units is required" in f for f in fails)
    assert not any("missing required field 'legend'" in f for f in fails)
    risk = rasters.validate_manifest({**base, "layer": "risk_cr_agb", "legend": {1: "a"},
                                      "declared": {"dtype": "int16", "valid_min": 0, "valid_max": 3734}})
    msg = next(f for f in risk if "classes 1..5" in f)
    assert "measure_<name>" in msg
    text = staging.RASTER_FIELDS["required"]["layer"]
    assert "risk_<name> (a risk map in classes 1-5)" in text
    assert "measure_<name>" in text and "elevation (a DEM)" in text and "biomass" in text
    for p in rasters.namespaces():
        assert f"{p}<name>" in text


def test_how_to_test_a_measure_names_layers(gate, log):
    out = _agb(gate)
    how = out["preview"]["how_to_test"]
    log("OUTPUT", how)
    assert "assemble_pack(pack='land-cover'" in how and "layers=['measure_toy_agb']" in how


def test_a_projected_measure_is_regridded_bilinear_and_a_class_map_nearest(tmp_path):
    arr = np.linspace(0, 20, 400, dtype="float32").reshape(20, 20)
    p = _tif(tmp_path / "utm.tif", arr, "float32", nodata=-9999, crs="EPSG:32616",
             transform=from_origin(700000, 1120000, 30, 30))
    out, notes = rasters.normalise(p, "measure")
    assert "bilinear" in notes[0]
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326
    cls = _tif(tmp_path / "utmc.tif", (arr > 10).astype("uint8") + 1, "uint8", nodata=255,
               crs="EPSG:32616", transform=from_origin(700000, 1120000, 30, 30))
    _, notes = rasters.normalise(cls, "landcover")
    assert "nearest neighbour" in notes[0]


def test_density_units_are_recognised_and_others_are_not():
    assert layer_values.density("Mg/ha")["numerator"] == "Mg"
    assert layer_values.density("t C/ha") == {"numerator": "t C", "per": "ha", "km2_per_unit": 0.01}
    assert layer_values.density("Mg C ha-1")["per"] == "ha"
    assert layer_values.density("people per km2")["numerator"] == "people"
    assert layer_values.density("kg m-2")["per"] == "m2"
    for not_density in ("m", "degC", "mm", "people per pixel", "cm", "class", "km-1"):
        assert layer_values.density(not_density) is None, not_density


# --------------------------------------------------------- 2. categorical layers
def test_class_areas_are_counted_on_full_resolution_cells(world, monkeypatch, log):
    """Even when the area exceeds the read budget, a class layer's areas are counted on
    every full-resolution cell with its own area — not on the decimated read."""
    monkeypatch.setattr(layer_values, "READ_BUDGET", 30)
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["vulnerability_toy"])
    rec = p["layers"]["vulnerability_toy"]
    s = rec["summary"]
    log("OUTPUT", json.dumps(s["classes"]))
    assert rec["read_level"]["overview_level"] >= 2
    assert s["classes_counted"].startswith("on every cell at full resolution")
    assert s["classes"]["2"]["cells"] == 50 == s["classes"]["4"]["cells"]
    assert s["classes"]["2"]["km2"] == pytest.approx(_rows_km2(range(5, 15), range(5, 10)), rel=1e-3)
    assert s["classes"]["2"]["share_pct"] == pytest.approx(50.0, abs=0.1)
    cit = next(c for c in p["citations"] if c.get("layer") == "vulnerability_toy")
    assert "Area by class, counted on every cell at full resolution" in cit["text"]
    assert "class areas are counted on every full-resolution cell" in cit["text"]
    # the risk pack's own reading of the layer is untouched: min, max, mean, by part
    assert (s["min"], s["max"]) == (2, 4) and "mean" in s and s["by_part"]
    monkeypatch.setattr(layer_values, "READ_BUDGET", 4_000_000)
    full = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                             layers=["vulnerability_toy"])["layers"]["vulnerability_toy"]["summary"]
    assert full["classes"] == s["classes"]          # the same areas, whatever the read level


# ------------------------------------------------- 3-6. land-cover years at the gate
def _year(gate, name, arr, nodata=None, dtype="uint8"):
    return gate["serve"](name, _tif(gate["tmp"] / name, arr, dtype, nodata=nodata))


def test_a_year_whose_legend_differs_from_its_stack_is_declined_before_fetching(gate, monkeypatch, log):
    a = np.ones((20, 20), dtype="uint8")
    a[:, 10:] = 2
    first = _submit("landcover_toycatie_1956", _year(gate, "y56.tif", a, nodata=3),
                    stack="landcover_toycatie", year=1956, stack_title="Toy CATIE",
                    legend={1: "Mangrove", 2: "Non-mangrove"}, outside_values=[3],
                    declared={"dtype": "uint8", "valid_min": 1, "valid_max": 2, "nodata": 3})
    assert first["status"] == "approved", first

    def no_fetch(url, max_bytes=None):
        raise AssertionError("the gate fetched a file it should have declined")
    monkeypatch.setattr(fetch_policy, "fetch", no_fetch)
    out = _submit("landcover_toycatie_1985", "https://hub.test/y85.tif", stack="landcover_toycatie",
                  year=1985, legend={10: "Mangrove", 20: "Non-mangrove"}, outside_values=[30],
                  declared={"dtype": "uint8", "valid_min": 10, "valid_max": 20, "nodata": 30})
    log("OUTPUT", out["problems"])
    assert out["status"] == "declined"
    msg = " ".join(out["problems"])
    assert "recode 10 -> 1, 20 -> 2 to match the stack's legend {1: Mangrove, 2: Non-mangrove}" in msg
    assert "name a new stack" in msg and "cannot be compared with this one year to year" in msg
    other = _submit("landcover_toycatie_2014", "https://hub.test/y14.tif", stack="landcover_toycatie",
                    year=2014, legend={1: "Forest", 2: "Non-mangrove"},
                    declared={"dtype": "uint8", "valid_min": 1, "valid_max": 2, "nodata": 3})
    assert "not in the stack's legend: Forest" in " ".join(other["problems"])


def test_outside_values_are_not_checked_against_the_declared_range(gate, log):
    """GWL_FCS30D: 255 over the sea is declared outside; the classes run to 187."""
    a = np.zeros((20, 20), dtype="uint8")
    a[5:10, :] = 185
    a[:2, :] = 255
    out = _submit("landcover_toygwl_2020", _year(gate, "gwl.tif", a), stack="landcover_toygwl",
                  year=2020, legend={0: "Non-wetland", 185: "Mangrove forest", 187: "Tidal flat"},
                  outside_values=[255],
                  declared={"dtype": "uint8", "valid_min": 0, "valid_max": 187})
    log("OUTPUT", out.get("problems") or out["status"])
    assert out["status"] == "approved", out


def test_a_legend_class_that_is_also_outside_is_declined_and_nodata_is_outside_by_default(gate, log):
    """mangrove_56.img: legend {0: Mangrove}, nodata 255, outside_values left out — the
    default [0] would count mangrove as outside."""
    a = np.full((20, 20), 255, dtype="uint8")
    a[5:15, 5:15] = 0
    url = _year(gate, "m56.tif", a, nodata=255)
    m = dict(stack="landcover_toym56", year=1956, legend={0: "Mangrove"},
             declared={"dtype": "uint8", "valid_min": 0, "valid_max": 0, "nodata": 255})
    out = _submit("landcover_toym56_1956", url, **m)
    log("OUTPUT", out["problems"])
    msg = " ".join(out["problems"])
    assert out["status"] == "declined"
    assert "class 0 'Mangrove' is also listed as outside the map" in msg
    assert "[255], the file's nodata" in msg
    ok = _submit("landcover_toym56_1956", url, outside_values=[255], **m)
    assert ok["status"] == "approved", ok
    assert tiffs.catalog()["landcover_toym56"]["outside_values"] == [255]
    # left out, the default is 0 AND the declared nodata — never the nodata as a class
    b = np.ones((20, 20), dtype="uint8")
    b[:, 10:] = 2
    b[0, :] = 3
    dflt = _submit("landcover_toyab_2020", _year(gate, "ab.tif", b, nodata=3), stack="landcover_toyab",
                   year=2020, legend={1: "A", 2: "B"},
                   declared={"dtype": "uint8", "valid_min": 1, "valid_max": 2, "nodata": 3})
    assert dflt["status"] == "approved", dflt
    assert tiffs.catalog()["landcover_toyab"]["outside_values"] == [0, 3]
    # a contract that does not repeat the file's nodata tag: the tag is outside all the same
    c = b.copy()
    c[0, :] = 255
    tag = _submit("landcover_toycd_2020", _year(gate, "cd.tif", c, nodata=255), stack="landcover_toycd",
                  year=2020, legend={1: "A", 2: "B"},
                  declared={"dtype": "uint8", "valid_min": 1, "valid_max": 2})
    assert tag["status"] == "approved", tag
    assert tiffs.catalog()["landcover_toycd"]["outside_values"] == [0, 255]
    assert tiffs.catalog()["landcover_toycd_2020"]["outside_values"] == [0, 255]


def test_a_one_class_stack_lands_and_says_change_will_not_be_computed(gate, log):
    a = np.zeros((20, 20), dtype="uint8")
    a[5:15, 5:15] = 1
    out = _submit("landcover_toygmw_2020", _year(gate, "gmw.tif", a, nodata=0), stack="landcover_toygmw",
                  year=2020, legend={1: "Mangrove"}, outside_values=[0],
                  declared={"dtype": "uint8", "valid_min": 1, "valid_max": 1, "nodata": 0})
    log("OUTPUT", out["preview"]["note"])
    assert out["status"] == "approved"
    note = out["preview"]["note"]
    assert "change between years will NOT be computed on this stack" in note
    assert "{0: 'Not mangrove', 1: 'Mangrove'}" in note


# ------------------------------------------------------------------- 7. bands
def test_a_colour_rendering_is_declined_and_a_second_band_is_said(gate, tmp_path, monkeypatch, log):
    rgba = np.zeros((4, 20, 20), dtype="uint8")
    rgba[0] = 1
    rgba[3] = 255
    path = tmp_path / "rgba.tif"
    with rasterio.open(path, "w", driver="GTiff", height=20, width=20, count=4, dtype="uint8",
                       crs="EPSG:4326", transform=from_bounds(*B, 20, 20), photometric="RGB") as d:
        d.write(rgba)
        d.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
    url = gate["serve"]("rgba.tif", str(path))
    out = _submit("landcover_toyrgba_2014", url, stack="landcover_toyrgba", year=2014,
                  legend={1: "Mangrove", 2: "Other"}, outside_values=[0],
                  declared={"dtype": "uint8", "valid_min": 1, "valid_max": 2})
    log("OUTPUT", out["problems"])
    assert out["status"] == "declined" and "4-band RGBA rendering" in out["problems"][0]
    assert "single-band" in out["problems"][0]
    two = np.stack([np.full((20, 20), 5.5), np.full((20, 20), 99.0)]).astype("float32")
    url2 = gate["serve"]("two.tif", _tif(tmp_path / "two.tif", two, "float32", nodata=-9999, count=2))
    ok = _submit("measure_toy_twoband", url2, declared={
        "dtype": "float32", "valid_min": 0, "valid_max": 10, "nodata": -9999, "units": "m"})
    assert ok["status"] == "approved", ok
    assert "Read band 1 of 2" in ok["preview"]["note"]
    # the landed row keeps saying it, and so does every answer that reads the layer
    assert any("read band 1 of 2" in n for n in tiffs.catalog()["measure_toy_twoband"]["prepared"])
    _toy_pack(monkeypatch)
    p = assemble.assemble(pack="toy", place="Toy bay", layers=["measure_toy_twoband"])
    cit = next(c for c in p["citations"] if c.get("layer") == "measure_toy_twoband")
    assert "At ingest: read band 1 of 2" in cit["text"]
    assert p["layers"]["measure_toy_twoband"]["summary"]["max"] == 5.5     # band 2's 99 never read


# ------------------------------------------------------------- 11. GeoPackages
def test_a_geopackage_of_several_rasters_needs_a_layer_name(gate, tmp_path, log):
    path = tmp_path / "topo.gpkg"
    for i, name in enumerate(("elevacion_s1", "pendiente_s1")):
        opts = {"RASTER_TABLE": name, "TILE_FORMAT": "TIFF", **({"APPEND_SUBDATASET": "YES"} if i else {})}
        with rasterio.open(path, "w", driver="GPKG", width=16, height=16, count=1, dtype="float32",
                           crs="EPSG:4326", transform=from_bounds(*B, 16, 16), **opts) as d:
            d.write(np.full((16, 16), 1.5 + 10 * i, dtype="float32"), 1)
    url = gate["serve"]("topo.gpkg", str(path))
    m = dict(declared={"dtype": "float32", "valid_min": 0, "valid_max": 5, "units": "m"})
    out = _submit("measure_toy_elev", url, **m)
    log("OUTPUT", out["problems"])
    assert out["status"] == "declined" and "elevacion_s1, pendiente_s1" in out["problems"][0]
    assert "layer_name" in out["problems"][0]
    ok = _submit("measure_toy_elev", url, layer_name="elevacion_s1", **m)
    assert ok["status"] == "approved", ok
    assert "Read the raster 'elevacion_s1'" in ok["preview"]["note"]


# ------------------------------------------------------------- 9-10. figures
def test_a_share_short_of_whole_is_never_printed_as_only_100_percent():
    assert layer_values._coverage_words(0.998) == "only 99.8%"
    assert layer_values._coverage_words(0.5) == "only 50.0%"
    assert "100%" not in layer_values._coverage_words(0.9996)
    assert "more than 99.9%" in layer_values._coverage_words(0.9996)


def test_pixel_areas_are_exact_on_the_wgs84_ellipsoid():
    """The whole Earth sums to the WGS84 area; the authalic sphere overstated a cell by
    ~0.45% at the equator, ~0.41% at 10 N and ~0.11% at 30 N."""
    whole = stacks.row_pixel_areas_km2(from_origin(-180, 90, 360, 1), 180).sum()
    assert whole == pytest.approx(510_065_621.7, rel=1e-9)
    for lat, over in ((0.5, 0.45), (10, 0.41), (30, 0.11)):
        cell = stacks.row_pixel_areas_km2(from_origin(0, lat + 0.0005, 0.001, 0.001), 1)[0]
        assert cell == pytest.approx(_cell_km2(lat + 0.0005, 0.001, 0.001), rel=1e-9)
        sphere = stacks.R_KM ** 2 * math.radians(0.001) * (
            math.sin(math.radians(lat + 0.0005)) - math.sin(math.radians(lat - 0.0005)))
        assert (sphere / cell - 1) * 100 == pytest.approx(over, abs=0.01)
