"""Layer values for an area — `assemble_pack(layers=[...])`, built once at the seam.

Not a risk feature: the step runs after any pack's gather, so these tests drive it
through the risk pack (its recipe layers are the default), an extension pack with no
area of its own, the land-cover pack, food security, and a raster contributed
through the gate a moment earlier. The rasters here deliberately extend past the
polygon with a value that must never appear, so an unmasked clip fails loudly."""

import asyncio
import json
import pathlib

import numpy as np
import pytest
import rasterio
import yaml
from rasterio.transform import from_bounds

from app.config import get_settings
from app.contrib import fetch_policy, identity, staging
from app.graph.geo import ingest, layer_values, vectors
from app.mcp import assemble, packs, publish, record, store
from app.risk import synthesis as risk

from test_landcover_pack import lc_env  # noqa: F401  (fixture reuse)

B = (103.0, 13.0, 103.02, 13.02)                       # 20 x 20 cells of 0.001 deg
POLY = {"type": "Polygon", "coordinates": [[[103.005, 13.005], [103.015, 13.005],
        [103.015, 13.015], [103.005, 13.015], [103.005, 13.005]]]}   # the middle 10 x 10

OWNER = identity.Caller("user_owner", "analyst@hub.test", False, "bound")
OTHER = identity.Caller("user_other", "planner@hub.test", False, "bound")
REVIEWER = identity.Caller("user_rev", "reviewer@hub.test", True, "bound")


def _tif(path, arr, dtype, nodata=None, bounds=B):
    h, w = arr.shape
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1, dtype=dtype,
                       crs="EPSG:4326", transform=from_bounds(*bounds, w, h),
                       nodata=nodata) as d:
        d.write(arr.astype(dtype), 1)
    return pathlib.Path(path)


def _as(caller, fn):
    tok = identity.bind(caller)
    try:
        return fn()
    finally:
        identity.unbind(tok)


def _decode(grid):
    """Every value in a grid, either encoding, empty cells dropped."""
    if grid["encoding"] == "palette":
        return [grid["palette"][layer_values.PALETTE_SYMBOLS.index(ch)]
                for ch in grid["cells"] if ch != "."]
    return [v for v in grid["cells"] if v is not None]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A toy flood hazard, a 1-5 vulnerability layer that is 2 | 4 inside the polygon
    and 1 outside it, and a float index with one value everywhere (a district-level
    layer seen from inside one sub-district) — hand-authored rows, hermetic store."""
    s = get_settings()
    tdir = tmp_path / "cache" / "tiffs"
    tdir.mkdir(parents=True)
    haz = np.zeros((20, 20))
    haz[:, :10] = 3
    _tif(tdir / "hazard_toyflood.tif", haz, "int16")
    vul = np.ones((20, 20))
    vul[5:15, 5:10] = 2
    vul[5:15, 10:15] = 4
    _tif(tdir / "vulnerability_toy.tif", vul, "uint8")
    _tif(tdir / "vulnerability_flat_index.tif", np.full((20, 20), 0.476), "float32", nodata=-9999)
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "tiffs.yml").write_text(yaml.safe_dump({
        "hazard_toyflood": {"local_path": "tiffs/hazard_toyflood.tif", "title": "Toy flood",
                            "legend": {1: "Very Low", 3: "Moderate", 5: "Very High"}},
        "vulnerability_toy": {"local_path": "tiffs/vulnerability_toy.tif", "internal": True,
                              "title": "Toy vulnerability", "source": "test fixture",
                              "license": "CC0-1.0", "vintage": "2026-10"},
        "vulnerability_flat_index": {"local_path": "tiffs/vulnerability_flat_index.tif",
                                     "internal": True, "title": "District index",
                                     "source": "test fixture", "license": "CC0-1.0",
                                     "vintage": "2026-10"}}))
    (conf / "raster_schema.yml").write_text(yaml.safe_dump({
        "defaults": {"crs": "EPSG:4326", "float_tol": 0.0001},
        "roles": {"hazard": {"zero": "value"}, "vulnerability_reclass": {"zero": "nodata"}},
        "layers": {
            "hazard_toyflood": {"role": "hazard", "dtype": "int16", "valid_min": 0,
                                "valid_max": 5, "units": "class"},
            "vulnerability_toy": {"role": "vulnerability_reclass", "dtype": "uint8",
                                  "valid_min": 0, "valid_max": 5, "units": "class",
                                  "nodata": None},
            "vulnerability_flat_index": {"role": "vulnerability", "dtype": "float32",
                                         "valid_min": 0, "valid_max": 1,
                                         "units": "index 0-1", "nodata": -9999}}}))
    (conf / "risk_l2.yml").write_text(yaml.safe_dump(
        {"class_max": 5, "weights": {"toyflood": {"vulnerability_toy": 1.0}}}))
    for k, v in {"tiffs_config_path": conf / "tiffs.yml",
                 "raster_schema_path": conf / "raster_schema.yml",
                 "risk_l2_config_path": conf / "risk_l2.yml",
                 "tiffs_contrib_path": conf / "tiffs.contrib.yml",
                 "raster_schema_contrib_path": conf / "raster_schema.contrib.yml",
                 "risk_l2_contrib_path": conf / "risk_l2.contrib.yml",
                 "cache_dir": tmp_path / "cache", "tiffs_dir": tdir,
                 "grp_mattermost_webhook": ""}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(ingest, "_fetch_layer", lambda layer, bbox, boundary: [])
    monkeypatch.setattr(vectors, "visible", lambda: {})
    monkeypatch.setattr(risk, "_pack_feeds", lambda *a, **k: [])
    monkeypatch.setattr(risk, "_document_hits", lambda *a, **k: (None, []))
    for g in ("STAGED_RASTERS", "STAGED_WEIGHTS", "STAGED_FEEDS", "STAGED_VECTORS",
              "STAGED_AMENDS"):
        monkeypatch.setattr(staging, g, {}, raising=False)
    monkeypatch.setattr(staging, "_STAGED_LOADED", False)
    return tmp_path


def test_by_default_the_risk_pack_returns_its_recipe_layers_clipped_to_the_polygon(world, log):
    """Acceptance 1 + 3: no `layers` argument -> the recipe's layers, values from inside
    the supplied polygon only, each with unit, source, vintage, validation, no-data."""
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY)
    assert p["status"] == "ok", p.get("note")
    rec = p["layers"]["vulnerability_toy"]
    log("OUTPUT", json.dumps(rec["summary"]))
    assert "recipe" in rec["why"]
    assert rec["summary"]["cells_in_area"] == 100 == rec["summary"]["cells_with_value"]
    assert (rec["summary"]["min"], rec["summary"]["max"]) == (2, 4)    # never the 1 outside
    assert sorted(set(_decode(rec["grid"]))) == [2, 4]
    assert rec["grid"]["encoding"] == "palette" and rec["grid"]["palette"] == [2, 4]
    assert rec["unit"] == "class" and rec["source"] == "test fixture"
    assert rec["vintage"] == "2026-10" and rec["validation"] == "structural-contract-checked"
    assert rec["nodata"]["value"] == 0 and "vulnerability_reclass" in rec["nodata"]["rule"]
    assert rec["downsampled"] is None and rec["read_level"] is None
    # the target is unchanged when the caller named nothing
    assert p["target"] == {"place": "Toyville", "hazard": "toyflood"}


def test_layer_values_are_cited_before_the_gaps_and_never_ride_stats(world, log):
    """The citation states the numbers (the gate reads citation text); the grid stays
    out of `stats`, which the gate scans for numbers wholesale."""
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY)
    rec = p["layers"]["vulnerability_toy"]
    cit = next(c for c in p["citations"] if c["kind"] == "layer_values")
    log("OUTPUT", cit["text"][:240])
    assert cit["n"] == rec["n"] and cit["layer"] == "vulnerability_toy"
    assert cit["retrieval"] == "computed-at-pack-time" and cit["pub_date"] == "2026-10"
    assert "min 2, max 4, mean 3" in cit["text"] and "100 of 100 cells" in cit["text"]
    # where: the west of the area holds the 2s and the east the 4s, and the text says so
    assert rec["summary"]["by_part"]["west"] == 2 and rec["summary"]["by_part"]["east"] == 4
    assert "Mean by part of the area: north-west 2" in cit["text"]
    assert p["citations"][-1]["kind"] == "gaps" and p["citations"][-1]["n"] == cit["n"] + 1
    blob = json.dumps(p["stats"])
    assert "_layer_defaults" not in p["stats"] and "_area" not in p["stats"]
    assert '"grid"' not in blob and '"palette"' not in blob


def test_a_named_layer_replaces_the_defaults_and_bad_names_decline(world, log):
    """Acceptance 2: a registered layer by name; an unknown name declines with the list;
    too many layers decline; [] means none."""
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["vulnerability_flat_index"])
    assert p["status"] == "ok" and list(p["layers"]) == ["vulnerability_flat_index"]
    assert p["target"]["layers"] == ["vulnerability_flat_index"]
    assert p["layers"]["vulnerability_flat_index"]["why"] == "requested by name"
    bad = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                            layers=["vulnerability_moon"])
    log("OUTPUT", bad["note"][:160])
    assert bad["status"] == "declined" and "vulnerability_moon" in bad["note"]
    assert "vulnerability_toy" in bad["note"]                  # the registered list
    many = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                             layers=["vulnerability_toy"] * 3 + [f"x{i}" for i in range(9)])
    assert many["status"] == "declined" and "at most" in many["note"]
    none = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                             layers=[])
    assert none["status"] == "ok" and "layers" not in none


def test_one_value_across_the_area_is_a_declared_gap(world, log):
    """The district-level finding, caught for any layer: a value that does not vary
    inside the area is said, so nobody reads it as 'where'."""
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["vulnerability_flat_index"])
    rec = p["layers"]["vulnerability_flat_index"]
    gap = next(g for g in p["gaps"] if "ONE value" in g)
    log("OUTPUT", gap)
    assert rec["summary"]["uniform"] is True and rec["summary"]["min"] == 0.476
    assert rec["unit"] == "index 0-1" and rec["nodata"]["value"] == -9999
    assert "cannot show where" in gap and "single value (0.476)" in next(
        c["text"] for c in p["citations"] if c.get("layer") == "vulnerability_flat_index")


def test_values_ride_the_pack_and_the_receipt(world, log):
    """Acceptance 5: the values are stored with the pack and the receipt fingerprints
    them per layer, so what a brief rested on can be checked later."""
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY)
    rec = p["layers"]["vulnerability_toy"]
    assert store.load_pack(p["pack_id"])["layers"] == p["layers"]
    r = record.record(pack_id=p["pack_id"], question="toy")
    src = next(x for x in r["sources"] if x.get("layer") == "vulnerability_toy")
    log("OUTPUT", f"receipt {r['receipt_id']} values_sha256 {src['values_sha256'][:16]}")
    assert src["values_sha256"] == rec["values_sha256"]
    again = record.record(receipt_id=r["receipt_id"])
    assert again["render_with"]["layers"]["layers"] == ["vulnerability_toy"]
    # the fingerprint is of the values: a different area gives a different one
    q = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood",
                          geometry=[103.005, 13.005, 103.01, 13.015])
    assert q["layers"]["vulnerability_toy"]["values_sha256"] != rec["values_sha256"]


def test_large_areas_are_downsampled_and_say_so(world, monkeypatch, log):
    """Acceptance 6: past the grid cap the grid is thinned, past the read budget the
    file is read at an overview, and both are stated in the record and the citation."""
    monkeypatch.setattr(layer_values, "GRID_SIDE", 4)
    monkeypatch.setattr(layer_values, "READ_BUDGET", 30)
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY)
    rec = p["layers"]["vulnerability_toy"]
    log("OUTPUT", f"{rec['downsampled']} {rec['read_level']}")
    assert rec["read_level"]["overview_level"] >= 2
    assert max(rec["grid"]["w"], rec["grid"]["h"]) <= 4
    cit = next(c for c in p["citations"] if c["kind"] == "layer_values")
    assert "resolution" in cit["text"]


def test_a_raster_contributed_through_the_gate_works_with_no_code_change(world, monkeypatch, log):
    """Acceptance 4, end to end through the gate: staged -> its author can read it,
    nobody else can name it; approved -> everyone can. Read through an extension
    pack that has no area of its own, so the caller's polygon is used."""
    src = _tif(world / "upload.tif", np.full((20, 20), 7.5), "float32", nodata=-1)
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None:
                        (src.read_bytes(), "upload.tif"))

    def gather(target, focus, trace, extras):
        return ([{"n": 1, "kind": "measurement", "retrieval": "computed-at-pack-time",
                  "source": "toy", "title": "reading", "text": "42 units."}], [],
                {"queries": None})
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy", "version": "v0", "target_keys": ("place",),
        "target_doc": {"place": "site"}, "gather": gather,
        "sections": lambda: ["## Reading"], "corpus": None,
        "default_focus": lambda t: t.get("place", "")})
    man = {"layer": "population_toy_density", "url": "https://example.org/u.tif",
           "title": "Toy people", "description": "people per pixel", "source": "a hub",
           "license": "CC-BY-4.0", "vintage": "2026-10",
           "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 100, "nodata": -1,
                        "units": "people per pixel"}}
    out = _as(OWNER, lambda: staging.submit("raster", man, OWNER))
    log("OUTPUT", f"{out['status']} granularity={out['preview']['observed'].get('granularity', {}).get('note', '')[:120]}")
    assert out["status"] == "staged", out
    mine = _as(OWNER, lambda: assemble.assemble(pack="toy", place="Site", geometry=POLY,
                                                layers=["population_toy_density"]))
    assert mine["status"] == "ok", mine.get("note")
    rec = mine["layers"]["population_toy_density"]
    assert rec["unit"] == "people per pixel" and rec["summary"]["min"] == 7.5
    assert rec["staged_by"] == OWNER.id and mine.get("staged_by") == OWNER.id   # a preview
    theirs = _as(OTHER, lambda: assemble.assemble(pack="toy", place="Site", geometry=POLY,
                                                  layers=["population_toy_density"]))
    assert theirs["status"] == "declined"                    # not visible to them yet
    _as(REVIEWER, lambda: staging.approve(out["contribution_id"], REVIEWER))
    after = _as(OTHER, lambda: assemble.assemble(pack="toy", place="Site", geometry=POLY,
                                                 layers=["population_toy_density"]))
    assert after["status"] == "ok" and "staged_by" not in after
    assert after["layers"]["population_toy_density"]["unit"] == "people per pixel"


def test_a_pack_with_no_area_says_so_instead_of_skipping(world, monkeypatch, log):
    """Food security resolves no area. Asked for layers without a polygon it declares
    that; with one, it reads them over the caller's polygon."""
    def fs_gather(target, focus, trace, extras):
        return ([{"n": 1, "kind": "document", "retrieval": "archived-document",
                  "source": "s", "title": "t", "text": "x"}], [], {"queries": {}})
    monkeypatch.setitem(packs.PACKS, "food-security",
                        {**packs.PACKS["food-security"], "gather": fs_gather})
    p = assemble.assemble(country="Kenya", crop="maize", layers=["vulnerability_toy"])
    gap = next(g for g in p["gaps"] if "resolves no area" in g)
    log("OUTPUT", gap[:120])
    assert p["status"] == "ok" and "layers" not in p
    q = assemble.assemble(country="Kenya", crop="maize", layers=["vulnerability_toy"],
                          geometry=POLY)
    assert q["layers"]["vulnerability_toy"]["summary"]["max"] == 4


def test_the_land_cover_pack_honours_layers(lc_env, log):
    """A second pack, no land-cover code: the class map of a year read as values over
    the pack's own boundary (64 cells inside, by construction)."""
    p = assemble.assemble(pack="land-cover", place="Test District", year="2022",
                          layers=["landcover_test_2022"])
    assert p["status"] == "ok", p.get("note")
    rec = p["layers"]["landcover_test_2022"]
    log("OUTPUT", json.dumps(rec["summary"]))
    assert rec["summary"]["cells_in_area"] == 64
    assert rec["validation"] == "structural-contract-checked"
    kinds = [c["kind"] for c in p["citations"]]
    assert kinds[-2:] == ["layer_values", "gaps"]


def test_the_panel_copy_thins_then_drops_grids_and_keeps_every_row(world, log):
    """insight.layers has its own budget: grids are thinned, then dropped (defaults
    first), and every layer keeps its row and a note."""
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["vulnerability_toy", "vulnerability_flat_index"])
    pack = store.load_pack(p["pack_id"])
    rows = publish._insight_layers(pack)
    assert [r["layer"] for r in rows] == ["vulnerability_toy", "vulnerability_flat_index"]
    assert rows[0]["range"] == {"lo": 0, "hi": 5, "from": "declared"}
    full = len(json.dumps(rows))
    thin = publish._insight_layers(pack, budget=full - 50)
    log("OUTPUT", [(r["layer"], (r["grid"] or {}).get("w"), r.get("note")) for r in thin])
    assert len(thin) == 2 and any("thinned" in (r.get("note") or "") for r in thin)
    assert max(r["grid"]["w"] for r in thin if r["grid"]) < 10
    tiny = publish._insight_layers(pack, budget=300)
    assert all(r["grid"] is None for r in tiny) and len(tiny) == 2
    assert all("omitted" in r["note"] for r in tiny)


def test_the_tool_takes_layers(log):
    """The MCP signature carries the argument — the suite calls modules directly, so
    a parameter dropped at the tool would otherwise pass unnoticed."""
    from app.mcp.server import mcp
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    props = tools["assemble_pack"].parameters["properties"]
    assert "layers" in props
    assert "LAYER VALUES" in tools["assemble_pack"].description


def test_review_fixes_hold(world, monkeypatch, log):
    """Found by adversarial review: a NaN no-data tag must not poison the stored pack; a
    replayed receipt re-verifies every layer's fingerprint from the pack alone; the
    citation states each number the layers block hands a drafter; a pack's own `area`
    figure is not swallowed by the seam's reserved keys."""
    _tif(world / "cache" / "tiffs" / "vulnerability_flat_index.tif",
         np.where(np.arange(400).reshape(20, 20) % 7 == 0, np.nan, 0.476), "float32",
         nodata=float("nan"))
    sch = world / "conf" / "raster_schema.yml"
    doc = yaml.safe_load(sch.read_text())
    doc["layers"]["vulnerability_flat_index"].pop("nodata")      # the file's tag decides
    sch.write_text(yaml.safe_dump(doc))
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["vulnerability_flat_index", "vulnerability_toy"])
    rec = p["layers"]["vulnerability_flat_index"]
    log("OUTPUT", rec["nodata"])
    assert rec["nodata"]["value"] is None and "NaN" in rec["nodata"]["rule"]
    json.dumps(store.load_pack(p["pack_id"]), allow_nan=False)     # no NaN anywhere
    r = record.record(pack_id=p["pack_id"], question="toy")
    view = record.record(receipt_id=r["receipt_id"])
    assert view["layers_verified"] == {"vulnerability_flat_index": True, "vulnerability_toy": True}
    cit = next(c for c in p["citations"] if c.get("layer") == "vulnerability_toy")
    assert "and 0 do not" in cit["text"] and "covers 50.0% of the" in cit["text"]
    # a contributed pack's own figure named `area` is left alone
    from app.mcp import layers as layers_step
    stats = {"area": 12.5, "_area": {"geometry": None}, "_layer_defaults": []}
    assert layers_step.strip_reserved(stats) == (None, []) and stats == {"area": 12.5}


def test_inline_grids_are_budgeted_by_size_and_say_so(world, monkeypatch, log):
    """A float layer with many distinct values costs ~7 characters a cell; the inline
    grids are thinned until they fit, the record and citation say so, and the hash is
    of what was returned."""
    rng = np.random.default_rng(3)
    _tif(world / "cache" / "tiffs" / "vulnerability_flat_index.tif",
         rng.random((20, 20)).astype("float32"), "float32", nodata=-9999)
    monkeypatch.setattr(layer_values, "INLINE_CHARS", 300)
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["vulnerability_flat_index"])
    rec = p["layers"]["vulnerability_flat_index"]
    log("OUTPUT", f"{rec['grid']['encoding']} {rec['grid']['w']}x{rec['grid']['h']} {rec.get('grid_thinned')}")
    assert rec["grid"]["encoding"] == "values" and rec["grid_thinned"]
    assert rec["summary"]["cells_with_value"] == 100            # the summary is untouched
    cit = next(c for c in p["citations"] if c.get("layer") == "vulnerability_flat_index")
    assert "proportionate" in cit["text"]
    assert rec["values_sha256"] == layer_values.values_sha256(
        "vulnerability_flat_index", rec, rec["area_sha256"])


def _register(world, layer, row, contract):
    """Add a hand-authored row and contract to the world's catalog."""
    cat = world / "conf" / "tiffs.yml"
    sch = world / "conf" / "raster_schema.yml"
    c, d = yaml.safe_load(cat.read_text()), yaml.safe_load(sch.read_text())
    c[layer] = row
    d["layers"][layer] = contract
    cat.write_text(yaml.safe_dump(c))
    sch.write_text(yaml.safe_dump(d))


def test_every_no_value_marker_is_masked_and_nominal_codes_are_never_averaged(world, log):
    """The contract's nodata, the file's tag and the row's outside_values all mean 'no
    value' at once; a nominal (land-cover) layer reports classes by part, never a mean."""
    lc = np.full((20, 20), 3)
    lc[5:10, 5:15] = 0                     # outside the footprint, inside the polygon
    lc[10:15, 10:15] = 9
    _tif(world / "cache" / "tiffs" / "landcover_toy_2020.tif", lc, "uint8", nodata=15)
    _register(world, "landcover_toy_2020",
              {"local_path": "tiffs/landcover_toy_2020.tif", "title": "Toy land cover 2020",
               "source": "test fixture", "license": "CC0-1.0", "vintage": "2020",
               "outside_values": [0, 15], "legend": {3: "Forest", 9: "Grassland"}},
              {"role": "landcover", "dtype": "uint8", "valid_min": 0, "valid_max": 15,
               "nodata": 15})
    sch = world / "conf" / "raster_schema.yml"
    d = yaml.safe_load(sch.read_text())
    d["roles"]["landcover"] = {"zero": "nodata", "scale": "nominal"}
    sch.write_text(yaml.safe_dump(d))
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["landcover_toy_2020"])
    rec = p["layers"]["landcover_toy_2020"]
    cit = next(c for c in p["citations"] if c.get("layer") == "landcover_toy_2020")
    log("OUTPUT", f"{rec['nodata']} | {rec['summary'].get('by_part', {}).get('south-east')}")
    assert set(rec["nodata"]["values"]) == {15, 0}
    assert rec["summary"]["cells_with_value"] == 50 and 0 not in _decode(rec["grid"])
    assert "mean" not in rec["summary"] and ", mean" not in cit["text"]
    assert rec["summary"]["by_part"]["south-east"]["label"] == "Grassland"
    assert "Most common class by part of the area" in cit["text"]


def test_coverage_orientation_and_placement_are_said_not_assumed(world, log):
    """Half the area beyond the raster is a declared gap; a south-up file reads like a
    north-up one; a file with no CRS is a gap, never assumed to be degrees."""
    half = [103.01, 13.005, 103.03, 13.015]                 # east half beyond the file
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=half,
                          layers=["vulnerability_toy"])
    rec = p["layers"]["vulnerability_toy"]
    gap = next(g for g in p["gaps"] if "covers only" in g)
    log("OUTPUT", gap)
    # one decimal place: a share just short of 100% must never print as 'only 100%'
    assert rec["summary"]["cells_beyond_layer"] > 0 and "50.0%" in gap
    up = np.ones((20, 20))
    up[5:15, 5:10] = 2
    up[5:15, 10:15] = 4
    path = world / "cache" / "tiffs" / "vulnerability_southup.tif"
    from rasterio.transform import Affine
    with rasterio.open(path, "w", driver="GTiff", height=20, width=20, count=1, dtype="uint8",
                       crs="EPSG:4326", transform=Affine(0.001, 0, 103.0, 0, 0.001, 13.0)) as dst:
        dst.write(up[::-1].astype("uint8"), 1)                # row 0 is the SOUTH edge
    _register(world, "vulnerability_southup",
              {"local_path": "tiffs/vulnerability_southup.tif", "title": "South-up",
               "source": "test fixture", "license": "CC0-1.0", "vintage": "2026-10"},
              {"role": "vulnerability_reclass", "dtype": "uint8", "valid_min": 0, "valid_max": 5})
    q = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["vulnerability_southup"])
    s = q["layers"]["vulnerability_southup"]["summary"]
    assert (s["min"], s["max"], s["cells_with_value"]) == (2, 4, 100)
    assert s["by_part"]["west"] == 2 and s["by_part"]["east"] == 4
    nocrs = world / "cache" / "tiffs" / "vulnerability_nocrs.tif"
    with rasterio.open(nocrs, "w", driver="GTiff", height=20, width=20, count=1, dtype="uint8",
                       transform=from_bounds(*B, 20, 20)) as dst:
        dst.write(up.astype("uint8"), 1)
    _register(world, "vulnerability_nocrs",
              {"local_path": "tiffs/vulnerability_nocrs.tif", "title": "No CRS",
               "source": "test fixture", "license": "CC0-1.0", "vintage": "2026-10"}, {})
    r = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=POLY,
                          layers=["vulnerability_nocrs"])
    assert "layers" not in r and any("coordinate reference system" in g for g in r["gaps"])


def test_a_budgeted_read_never_takes_values_from_averaged_overviews(world, monkeypatch, log):
    """A file whose own overviews were averaged holds classes 1 and 5 only; read under a
    budget, the values are still only 1 and 5 — never an invented 3."""
    from rasterio.enums import Resampling
    big = np.ones((400, 400), dtype="uint8")
    big[:, 200:] = 5
    big[::2, :] = np.where(big[::2, :] == 1, 5, 1)          # alternate rows: averages to 3
    path = world / "cache" / "tiffs" / "vulnerability_ovr.tif"
    with rasterio.open(path, "w", driver="GTiff", height=400, width=400, count=1, dtype="uint8",
                       crs="EPSG:4326", transform=from_bounds(103.0, 13.0, 103.4, 13.4, 400, 400),
                       tiled=True, blockxsize=256, blockysize=256) as dst:
        dst.write(big, 1)
        dst.build_overviews([2, 4, 8], Resampling.average)
    _register(world, "vulnerability_ovr",
              {"local_path": "tiffs/vulnerability_ovr.tif", "title": "Averaged overviews",
               "source": "test fixture", "license": "CC0-1.0", "vintage": "2026-10"},
              {"role": "vulnerability_reclass", "dtype": "uint8", "valid_min": 0, "valid_max": 5})
    monkeypatch.setattr(layer_values, "READ_BUDGET", 5_000)
    rec = layer_values.read("vulnerability_ovr", __import__("shapely.geometry").geometry.box(
        103.0, 13.0, 103.4, 13.4))
    log("OUTPUT", f"level {rec['read_level']['overview_level']} values {sorted(set(_decode(rec['grid'])))}")
    assert rec["read_level"]["overview_level"] >= 2
    assert set(_decode(rec["grid"])) <= {1, 5}
