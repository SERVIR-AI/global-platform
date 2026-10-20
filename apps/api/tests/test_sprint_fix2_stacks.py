"""Stacks after the mangrove re-test (2026-10-19): a land-cover year stored as int32 with
nodata -2147483648 (or int8, -128) is counted like any other; a CHANGE map has its own
namespace (change_<name>), read as classes beside a stack and never a stack itself; the
default stack does not turn on a reprojection's hundredths of a percent; a legend giving
one label to two codes is declined; a float32 fill declared -3.4e38 is the file's fill.

Every fixture is synthetic and built here. Layers arrive through the gate a moment before
they are read, and are read by the land-cover pack and by a contributed extension pack."""

import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box, shape

from app.contrib import fetch_policy, identity, rasters, staging
from app.graph.geo import layer_values, schema, stacks, tiffs
from app.land_cover import synthesis as lc
from app.mcp import assemble, packs

from test_sprint_fix_landcover import H, LEFT, POLY, PX, TOP, W, _write, lc_env  # noqa: F401

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
MANGROVE = {1: "Mangrove", 2: "Other"}
CHANGE_LEGEND = {110: "Mangrove persistence", 120: "Mangrove gain", 210: "Mangrove loss",
                 220: "Non-mangrove both years"}
I32_FILL = -2147483648


@pytest.fixture
def env(lc_env, monkeypatch):
    """The four-stack bay of test_sprint_fix_landcover, auto-approving, with a fetcher
    that serves the files written here by URL, and the platform's own role table (what a
    role's values mean when read back) as a deployment configures it."""
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


def _years(fill_85, fill_14, dtype_85, dtype_14):
    """1985: columns 0-4 mangrove; 2014: columns 0-3 and 5 mangrove (column 4 lost, 5
    gained). Row 2 has no data in 1985, row 7 none in 2014."""
    a = np.full((H, W), 2, dtype=dtype_85)
    a[:, :5] = 1
    a[2, :] = fill_85
    b = np.full((H, W), 2, dtype=dtype_14)
    b[:, :4] = 1
    b[:, 5] = 1
    b[7, :] = fill_14
    return a, b


def _land_stack(env, sid, a, b, fill_85, fill_14):
    for y, arr, fill in ((1985, a, fill_85), (2014, b, fill_14)):
        out = _submit(f"{sid}_{y}", env["serve"](f"{sid}-{y}.tif", arr, nodata=fill), stack=sid,
                      year=y, stack_title=f"{sid} (synthetic)", legend=MANGROVE,
                      declared={"dtype": str(arr.dtype), "valid_min": 1, "valid_max": 2,
                                "nodata": fill})
        assert out["status"] == "approved", out


# ------------------------------------------------------ 1. negative nodata
def test_a_stack_stored_as_int8_and_int32_with_negative_fills_reads_like_uint8(env, log):
    """The re-test's crash: "'list' argument must have no negative elements". An int8 1985
    (nodata -128) and an int32 2014 (nodata -2147483648) give exactly the figures of the
    same maps stored as uint8 with nodata 255 — areas, change and transitions."""
    _land_stack(env, "landcover_bayneg", *_years(-128, I32_FILL, "int8", "int32"), -128, I32_FILL)
    _land_stack(env, "landcover_bayu8", *_years(255, 255, "uint8", "uint8"), 255, 255)
    assert tiffs.catalog()["landcover_bayneg"]["outside_values"] == [0, -128, I32_FILL]
    got = {}
    for sid in ("landcover_bayneg", "landcover_bayu8"):
        p = assemble.assemble(pack="land-cover", place="Test bay", year="1985", year_to="2014",
                              geometry=POLY, stack=sid, classes=["Mangrove"])
        assert p["status"] == "ok", p.get("note")
        got[sid] = p
    neg, u8 = got["landcover_bayneg"]["stats"], got["landcover_bayu8"]["stats"]
    log("OUTPUT", json.dumps(neg["change"])[:400])
    assert neg["change"] == u8["change"]
    mg = neg["change"]["by_class"]["Mangrove"]
    assert mg["loss_km2"] > 0 and mg["gain_km2"] > 0
    # the two rows with no data in one year are said, never counted as change
    assert neg["change"]["km2_one_year_only"] == pytest.approx(u8["change"]["km2_one_year_only"])
    assert neg["change"]["km2_one_year_only"] > 0
    texts = [c["text"] for c in got["landcover_bayneg"]["citations"] if c["kind"] == "class_area"]
    assert texts == [c["text"].replace("landcover_bayu8", "landcover_bayneg")
                     for c in got["landcover_bayu8"]["citations"] if c["kind"] == "class_area"]


def test_class_areas_and_change_take_any_integer_codes(tmp_path):
    """Change codes far above 255 beside an int32 fill: counted by class, never binned by
    their raw value; a code not in the legend is reported, not dropped."""
    a = np.full((H, W), 220, dtype="int32")
    a[:, :3] = 110
    a[:, 3] = 210
    a[:, 4] = 120
    a[0, :] = I32_FILL
    a[9, 9] = 99999                              # in no legend
    pa = _write(tmp_path / "chg.tif", a, nodata=I32_FILL)
    bay = box(LEFT, TOP - H * PX, LEFT + W * PX, TOP)
    st = stacks.class_areas(pa, bay, CHANGE_LEGEND, [I32_FILL])
    cell = stacks.row_pixel_areas_km2(from_origin(LEFT, TOP, PX, PX), H)
    rows = cell[1:].sum()                        # one cell per row, rows 1-9
    assert st["by_class"][110]["pixels"] == 27 and st["by_class"][110]["km2"] == pytest.approx(3 * rows)
    assert st["by_class"][210]["km2"] == pytest.approx(rows)
    assert st["km2_outside"] == pytest.approx(W * cell[0])
    assert st["values_not_in_legend"] == [99999]
    assert st["pixels_classified"] == 90
    # change between two int32 years, codes > 255 on both sides
    b = a.copy()
    b[:, 3] = 110                                # 210 -> 110 in column 3
    pb = _write(tmp_path / "chg2.tif", b, nodata=I32_FILL)
    ch = stacks.change(pa, pb, bay, CHANGE_LEGEND, [I32_FILL])
    assert ch["by_class"][210]["loss_to"] == {"Mangrove persistence": pytest.approx(rows)}
    assert ch["km2_changed"] == pytest.approx(rows)
    codes, idx = stacks.dense_codes(np.array([I32_FILL, 5, 2 ** 31 - 1, 5], dtype="int64"))
    assert codes.tolist() == [I32_FILL, 5, 2 ** 31 - 1] and idx.tolist() == [0, 1, 2, 1]


def test_the_readers_own_count_runs_on_a_file_before_approval(tmp_path):
    """stacks.read_check: what the gate can run on a staged class map — the readers' own
    count over the whole file, or what fails in words."""
    a = np.full((H, W), 1, dtype="int32")
    a[0, :] = I32_FILL
    assert stacks.read_check(_write(tmp_path / "ok.tif", a, nodata=I32_FILL), MANGROVE,
                             [0, I32_FILL]) is None
    empty = np.full((H, W), I32_FILL, dtype="int32")
    msg = stacks.read_check(_write(tmp_path / "empty.tif", empty, nodata=I32_FILL), MANGROVE,
                            [0, I32_FILL])
    assert "every cell read is outside the map" in msg and str(I32_FILL) in msg
    assert "could not be counted" in stacks.read_check(str(tmp_path / "missing.tif"), MANGROVE, [0])


# ------------------------------------------------------ 2. change maps
def _change_file(env, name="chg8514.tif"):
    """A producer change map: columns 0-2 persistence, 3 loss, 4 gain, the rest
    non-mangrove both years; the int32 fill over row 0 (outside the bay polygon)."""
    a = np.full((H, W), 220, dtype="int32")
    a[:, :3] = 110
    a[:, 3] = 210
    a[:, 4] = 120
    a[0, :] = I32_FILL
    return env["serve"](name, a, nodata=I32_FILL)


def cit_of(p, layer):
    return next(c for c in p["citations"] if c.get("layer") == layer)["text"]


def _toy_pack(monkeypatch, class_grid=None):
    """A contributed extension pack: the bay as its area and, optionally, a class map."""
    def gather(target, focus, trace, extras):
        stats = {"queries": None, "_area": {"name": "Toy bay", "how": "test polygon",
                                            "geometry": POLY}}
        if class_grid:
            stats["_class_grid"] = class_grid
        return ([{"n": 1, "kind": "measurement", "retrieval": "computed-at-pack-time",
                  "source": "toy", "title": "reading", "text": "42 units."}], [], stats)
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy", "version": "v0", "target_keys": ("place",),
        "target_doc": {"place": "site"}, "gather": gather,
        "sections": lambda: ["## Reading"], "corpus": None,
        "default_focus": lambda t: t.get("place", "")})


def test_a_change_map_lands_as_its_own_kind_and_reads_as_classes_beside_a_stack(env, monkeypatch, log):
    out = _submit("change_bay_8514", _change_file(env), legend=CHANGE_LEGEND,
                  outside_values=[I32_FILL],
                  declared={"dtype": "int32", "valid_min": 110, "valid_max": 220,
                            "nodata": I32_FILL, "units": "class"})
    assert out["status"] == "approved", out
    row = tiffs.catalog()["change_bay_8514"]
    assert "stack" not in row and "year" not in row and row.get("kind") != "stack"
    assert schema.schema_for("change_bay_8514")["role"] == "change"
    # never a land-cover stack, so never the default and never a candidate
    assert "change_bay_8514" not in lc.landcover_stacks()
    sid, _row, others, _why = lc._pick_stack(shape(POLY), years=(2020,))
    assert sid != "change_bay_8514" and "change_bay_8514" not in {s for s, _ in others}
    # read beside the real stack: km² per change class, and per class of the stack's map
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=POLY,
                          stack="landcover_two", layers=["change_bay_8514"])
    assert p["status"] == "ok", p.get("note")
    rec = p["layers"]["change_bay_8514"]
    s = rec["summary"]
    log("OUTPUT", json.dumps(s["classes"]))
    # change codes are names, never averaged — the platform's role table, even where the
    # configured schema states no roles
    assert rec["values"] == "classes" and rec["role"] == "change"
    assert rec["scale"] == "nominal" and "mean" not in s and ", mean " not in cit_of(p, "change_bay_8514")
    assert set(s["classes"]) == {"110", "120", "210", "220"}          # the fill is no class
    assert s["classes"]["110"]["label"] == "Mangrove persistence"
    assert s["classes"]["110"]["cells"] == 2 * 8 and s["classes"]["220"]["cells"] == 4 * 8
    cross = s["by_class_of"]
    assert cross["stack"] == "landcover_two" and cross["year"] == "2020"
    # landcover_two 2020 maps mangrove in columns 1, 2 and 6
    assert set(cross["classes"]["Mangrove"]["by_value"]) == {"Mangrove persistence",
                                                             "Non-mangrove both years"}
    assert "Mangrove loss" in cross["classes"]["Not mangrove"]["by_value"]
    cit = next(c for c in p["citations"] if c.get("layer") == "change_bay_8514")
    assert "Mangrove persistence" in cit["text"] and "Per class of" in cit["text"]
    # factor tables multiply the stack's classes, never a change class
    assert not any("persistence" in c["text"] for c in p["citations"] if c["kind"] == "class_factor")
    # and a contributed extension pack reads the same layer the same way, crossed with
    # its own class map (columns 0-4 mangrove)
    extent = np.full((H, W), 2, "uint8")
    extent[:, :5] = 1
    _toy_pack(monkeypatch, class_grid={"path": _write(env["tmp"] / "map.tif", extent),
                                       "legend": MANGROVE, "outside_values": [0],
                                       "label": "Toy extent map"})
    t = assemble.assemble(pack="toy", place="Toy bay", layers=["change_bay_8514"])
    assert t["status"] == "ok", t.get("note")
    tc = t["layers"]["change_bay_8514"]["summary"]
    assert tc["classes"] == s["classes"]
    assert set(tc["by_class_of"]["classes"]["Mangrove"]["by_value"]) == {
        "Mangrove persistence", "Mangrove loss", "Mangrove gain"}


def test_what_a_change_map_compares_rides_every_read(env, log):
    """from_year, to_year and compares — collected by the gate, kept on the row, stated in
    every citation of the layer. The row here is written the way the gate lands one."""
    _change_file(env)
    conf = env["tmp"] / "conf"
    cat = json.loads((conf / "tiffs.yml").read_text())
    cat["change_bay_9620"] = {"local_path": "tiffs/change_bay_9620.tif", "title": "Bay change",
                              "legend": CHANGE_LEGEND, "outside_values": [I32_FILL],
                              "source": "test fixture", "license": "CC0-1.0", "vintage": "2026-10",
                              **rasters.declared_fields({"layer": "change_bay_9620",
                                                         "from_year": "1996", "to_year": 2020,
                                                         "compares": "landcover_two"})}
    (conf / "tiffs.yml").write_text(json.dumps(cat))
    (env["cache"] / "tiffs").mkdir(parents=True, exist_ok=True)
    (env["cache"] / "tiffs" / "change_bay_9620.tif").write_bytes(
        (env["tmp"] / "chg8514.tif").read_bytes())
    assert cat["change_bay_9620"]["from_year"] == 1996 and cat["change_bay_9620"]["to_year"] == 2020
    recs, _gaps = layer_values.for_area(shape(POLY), [{"layer": "change_bay_9620", "why": "asked"}],
                                        "Test bay")
    rec = recs["change_bay_9620"]
    assert (rec["from_year"], rec["to_year"], rec["compares"]) == (1996, 2020, "landcover_two")
    assert rec["role"] == "change" and "mean" not in rec["summary"]     # no contract: its prefix
    text = layer_values.citation_text("change_bay_9620", rec, "Test bay")
    log("OUTPUT", text[:600])
    assert "A change map from 1996 to 2020, comparing the maps of landcover_two" in text
    assert "not of land cover in either year" in text


def _fails(m):
    """The gate's failures for a manifest checked before any file is fetched."""
    return [f for f in rasters.validate_manifest({**m, "file": "-"}) if not f.startswith("file ")]


def test_the_gate_says_what_a_change_map_may_declare(env):
    base = {"layer": "change_bay_x", "title": "t", "description": "d", "source": "s",
            "license": "CC-BY-4.0", "vintage": "2026-10", "legend": CHANGE_LEGEND,
            "declared": {"dtype": "int32", "valid_min": 110, "valid_max": 220}}
    assert _fails({**base, "from_year": 1996, "to_year": 2020, "compares": "landcover_two"}) == []
    assert _fails(base) == []                                         # every field is optional
    as_year = " ".join(_fails({**base, "stack": "landcover_two", "year": 2020}))
    assert "stack and year place one year of a land-cover stack (landcover_<name>)" in as_year
    assert "a change_* layer is not one" in as_year and "from_year, to_year, compares" in as_year
    half = " ".join(_fails({**base, "from_year": 1996}))
    assert "from_year and to_year go together" in half
    back = " ".join(_fails({**base, "from_year": 2020, "to_year": 1996}))
    assert "from_year 2020 must come before to_year 1996" in back
    word = " ".join(_fails({**base, "from_year": "mid-80s", "to_year": 2014}))
    assert "must be years" in word and "'mid-80s'" in word
    nope = " ".join(_fails({**base, "compares": "landcover_nope"}))
    assert "'landcover_nope', which is not a registered land-cover stack" in nope
    assert "landcover_two" in nope
    off = " ".join(_fails({**base, "from_year": 1985, "to_year": 2020, "compares": "landcover_two"}))
    assert "landcover_two has no 1985 map (its years: 1996, 2020)" in off
    # another namespace's field is refused, never dropped; a year of nothing is said
    measure = _fails({"layer": "measure_bay_dem", "title": "t", "description": "d", "source": "s",
                      "license": "CC0-1.0", "vintage": "2026-10", "from_year": 1996, "year": 2020,
                      "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 9, "units": "m"}})
    assert any("from_year is a field of a change_<name> layer" in f for f in measure)
    assert any("the year a layer describes goes in its title or description" in f for f in measure)
    # the form says it, derived from the schema
    assert "change_<name> (a CHANGE map between two years" in rasters.namespace_text()
    assert "change_<name>" in staging.RASTER_FIELDS["required"]["layer"]
    assert set(layer_values.namespace_fields()) == {"from_year", "to_year", "compares"}


# ------------------------------------------------------ 3. default stack ranking
def test_cells_within_one_percent_are_one_resolution_then_more_years_win():
    """The CATIE series' cell is 0.000272930°; a change map of it reprojected to 0.00027287°
    (0.02% finer) won the default. Within 1% the series with more years wins; a cell 5%
    finer still wins; a one-class map is still last."""
    rows = {"landcover_series": {"years": [1956, 1985, 2014]},
            "landcover_chg": {"years": [2014]},
            "landcover_fine": {"years": [2014]},
            "landcover_one": {"years": [1956, 1985, 2014, 2020]},
            "landcover_nocell": {"years": [2014, 2015, 2016, 2017]}}
    facts = {"landcover_chg": (4, 1.0, 0.00027287), "landcover_series": (2, 1.0, 0.000272930),
             "landcover_one": (1, 1.0, 0.0001), "landcover_nocell": (3, 1.0, None)}
    order = [s for s, _ in stacks.rank([(s, rows[s]) for s in facts], facts)]
    assert order == ["landcover_series", "landcover_chg", "landcover_nocell", "landcover_one"]
    facts["landcover_fine"] = (2, 1.0, 0.000259)                       # 5% finer
    order = [s for s, _ in stacks.rank([(s, rows[s]) for s in facts], facts)]
    assert order[0] == "landcover_fine"
    # equal on everything: the earlier registered stays first
    twin = {"landcover_a": (2, 1.0, 0.001), "landcover_b": (2, 1.0, 0.001004)}
    rows2 = {"landcover_a": {"years": [2020]}, "landcover_b": {"years": [2021]}}
    assert [s for s, _ in stacks.rank([(s, rows2[s]) for s in ("landcover_b", "landcover_a")], twin)] \
        == ["landcover_b", "landcover_a"]
    assert "within 1% counts as equal" in stacks.RANK_RULE and "most years" in stacks.RANK_RULE


# ------------------------------------------------------ 4. one label, one code
def test_a_legend_giving_one_label_to_two_codes_is_declined_before_fetching(env, monkeypatch, log):
    def no_fetch(url, max_bytes=None):
        raise AssertionError("the gate fetched a file it should have declined")
    monkeypatch.setattr(fetch_policy, "fetch", no_fetch)
    out = _submit("landcover_bayall_1956", "https://hub.test/all.tif", stack="landcover_bayall",
                  year=1956, legend={1: "Mangrove", 2: "Non-mangrove", 10: "Mangrove",
                                     20: "Non-mangrove", 100: "Mangrove", 200: "non-mangrove "},
                  declared={"dtype": "uint8", "valid_min": 1, "valid_max": 200, "nodata": 255})
    log("OUTPUT", out["problems"])
    assert out["status"] == "declined"
    msg = " ".join(out["problems"])
    assert ("labels must be unique: codes 1, 10 and 100 are all 'Mangrove'; codes 2, 20 and 200 "
            "are all 'Non-mangrove' — recode the file so one code means one class") in msg
    chg = _fails({"layer": "change_bay_dup", "title": "t", "description": "d", "source": "s",
                  "license": "CC0-1.0", "vintage": "2026-10", "legend": {1: "Mangrove", 10: "Mangrove"},
                  "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 10}})
    assert chg == ["labels must be unique: codes 1 and 10 are both 'Mangrove' — recode the file so "
                   "one code means one class — or resubmit the same link with recode: {10: 1} and "
                   "legend {1: Mangrove} — the platform rewrites the cells at ingest, so one code "
                   "means one class"]


def test_a_stack_that_landed_with_repeated_labels_declines_change_in_words(env, log):
    """Landed before the gate refused it: each year's areas still read; change, which
    compares codes, declines instead of reporting every cell as changed."""
    conf = env["tmp"] / "conf"
    cat = json.loads((conf / "tiffs.yml").read_text())
    a = np.full((H, W), 2, "uint8")
    a[:, :5] = 1
    b = np.full((H, W), 20, "uint8")
    b[:, :5] = 10
    data = env["tmp"] / "data"
    _write(data / "dup-1956.tif", a)
    _write(data / "dup-2014.tif", b)
    cat["landcover_dup"] = {**cat["landcover_two"], "title": "Repeated labels (synthetic)",
                            "legend": {1: "Mangrove", 2: "Other", 10: "Mangrove", 20: "Other"},
                            "outside_values": [], "years": [1956, 2014],
                            "layer_pattern": "landcover_dup_{year}", "file_pattern": "dup-{year}.tif"}
    (conf / "tiffs.yml").write_text(json.dumps(cat))
    sch = json.loads((conf / "raster_schema.yml").read_text())
    sch["stacks"]["landcover_dup"] = sch["stacks"]["landcover_two"]
    (conf / "raster_schema.yml").write_text(json.dumps(sch))
    one = assemble.assemble(pack="land-cover", place="Test bay", year="2014", geometry=POLY,
                            stack="landcover_dup")
    assert one["status"] == "ok", one.get("note")
    p = assemble.assemble(pack="land-cover", place="Test bay", year="1956", year_to="2014",
                          geometry=POLY, stack="landcover_dup")
    log("OUTPUT", p.get("note"))
    assert p["status"] == "declined"
    assert "codes 1, 10 are all 'Mangrove'" in p["note"] and "change compares codes" in p["note"]
    assert "ask without year_to" in p["note"]


# ------------------------------------------------------ 5. float32 nodata
def test_a_float32_fill_declared_as_minus_3_4e38_is_the_files_fill(env):
    assert schema.same_nodata(-3.4e38, -3.3999999521443642e+38)
    assert schema.same_nodata(-3.4028235e38, -3.4028234663852886e+38)
    assert schema.same_nodata(-9999, -9999.00001)
    assert not schema.same_nodata(-9999, -9998)
    assert not schema.same_nodata(0.1, 0.1000001, tol=0.0)           # float32 ulp at 0.1 is 7e-9
    assert schema.same_nodata(float("nan"), float("nan"))
    assert schema.same_nodata(None, None) and not schema.same_nodata(None, 0)
    assert schema.same_nodata("-3.4e38", -3.3999999521443642e+38)
    assert not schema.same_nodata(-1e300, -1e301)                     # beyond float32: not equal
    # a reader holds the declared -3.4e38 and the file's tag as one no-value, and masks it
    dem = np.full((H, W), 5.0, "float32")
    dem[:, 7:] = np.float32(-3.4e38)
    for name, tag in (("tagged", -3.3999999521443642e+38), ("untagged", None)):
        conf = env["tmp"] / "conf"
        cat = json.loads((conf / "tiffs.yml").read_text())
        cat[f"measure_bay_{name}"] = {"local_path": f"tiffs/measure_bay_{name}.tif", "title": name,
                                      "source": "test fixture", "license": "CC0-1.0",
                                      "vintage": "2026-10"}
        (conf / "tiffs.yml").write_text(json.dumps(cat))
        sch = json.loads((conf / "raster_schema.yml").read_text())
        sch["layers"][f"measure_bay_{name}"] = {"role": "measure", "dtype": "float32", "valid_min": 0,
                                                "valid_max": 10, "nodata": -3.4e38, "units": "m"}
        (conf / "raster_schema.yml").write_text(json.dumps(sch))
        (env["cache"] / "tiffs").mkdir(parents=True, exist_ok=True)
        _write(env["cache"] / "tiffs" / f"measure_bay_{name}.tif", dem, nodata=tag)
        rec = layer_values.read(f"measure_bay_{name}", shape(POLY))
        s = rec["summary"]
        assert s["cells_with_value"] == 6 * 8 and s["max"] == 5.0, (name, s)
        assert len(rec["nodata"]["values"]) == 1, rec["nodata"]
