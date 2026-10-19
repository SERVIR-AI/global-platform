"""Round-2 hand-offs across files (mangrove sprint, 2026-10): the gate runs the readers' own
count before approving a class map and takes a float32 fill as declared; a change map's
from_year/to_year/compares ride the staged and landed rows; one stack ranking rule.

Every fixture is synthetic and built here, on the four-stack bay of
test_sprint_fix_landcover; layers arrive through the gate a moment before they are read."""

import json
import os
import shutil
import subprocess
import threading
import time

import numpy as np
import pytest
import rasterio
from shapely.geometry import box, mapping, shape

from app.config import get_settings
from app.contrib import catalog_io, fetch_policy, identity, rasters, removal, staging, tables
from app.graph.geo import align, combine, ingest, schema, stacks, tiffs, vectors
from app.land_cover import synthesis as lc
from app.food_security import synthesis as fs
from app.mcp import app_ui, assemble, feeds, packs, publish, record, registry, store
from app.rag import filters as rag_filters
from app.risk import synthesis as risk

from test_layer_values import POLY as RISK_POLY, world  # noqa: F401  (the risk pack's toy area)
from test_sprint_fix_landcover import H, LEFT, POLY, PX, TOP, W, _write, lc_env  # noqa: F401

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
MANGROVE = {1: "Mangrove", 2: "Other"}
CHANGE_LEGEND = {110: "Mangrove persistence", 120: "Mangrove gain", 210: "Mangrove loss",
                 220: "Non-mangrove both years"}
I32_FILL = -2147483648


@pytest.fixture
def env(lc_env, monkeypatch):
    """The bay, auto-approving, with the platform's role table and a fetcher serving the
    files written here by URL."""
    sch = lc_env["tmp"] / "conf" / "raster_schema.yml"
    doc = json.loads(sch.read_text())
    doc["roles"] = schema._shipped()["roles"]
    sch.write_text(json.dumps(doc))
    files = {}
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch",
                        lambda url, max_bytes=None: (files[url], url.rsplit("/", 1)[-1]))
    for g in ("STAGED_RASTERS", "STAGED_AMENDS", "STAGED_WEIGHTS"):
        monkeypatch.setattr(staging, g, {}, raising=False)

    def serve(name, arr, nodata=None):
        path = lc_env["tmp"] / name
        _write(path, arr, nodata=nodata)
        url = f"https://hub.test/{name}"
        files[url] = path.read_bytes()
        return url
    return {**lc_env, "serve": serve}


def _submit(layer, url, **over):
    m = {"layer": layer, "url": url, "title": f"{layer} (synthetic)",
         "description": "synthetic test layer", "source": "test fixture",
         "license": "CC0-1.0", "vintage": "2026-10", "countries": ["Testland"], **over}
    tok = identity.bind(OWNER)
    try:
        return staging.submit("raster", m, OWNER)
    finally:
        identity.unbind(tok)


def _change_map():
    """Columns 0-2 persistence, 3 loss, 4 gain, the rest neither; the int32 fill on row 0."""
    a = np.full((H, W), 220, dtype="int32")
    a[:, :3] = 110
    a[:, 3] = 210
    a[:, 4] = 120
    a[0, :] = I32_FILL
    return a


CHANGE_DECL = {"dtype": "int32", "valid_min": 110, "valid_max": 220, "nodata": I32_FILL,
               "units": "class"}


# ------------------------------------------------- 1. the gate, as the readers read
def test_a_float32_measure_declared_minus_3_4e38_lands_through_the_gate(env):
    """The file's tag is -3.3999999521443642e+38; the contributor declares -3.4e38."""
    dem = np.full((H, W), 5.0, "float32")
    dem[:, 7:] = np.float32(-3.4e38)
    url = env["serve"]("dem.tif", dem, nodata=float(np.float32(-3.4e38)))
    out = _submit("measure_bay_dem", url, declared={"dtype": "float32", "valid_min": 0,
                                                    "valid_max": 10, "nodata": -3.4e38, "units": "m"})
    assert out["status"] == "approved", out
    assert "measure_bay_dem" in tiffs.catalog()


def test_the_gate_declines_a_class_map_its_readers_cannot_count(env, monkeypatch):
    """The gate approved an int32 map whose -2**31 fill crashed every read. Now the
    readers' own count runs first: a file it cannot count is declined in words and
    nothing lands — for a stack year and for a change_ map alike."""
    real = stacks._count_classes

    def crashing(arr, *a, **k):
        if (arr == I32_FILL).any():
            raise ValueError("'list' argument must have no negative elements")
        return real(arr, *a, **k)
    monkeypatch.setattr(stacks, "_count_classes", crashing)
    a = np.full((H, W), 1, "int32")
    a[:, 5:] = 2
    a[0, :] = I32_FILL
    out = _submit("landcover_bayi32_2014", env["serve"]("i32.tif", a, nodata=I32_FILL),
                  stack="landcover_bayi32", year=2014, stack_title="i32 (synthetic)",
                  legend=MANGROVE, declared={"dtype": "int32", "valid_min": 1, "valid_max": 2,
                                             "nodata": I32_FILL})
    msg = json.dumps(out)
    assert out["status"] == "declined", out
    assert "does not read as a class map" in msg and "no negative elements" in msg
    assert "landcover_bayi32_2014" not in tiffs.catalog() and "landcover_bayi32" not in stacks.rows()
    chg = _submit("change_bay_bad", env["serve"]("chg-bad.tif", _change_map(), nodata=I32_FILL),
                  legend=CHANGE_LEGEND, declared=CHANGE_DECL)
    assert chg["status"] == "declined" and "does not read as a class map" in json.dumps(chg)
    assert "change_bay_bad" not in tiffs.catalog()
    # with the readers fixed, the same int32 file lands and is counted
    monkeypatch.setattr(stacks, "_count_classes", real)
    ok = _submit("change_bay_ok", env["serve"]("chg-ok.tif", _change_map(), nodata=I32_FILL),
                 legend=CHANGE_LEGEND, declared=CHANGE_DECL)
    assert ok["status"] == "approved", ok


def test_an_all_fill_class_map_is_declined_with_the_readers_words(env):
    empty = np.full((H, W), I32_FILL, "int32")
    out = _submit("change_bay_empty", env["serve"]("empty.tif", empty, nodata=I32_FILL),
                  legend=CHANGE_LEGEND, declared=CHANGE_DECL)
    assert out["status"] == "declined", out
    assert "every cell read is outside the map" in json.dumps(out)
    assert "change_bay_empty" not in tiffs.catalog()


def test_a_change_maps_fields_ride_the_staged_and_the_landed_row(env, monkeypatch, log):
    """from_year/to_year/compares: collected by the form (the schema's namespace fields,
    never typed in staging), kept on the staged preview and on the landed row, and stated
    by every read of the layer."""
    for f in ("from_year", "to_year", "compares"):
        assert f in staging.RASTER_FIELDS["optional"] and f in staging._RASTER_KNOWN
    monkeypatch.setattr(get_settings(), "grp_auto_approve", False)
    url = env["serve"]("chg.tif", _change_map(), nodata=I32_FILL)
    out = _submit("change_bay_9620", url, legend=CHANGE_LEGEND, declared=CHANGE_DECL,
                  from_year="1996", to_year=2020, compares="landcover_two")
    assert out["status"] in ("pending", "staged", "pending_review"), out
    entry = staging.STAGED_RASTERS["change_bay_9620"]["entry"]
    assert (entry["from_year"], entry["to_year"], entry["compares"]) == (1996, 2020, "landcover_two")
    assert "UNREVIEWED PREVIEW" in entry["review"]
    contrib = env["tmp"] / "conf" / "tiffs.contrib.yml"
    assert not contrib.exists() or "change_bay_9620" not in contrib.read_text()   # staged only
    monkeypatch.setattr(get_settings(), "grp_auto_approve", True)
    out = _submit("change_bay_8514", url, legend=CHANGE_LEGEND, declared=CHANGE_DECL,
                  from_year=1996, to_year=2020, compares="landcover_two")
    assert out["status"] == "approved", out
    row = tiffs.catalog()["change_bay_8514"]
    assert (row["from_year"], row["to_year"], row["compares"]) == (1996, 2020, "landcover_two")
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=POLY,
                          stack="landcover_two", layers=["change_bay_8514"])
    assert p["status"] == "ok", p.get("note")
    text = next(c for c in p["citations"] if c.get("layer") == "change_bay_8514")["text"]
    log("OUTPUT", text[:300])
    assert "A change map from 1996 to 2020, comparing the maps of landcover_two" in text


# ------------------------------------------------------------ 2. one ranking rule
def test_the_default_stack_is_ranked_by_the_one_rule_in_stacks(env, monkeypatch):
    """land_cover._pick_stack reads stacks.rank: replacing the rule changes the pick."""
    sid, _row, _others, why = lc._pick_stack(shape(POLY), years=(2020,), name="Test bay")
    assert stacks.RANK_RULE in why
    seen = {}

    def reversed_rank(cands, facts):
        seen["called"] = [s for s, _ in cands]
        return list(reversed(stacks.__dict__["_orig_rank"](cands, facts)))
    monkeypatch.setitem(stacks.__dict__, "_orig_rank", stacks.rank)
    monkeypatch.setattr(stacks, "rank", reversed_rank)
    sid2, *_ = lc._pick_stack(shape(POLY), years=(2020,), name="Test bay")
    assert seen["called"] and sid2 != sid
    assert not hasattr(lc, "_cell_tiers") and not hasattr(lc, "CELL_TIE")


def test_a_change_layer_is_never_a_stack_nor_a_candidate(env):
    out = _submit("change_bay_8514", env["serve"]("chg.tif", _change_map(), nodata=I32_FILL),
                  legend=CHANGE_LEGEND, declared=CHANGE_DECL)
    assert out["status"] == "approved", out
    assert "change_bay_8514" not in stacks.rows()
    assert all(not k.startswith("change_") for k in lc.landcover_stacks())
    assert all(r.get("role") == "landcover" for r in lc.landcover_stacks().values())
    sid, _row, others, why = lc._pick_stack(shape(POLY), years=(2020,), name="Test bay")
    assert not sid.startswith("change_") and not any(s.startswith("change_") for s, _ in others)
    # a stack row of another role, hand-registered, is still never a land-cover candidate
    conf = env["tmp"] / "conf"
    cat = json.loads((conf / "tiffs.yml").read_text())
    cat["landcover_oddrole"] = {**cat["landcover_two"], "role": "change"}
    (conf / "tiffs.yml").write_text(json.dumps(cat))
    assert "landcover_oddrole" in stacks.rows() and "landcover_oddrole" not in lc.landcover_stacks()


# ------------------------------------------------------- 3. the map's own colours
_RUN_MAPCARD = """
const fs = require("fs"), vm = require("vm");
const el = new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => 0 : k === "then" ? undefined : el),
  apply: () => el, construct: () => el, set: () => true });
const ctx = { window: el, document: el, ResizeObserver: class { observe() {} }, console,
              setTimeout: () => 0, clearTimeout: () => 0, navigator: el, location: el };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), ctx);
const m = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
process.stdout.write(vm.runInContext("mapCard", ctx)(m));
"""


def _draw(tmp_path, panel_map):
    """The evidence view's own mapCard, run in node over a recorded map: the fills drawn."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed: the view's map is not drawn here")
    html = app_ui.template()
    (tmp_path / "app.js").write_text(html.split("<script>")[-1].split("</script>")[0])
    (tmp_path / "run.js").write_text(_RUN_MAPCARD)
    (tmp_path / "m.json").write_text(json.dumps(panel_map))
    r = subprocess.run([node, str(tmp_path / "run.js"), str(tmp_path / "app.js"),
                        str(tmp_path / "m.json")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:500]
    import re
    return re.findall(r'<rect x="[^"]*" y="[^"]*"\s+width="[^"]*" height="[^"]*"\s+fill="([^"]*)"',
                      r.stdout)


def test_a_worldcover_like_map_draws_three_classes_not_one(env, tmp_path, log):
    """WorldCover codes 10/80/95 (95 = Mangroves) were clipped to the digit 9 and drew in
    one colour. The cells are now positions in the legend that rides with them."""
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=POLY,
                          stack="landcover_wc")
    assert p["status"] == "ok", p.get("note")
    viz = store.load_pack(p["pack_id"])["viz"]
    g = viz["hazard_grid"]
    assert g["encoding"] == "legend"
    assert [e["code"] for e in g["legend"]] == [10, 80, 95]
    assert set(g["cells"]) == {"1", "2", "3"}               # three classes; nothing outside
    colors = {e["code"]: e["color"] for e in g["legend"]}
    assert len(set(colors.values())) == 3
    fills = _draw(tmp_path, publish._panel_map(store.load_pack(p["pack_id"])))
    log("OUTPUT", f"{len(fills)} cells, fills {sorted(set(fills))}")
    assert set(fills) == set(colors.values())
    # the bay's columns 0-2 are Mangroves (95) and 8-9 water (80): the left cell is 95's
    assert fills[0] == colors[95] and fills[g["w"] - 1] == colors[80]


def test_a_contributed_stack_coded_past_nine_draws_each_class(env, tmp_path):
    """A hub's map coded 10/20/100/200 (CATIE) through the gate: four classes, four colours,
    its own declared colours first; the fill is no class."""
    a = np.full((H, W), 200, "int32")
    a[:, :3] = 10
    a[:, 3:5] = 20
    a[:, 5:7] = 100
    a[0, :] = I32_FILL
    legend = {10: "Mangrove", 20: "Forest", 100: "Water", 200: "Other"}
    out = _submit("landcover_catie_2014", env["serve"]("catie-2014.tif", a, nodata=I32_FILL),
                  stack="landcover_catie", year=2014, stack_title="CATIE-like (synthetic)",
                  legend=legend, colors={10: "#0b6e4f"},
                  declared={"dtype": "int32", "valid_min": 10, "valid_max": 200, "nodata": I32_FILL})
    assert out["status"] == "approved", out
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2014", geometry=POLY,
                          stack="landcover_catie")
    assert p["status"] == "ok", p.get("note")
    g = store.load_pack(p["pack_id"])["viz"]["hazard_grid"]
    assert [e["code"] for e in g["legend"]] == [10, 20, 100, 200]
    assert set(g["cells"]) == {"0", "1", "2", "3", "4"}      # 0: the fill row
    fills = _draw(tmp_path, publish._panel_map(store.load_pack(p["pack_id"])))
    assert len(set(fills)) == 4 and len(fills) == g["w"] * g["h"] - g["w"]
    assert g["legend"][0]["color"] == "#0b6e4f" and "#0b6e4f" in fills


def test_a_grid_recorded_before_the_legend_encoding_still_draws(tmp_path):
    """Receipts recorded earlier carry the digit itself (severity grids still do)."""
    m = {"bounds": [0, 0, 1, 1], "legend": {"1": {"label": "Low", "color": "#aaaaaa"},
                                            "3": {"label": "High", "color": "#cc0000"}},
         "hazard_grid": {"w": 3, "h": 1, "cells": "013", "bounds": [0, 0, 1, 1]}}
    assert _draw(tmp_path, m) == ["#aaaaaa", "#cc0000"]


def test_more_classes_than_symbols_ride_as_positions(tmp_path):
    a = np.arange(W * H, dtype="int32").reshape(H, W)
    path = _write(tmp_path / "many.tif", a)
    legend = {i: {"label": f"c{i}", "color": f"#{i:06x}"} for i in range(W * H)}
    g = stacks.category_grid(path, [], legend)
    assert isinstance(g["cells"], list) and sorted(g["cells"]) == list(range(1, W * H + 1))
    assert len(set(_draw(tmp_path, {"bounds": g["bounds"], "legend": {}, "hazard_grid": g}))) == W * H


def test_colours_given_by_class_name_are_read_and_bad_ones_declined(env):
    """The form asks {class: '#rrggbb'}; a label key passed the gate and then every read of
    the stack failed on int('Mangrove')."""
    a = np.full((H, W), 20, "uint8")
    a[:, :4] = 10
    url = env["serve"]("named-2014.tif", a, nodata=255)
    base = dict(stack="landcover_named", year=2014, stack_title="Named colours (synthetic)",
                legend={10: "Mangrove", 20: "Other"},
                declared={"dtype": "uint8", "valid_min": 10, "valid_max": 20, "nodata": 255})
    bad = _submit("landcover_named_2014", url, **base, colors={"Mangrov": "#0b6e4f", 20: "green"})
    msg = " ".join(bad.get("problems") or bad.get("failures") or [])
    assert bad["status"] == "declined" and "['Mangrov'] name no class" in msg and "['green']" in msg
    ok = _submit("landcover_named_2014", url, **base, colors={"mangrove": "#0b6e4f"})
    assert ok["status"] == "approved", ok
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2014", geometry=POLY,
                          stack="landcover_named")
    assert p["status"] == "ok", p.get("note")
    g = store.load_pack(p["pack_id"])["viz"]["hazard_grid"]
    assert g["legend"][0] == {"code": 10, "label": "Mangrove", "color": "#0b6e4f"}


# ------------------------------------------------- 4. many callers, one file
def _together(n, fn):
    """Run fn(i) on n threads released at once; their results, or the first error."""
    gate, out, errs = threading.Barrier(n), [None] * n, []

    def run(i):
        gate.wait()
        try:
            out[i] = fn(i)
        except BaseException as exc:        # noqa: BLE001 — surfaced below
            errs.append(exc)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    return out


def _slow(monkeypatch, mod, name, delay=0.02):
    """Widen a read-change-write window: `mod.name` sleeps after it runs."""
    real = getattr(mod, name)

    def slow(*a, **k):
        r = real(*a, **k)
        time.sleep(delay)
        return r
    monkeypatch.setattr(mod, name, slow)


def _no_tmp(d):
    return [f for f in os.listdir(d) if f.endswith(".tmp")]


def test_two_vector_approvals_at_once_keep_both_layers(env, monkeypatch):
    _slow(monkeypatch, vectors, "landed")
    _together(8, lambda i: vectors.register(f"vec_{i}", {"local_path": f"vectors/v{i}.geojson"}))
    assert set(vectors.landed()) == {f"vec_{i}" for i in range(8)}
    _together(4, lambda i: vectors.unregister(f"vec_{i}"))
    assert set(vectors.landed()) == {f"vec_{i}" for i in range(4, 8)}
    assert _no_tmp(vectors._dir()) == []


def test_rasters_landing_at_once_keep_every_row_and_one_name_lands_once(env, monkeypatch):
    """The gate read tiffs.contrib.yml, changed it and wrote it back; two approvals at
    once each wrote the old file plus their own row, and the first row was lost."""
    _slow(monkeypatch, rasters, "declared_fields")
    dem = np.full((H, W), 5.0, "float32")
    decl = {"dtype": "float32", "valid_min": 0, "valid_max": 20, "nodata": -9999.0, "units": "m"}

    def add(layer, i):
        return rasters.add({"layer": layer, "title": "t", "description": "d", "source": "s",
                            "license": "CC0-1.0", "vintage": "2026-10", "declared": decl,
                            "file": _write(env["tmp"] / f"{layer}-{i}.tif", dem + i, nodata=-9999.0)})
    outs = _together(5, lambda i: add(f"measure_bay_m{i}", i))
    assert [o["status"] for o in outs] == ["landed"] * 5, outs
    cat = tiffs.catalog()
    assert all(f"measure_bay_m{i}" in cat for i in range(5))
    assert all(f"measure_bay_m{i}" in schema._doc().get("layers", {}) for i in range(5))
    # one name landed twice at once: one lands, the other is declined in words
    twice = _together(2, lambda i: add("measure_bay_twice", i))
    assert sorted(o["status"] for o in twice) == ["declined", "landed"], twice
    assert "already in the catalog" in json.dumps(twice)
    assert _no_tmp(get_settings().tiffs_dir) == []


def test_tables_and_feeds_land_whole_and_once(env, monkeypatch):
    _slow(monkeypatch, tables, "scope_fields")
    csv = env["tmp"] / "t.csv"
    csv.write_text("site,depth\nA,1\nB,2\n")
    m = {"file": str(csv), "title": "t", "description": "d", "source": "s",
         "validation": "single-agency", "license": "CC0-1.0", "vintage": "2026-10",
         "cadence": "static", "columns": {"site": "site", "depth": "depth"},
         "units": {"depth": "m"}}
    outs = _together(2, lambda i: tables.add({**m, "dataset": "same_table"}))
    assert sorted(o["status"] for o in outs) == ["declined", "landed"], outs
    outs = _together(4, lambda i: tables.add({**m, "dataset": f"table_{i}"}))
    assert [o["status"] for o in outs] == ["landed"] * 4
    assert _no_tmp(env["feeds"]) == []


def test_the_catalog_writers_go_through_one_lock_and_one_rename(env, monkeypatch):
    """Every machine-owned catalog file the gate rewrites: removal, amend, weights, feeds."""
    written = []
    real = catalog_io.write_text

    def spy(path, text):
        assert catalog_io.LOCK._is_owned(), f"{path} written outside the catalog lock"
        written.append(os.path.basename(path))
        return real(path, text)
    monkeypatch.setattr(catalog_io, "write_text", spy)
    dem = np.full((H, W), 5.0, "float32")
    decl = {"dtype": "float32", "valid_min": 0, "valid_max": 20, "nodata": -9999.0, "units": "m"}
    out = _submit("measure_bay_spy", env["serve"]("spy.tif", dem, nodata=-9999.0), declared=decl)
    assert out["status"] == "approved", out
    assert {"tiffs.contrib.yml", "raster_schema.contrib.yml"} <= set(written)
    written.clear()
    assert removal.remove_raster("measure_bay_spy")["status"] == "removed"
    assert {"tiffs.contrib.yml", "raster_schema.contrib.yml"} <= set(written)
    assert "measure_bay_spy" not in tiffs.catalog()


def test_bundle_rasters_are_built_once_and_written_whole(env, monkeypatch, tmp_path):
    """align_to, combine_l2 and a land-cover change map write into the area bundle: two
    callers used to serve each other a half-written grid. One builds, the other reads."""
    adir = tmp_path / "aoi"
    adir.mkdir()
    aoi = {"admin": str(adir / "admin.geojson")}
    hz = np.full((H, W), 3, "uint8")
    ref_path = _write(adir / "hazard_toy.tif", hz, nodata=0)
    v = np.full((H, W), 4, "uint8")
    v_path = _write(adir / "vuln_src.tif", v, nodata=0)
    with rasterio.open(ref_path) as r:
        ref = {"path": ref_path, "transform": r.transform, "crs": r.crs,
               "width": r.width, "height": r.height}
    monkeypatch.setattr(ingest, "hazard_clip", lambda a, layer, **k: v_path)
    monkeypatch.setattr(align.verify, "verify_raster",
                        lambda layer: type("Report", (), {"ok": True, "mismatches": []})())
    used = []
    real_atomic = ingest.atomic_path

    def spy(dest):
        used.append(os.path.basename(dest))
        return real_atomic(dest)
    monkeypatch.setattr(ingest, "atomic_path", spy)
    outs = _together(6, lambda i: align.align_to(ref, "vulnerability_toy", aoi, verify_first=False))
    assert len(set(outs)) == 1 and rasterio.open(outs[0]).read(1).max() == 4
    assert used.count(os.path.basename(outs[0])) == 6
    # combine_l2: six callers, one computation, one grid
    monkeypatch.setattr(align, "reference_grid", lambda a, h: ref)
    calls = []
    real_combine = combine._combine

    def counting(*a, **k):
        calls.append(1)
        time.sleep(0.05)
        return real_combine(*a, **k)
    monkeypatch.setattr(combine, "_combine", counting)
    grids = _together(6, lambda i: combine.combine_l2(aoi, "hazard_toy",
                                                      vuln_weights={"vulnerability_toy": 1.0}))
    assert len(set(grids)) == 1 and len(calls) == 1
    assert rasterio.open(grids[0]).read(1).max() >= 1
    # a land-cover change map
    a = np.full((H, W), 2, "uint8"); a[:, :5] = 1
    b = np.full((H, W), 2, "uint8"); b[:, :4] = 1
    pa, pb = _write(adir / "a.tif", a), _write(adir / "b.tif", b)
    out_path = str(adir / "landcover_change_1_1996_2020.tif")
    bay = box(LEFT, TOP - H * PX, LEFT + W * PX, TOP)
    _together(4, lambda i: stacks.change(pa, pb, bay, MANGROVE, [], focus_class=1, out_path=out_path))
    assert "landcover_change_1_1996_2020.tif" in used
    with rasterio.open(out_path) as ds:
        assert set(np.unique(ds.read(1)).tolist()) == {1, 2, 15}     # stable, loss, neither
    assert _no_tmp(adir) == []


# ------------------------------------------------- 5. provenance, carried through
FETCHED = {"url": "https://hub.test/big.tif", "clip": [LEFT, TOP - H * PX, LEFT + W * PX, TOP],
           "member": "dem/big.tif", "window": [0, 0, W, H], "of": [4000, 4000],
           "read": "range requests, only the window", "sha256": "ab" * 32, "bytes": 1234}


def _toy_pack(monkeypatch):
    """A contributed extension pack: the bay as its area, nothing else."""
    def gather(target, focus, trace, extras):
        return ([{"n": 1, "kind": "measurement", "retrieval": "computed-at-pack-time",
                  "source": "toy", "title": "reading", "text": "42 units."}], [],
                {"queries": None, "_area": {"name": "Toy bay", "how": "test polygon",
                                            "geometry": POLY}})
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy", "version": "v0", "target_keys": ("place",),
        "target_doc": {"place": "site"}, "gather": gather,
        "sections": lambda: ["## Reading"], "corpus": None,
        "default_focus": lambda t: t.get("place", "")})


def test_a_clips_provenance_rides_the_record_the_citation_and_the_receipt(env, monkeypatch, log):
    dem = np.full((H, W), 5.0, "float32")
    out = rasters.add({"layer": "measure_bay_clip", "title": "Clipped DEM", "description": "d",
                       "source": "s", "license": "CC0-1.0", "vintage": "2026-10",
                       "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 20,
                                    "nodata": -9999.0, "units": "m"},
                       "file": _write(env["tmp"] / "clip.tif", dem, nodata=-9999.0)},
                      prepared_before=["clipped at the source to the box asked"], fetched=FETCHED)
    assert out["status"] == "landed", out
    # a key the gate starts writing tomorrow rides too; a server path or a label never does
    conf = env["tmp"] / "conf"
    import yaml
    cat = yaml.safe_load((conf / "tiffs.contrib.yml").read_text())
    cat["measure_bay_clip"].update({"hub_ticket": "HUB-42", "contributor_label": "gis@hub.test"})
    (conf / "tiffs.contrib.yml").write_text(yaml.safe_dump(cat))
    _toy_pack(monkeypatch)
    for pack, kw in (("land-cover", {"year": "2020", "geometry": POLY, "stack": "landcover_two"}),
                     ("toy", {})):
        p = assemble.assemble(pack=pack, place="Test bay", layers=["measure_bay_clip"], **kw)
        assert p["status"] == "ok", (pack, p.get("note"))
        rec = p["layers"]["measure_bay_clip"]
        assert rec["fetched"] == FETCHED and rec["prepared"][0] == "clipped at the source to the box asked"
        assert rec["hub_ticket"] == "HUB-42"
        assert not {"local_path", "contributor_label", "license"} & set(rec)
        cit = next(c for c in p["citations"] if c.get("layer") == "measure_bay_clip")
        assert cit["fetched"] == FETCHED and cit["prepared"] == rec["prepared"]
        r = record.record(pack_id=p["pack_id"])
        src = next(x for x in store.load_receipt(r["receipt_id"])["sources"]
                   if x.get("layer") == "measure_bay_clip")
        log("OUTPUT", f"{pack}: {json.dumps({k: src[k] for k in ('prepared', 'fetched')})[:200]}")
        assert src["fetched"] == FETCHED and src["prepared"] == rec["prepared"]


def test_a_vectors_ingest_notes_ride_its_measured_record(env, monkeypatch):
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"NAME": "Stand A"},
         "geometry": mapping(box(LEFT + 2 * PX, TOP - 6 * PX, LEFT + 4 * PX, TOP - 2 * PX))}]}
    monkeypatch.setattr(fetch_policy, "fetch",
                        lambda url, max_bytes=None: (json.dumps(fc).encode(), "m.geojson"))
    tok = identity.bind(OWNER)
    try:
        out = staging.submit("vector", {"layer": "test_bay_stands", "url": "https://hub.test/m.geojson",
                                        "title": "Bay stands", "description": "d", "source": "s",
                                        "license": "CC0-1.0", "vintage": "2026",
                                        "name_field": "NAME"}, OWNER)
    finally:
        identity.unbind(tok)
    assert out["status"] == "approved", out
    entry = vectors.landed()["test_bay_stands"]
    vectors.register("test_bay_stands", {**entry, "prepared": ["reprojected from EPSG:32617"]})
    _toy_pack(monkeypatch)
    for pack, kw in (("land-cover", {"year": "2020", "geometry": POLY, "stack": "landcover_two"}),
                     ("toy", {})):
        p = assemble.assemble(pack=pack, place="Test bay", vectors=["test_bay_stands"], **kw)
        assert p["status"] == "ok", (pack, p.get("note"))
        assert store.load_pack(p["pack_id"])["vectors"]["test_bay_stands"]["prepared"] == [
            "reprojected from EPSG:32617"]
        cit = next(c for c in p["citations"] if c.get("layer") == "test_bay_stands")
        assert cit["prepared"] == ["reprojected from EPSG:32617"]
        src = next(x for x in record._sources(store.load_pack(p["pack_id"]))
                   if x.get("layer") == "test_bay_stands")
        assert src["prepared"] == ["reprojected from EPSG:32617"]


def test_the_view_says_where_a_clip_was_cut_from():
    html = app_ui.template()
    assert "fetchedHTML(s.fetched)" in html and "Cut from" in html


# ------------------------------------------- 6. scope and preview in every pack
NICOYA = [-85.2977, 9.9337, -84.7561, 10.2968]
TERRABA = (-83.9, 8.8, -83.2, 9.3)                 # Costa Rica's south, outside the Nicoya box
INSIDE = (-85.1, 10.0, -84.9, 10.2)


def test_the_scope_rule_is_one_for_every_pack():
    gulf = {"countries": ["Costa Rica"], "bbox": NICOYA}
    assert feeds.scope_miss(gulf, ["Costa Rica"], TERRABA)[1] == "this area lies outside that box"
    assert feeds.scope_miss(gulf, ["Costa Rica"], INSIDE) is None
    assert feeds.scope_miss(gulf, ["Ecuador"], INSIDE)[1] == "this area is in Ecuador"
    assert feeds.scope_miss({"bbox": NICOYA}, [], INSIDE) is None
    # a pack with no area: a box is placed through the countries, a box alone is said
    assert feeds.scope_miss(gulf, ["Costa Rica"], None) is None
    assert "holds no area to place that box" in feeds.scope_miss({"bbox": NICOYA}, ["Kenya"], None)[1]
    assert feeds.scope_miss({"countries": ["Kenya"]}, [], None) is None       # unknown rules nothing out
    assert feeds.scope_miss({}, ["Kenya"], None) is None
    assert lc._scope_miss(gulf, ["Costa Rica"], box(*TERRABA)) == feeds.scope_miss(gulf, ["Costa Rica"], TERRABA)


def _bound_feeds(monkeypatch, pack, rows):
    monkeypatch.setattr(registry, "FEEDS", {k: {"pack": pack, "status": "available", "title": k, **v}
                                            for k, v in rows.items()})
    monkeypatch.setattr(staging, "visible_staged_feeds_for_pack", lambda pack: {})
    monkeypatch.setattr(feeds, "query", lambda ds, q=None: {
        "status": "ok", "records": [{"v": 1}], "count": 1, "as_of": None, "passport": {}})


def test_risk_feeds_honour_a_declared_box(monkeypatch, log):
    _bound_feeds(monkeypatch, "risk", {"nicoya_obis": {"countries": ["Costa Rica"], "bbox": NICOYA},
                                       "cr_gauges": {"countries": ["Costa Rica"]},
                                       "global_quakes": {}})
    gaps = []
    out = risk._pack_feeds("Térraba", [], gaps, country="Costa Rica", bounds=TERRABA)
    log("OUTPUT", gaps[-1])
    assert sorted(d for d, _, _ in out) == ["cr_gauges", "global_quakes"]
    assert "nicoya_obis speaks to Costa Rica (lon -85.30 to -84.76" in gaps[-1]
    assert "this area lies outside that box" in gaps[-1]
    out = risk._pack_feeds("Nicoya", [], [], country="Costa Rica", bounds=INSIDE)
    assert sorted(d for d, _, _ in out) == ["cr_gauges", "global_quakes", "nicoya_obis"]


def test_food_security_sources_honour_a_declared_box(monkeypatch):
    _bound_feeds(monkeypatch, "food-security", {
        "trans_nzoia_plots": {"countries": ["Kenya"], "bbox": [34.6, 0.7, 35.4, 1.3]},
        "box_only": {"bbox": [34.6, 0.7, 35.4, 1.3]}, "global_table": {}})
    gaps = []
    out = fs._pack_bound_sources([], gaps, country="Kenya")
    assert sorted(c["title"] for c in out) == ["global_table", "trans_nzoia_plots"]
    assert "box_only speaks to (lon 34.60 to 35.40" in gaps[-1] and "holds no area" in gaps[-1]
    gaps = []
    out = fs._pack_bound_sources([], gaps, country="Zambia")
    assert [c["title"] for c in out] == ["global_table"]
    assert "trans_nzoia_plots speaks to Kenya" in gaps[-1] and "this area is in Zambia" in gaps[-1]


def test_the_risk_gatherer_passes_its_areas_country_and_box(world, monkeypatch):
    """And a staged document it cites says so in the citation's own text."""
    seen = {}
    monkeypatch.setattr(ingest, "_reverse_country", lambda lon, lat: "Cambodia")

    def feeds_spy(place, trace, gaps, hazard=None, country=None, bounds=None):
        seen["feeds"] = (country, bounds)
        return []

    def docs_spy(place, hz, focus, trace, gaps, country=None):
        seen["docs"] = country
        return None, [_hit("hub_memo", ["Cambodia"], **STAGED_DOC)]
    monkeypatch.setattr(risk, "_pack_feeds", feeds_spy)
    monkeypatch.setattr(risk, "_document_hits", docs_spy)
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY)
    assert p["status"] == "ok", p.get("note")
    assert seen["docs"] == "Cambodia" and seen["feeds"][0] == "Cambodia"
    assert tuple(round(v, 3) for v in seen["feeds"][1]) == (103.005, 13.005, 103.015, 13.015)
    doc = next(c for c in p["citations"] if c["kind"] == "document")
    assert "Review: UNREVIEWED PREVIEW — staged contribution c0ffee" in doc["text"]


class _Library:
    """A corpus of fixed passages, searched by the platform's own sift."""

    def __init__(self, hits):
        self.hits, self.calls = hits, []

    def documents(self):
        return [{"doc_id": h["doc_id"]} for h in self.hits]

    def raw_path(self, doc_id):
        return None

    def search(self, q, k=5, min_relevance=None, *, where=None, skipped=None, references=False,
               **filters):
        self.calls.append({"where": where, "temporal": filters.get("temporal")})
        got = [h for h in self.hits if not filters.get("temporal")
               or h["metadata"].get("temporal") == filters["temporal"]]
        return rag_filters.sift(got, where=where, skipped=skipped, references=references)[:k]


def _hit(doc, countries=None, temporal="forecast", score=0.9, **meta):
    return {"id": f"{doc}-0", "doc_id": doc, "score": score,
            "text": f"{doc} says the rains failed in the season.",
            "metadata": {"title": doc, "source": "s", "temporal": temporal,
                         **({"countries": countries} if countries else {}), **meta}}


def test_risk_documents_about_other_countries_are_passed_over_and_said(monkeypatch, log):
    lib = _Library([_hit("ecuador_floods", ["Ecuador"], score=0.95),
                    _hit("cr_floods", ["Costa Rica"]), _hit("global_method", score=0.8),
                    _hit("past_ecuador", ["Ecuador"], temporal="retrospective")])
    import app.rag.store as rag_store
    monkeypatch.setattr(rag_store, "Corpus", lambda name: lib)
    gaps = []
    _corpus, hits = risk._document_hits("Térraba", "hazard_flood", "", [], gaps, country="Costa Rica")
    log("OUTPUT", gaps[-1])
    assert [h["doc_id"] for h in hits] == ["cr_floods", "global_method"]
    assert all(c["where"] == ["Costa Rica"] for c in lib.calls)
    assert "documents about other countries than Costa Rica were passed over" in gaps[-1]
    assert "Ecuador: ecuador_floods" in gaps[-1] or "Ecuador: past_ecuador" in gaps[-1]


def _fs_offline(monkeypatch, lib):
    monkeypatch.setattr(fs, "Corpus", lambda name: lib)
    monkeypatch.setattr(fs, "_conditions_citation", lambda crop, country, trace: (None, "no conditions"))
    monkeypatch.setattr(fs, "_driver_citations", lambda trace: ([], []))
    monkeypatch.setattr(fs, "_pack_bound_sources", lambda trace, gaps, country=None: [])
    monkeypatch.setattr(fs.crop_calendar, "citation", lambda *a, **k: None)


def test_food_security_retrieval_keeps_to_the_country_and_says_when_the_library_does_not(monkeypatch):
    lib = _Library([_hit("kenya_outlook", ["Kenya"]), _hit("zambia_outlook", ["Zambia"], score=0.95),
                    _hit("kenya_past", ["Kenya"], temporal="retrospective")])
    _fs_offline(monkeypatch, lib)
    cits, gaps, _ = fs.gather_evidence({"crop": "maize", "country": "Kenya", "focus": ""}, [])
    assert [c["doc_id"] for c in cits if c["kind"] == "document"] == ["kenya_outlook", "kenya_past"]
    assert lib.calls and all(c["where"] == ["Kenya"] for c in lib.calls)
    assert not any("NO DOCUMENT IN THE LIBRARY" in g for g in gaps)
    cits, gaps, _ = fs.gather_evidence({"crop": "maize", "country": "Ethiopia", "focus": ""}, [])
    assert not [c for c in cits if c["kind"] == "document"]
    assert gaps[0].startswith("NO DOCUMENT IN THE LIBRARY IS ABOUT ETHIOPIA. Its closest matches "
                              "are about Kenya, Zambia and were NOT cited")


STAGED_DOC = {"staged_by": "user_owner", "contribution_id": "c0ffee"}       # staged before the marker


def test_a_staged_document_says_unreviewed_preview_in_every_packs_citation(monkeypatch):
    lib = _Library([_hit("hub_memo", ["Kenya"], **STAGED_DOC)])
    _fs_offline(monkeypatch, lib)
    cits, _gaps, _ = fs.gather_evidence({"crop": "maize", "country": "Kenya", "focus": ""}, [])
    doc = next(c for c in cits if c["kind"] == "document")
    assert "Review: UNREVIEWED PREVIEW — staged contribution c0ffee" in doc["text"]
    assert doc["review_status"].startswith(staging.PREVIEW)
    landc = lc._document_citation(1, _hit("hub_memo", **STAGED_DOC), None)
    assert "Review: UNREVIEWED PREVIEW — staged contribution c0ffee" in landc["text"]
    # an approved document with a review of its own says that, not a preview
    approved = lc._document_citation(1, _hit("ok", review="auto-approved — no human reviewed it"), None)
    assert approved["text"].endswith("Review: auto-approved — no human reviewed it.")
    # the receipt uses the gate's own words, whether the citation carried them or not
    src = record._sources({"citations": [{**doc}, {"n": 2, **STAGED_DOC, "kind": "document"}]})
    assert all(s["review_status"].startswith("UNREVIEWED PREVIEW — staged contribution c0ffee")
               for s in src)
