"""Sprint fix round 2 (2026-10-19): land-cover answers, tool texts, retrieval and the
evidence view.

An answer for an area alone cites the tables and feeds bound to the pack by their rows
inside it; the default stack treats cell sizes within 1% as one size and then prefers
more years, and a stack whose role is not land cover is never a candidate; the evidence
view shows a measure's percentiles, its stated total, km² per class and the per-class
split, and what ingest did to each file; assemble_pack's text says what a measure_
layer returns, from the schema; the risk pack's region is read from its maps; every
pack's retrieval passes over reference lists and documents about other countries.
All on synthetic data, never presented as real."""
import hashlib
import json
import re
import shutil
import subprocess

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, mapping, shape

from app.config import get_settings
from app.contrib import fetch_policy, identity, rasters, staging
from app.graph.geo import stacks
from app.land_cover import synthesis as lc
from app.mcp import app_ui, assemble, fetch, packs, publish, registry, store
from app.rag import filters as rag_filters
from app.rag.store import Corpus

W = H = 10
PX = 0.001
LEFT, TOP = -85.0, 10.0
POLY = mapping(box(LEFT + 1 * PX, TOP - 9 * PX, LEFT + 9 * PX, TOP - 1 * PX))   # columns/rows 1-8
FAR = [39.80, -3.90, 39.85, -3.85]                                            # no stack reaches it
OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
BASE_LEGEND = {1: "Mangrove", 2: "Other", 3: "Water", 4: "Salt flat"}
PREPARED = ["reprojected at ingest from EPSG:32616 to EPSG:4326, nearest neighbour — class values unchanged",
            "band 1 of 4 kept; the other bands are not read"]


def _write(path, arr, nodata=None, px=PX):
    with rasterio.open(path, "w", driver="GTiff", width=arr.shape[1], height=arr.shape[0], count=1,
                       dtype=arr.dtype, crs="EPSG:4326", transform=from_origin(LEFT, TOP, px, px),
                       nodata=nodata) as dst:
        dst.write(arr, 1)
    return str(path)


def _stack(title, legend, pattern, years, data, role="landcover", prefix="landcover_"):
    return {"kind": "stack", "role": role, "internal": True, "title": title,
            "description": "synthetic", "source": "test fixture", "license": "CC0-1.0",
            "pub_date": "2026-01-01", "authority": "test", "accuracy": "unstated", "legend": legend,
            "outside_values": [0], "years": years, "countries": ["Testland"],
            "extent": {"lon": [LEFT, LEFT + W * PX], "lat": [TOP - H * PX, TOP]},
            "layer_pattern": f"{prefix}{pattern}_{{year}}", "file_pattern": f"{pattern}-{{year}}.tif",
            "data_root": str(data)}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Three stacks over one 10x10 bay, registered in this order: a change map landed as
    a one-year land-cover stack whose reprojected cells are 0.02% finer, the three-year
    cover map it was made from (its 2014 year reprojected and cut to band 1 at ingest),
    and a finer change map whose role is `change`, not land cover."""
    s = get_settings()
    data = tmp_path / "data"; data.mkdir()
    base = np.full((H, W), 2, "uint8"); base[:, :4] = 1; base[:, 8:] = 3   # Mangrove 0-3, Water 8-9
    for y in (1956, 1985, 2014):
        _write(data / f"base-{y}.tif", base, nodata=0)
    chg = np.full((H, W), 220, "uint8"); chg[:, :3] = 110; chg[:, 3] = 210
    _write(data / "chg-2014.tif", chg, nodata=0, px=PX * 0.9998)
    _write(data / "cx-2014.tif", np.ones((2 * H, 2 * W), "uint8"), nodata=0, px=PX / 2)
    cat = {
        "landcover_chg": _stack("Change map landed as a stack (synthetic)",
                                {110: "Mangrove persistence", 120: "Mangrove gain", 210: "Mangrove loss",
                                 220: "Non-mangrove"}, "chg", [2014], data),
        "landcover_base": _stack("Base cover map (synthetic)", BASE_LEGEND, "base", [1956, 1985, 2014], data),
        "change_cx": _stack("Change map of the change role (synthetic)", {1: "Gain", 2: "Loss", 3: "Stable"},
                            "cx", [2014], data, role="change", prefix="change_"),
        "landcover_base_2014": {"stack": "landcover_base", "year": 2014, "internal": True,
                                "path": str(data / "base-2014.tif"), "local_path": "stacks/base-2014.tif",
                                "title": "Base cover map (synthetic) — 2014", "legend": BASE_LEGEND,
                                "outside_values": [0], "source": "test fixture", "license": "CC0-1.0",
                                "prepared": PREPARED},
    }
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "tiffs.yml").write_text(json.dumps(cat))
    contract = {"role": "landcover", "dtype": "uint8", "valid_min": 0, "valid_max": 255}
    (conf / "raster_schema.yml").write_text(json.dumps({
        "defaults": {"crs": "EPSG:4326", "float_tol": 0.0001}, "layers": {},
        "stacks": {"landcover_chg": contract, "landcover_base": contract,
                   "change_cx": {**contract, "role": "change"}}}))
    feeds_dir = tmp_path / "feeds"; feeds_dir.mkdir()
    cache = tmp_path / "cache"
    for k, v in (("tiffs_config_path", conf / "tiffs.yml"), ("tiffs_contrib_path", conf / "tiffs.contrib.yml"),
                 ("raster_schema_path", conf / "raster_schema.yml"),
                 ("raster_schema_contrib_path", conf / "raster_schema.contrib.yml"),
                 ("cache_dir", cache), ("tiffs_dir", cache / "tiffs"), ("feeds_conf_dir", feeds_dir),
                 ("grp_mattermost_webhook", ""), ("grp_auto_approve", True)):
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    files = {}
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (files[url], url.rsplit("/", 1)[-1]))
    registry.reload_declarative_feeds()
    staging.STAGED_FEEDS.clear()
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_LOADED", True)
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    yield {"tmp": tmp_path, "files": files, "data": data}
    monkeypatch.undo()
    registry.reload_declarative_feeds()
    staging.STAGED_FEEDS.clear()
    staging.STAGED_VECTORS.clear()


def _submit(kind, manifest):
    tok = identity.bind(OWNER)
    try:
        return staging.submit(kind, manifest, OWNER)
    finally:
        identity.unbind(tok)


# ------------------------------------------------- 1. area-only answers cite tables
def test_an_area_only_answer_cites_the_tables_bound_to_the_pack_by_their_rows_inside(env, log):
    """Kenya had mangrove polygons and soil cores bound to the land-cover pack inside the
    asked box, and the area-only answer cited the polygons alone. A vector, a context
    table and a factor table land through the gate a moment before the question."""
    w, s_, e, n = FAR
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"NAME": "Stand A"},
         "geometry": mapping(box(w + 0.01, s_ + 0.01, w + 0.02, s_ + 0.02))}]}
    env["files"]["https://x.test/m.geojson"] = json.dumps(fc).encode()
    out = _submit("vector", {"layer": "test_far_stands", "url": "https://x.test/m.geojson",
                             "title": "Far mangrove stands (synthetic)", "description": "synthetic",
                             "source": "test", "license": "CC0-1.0", "vintage": "2026", "name_field": "NAME"})
    assert out["status"] == "approved", out
    cores = (f"Site,Latitude,Longitude,Depth_cm\nin-1,{s_ + 0.015},{w + 0.015},100\n"
             f"in-2,{s_ + 0.02},{w + 0.02},300\nlondon,51.5,-0.1,50\n")
    base = {"pack": "land-cover", "description": "synthetic rows", "source": "test fixture",
            "validation": "unvalidated", "license": "CC0-1.0", "vintage": "2026-10", "cadence": "irregular",
            "units": "cm"}
    out = _submit("table", {**base, "dataset": "test_far_cores", "title": "Soil cores, worldwide (synthetic)",
                            "columns": {"site": "Site", "latitude": "Latitude", "longitude": "Longitude",
                                        "depth_cm": "Depth_cm"}, "csv_text": cores})
    assert out["status"] == "approved", out
    out = _submit("table", {**base, "dataset": "test_far_factor", "title": "Mangrove carbon factor (synthetic)",
                            "columns": {"class": "Class", "factor": "Factor"}, "units": "Mg C/ha",
                            "csv_text": "Class,Factor\nMangrove,150\n"})
    assert out["status"] == "approved", out

    p = assemble.assemble(pack="land-cover", place="Far coast", geometry=FAR, vectors=["test_far_stands"])
    assert p["status"] == "ok", p.get("note")
    kinds = [c["kind"] for c in p["citations"]]
    log("OUTPUT", kinds)
    assert kinds[:2] == ["area", "table"] and "vector_summary" in kinds and "class_factor" not in kinds
    table = next(c for c in p["citations"] if c["kind"] == "table")
    log("OUTPUT", table["text"][:240])
    assert table["dataset"] == "test_far_cores" and table["rows_about_area"] == 2
    assert "2 of its 3 row(s) are about Far coast" in table["text"]
    assert "depth_cm min 100, mean 200, max 300" in table["text"] and "london" not in table["text"]
    assert "each table or feed bound to this pack that has rows inside it" in p["citations"][0]["text"]
    assert p["gaps"][0].startswith("no land-cover class is counted in Far coast")
    assert any("factors[test_far_factor] not applied: no class area is counted" in t for t in p["trace"])
    # the numbers stand in a citation, so the gate can check a brief that quotes them
    assert any(c["n"] == table["n"] for c in p["citations"]) and table["n"] == 2


# ------------------------------------------------------------ 2. the default stack
def _rows(**cells):
    return {sid: {"kind": "stack", "role": "landcover", "title": sid, "legend": {1: "A", 2: "B", 3: "C"},
                  "years": years, "extent": {"lon": [LEFT, LEFT + W * PX], "lat": [TOP - H * PX, TOP]}}
            for sid, (cell, years) in cells.items()}


def _pick(monkeypatch, rows, cells):
    monkeypatch.setattr(lc, "landcover_stacks", lambda: rows)
    monkeypatch.setattr(lc, "stack_cell_deg", lambda sid, row: cells[sid])
    return lc._pick_stack(shape(POLY), years=(2014,), name="Test bay")


def test_cells_within_one_percent_are_one_size_then_more_years_then_first_registered(monkeypatch, log):
    """The CATIE change map reprojected to cells 0.02% finer than the CATIE map it was made
    from, and every plain Nicoya question read the change map."""
    cells = {"reprojected_copy": PX * 0.9998, "the_map": PX, "same_again": PX}
    rows = _rows(reprojected_copy=(0, [2014]), the_map=(0, [1956, 1985, 2014]),
                 same_again=(0, [1956, 1985, 2014]))
    sid, _row, others, why = _pick(monkeypatch, rows, cells)
    log("OUTPUT", why)
    assert sid == "the_map"                                  # more years; registered before same_again
    assert [s for s, _ in others] == ["reprojected_copy", "same_again"]
    assert stacks.RANK_RULE in why and "it holds 3 year(s)" in why
    # a stack really finer (5%) still wins on its cells, whatever its years
    cells["finer"] = PX * 0.95
    rows = {**_rows(finer=(0, [2014])), **rows}
    assert _pick(monkeypatch, rows, cells)[0] == "finer"
    # all else equal, the first registered is read
    rows = _rows(first=(0, [2014]), second=(0, [2014]))
    assert _pick(monkeypatch, rows, {"first": PX, "second": PX * 1.005})[0] == "first"
    assert stacks.cell_tiers([PX, PX * 1.005, PX * 1.02, PX * 1.025]) == {
        PX: 0, PX * 1.005: 0, PX * 1.02: 1, PX * 1.025: 1}


def test_the_default_reads_the_map_not_its_reprojected_change_and_never_another_role(env, log):
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2014", geometry=POLY, layers=[])
    assert p["status"] == "ok", p.get("note")
    assert p["stats"]["stack"] == "landcover_base"
    gap = next(g for g in p["gaps"] if "also cover" in g)
    log("OUTPUT", gap)
    assert "1 other registered land-cover stack(s)" in gap and "landcover_chg" in gap
    assert "change_cx" not in json.dumps(p["gaps"]) and "change_cx" not in json.dumps(p["citations"])
    assert set(lc.landcover_stacks()) == {"landcover_chg", "landcover_base"}
    assert "Change map of the change role" not in packs.text(packs.PACKS["land-cover"], "summary")
    # naming it is refused in words: it is not a land-cover stack
    q = assemble.assemble(pack="land-cover", place="Test bay", year="2014", geometry=POLY, stack="change_cx")
    assert q["status"] == "declined" and "no land-cover stack named 'change_cx'" in q["note"]


# ------------------------------------------------------- 3. the evidence view
def test_the_evidence_view_carries_a_measures_spread_total_per_class_split_and_ingest_notes(env, log):
    """Biomass read beside the cover map: the panel row states p10/median/p90, the total in
    Mg over the area in ha, and mean and total biomass inside each class; the 2014 map's
    passport and the view say it was reprojected and cut to band 1."""
    agb = np.tile((100 + 10 * np.arange(W)).astype("float32"), (H, 1))      # column c: 100 + 10c Mg/ha
    path = _write(env["tmp"] / "agb.tif", agb, nodata=-9999.0)
    out = rasters.add({"layer": "measure_t_agb", "file": path, "title": "Biomass (synthetic)",
                       "description": "aboveground biomass", "source": "test fixture", "license": "CC0-1.0",
                       "vintage": "2026-10", "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 500,
                                                          "nodata": -9999, "units": "Mg/ha"}})
    assert out["status"] == "landed", out
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2014", geometry=POLY,
                          stack="landcover_base", layers=["measure_t_agb"])
    assert p["status"] == "ok", p.get("note")
    passport = next(c for c in p["citations"] if c["kind"] == "landcover_layer")
    assert passport["prepared"] == PREPARED and "At ingest: reprojected at ingest from EPSG:32616" in passport["text"]
    ins = publish._insight(p["pack_id"], "draft")
    row = next(r for r in ins["layers"] if r["layer"] == "measure_t_agb")
    log("OUTPUT", json.dumps({k: row.get(k) for k in ("percentiles", "total", "units")}))
    assert row["units"] == "Mg/ha" and set(row["percentiles"]) == {"p10", "p50", "p90"}
    assert row["percentiles"]["p10"] < row["percentiles"]["p50"] < row["percentiles"]["p90"]
    assert row["total"]["unit"] == "Mg" and row["total"]["area_unit"] == "ha" and row["total"]["value"] > 0
    split = row["by_class_of"]
    by = {c["class"]: c for c in split["classes"]}
    log("OUTPUT", json.dumps(by["Mangrove"]))
    assert split["total_unit"] == "Mg" and split["year"] == "2014"
    assert by["Mangrove"]["mean"] == pytest.approx(120.0, abs=0.5)        # columns 1-3 inside
    assert by["Water"]["mean"] == pytest.approx(180.0, abs=0.5) and by["Mangrove"]["total"] > 0
    assert ins["prepared"][str(passport["n"])] == PREPARED
    # a class layer read as values: km² per class, largest first
    q = assemble.assemble(pack="land-cover", place="Test bay", year="2014", geometry=POLY,
                          stack="landcover_base", layers=["landcover_base_1956"])
    crow = publish._insight(q["pack_id"], "draft")["layers"][0]
    assert [c["label"] for c in crow["classes"]][:1] == ["Other"] and crow["classes"][0]["km2"] > 0


def test_the_panel_rows_keep_their_numbers_and_notes_when_grids_go():
    s = {"min": 10, "max": 90, "mean": 50, "distinct": 9, "cells_with_value": 9, "cells_in_area": 9,
         "p10": 18.0, "p50": 50.0, "p90": 82.0,
         "total": {"value": 4500.0, "unit": "Mg", "area": 90.0, "area_unit": "ha", "mean_area_weighted": 50.0},
         "by_class_of": {"map": "Cover", "year": "2020", "total_unit": "Mg", "km2_outside_map": 0.0,
                         "classes": {"Mangrove": {"km2": 0.5, "km2_with_value": 0.5, "mean": 70.0, "p50": 70.0,
                                                  "total": 3500.0}}}}
    cls = {"classes": {"1": {"label": "Low", "cells": 4, "km2": 0.4, "share_pct": 40.0},
                       "2": {"label": "High", "cells": 6, "km2": 0.6, "share_pct": 60.0}},
           "by_class_of": {"map": "Cover", "classes": {"Mangrove": {"km2": 1.0, "km2_with_value": 1.0,
                           "by_value": {str(v): 0.1 * v for v in range(1, 10)}}}}}
    grid = {"w": 40, "h": 40, "bounds": [0, 0, 1, 1], "encoding": "values", "cells": [1.0] * 1600}
    pack = {"layers": {"measure_x": {"n": 3, "title": "X", "unit": "Mg/ha", "summary": s, "grid": dict(grid),
                                     "prepared": ["clipped to the declared box"]},
                       "risk_y": {"n": 4, "title": "Y", "legend": {"1": "Low", "2": "High"}, "summary": cls,
                                  "grid": dict(grid)}}}
    rows = publish._insight_layers(pack, budget=600)
    assert all(r["grid"] is None and "omitted" in r["note"] for r in rows)
    m, c = rows
    assert m["percentiles"] == {"p10": 18.0, "p50": 50.0, "p90": 82.0} and m["total"]["value"] == 4500.0
    assert m["by_class_of"]["classes"][0] == {"class": "Mangrove", "km2": 0.5, "km2_with_value": 0.5,
                                              "mean": 70.0, "p50": 70.0, "total": 3500.0}
    assert m["prepared"] == ["clipped to the declared box"]
    assert [r["label"] for r in c["classes"]] == ["High", "Low"]
    cross = c["by_class_of"]["classes"][0]
    assert [v["value"] for v in cross["by_value"]] == ["9", "8", "7", "6", "5", "4"]
    assert cross["by_value_rest"] == {"values": 3, "km2": pytest.approx(0.6)}
    assert publish._insight_prepared(pack) == {"3": ["clipped to the declared box"]}


def test_the_view_renders_the_measure_fields_and_its_script_parses(tmp_path):
    html = app_ui.template()
    for needle in ("function measureHTML", "function byClassHTML", "function classTableHTML",
                   "preparedHTML(r.prepared)", "r.percentiles", "r.by_class_of", "ins.prepared"):
        assert needle in html, needle
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed: the script's syntax is not checked here")
    script = html.split("<script>")[-1].split("</script>")[0]
    (tmp_path / "app.js").write_text(script)
    r = subprocess.run([node, "--check", str(tmp_path / "app.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:500]


# ------------------------------------------------------------ 4. the tool texts
def test_assemble_pack_says_what_a_measure_returns_from_the_schema(monkeypatch, log):
    from app.graph.geo import schema
    d = registry.describe_assemble()
    line = d[d.index("What a layer's values state"):d.index("VECTOR LAYERS")]
    log("OUTPUT", line[:400])
    assert "measure_<name>: mean, p10 / median / p90" in line and "TOTAL over the area" in line
    assert "landcover_<name>" in line and "km² and share of the area per class" in line
    assert "summary.by_class_of" in line and "land-cover: the year map" in line
    # a namespace added to the schema, and a pack that declares a class map, appear by themselves
    rows = schema.namespaces()
    monkeypatch.setattr(schema, "namespaces", lambda: {**rows, "canopy": {"prefix": "canopy_", "values": "amount"}})
    monkeypatch.setitem(packs.PACKS, "toy", {"target_keys": ("site",), "class_split_doc": "its habitat map"})
    d = registry.describe_assemble()
    assert "canopy_<name>" in d and "toy: its habitat map" in d
    monkeypatch.setitem(packs.PACKS, "land-cover", {k: v for k, v in packs.PACKS["land-cover"].items()
                                                    if k != "class_split_doc"})
    monkeypatch.delitem(packs.PACKS, "toy")
    assert "by_class_of" not in registry.describe_assemble()


def test_the_risk_region_is_read_from_its_maps(monkeypatch, log):
    from app.graph.geo import tiffs
    cov = {"hazard_flood": {"title": "100-year flood hazard (Southeast Asia)", "bounds": [92.0, -11.1, 141.2, 28.7],
                            "countries": []},
           "hazard_flood_rp10": {"title": "10-year flood hazard (Southeast Asia, JRC)", "bounds": None,
                                 "countries": []},
           "hazard_cr_flood": {"title": "Flood depth (Fathom, 90 m)", "bounds": [-86.0, 8.0, -82.5, 11.3],
                               "countries": ["Costa Rica"]},
           "hazard_ni_slides": {"title": "Landslide susceptibility (INETER)", "bounds": [-87.7, 10.7, -83.1, 15.0],
                                "countries": []},
           "hazard_demo": {"title": "Demo heat (Somewhere)", "bounds": None, "countries": ["Nowhere"]}}
    monkeypatch.setattr(packs, "risk_coverage", lambda: cov)
    monkeypatch.setattr(tiffs, "catalog", lambda: {"hazard_demo": {"title": "Demo heat", "description": "synthetic"}})
    summary = packs._risk_summary()
    log("OUTPUT", summary)
    assert "for any place in Southeast Asia; Costa Rica; lon -87.7 to -83.1, lat 10.7 to 15.0," in summary
    assert "INETER" not in summary and "Fathom" not in summary and "Nowhere" not in summary
    assert "(Southeast Asia; Costa Rica;" in packs._risk_keywords()
    monkeypatch.setattr(packs, "risk_coverage", lambda: {})
    assert "where a hazard map in the catalog reaches" in packs._risk_summary()
    assert "Southeast Asia" not in packs._risk_summary()


# ------------------------------------------------------------ 5. retrieval, every pack
class WordEmbedder:
    """A bag of words on 1,024 hashed dimensions: passages sharing a query's words score
    high, unrelated ones near zero — no network."""
    provider, model = "test", "words-v1"

    def embed(self, texts):
        out = []
        for t in texts:
            v = np.zeros(1024, np.float32)
            for w in re.findall(r"[a-záéíóúñ]+", t.lower()):
                v[int(hashlib.sha1(w.encode()).hexdigest(), 16) % 1024] += 1
            n = np.linalg.norm(v)
            out.append(v / n if n else v)
        return np.stack(out)


REFS = ("Referencias\nApellido, A. 2004. Mangrove carbon of the Gulf of Nicoya. San José. 120p.\n"
        "Autora, B. 2010. Mangrove carbon and flood risk in Nicoya. Turrialba. 45p.\n"
        "Nombre, C. 2012. Mangrove carbon, Nicoya flood plains. San José. 30p.")
DOCS = (
    (REFS, {"title": "Nicoya report, references", "source": "test", "countries": ["Costa Rica"]}),
    ("Mangrove carbon in the Gulf of Nicoya was measured on plots; flood risk there is low.",
     {"title": "Nicoya report", "source": "test", "countries": ["Costa Rica"]}),
    ("Mangrove carbon in the Gulf of Guayaquil was measured on plots; flood risk there is high.",
     {"title": "Guayaquil report", "source": "test", "countries": ["Ecuador"]}),
    ("Mangrove carbon guidelines for flood risk, for any coast.",
     {"title": "Global guideline", "source": "test"}),
    ("Mangrove carbon in the Mekong delta after the flood.",
     {"title": "Mekong note", "source": "test", "countries": ["Viet Nam"]}),
)


@pytest.fixture
def library(tmp_path, monkeypatch):
    import app.rag.embed as rag_embed
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "rag_min_relevance", 0.05)
    monkeypatch.setattr(rag_embed, "get_embedder", lambda: WordEmbedder())

    def fill(name, docs=DOCS):
        c = Corpus(name)
        for text, meta in docs:
            c.ingest(text, {**meta, "temporal": "retrospective"})
        return Corpus(name)
    return fill


def test_the_library_passes_over_reference_lists_and_documents_about_other_countries(library, log):
    c = library("land-cover")
    skipped = {}
    hits = c.search("mangrove carbon Nicoya flood", k=3, skipped=skipped, skip_references=True)
    titles = [h["metadata"]["title"] for h in hits]
    log("OUTPUT", titles)
    assert len(hits) == 3 and "Nicoya report, references" not in titles        # k stays k
    assert skipped["references"] == ["Nicoya report, references"]
    assert "Nicoya report, references" in [h["metadata"]["title"] for h in
                                           c.search("mangrove carbon Nicoya flood", k=5)]
    skipped = {}
    here = [h["metadata"]["title"] for h in c.search("mangrove carbon flood", k=5, where=["Costa Rica"],
                                                     skipped=skipped)]
    assert "Nicoya report" in here and "Global guideline" in here
    assert "Guayaquil report" not in here and "Mekong note" not in here
    assert skipped["elsewhere"] == {"Ecuador": ["Guayaquil report"], "Viet Nam": ["Mekong note"]}
    # one rule, one home: the land-cover name is the shared test
    assert lc._looks_like_references is rag_filters.looks_like_references
    assert rag_filters.speaks_to(["Viet Nam"], ["Vietnam"]) and rag_filters.speaks_to([], ["Kenya"])


def test_corpus_search_returns_passages_never_bibliographies_and_says_so(library, log):
    library("food-security")
    out = fetch.search("mangrove carbon Nicoya flood", k=5)
    log("OUTPUT", out.get("passed_over"))
    assert out["status"] == "ok" and all("Referencias" not in h["text"] for h in out["hits"])
    assert "1 reference-list or contents passage(s)" in out["passed_over"]
    # a country is matched however its name is written
    vn = fetch.search("mangrove carbon flood", k=5, country="Vietnam")
    assert [h["passport"]["title"] for h in vn["hits"]] == ["Mekong note"]


def test_a_shelf_holding_only_a_bibliography_declines_in_words(library):
    library("land-cover", docs=DOCS[:1] + (("Rice prices rose in the markets.", {"title": "Prices"}),))
    out = fetch.search("mangrove carbon Nicoya flood", k=5, pack="land-cover")
    assert out["status"] == "empty" and out["note"].startswith("Only reference lists, bibliographies")
    assert "Nicoya report, references" in out["note"]


def test_a_pack_that_does_not_opt_in_loses_no_passage(library, log):
    """Passing over reference lists is opt-in (land cover, corpus_search): a heuristic that
    misreads a dated table must not silently drop another pack's evidence."""
    from app.risk import synthesis as risk
    library(risk.RISK_CORPUS)
    trace, gaps = [], []
    _corpus, hits = risk._document_hits("Nicoya", "hazard_flood", "mangrove carbon Nicoya flood", trace, gaps)
    titles = [h["metadata"]["title"] for h in hits]
    log("OUTPUT", titles)
    assert "Nicoya report, references" in titles and "Nicoya report" in titles
