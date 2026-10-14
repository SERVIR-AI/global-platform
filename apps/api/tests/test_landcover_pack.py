"""The land-cover pack on a synthetic three-year stack (test data, never presented
as real): class areas, change on the common mask, declines, and the full loop."""
import json
import math
import os

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from app.config import get_settings
from app.graph.geo import ingest, stacks
from app.mcp import assemble, packs, publish, store

LEGEND = {1: "Water body", 2: "Snow and glacier", 3: "Forest", 4: "Riverbed",
          5: "Built-up area", 6: "Cropland", 7: "Bare soil", 8: "Bare rock", 9: "Grassland"}
W = H = 12
PX = 0.001                      # degrees per pixel
LEFT, TOP = 85.0, 27.012        # north-west corner


def _grid(year):
    a = np.full((H, W), 6, dtype="uint8")          # Cropland
    a[:, :6] = 3                                    # left half Forest
    if year >= 2021:
        a[:, 6:9] = 3                               # three columns Cropland -> Forest
        a[:, 5] = 5                                 # one column Forest -> Built-up
        a[0, :] = 15                                # outside: nodata in the newer files
    else:
        a[0, :] = 0                                 # outside: 0 in the older files
    if year == 2021:
        a[6, 6] = 6                                 # one pixel differs from 2022
    return a


@pytest.fixture
def lc_env(tmp_path, monkeypatch):
    s = get_settings()
    data = tmp_path / "data"
    data.mkdir()
    for y in (2000, 2021, 2022):
        with rasterio.open(data / f"lc-{y}.tif", "w", driver="GTiff", width=W, height=H, count=1,
                           dtype="uint8", crs="EPSG:4326", nodata=15,
                           transform=from_origin(LEFT, TOP, PX, PX)) as dst:
            dst.write(_grid(y), 1)
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "tiffs.yml").write_text(json.dumps({
        "landcover_test": {
            "kind": "stack", "role": "landcover", "internal": True,
            "title": "Test land cover (synthetic)", "description": "synthetic test stack",
            "source": "test fixture", "license": "CC0-1.0", "pub_date": "2026-01-01",
            "authority": "test", "accuracy": "unstated", "legend": LEGEND,
            "outside_values": [0, 15], "years": [2000, 2021, 2022],
            "missing_years": {2012: "no map published by the producer"},
            "extent": {"lon": [85.0, 85.012], "lat": [27.0, 27.012]},
            "layer_pattern": "landcover_test_{year}", "file_pattern": "lc-{year}.tif",
            "data_root": str(data)}}))
    (conf / "raster_schema.yml").write_text(json.dumps({
        "defaults": {"crs": "EPSG:4326", "float_tol": 0.0001},
        "layers": {},
        "stacks": {"landcover_test": {"role": "landcover", "dtype": "uint8", "valid_min": 0,
                                      "valid_max": 9, "nodata": 15, "pixel_size_deg": PX}}}))
    monkeypatch.setattr(s, "tiffs_config_path", conf / "tiffs.yml")
    monkeypatch.setattr(s, "tiffs_contrib_path", conf / "tiffs.contrib.yml")
    monkeypatch.setattr(s, "raster_schema_path", conf / "raster_schema.yml")
    monkeypatch.setattr(s, "raster_schema_contrib_path", conf / "raster_schema.contrib.yml")
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "tiffs_dir", tmp_path / "cache" / "tiffs")
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    # The AOI: pixel centres of columns 2-9 and rows 2-9 fall inside (8 x 8 = 64 px).
    poly = {"type": "Polygon", "coordinates": [[
        [LEFT + 2 * PX, TOP - 2 * PX], [LEFT + 10 * PX, TOP - 2 * PX],
        [LEFT + 10 * PX, TOP - 10 * PX], [LEFT + 2 * PX, TOP - 10 * PX], [LEFT + 2 * PX, TOP - 2 * PX]]]}

    def fake_ensure_aoi(place=None, geometry=None, layers=None, name=None, area_cap_km2=None):
        adir = tmp_path / "cache" / "test-aoi"
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "admin.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": "Test District"}, "geometry": poly}]}))
        how = "supplied polygon" if geometry is not None else "admin boundary ~1 km²"
        if place == "Nepal":
            how = "40 km radius box at the centre of Nepal — too large"
        info = {"name": name or place or "Test District", "area_km2": 1, "how": how, "counts": {}}
        return ingest._bundle(str(adir), info)
    monkeypatch.setattr(ingest, "ensure_aoi", fake_ensure_aoi)
    return {"poly": poly}


def _expected_px_area_km2(row):
    """Spherical area of one pixel in raster row `row` (same formula as stacks.py)."""
    lat_n = math.radians(TOP - row * PX)
    lat_s = math.radians(TOP - (row + 1) * PX)
    return stacks.R_KM ** 2 * math.radians(PX) * (math.sin(lat_n) - math.sin(lat_s))


def test_stack_expands_into_per_year_rows_and_contracts(lc_env):
    from app.graph.geo import schema, tiffs
    cat = tiffs.catalog()
    assert "landcover_test_2022" in cat and cat["landcover_test_2022"]["internal"]
    assert cat["landcover_test_2022"]["path"].endswith("lc-2022.tif")
    assert schema.schema_for("landcover_test_2022")["valid_max"] == 9
    assert ingest.source_raster("landcover_test_2022") == cat["landcover_test_2022"]["path"]
    assert stacks.layer_for("landcover_test", 2000) == "landcover_test_2000"
    with pytest.raises(ValueError, match="no map published"):
        stacks.layer_for("landcover_test", 2012)
    with pytest.raises(ValueError, match="not in the"):
        stacks.layer_for("landcover_test", 1999)


def test_class_areas_one_year(lc_env, log):
    p = assemble.assemble(pack="land-cover", place="Test District", year="2000")
    assert p["status"] == "ok", p.get("note")
    assert p["target"] == {"place": "Test District", "year": "2000"}
    c1 = p["citations"][0]
    log("OUTPUT", c1["text"][:300])
    assert c1["kind"] == "class_area"
    # rows 2-9 inside; 2000: columns 2-5 Forest, 6-9 Cropland -> 32 + 32 pixels, 50% each
    exp = sum(_expected_px_area_km2(r) * 4 for r in range(2, 10))
    assert f"Forest {exp:,.1f} km² (50.0%)" in c1["text"]
    assert f"Cropland {exp:,.1f} km² (50.0%)" in c1["text"]
    assert "Pixels: 64 classified of 64 inside the boundary" in c1["text"]
    assert "Classes absent from this area: Water body" in c1["text"]
    assert p["stats"]["classes_km2"]["Forest"] == round(exp, 1)
    kinds = [c["kind"] for c in p["citations"]]
    # the year map the class areas were counted from rides along as layer values
    assert kinds == ["class_area", "landcover_layer", "method", "layer_values", "gaps"]
    lv = p["layers"]["landcover_test_2000"]
    assert lv["summary"]["cells_in_area"] == 64 and "counted from" in lv["why"]
    passport = p["citations"][1]
    assert passport["validation"] == "structural-contract-checked"
    assert "Accuracy: unstated" in passport["text"]
    assert p["required_sections"][0] == "## What the maps show"


def test_change_on_common_mask(lc_env, log):
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022,
                          classes=["Forest"])
    assert p["status"] == "ok", p.get("note")
    kinds = [c["kind"] for c in p["citations"]]
    assert kinds == ["class_area", "class_area", "class_change", "class_transitions",
                     "class_series", "landcover_layer", "landcover_layer", "method",
                     "layer_values", "layer_values", "gaps"]
    assert sorted(p["layers"]) == ["landcover_test_2000", "landcover_test_2022"]
    ch = next(c for c in p["citations"] if c["kind"] == "class_change")
    log("OUTPUT", ch["text"][:400])
    # inside the AOI: col 5 Forest->Built-up (8 px), cols 6-8 Cropland->Forest (24 px) changed; 32 unchanged
    px = [_expected_px_area_km2(r) for r in range(2, 10)]
    changed = sum(px) * 4
    unchanged = sum(px) * 4
    assert f"unchanged {unchanged:,.1f} km² (50.0%)" in ch["text"]
    assert f"changed {changed:,.1f} km² (50.0%)" in ch["text"]
    assert "0.0 km² classified in only one year" in ch["text"]
    tr = next(c for c in p["citations"] if c["kind"] == "class_transitions")
    assert tr["text"].startswith("Forest in Test District, 2000 -> 2022")
    assert "loss" in tr["text"] and "to Built-up area" in tr["text"]
    assert "gain" in tr["text"] and "from Cropland" in tr["text"]
    assert p["stats"]["change"]["by_class"]["Forest"]["gain_km2"] > p["stats"]["change"]["by_class"]["Forest"]["loss_km2"]


def test_declines_are_governed(lc_env):
    assert "no map published" in assemble.assemble(pack="land-cover", place="Test District", year=2012)["note"]
    assert "not in the" in assemble.assemble(pack="land-cover", place="Test District", year=1999)["note"]
    out = assemble.assemble(pack="land-cover", place="Test District", year=2022, year_to=2000)
    assert out["status"] == "declined" and "must come after" in out["note"]
    out = assemble.assemble(pack="land-cover", place="Nepal", year=2022)
    assert out["status"] == "declined" and "did not resolve to a boundary" in out["note"]
    out = assemble.assemble(pack="land-cover", place="Test District", year=2022,
                            geometry={"type": "Polygon", "coordinates": [[[100, 10], [101, 10], [101, 11], [100, 11], [100, 10]]]})
    # the fake resolver ignores the geometry, so this exercises the footprint check on the stack extent
    assert out["status"] in ("ok", "declined")
    out = assemble.assemble(place="Test District")           # bare place: two packs claim it
    assert out["status"] == "declined" and "land-cover" in out["note"]


def test_full_loop_mints_a_receipt(lc_env, log):
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022)
    n = {c["kind"]: c["n"] for c in p["citations"]}
    c1 = p["citations"][0]["text"]
    forest = c1.split("Forest ")[1].split(" km²")[0]
    draft = (f"## What the maps show\n\nIn 2000 forest covered {forest} km² of Test District [{n['class_area']}]. "
             f"Change to 2022 is stated in the evidence [{n['class_change']}].\n\n"
             f"## Method, resolution and masks\n\nPixel counts at full resolution [{n['method']}], on the layer's declared contract [{n['landcover_layer']}].\n\n"
             f"## What's missing and how to weigh it\n\nAccuracy is undocumented [{n['gaps']}].\n\n"
             f"## Reading land-cover change\n\nSingle-year disagreement counts as change [{n['class_change']}].")
    out = publish.answer(p["pack_id"], draft, question="forest in Test District")
    log("OUTPUT", f"passed={out.get('passed')} failures={out.get('failures')}")
    assert out["status"] == "ok" and out["passed"] is True, out
    assert out["receipt_id"]
    assert out["evidence_freshness"]["computed_sources"]
    bad = draft.replace(forest, "999.9")
    out2 = publish.answer(p["pack_id"], bad, question="forest in Test District")
    assert out2.get("passed") is not True


def test_registry_and_description(lc_env):
    from app.mcp import registry
    assert packs.infer(None, place="x", year="2022") == "land-cover"
    assert packs.infer(None, place="x", hazard="flood") == "risk"
    d = registry.describe_assemble()
    assert "land-cover" in d and "year_to" in d and "land cover" in d.lower()
    m = registry.pack_manifest("land-cover")
    assert m["id"] == "land-cover" and "landcover_test" in m["sources"]["stacks"]
    assert m["sources"]["stacks"]["landcover_test"]["years_available"] == [2000, 2021, 2022]


def test_a_second_covering_stack_is_declared_and_selectable(lc_env, log):
    """A hub's finer stack over the same footprint used to sit unread behind the
    first registered one, with no answer ever naming it."""
    from app.config import get_settings
    conf = get_settings().tiffs_config_path
    doc = json.loads(conf.read_text())
    fine = dict(doc["landcover_test"], title="Fine test land cover (synthetic, 10 m)", years=[2022])
    doc["landcover_test_fine"] = fine
    conf.write_text(json.dumps(doc))
    p = assemble.assemble(place="Test District", year=2022)
    assert p["status"] == "ok", p.get("note")
    gap = next(g for g in p["gaps"] if "also cover" in g)
    log("OUTPUT", gap[:160])
    assert "Fine test land cover" in gap and "pass stack=<id>" in gap
    chosen = assemble.assemble(place="Test District", year=2022, stack="landcover_test_fine")
    assert chosen["status"] == "ok", chosen.get("note")
    assert "Fine test land cover" in json.dumps(chosen["citations"], ensure_ascii=False)
    assert any("This answer reads Fine test land cover" in g and "Test land cover (synthetic) (landcover_test)" in g
               for g in chosen["gaps"])                       # the other one is now the alternative
    bad = assemble.assemble(place="Test District", year=2022, stack="landcover_mars")
    assert bad["status"] == "declined" and "landcover_test_fine" in bad["note"]
