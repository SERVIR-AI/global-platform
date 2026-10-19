"""Round-2 hand-offs across files (mangrove sprint, 2026-10): the gate runs the readers' own
count before approving a class map and takes a float32 fill as declared; a change map's
from_year/to_year/compares ride the staged and landed rows; one stack ranking rule.

Every fixture is synthetic and built here, on the four-stack bay of
test_sprint_fix_landcover; layers arrive through the gate a moment before they are read."""

import json

import numpy as np
import pytest
from shapely.geometry import shape

from app.config import get_settings
from app.contrib import fetch_policy, identity, staging
from app.graph.geo import schema, stacks, tiffs
from app.land_cover import synthesis as lc
from app.mcp import assemble

from test_sprint_fix_landcover import H, POLY, PX, W, _write, lc_env  # noqa: F401

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
