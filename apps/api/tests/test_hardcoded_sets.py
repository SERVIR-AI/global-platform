"""Workstream H — the lists the gate outgrew. Each test is a hub's data meeting a
sentence typed when the set was smaller."""
import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from app.config import get_settings
from app.contrib import rasters, staging
from app.graph.geo import combine, ingest, store, vectors
from app.mcp import assemble, packs
from app.risk import synthesis as risk


# ----------------------------------------------------------------- H1: coverage
def _entry(bbox, **over):
    e = {"title": "Evacuation centres (Thailand)", "source": "hub list", "vintage": "2026-09",
         "local_path": "vectors/none.geojson", "bbox": bbox, "countries": ["Thailand"]}
    e.update(over)
    return e


def test_a_layer_that_does_not_cover_the_area_is_not_clipped(tmp_path, monkeypatch, log):
    """Battambang asks. Thailand's bounding box CONTAINS Battambang, so the bbox test
    passes; the contributor's declaration (countries: [Thailand]) is what keeps the
    Thai list out. A Cambodian list still clips; a layer declaring nothing still clips."""
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    monkeypatch.setattr(ingest, "_fetch_layer", lambda layer, bbox, boundary: [])
    asked = []
    monkeypatch.setattr(ingest, "_reverse_country", lambda lon, lat: (asked.append((lon, lat)), "Cambodia")[1])
    thai = _entry([97.3, 5.6, 105.6, 20.5])                  # Thailand's extent contains Battambang
    local = _entry([103.0, 13.0, 103.3, 13.2], title="Battambang wells", countries=["Kingdom of Cambodia"])
    silent = _entry([103.0, 13.0, 103.3, 13.2], title="undeclared", countries=None)
    far = _entry([120.0, 14.0, 121.0, 15.0], title="Manila list", countries=["Philippines"])
    monkeypatch.setattr(vectors, "visible", lambda: {"evac_th": thai, "wells": local, "undeclared": silent, "manila": far})
    clipped = []
    monkeypatch.setattr(vectors, "clip_into", lambda adir, boundary, layer, entry: (clipped.append(layer), 0)[1])
    b = ingest.ensure_aoi(geometry=[103.16, 13.07, 103.24, 13.15], layers=["hospitals"])
    log("OUTPUT", f"clipped={clipped} not_applicable={ {k: v['reason'] for k, v in b['vectors_not_applicable'].items()} }")
    assert sorted(clipped) == ["undeclared", "wells"]
    assert b["country"] == "Cambodia" and len(asked) == 1
    na = b["vectors_not_applicable"]
    assert "declares Thailand and this area is in Cambodia" in na["evac_th"]["reason"]
    assert "wholly outside" in na["manila"]["reason"]        # the cheap test still runs first
    assert "evac_th" not in b and "manila" not in b and "wells" in b
    # the country is kept with the area: a second call asks nobody
    ingest.ensure_aoi(geometry=[103.16, 13.07, 103.24, 13.15], layers=["hospitals"])
    assert len(asked) == 1


def test_country_names_are_matched_forgivingly():
    B = type("B", (), {"bounds": (0, 0, 1, 1)})()
    assert vectors.not_applicable(_entry([0, 0, 1, 1], countries=["Viet Nam"]), B, "Vietnam") is None
    assert vectors.not_applicable(_entry([0, 0, 1, 1], countries=["Lao PDR"]), B, "Laos") is None
    assert vectors.not_applicable(_entry([0, 0, 1, 1], countries=["Thailand"]), B, "Cambodia")["area_country"] == "Cambodia"
    assert vectors.not_applicable(_entry([0, 0, 1, 1], countries=["Thailand"]), B, None) is None   # unknown: clip as before


def test_the_pack_declares_a_non_covering_layer_instead_of_counting_zero(aoi, monkeypatch, log):
    thai = _entry([99.0, 5.6, 105.6, 20.5])
    monkeypatch.setattr(vectors, "visible", lambda: {"evac_th": thai})
    a = dict(aoi)
    a["vectors_not_applicable"] = {"evac_th": vectors.not_applicable(thai, type("B", (), {"bounds": (0, 0, 0.01, 0.01)})(), "Cambodia")}
    monkeypatch.setattr(risk.ingest, "ensure_aoi", lambda place=None, geometry=None, layers=None, name=None: dict(a))
    monkeypatch.setattr(risk.ingest, "hazard_clip", lambda x, layer: x["hazard_flood"])
    p = assemble.assemble(place="Testville", hazard="flood")
    assert p["status"] == "ok", p.get("note")
    titles = [c["title"] for c in p["citations"]]
    assert not any("evac_th" in t or "Evacuation centres (Thailand)" in t for t in titles)
    assert "evac_th" not in p["stats"]["counts"] and "evac_th" not in p["stats"]["assets"]
    gap = next(g for g in p["gaps"] if "evac_th" in g)
    log("OUTPUT", gap[:160])
    assert "not a zero" in gap and "Thailand" in gap


def test_an_entry_without_a_bbox_still_clips_as_before():
    assert vectors.not_applicable({"title": "old entry"}, type("B", (), {"bounds": (0, 0, 1, 1)})()) is None


# ----------------------------------------------------------------- H2: coverage text derived
def test_families_are_derived_from_the_recipe_not_typed(log):
    three = {"vulnerability_pop_all_total": .4, "vulnerability_reclass_blddensity": .35,
             "vulnerability_reclass_road": .25}
    covered, absent, outside = combine.families_covered(three)
    log("OUTPUT", f"covered={covered} absent={len(absent)} outside={outside}")
    assert covered == ["population density", "building density", "distance to road"]
    assert len(absent) == 8 and outside == []
    wider = {**three, "vulnerability_reclass_bldheight": .1, "vulnerability_wdpa": .05}
    covered, absent, outside = combine.families_covered(wider)
    assert "building height" in covered and len(absent) == 7
    assert outside == ["vulnerability_wdpa"]                 # declared, not dropped


def test_the_l2_gap_states_the_recipe_in_force(monkeypatch, log):
    monkeypatch.setattr(combine, "staged_weights_row", lambda hz: None)
    monkeypatch.setattr(combine, "adjustment_for", lambda hz: None)
    g = risk._l2_coverage_gap("hazard_flood", {"vulnerability_pop_all_total": .5,
                                               "vulnerability_reclass_bldheight": .5})
    log("OUTPUT", g[0][:200])
    assert "2 vulnerability layer(s)" in g[0] and "2 of the 11" in g[0]
    assert "building density" in g[0]                         # named among the absent
    assert "platform starting values" in g[0]
    monkeypatch.setattr(combine, "adjustment_for", lambda hz: {"by": "a hub"})
    assert "adjusted by a contributor" in risk._l2_coverage_gap("hazard_flood", {"vulnerability_pop_all_total": 1})[0]
    assert risk._l2_coverage_gap("hazard_flood", None) == []


def test_the_manifest_line_is_derived():
    line = packs._vulnerability_coverage_line()
    assert "representing" in line and "of the 11" in line


# ----------------------------------------------------------------- H3: the class contract
def test_the_gate_refuses_a_hazard_declared_outside_its_classes(tmp_path, log):
    f = tmp_path / "h.tif"
    f.write_bytes(b"")
    base = {"layer": "hazard_depth", "file": str(f), "title": "t", "description": "d",
            "source": "s", "license": "CC-BY-4.0", "vintage": "2026-09", "legend": {1: "a"}}
    bad = rasters.validate_manifest({**base, "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 12.5}})
    log("OUTPUT", [x for x in bad if "classes" in x][0][:140])
    assert any("classes 1..5" in x for x in bad)
    ok = rasters.validate_manifest({**base, "declared": {"dtype": "int16", "valid_min": 0, "valid_max": 5}})
    assert not any("classes" in x for x in ok)
    pop = rasters.validate_manifest({**base, "layer": "population_x", "legend": None,
                                     "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 200000}})
    assert not any("classes" in x for x in pop)              # a count grid has no classes


def test_a_landed_hazard_with_a_value_past_the_contract_declines_in_words(aoi, tmp_path, log):
    bad = tmp_path / "bad.tif"
    arr = np.zeros((10, 10), dtype="int16")
    arr[:, :5] = 300
    with rasterio.open(bad, "w", driver="GTiff", height=10, width=10, count=1, dtype="int16",
                       crs="EPSG:4326", transform=from_bounds(0, 0, 0.01, 0.01, 10, 10)) as dst:
        dst.write(arr, 1)
    a = {**aoi, "hazard_bad": str(bad)}
    with pytest.raises(ValueError) as e:
        store.count_in_hazard(a, "hazard_bad", "hospitals")
    log("OUTPUT", str(e.value)[:150])
    assert "300" in str(e.value) and "classes 1..5" in str(e.value)
    with pytest.raises(ValueError):
        store.roads_in_hazard(a, "hazard_bad")
    # and the pack turns it into a governed decline, not a transport error
    assert store.count_in_hazard(a, "hazard_flood", "hospitals")["count"] == 1   # the good layer still counts


# ----------------------------------------------------------------- H4: weights reach contributed layers
def test_a_contributed_vulnerability_layer_is_weightable(monkeypatch):
    from app.graph.geo import schema
    monkeypatch.setattr(schema, "_doc", lambda: {"layers": {
        "vulnerability_reclass_road": {"role": "vulnerability_reclass"},
        "vulnerability_shelter_dist": {"role": "vulnerability"},        # the gate's role
        "hazard_flood": {"role": "hazard"}}})
    assert staging.available_vulnerability_layers() == ["vulnerability_reclass_road", "vulnerability_shelter_dist"]


def test_a_contributed_hazard_can_get_a_recipe(monkeypatch, log):
    from app.graph.geo import tiffs
    monkeypatch.setattr(tiffs, "resolve", lambda k: "hazard_heatdays" if k == "heatdays" else None)
    monkeypatch.setattr(tiffs, "catalog", lambda include_staged=True: {"hazard_heatdays": {}, "hazard_flood": {}})
    monkeypatch.setattr(staging, "available_vulnerability_layers", lambda: ["vulnerability_pop_all_total"])
    problems = staging._validate_weights({"hazard": "heatdays", "rationale": "heat hits the old",
                                          "weights": {"vulnerability_pop_all_total": 1.0}})
    log("OUTPUT", problems)
    assert problems == []                                       # no recipe yet is not a refusal
    unknown = staging._validate_weights({"hazard": "volcano", "rationale": "x",
                                         "weights": {"vulnerability_pop_all_total": 1.0}})
    assert any("no hazard layer named 'volcano'" in p and "heatdays" in p for p in unknown)
    prepared = staging._prepare_weights({"hazard": "heatdays", "weights": {"vulnerability_pop_all_total": 1.0}})
    assert prepared["before"] == {} and prepared["after"] == {"vulnerability_pop_all_total": 1.0}


# ----------------------------------------------------------------- H5: gate vs consumer
def test_a_feed_or_table_bound_to_a_pack_that_cites_nothing_is_declined(monkeypatch, log):
    from app.mcp import packs as _packs
    # a scaffolded pack that reads no sources yet (land-cover used to be the example;
    # it now reads factor tables, so a table bound to it is accepted — asserted below)
    monkeypatch.setitem(_packs.PACKS, "scaffold-test", {"target_keys": ("place",), "sections": lambda: []})
    for pack, needle in (("scaffold-test", "cites no feeds or tables"), ("Risk", "unknown pack 'Risk'"),
                         ("asean-pack", "unknown pack")):
        f = staging._validate_feed({"dataset": "x", "pack": pack})
        t = staging._validate_table({"dataset": "x", "pack": pack, "csv_text": "a,b\n1,2"})
        log("OUTPUT", f"{pack}: {[x for x in f if 'pack' in x][:1]}")
        assert any(needle in x for x in f) and any(needle in x for x in t)
    assert not any("pack" in x for x in staging._validate_feed({"dataset": "x", "pack": "risk"}))
    assert not any("pack" in x for x in staging._validate_table({"dataset": "x", "pack": "land-cover",
                                                                   "csv_text": "a,b\n1,2"}))


def test_risk_feeds_are_cited_only_where_they_speak(monkeypatch, log):
    from app.mcp import feeds, registry
    monkeypatch.setattr(registry, "FEEDS", {
        "thai_gauge": {"pack": "risk", "status": "available", "countries": ["Thailand"], "title": "Thai gauge"},
        "global_quakes": {"pack": "risk", "status": "available", "title": "USGS"}})
    monkeypatch.setattr(staging, "visible_staged_feeds_for_pack", lambda pack: {})
    monkeypatch.setattr(feeds, "query", lambda ds, q=None: {"status": "ok", "records": [{"v": 1}],
                                                            "count": 1, "as_of": None, "passport": {}})
    trace, gaps = [], []
    out = risk._pack_feeds("Battambang", trace, gaps, country="Cambodia")
    log("OUTPUT", f"cited={[d for d, _, _ in out]} gap={gaps[-1][:90] if gaps else None}")
    assert [d for d, _, _ in out] == ["global_quakes"]
    assert any("declare other countries than Cambodia (thai_gauge)" in g for g in gaps)
    out = risk._pack_feeds("Bangkok", [], [], country="Thailand")
    assert sorted(d for d, _, _ in out) == ["global_quakes", "thai_gauge"]
    out = risk._pack_feeds("Somewhere", [], [], country=None)          # unknown: everything, as before
    assert len(out) == 2


def test_a_pack_bound_table_clears_the_food_security_evidence_bar():
    from app.food_security import synthesis as fs
    assert not fs._has_evidence([{"kind": "index", "brief_role": "driver"}, {"kind": "calendar"}], "Malawi")
    malawi_table = {"kind": "index", "brief_role": "pack-source", "countries": ["Malawi"]}
    assert fs._has_evidence([malawi_table], "Malawi")
    assert not fs._has_evidence([{"kind": "index", "brief_role": "pack-source"}], "Malawi")   # global: context
    assert not fs._has_evidence([{**malawi_table, "countries": ["Thailand"]}], "Malawi")
    assert fs._has_evidence([{"kind": "document"}], None)


def test_every_kind_carries_the_auto_approve_note_into_its_citation(monkeypatch, log):
    assert staging._review_note({"auto_approved": True}, "table") == "auto-approved — no human reviewed this table"
    assert staging._review_note({}, "table") is None
    monkeypatch.setattr(combine, "staged_weights_row", lambda hz: None)
    monkeypatch.setattr(combine, "adjustment_for", lambda hz: {"by": "a hub", "review": "auto-approved — no human reviewed this recipe"})
    prov = risk._weights_provenance("hazard_flood")
    log("OUTPUT", prov[:120])
    assert prov.startswith("Review: auto-approved")
    # a staged driver feed previews AS a driver, carrying who staged it
    from app.food_security import synthesis as fs
    from app.mcp import feeds, registry
    monkeypatch.setattr(registry, "FEEDS", {})
    monkeypatch.setattr(staging, "visible_staged_feeds_for_pack", lambda pack: {
        "oni_hub": {"brief_role": "driver", "status": "available", "staged_by": "user_1",
                    "contribution_id": "abc", "source": "NOAA", "title": "ONI (hub copy)",
                    "review": "auto-approved — no human reviewed this feed"}})
    monkeypatch.setattr(feeds, "query", lambda ds, q=None: {"status": "ok", "as_of": "2026-09",
                                                            "records": [{"period": "ASO 2026", "oni": 0.4}],
                                                            "passport": {}, "count": 1})
    res = fs._driver_citations([])
    cits = res[0] if isinstance(res, tuple) else res
    assert cits and cits[0]["brief_role"] == "driver" and cits[0]["staged_by"] == "user_1"
    assert cits[0]["review_status"].startswith("auto-approved")


# ----------------------------------------------------------------- H6: text derived from the catalog
def test_pack_summaries_name_what_the_catalog_holds_today(monkeypatch, log):
    from app.graph.geo import tiffs
    monkeypatch.setattr(tiffs, "catalog", lambda include_staged=True: {
        "hazard_flood": {}, "hazard_heatdays": {"title": "heat days"}, "hazard_flood_rp10": {},
        "hazard_flood_rp500": {}, "population_worldpop": {}, "hazard_demo": {"description": "SYNTHETIC DEMONSTRATION LAYER"}})
    monkeypatch.setattr(packs, "_is_synthetic", lambda row: "SYNTHETIC" in str(row.get("description", "")))
    summary = packs.text(packs.PACKS["risk"], "summary")
    log("OUTPUT", summary[:200])
    assert "heatdays" in summary and "flood at 10- to 500-year return periods" in summary
    assert "demo" not in summary and summary.startswith("HAZARD RISK — people, roads")
    from app.mcp import registry
    monkeypatch.setattr(registry, "coverage", lambda: {"crops": ["maize", "sorghum"], "countries": ["Kenya", "Malawi"],
                                                       "subjects": [], "publishers": []})
    fs_summary = packs.text(packs.PACKS["food-security"], "summary")
    assert "sorghum" in fs_summary and "Malawi" in fs_summary
    from app.food_security import synthesis as fs
    assert "Malawi" in fs._parse_system() and "anywhere is in scope" in fs._parse_system()
    assert packs.target_shapes() == "country+crop | place+hazard | place+year"
    assert "land cover: place + year [+ year_to, classes, stack]" in packs.target_shapes_long()
