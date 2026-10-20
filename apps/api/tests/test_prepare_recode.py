"""Preparation on the server, and its first op: recode.

The sprint case: a mangrove series delivered with other codes each year — 1956 as 1/2
(uint8, nodata 3), 1985 as 10/20 (int8, nodata -128), 2014 as 100/200 (uint8, nodata
255), every year on one 30 m UTM grid. A stack has one legend, so 1985 and 2014 were
recoded by hand before they could join. Here each year goes in from its own link: the
decline names the exact recode, the contributor's agent fills the field, the server
rewrites the cells, and the stored files match the hand-recoded ones cell for cell —
with every step (fetch, recode, sha256) on the staged entry and the landed row. A
preview says what would land without staging it; a clip still reads only the window at
the source. Beyond the asking pack: a freshly contributed vulnerability map coded 10-50
is recoded to 1-5 and read by the risk pack and by a contributed extension pack; a
vector op (a stand-in registered for the test) goes through the same seam.

Everything is synthetic and local: a loopback HTTP server in this file serves the files
(Range requests too), and no test touches the network."""

import hashlib
import io
import json
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
import rasterio
import yaml
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import transform_bounds

from app.config import get_settings
from app.contrib import fetch_policy, identity, prepare, rasters, staging
from app.contrib.prepare import recode
from app.graph.geo import ingest, schema, tiffs, vectors
from app.mcp import assemble, packs, registry, store
from app.risk import synthesis as risk

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
LEGEND = {1: "Mangrove", 2: "Non-mangrove"}
UTM = "EPSG:32616"
X0, Y0, PX = 686580.0, 1138890.0, 30.0       # the CATIE grid's corner, 30 m cells
W, H = 80, 60
B = (103.0, 13.0, 103.02, 13.02)              # the risk world: 20 x 20 cells of 0.001 deg
RISK_POLY = {"type": "Polygon", "coordinates": [[[103.005, 13.005], [103.015, 13.005],
             [103.015, 13.015], [103.005, 13.015], [103.005, 13.005]]]}


# --------------------------------------------------------------------------- a host
class _Host:
    def __init__(self):
        self.files, self.log, self.port, self.whole = {}, [], None, set()

    def put(self, name, data, ranges=True):
        self.files["/" + name] = data
        if not ranges:
            self.whole.add("/" + name)
        return f"http://127.0.0.1:{self.port}/{name}"

    def asked(self, name):
        return [rng for path, rng in self.log if path == "/" + name]


def _handler(host):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body):
            path = self.path.split("?")[0]
            data = host.files.get(path)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            rng = None if path in host.whole else self.headers.get("Range")
            host.log.append((path, rng))
            if rng:
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                a = int(m.group(1))
                b = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                part, code = data[a:b + 1], 206
            else:
                part, code = data, 200
            self.send_response(code)
            if rng:
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{path[1:]}"')
            self.send_header("Content-Length", str(len(part)))
            self.end_headers()
            if body:
                try:
                    self.wfile.write(part)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_GET(self):
            self._send(True)

        def do_HEAD(self):
            self._send(False)
    return Handler


@pytest.fixture
def host():
    h = _Host()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(h))
    h.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield h
    srv.shutdown()
    srv.server_close()


# --------------------------------------------------------------------------- a world
def _gtiff(arr, dtype, nodata=None, crs="EPSG:4326", transform=None, bounds=B, driver="GTiff"):
    h, w = arr.shape
    with MemoryFile() as mf:
        with mf.open(driver=driver, width=w, height=h, count=1, dtype=dtype, crs=crs,
                     transform=transform or from_bounds(*bounds, w, h), nodata=nodata) as d:
            d.write(arr.astype(dtype), 1)
        return mf.read()


def _write(path, arr, dtype, nodata=None, bounds=B):
    with open(path, "wb") as f:
        f.write(_gtiff(arr, dtype, nodata, bounds=bounds))
    return str(path)


@pytest.fixture
def env(tmp_path, monkeypatch, host):
    """A hermetic platform: an empty land-cover catalog, the risk pack's toy world (a
    flood hazard and a 1-5 vulnerability layer near 103 E, 13 N), auto-approve, the real
    fetch policy with the loopback host allowed, and a hermetic store."""
    s = get_settings()
    cache = tmp_path / "cache"
    tdir = cache / "tiffs"
    tdir.mkdir(parents=True)
    haz = np.zeros((20, 20))
    haz[:, :10] = 3
    _write(tdir / "hazard_toyflood.tif", haz, "int16")
    vul = np.ones((20, 20))
    vul[5:15, 5:15] = 4
    _write(tdir / "vulnerability_toy.tif", vul, "uint8")
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "tiffs.yml").write_text(yaml.safe_dump({
        "hazard_toyflood": {"local_path": "tiffs/hazard_toyflood.tif", "title": "Toy flood",
                            "legend": {1: "Very Low", 3: "Moderate", 5: "Very High"}},
        "vulnerability_toy": {"local_path": "tiffs/vulnerability_toy.tif", "internal": True,
                              "title": "Toy vulnerability", "source": "test fixture",
                              "license": "CC0-1.0", "vintage": "2026-10"}}))
    (conf / "raster_schema.yml").write_text(yaml.safe_dump({
        "defaults": {"crs": "EPSG:4326", "float_tol": 0.0001},
        "roles": schema._shipped()["roles"],
        "layers": {
            "hazard_toyflood": {"role": "hazard", "dtype": "int16", "valid_min": 0,
                                "valid_max": 5, "units": "class"},
            "vulnerability_toy": {"role": "vulnerability_reclass", "dtype": "uint8",
                                  "valid_min": 0, "valid_max": 5, "units": "class",
                                  "nodata": None}}}))
    (conf / "risk_l2.yml").write_text(yaml.safe_dump(
        {"class_max": 5, "weights": {"toyflood": {"vulnerability_toy": 1.0}}}))
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    for k, v in {"tiffs_config_path": conf / "tiffs.yml",
                 "raster_schema_path": conf / "raster_schema.yml",
                 "risk_l2_config_path": conf / "risk_l2.yml",
                 "tiffs_contrib_path": conf / "tiffs.contrib.yml",
                 "raster_schema_contrib_path": conf / "raster_schema.contrib.yml",
                 "risk_l2_contrib_path": conf / "risk_l2.contrib.yml",
                 "feeds_conf_dir": feeds, "cache_dir": cache, "tiffs_dir": tdir,
                 "grp_mattermost_webhook": "", "grp_auto_approve": True,
                 "grp_contrib_allow_private_hosts": True, "grp_contrib_hourly_cap": 0}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(ingest, "_fetch_layer", lambda layer, bbox, boundary: [])
    monkeypatch.setattr(risk, "_pack_feeds", lambda *a, **k: [])
    monkeypatch.setattr(risk, "_document_hits", lambda *a, **k: (None, []))
    for g in ("STAGED_RASTERS", "STAGED_WEIGHTS", "STAGED_FEEDS", "STAGED_VECTORS",
              "STAGED_AMENDS"):
        monkeypatch.setattr(staging, g, {}, raising=False)
    monkeypatch.setattr(staging, "_STAGED_LOADED", True)
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    registry.reload_declarative_feeds()
    yield {"tmp": tmp_path, "cache": cache, "host": host}
    monkeypatch.undo()
    registry.reload_declarative_feeds()


def _submit(kind, m):
    tok = identity.bind(OWNER)
    try:
        return staging.submit(kind, m, OWNER)
    finally:
        identity.unbind(tok)


def _year(layer_year, url, **over):
    stack, year = layer_year.rsplit("_", 1)
    return {"layer": layer_year, "stack": stack, "year": int(year), "url": url,
            "title": f"Gulf mangrove extent {year} (synthetic)",
            "description": "mangrove / non-mangrove, 30 m, the shape of the CATIE maps",
            "source": "test fixture shaped like the CATIE report layers", "license": "unstated",
            "vintage": "2014-07", "countries": ["Costa Rica"],
            "stack_title": "Synthetic gulf mangrove 1956-1985-2014", **over}


def _no_leftovers(env):
    prep = env["cache"] / "prep"
    strays = list((env["cache"] / "tiffs").glob("staged-tmp-*"))
    return (not prep.exists() or not list(prep.iterdir())) and not strays


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- the CATIE shape
def _catie():
    """The three years as delivered (codes per year as CATIE coded them) and the
    hand-recoded 1985 and 2014 (1 = mangrove, 2 = other, uint8, nodata 3)."""
    y56 = np.full((H, W), 2, "uint8")
    y56[:, :30] = 1
    y85 = np.full((H, W), 20, "int8")
    y85[:, :28] = 10
    y85[0, :] = -128                                # a row with no data
    y14 = np.full((H, W), 200, "uint8")
    y14[:, :32] = 100
    y14[-1, :] = 255
    hand85 = np.where(y85 == 10, 1, np.where(y85 == 20, 2, 3)).astype("uint8")
    hand14 = np.where(y14 == 100, 1, np.where(y14 == 200, 2, 3)).astype("uint8")
    return y56, y85, y14, hand85, hand14


def _utm(arr, dtype, nodata, driver="GTiff"):
    return _gtiff(arr, dtype, nodata, crs=UTM, transform=from_origin(X0, Y0, PX, PX),
                  driver=driver)


def _gulf_box():
    w, s, e, n = transform_bounds(UTM, "EPSG:4326", X0, Y0 - H * PX, X0 + W * PX, Y0)
    return [w - 0.01, s - 0.01, e + 0.01, n + 0.01]


def _poly(box):
    w, s, e, n = box
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


UINT8_3 = {"dtype": "uint8", "valid_min": 1, "valid_max": 2, "nodata": 3, "units": "class"}


def _land_1956(env, sid):
    y56 = _catie()[0]
    url = env["host"].put(f"{sid}_1956.img", _utm(y56, "uint8", 3, driver="HFA"))
    out = _submit("raster", _year(f"{sid}_1956", url, legend=LEGEND, outside_values=[3],
                                  declared=UINT8_3))
    assert out["status"] == "approved", out
    return out


def _mangrove_km2(sid, year):
    p = assemble.assemble(pack="land-cover", place="Gulf (synthetic)", year=str(year),
                          geometry=_poly(_gulf_box()), stack=sid, classes=["Mangrove"])
    assert p["status"] == "ok", p.get("note")
    return p["stats"]["classes_km2"]["Mangrove"]


# --------------------------------------------------------------------------- 1. the registry
def test_ops_are_derived_from_the_package_and_their_fields_reach_the_form():
    """The registry is the package's modules; the form, the known fields and the
    contribute_submit description all derive from it — no list of ops anywhere."""
    names = [op.NAME for op in prepare.ops()]
    assert "recode" in names
    for op in prepare.ops():
        assert op.STEP in prepare.ORDER and op.KINDS and op.FIELDS
        assert callable(op.validate) and callable(op.run) and callable(op.suggest)
    assert [op.NAME for op in prepare.ops("raster")] == [
        op.NAME for op in sorted(prepare.ops("raster"), key=lambda o: (prepare.ORDER[o.STEP], o.NAME))]
    text = staging.RASTER_FIELDS["optional"]["recode"]
    assert text == recode.FIELDS["recode"] and "AS IT LANDS" in text and "ask the contributor" in text
    assert "recode" in staging._RASTER_KNOWN and "recode" not in staging.VECTOR_FIELDS["optional"]
    assert "preview" in staging.RASTER_FIELDS["optional"] and "preview" in staging.VECTOR_FIELDS["optional"]
    desc = staging.describe_submit()
    assert "recode (for a CLASS map" in desc and "preview (true to see what WOULD land" in desc


def test_a_preparation_field_says_what_it_is_for_on_the_wrong_kind_or_namespace(env):
    """recode on a vector, or on a measure layer, is declined in words before any fetch."""
    url = env["host"].put("x.geojson", b"{}")
    out = _submit("vector", {"layer": "toy_points", "url": url, "title": "t", "description": "d",
                             "source": "s", "license": "CC0-1.0", "vintage": "2026-10",
                             "recode": {10: 1}})
    assert out["status"] == "declined"
    assert ("recode prepares a raster contribution (recode); it does not apply to a vector — "
            "drop recode") in out["problems"]
    assert not any("unknown fields" in p for p in out["problems"])
    out = _submit("raster", {"layer": "measure_toy_dem", "url": url, "title": "t",
                             "description": "d", "source": "s", "license": "CC0-1.0",
                             "vintage": "2026-10", "recode": {10: 1},
                             "declared": {"dtype": "int16", "valid_min": 0, "valid_max": 9,
                                          "units": "m"}})
    assert "recode rewrites class codes; a measure_* layer holds amount, not classes — drop recode" \
        in out["problems"]
    assert env["host"].log == []


# --------------------------------------------------------------------------- 2. the real case
def test_the_1985_and_2014_years_join_the_1956_stack_from_their_own_links(env, monkeypatch, log):
    """The decline proposes the exact recode before fetching (the platform's own
    preparation off, GRP_PREP_AUTO=0 — on, it applies the recode itself: test_prepare_auto);
    the manifest the agent builds from it lands; the stored files equal the hand-recoded
    ones cell for cell, the pack's mangrove km² are the hand-recoded stack's, and every
    step is on the record."""
    monkeypatch.setattr(get_settings(), "grp_prep_auto", False)
    y56, y85, y14, hand85, hand14 = _catie()
    host = env["host"]
    _land_1956(env, "landcover_gulf")
    raw85 = _utm(y85, "int8", -128, driver="HFA")             # an ERDAS .img, as delivered
    url85 = host.put("MANGLE_1985_RCLSS.IMG", raw85)
    delivered = _year("landcover_gulf_1985", url85, legend={10: "Mangrove", 20: "Non-mangrove"},
                      outside_values=[-128], declared={"dtype": "int8", "valid_min": 10,
                                                       "valid_max": 20, "nodata": -128})
    out = _submit("raster", delivered)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and host.asked("MANGLE_1985_RCLSS.IMG") == []
    said = out["problems"][0]
    assert ("resubmit the same link with recode: {10: 1, 20: 2}, legend: {1: Mangrove, 2: "
            "Non-mangrove} and declared valid_min 1, valid_max 2") in said
    assert "nothing to do by hand" in said

    # the agent fills the proposed fields — and only the person's own choices stay theirs
    fixed = {**delivered, "recode": {"10": 1, "20": 2}, "legend": LEGEND, "outside_values": [3],
             "declared": UINT8_3}
    out = _submit("raster", fixed)
    log("OUTPUT", out["preview"].get("note"))
    assert out["status"] == "approved", out
    assert "Recoded at ingest 10 -> 1 (1,652 cells), 20 -> 2 (3,068 cells); written as uint8, " \
           "nodata 3 (the file was int8, nodata -128, 80 cells)" in out["preview"]["note"]
    raw14 = _utm(y14, "uint8", 255, driver="HFA")
    url14 = host.put("MANGLE_2014_RCLSS.IMG", raw14)
    out14 = _submit("raster", _year("landcover_gulf_2014", url14, legend=LEGEND,
                                    outside_values=[3], declared=UINT8_3,
                                    recode={100: 1, 200: 2}))
    assert out14["status"] == "approved", out14
    cat = tiffs.catalog()
    assert cat["landcover_gulf"]["years"] == [1956, 1985, 2014]

    # the hand-recoded twins, contributed as they were on the sprint (no preparation)
    _land_1956(env, "landcover_gulfhand")
    for y, arr in ((1985, hand85), (2014, hand14)):
        url = host.put(f"hand_{y}.tif", _utm(arr, "uint8", 3))
        out = _submit("raster", _year(f"landcover_gulfhand_{y}", url, legend=LEGEND,
                                      outside_values=[3], declared=UINT8_3))
        assert out["status"] == "approved", out
        assert "steps" not in (tiffs.catalog()[f"landcover_gulfhand_{y}"].get("fetched") or {})
    cat = tiffs.catalog()
    s = get_settings()
    for y in (1985, 2014):
        mine, hand = (s.cache_dir / cat[f"{sid}_{y}"]["local_path"]
                      for sid in ("landcover_gulf", "landcover_gulfhand"))
        with rasterio.open(mine) as a, rasterio.open(hand) as b:
            assert (a.dtypes, a.nodata, a.crs, a.transform, a.shape) == \
                   (b.dtypes, b.nodata, b.crs, b.transform, b.shape)
            assert np.array_equal(a.read(1), b.read(1))            # cell for cell
        got, want = _mangrove_km2("landcover_gulf", y), _mangrove_km2("landcover_gulfhand", y)
        log("OUTPUT", f"{y}: mangrove {got} km2 (hand-recoded: {want})")
        assert got == want

    # the record: each step's inputs and sha256, the stored file's sha256, on the landed row
    row = cat["landcover_gulf_1985"]
    f = row["fetched"]
    assert [st["op"] for st in f["steps"]] == ["fetch", "recode"]
    fetch, rec = f["steps"]
    assert fetch["inputs"] == {"url": url85, "filename": "MANGLE_1985_RCLSS.IMG"}
    assert fetch["sha256"] == _sha(raw85) and fetch["bytes"] == len(raw85)
    assert rec["inputs"]["recode"] == {"10": 1, "20": 2}
    assert rec["inputs"]["from"] == {"dtype": "int8", "nodata": -128}
    assert rec["inputs"]["to"] == {"dtype": "uint8", "nodata": 3}
    assert rec["inputs"]["cells"] == {"10": 1652, "20": 3068}
    with open(s.cache_dir / row["local_path"], "rb") as fh:
        assert f["sha256"] == _sha(fh.read())
    assert f["prepared"] == "on the server: fetch -> recode"
    assert any(n.startswith("recoded at ingest 10 -> 1") for n in row["prepared"])
    assert any("reprojected at ingest from EPSG:32616" in n for n in row["prepared"])
    rec_store = store.list_contributions(status="approved")
    assert any((r["manifest"].get("recode") or {}) == {"10": 1, "20": 2} for r in rec_store)
    assert _no_leftovers(env)


def test_a_recode_that_names_the_delivered_legend_says_how_it_lands(env):
    """legend and declared describe the file as it lands: the delivered legend beside a
    recode is declined with the legend as it would land, before any fetch."""
    _land_1956(env, "landcover_gulf")
    url = env["host"].put("y85.img", b"never fetched")
    out = _submit("raster", _year("landcover_gulf_1985", url, recode={10: 1, 20: 2},
                                  legend={10: "Mangrove", 20: "Non-mangrove"},
                                  declared={"dtype": "uint8", "valid_min": 10, "valid_max": 20}))
    msg = " ".join(out["problems"])
    assert out["status"] == "declined" and env["host"].asked("y85.img") == []
    assert "as it lands it is {1: Mangrove, 2: Non-mangrove}" in msg
    assert "declare valid_min 1, valid_max 2" in msg


def test_a_recode_that_changes_no_cell_says_what_the_file_holds(env):
    _land_1956(env, "landcover_gulf")
    y56 = _catie()[0]
    url = env["host"].put("already.tif", _utm(y56, "uint8", 3))
    out = _submit("raster", _year("landcover_gulf_1985", url, recode={10: 1, 20: 2},
                                  legend=LEGEND, outside_values=[3], declared=UINT8_3))
    assert out["status"] == "declined"
    assert out["problems"] == ["recode changed no cell: the file holds none of the codes 10, 20 — "
                               "its values are 1, 2; give the codes as they are in the file"]
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 3. preview
def test_preview_says_what_would_land_and_stages_nothing(env, log):
    """preview: true runs the fetch, the recode and every gate check, and returns the
    classes (cells, km²), extent and stored bytes — nothing staged, stored or recorded."""
    _land_1956(env, "landcover_gulf")
    y14 = _catie()[2]
    url = env["host"].put("MANGLE_2014_RCLSS.IMG", _utm(y14, "uint8", 255, driver="HFA"))
    m = _year("landcover_gulf_2014", url, legend=LEGEND, outside_values=[3], declared=UINT8_3,
              recode={100: 1, 200: 2})
    before = store.list_contributions()
    out = _submit("raster", {**m, "preview": True})
    log("OUTPUT", json.dumps(out)[:600])
    assert out["status"] == "preview"
    wl = out["would_land"]
    assert wl["classes"]["Mangrove"]["code"] == 1 and wl["classes"]["Mangrove"]["km2"] > 0
    assert wl["classes"]["Mangrove"]["cells"] > 0 and wl["classes"]["Non-mangrove"]["cells"] > 0
    assert wl["bytes"] > 0 and wl["crs"] == "EPSG:4326" and len(wl["extent"]) == 4
    assert [st["op"] for st in wl["fetched"]["steps"]] == ["fetch", "recode"]
    assert any(n.startswith("recoded at ingest 100 -> 1") for n in wl["prepared"])
    assert store.list_contributions() == before
    assert "landcover_gulf_2014" not in tiffs.catalog() and not staging.STAGED_RASTERS
    assert _no_leftovers(env)
    # and the same manifest without preview lands with the areas the preview stated
    out = _submit("raster", m)
    assert out["status"] == "approved", out
    p = assemble.assemble(pack="land-cover", place="Gulf (synthetic)", year="2014",
                          geometry=_poly(_gulf_box()), stack="landcover_gulf")
    assert p["status"] == "ok", p.get("note")
    assert p["stats"]["classes_km2"]["Mangrove"] == round(wl["classes"]["Mangrove"]["km2"], 1)


# --------------------------------------------------------------------------- 4. with a clip
def test_a_recode_with_a_clip_reads_only_the_window_at_the_source(env, log):
    """recode is cell-by-cell, so a clip is still read at the source over range requests
    — the record says clip -> recode — and the stored cells are the window's, recoded."""
    arr = np.full((400, 400), 20, "uint8")
    arr[:, :150] = 10
    data = _gtiff(arr, "uint8", 255, bounds=(-86.0, 9.0, -84.0, 11.0))
    url = env["host"].put("national.tif", data)
    box = [-85.2, 10.0, -85.0, 10.2]
    out = _submit("raster", {"layer": "landcover_nat_2020", "stack": "landcover_nat", "year": 2020,
                             "url": url, "title": "t", "description": "d", "source": "s",
                             "license": "CC0-1.0", "vintage": "2026-10", "legend": LEGEND,
                             "outside_values": [255], "clip": box, "recode": {10: 1, 20: 2},
                             "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 2,
                                          "nodata": 255}})
    assert out["status"] == "approved", out
    row = tiffs.catalog()["landcover_nat_2020"]
    f = row["fetched"]
    log("OUTPUT", json.dumps(f)[:500])
    assert [st["op"] for st in f["steps"]] == ["clip", "recode"]
    assert f["read"] == "range requests, only the window" and f["clip"] == box
    assert any(env["host"].asked("national.tif"))         # read over range requests
    c0, r0, w, h = f["window"]
    want = np.where(arr[r0:r0 + h, c0:c0 + w] == 10, 1, 2)
    with rasterio.open(get_settings().cache_dir / row["local_path"]) as src:
        assert np.array_equal(src.read(1), want)
    assert _no_leftovers(env)


def test_a_host_without_range_requests_sends_the_file_whole_and_the_clip_is_cut_last(env, log):
    """No range requests: the file comes whole (streamed to disk, under the preparation
    budget, not the stored cap), is recoded, and the clip is cut from it last."""
    arr = np.full((400, 400), 20, "uint8")
    arr[:, :150] = 10
    url = env["host"].put("whole.tif", _gtiff(arr, "uint8", 255, bounds=(-86.0, 9.0, -84.0, 11.0)),
                          ranges=False)
    box = [-85.2, 10.0, -85.0, 10.2]
    out = _submit("raster", {"layer": "landcover_whole_2020", "stack": "landcover_whole",
                             "year": 2020, "url": url, "title": "t", "description": "d",
                             "source": "s", "license": "CC0-1.0", "vintage": "2026-10",
                             "legend": LEGEND, "outside_values": [255], "clip": box,
                             "recode": {10: 1, 20: 2},
                             "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 2,
                                          "nodata": 255}})
    assert out["status"] == "approved", out
    f = tiffs.catalog()["landcover_whole_2020"]["fetched"]
    log("OUTPUT", json.dumps({k: v for k, v in f.items() if k != "steps"}))
    assert [st["op"] for st in f["steps"]] == ["fetch", "recode", "clip"]
    assert f["read"] == "downloaded whole" and f["clip"] == box
    c0, r0, w, h = f["window"]
    want = np.where(arr[r0:r0 + h, c0:c0 + w] == 10, 1, 2)
    row = tiffs.catalog()["landcover_whole_2020"]
    with rasterio.open(get_settings().cache_dir / row["local_path"]) as src:
        assert np.array_equal(src.read(1), want)
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 5. other packs
def _toy_pack(monkeypatch):
    def gather(target, focus, trace, extras):
        stats = {"queries": None, "_area": {"name": "Toy site", "how": "test polygon",
                                            "geometry": RISK_POLY}}
        return ([{"n": 1, "kind": "measurement", "retrieval": "computed-at-pack-time",
                  "source": "toy", "title": "reading", "text": "42 units."}], [], stats)
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy", "version": "v0", "target_keys": ("place",),
        "target_doc": {"place": "site"}, "gather": gather,
        "sections": lambda: ["## Reading"], "corpus": None,
        "default_focus": lambda t: t.get("place", "")})


def test_a_fresh_vulnerability_map_coded_10_to_50_lands_recoded_and_the_risk_pack_reads_it(
        env, monkeypatch, log):
    """Beyond the asking pack: a vulnerability map delivered as 10-50 is declined with the
    recode proposal (a 1-5 class contract), lands recoded, and the risk pack and a
    contributed extension pack read it as classes 1-5."""
    arr = np.full((20, 20), 10, "uint8")
    arr[:, 10:] = 30
    arr[8:12, 8:12] = 50
    url = env["host"].put("vuln_coded.tif", _gtiff(arr, "uint8", 0))
    m = {"layer": "vulnerability_toy_coded", "url": url, "title": "Coded vulnerability",
         "description": "five classes coded 10-50", "source": "s", "license": "CC0-1.0",
         "vintage": "2026-10", "legend": {1: "Very Low", 3: "Moderate", 5: "Very High"},
         "declared": {"dtype": "uint8", "valid_min": 10, "valid_max": 50, "nodata": 0}}
    out = _submit("raster", m)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined"
    assert "resubmit with recode: {<code in the file>: <class 1-5>, ...}" in out["problems"][0]
    assert "ASK the contributor which class each code is" in out["problems"][0]
    out = _submit("raster", {**m, "recode": {10: 1, 30: 3, 50: 5},
                             "declared": {**m["declared"], "valid_min": 1, "valid_max": 5}})
    assert out["status"] == "approved", out
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY,
                          layers=["vulnerability_toy_coded"])
    assert p["status"] == "ok", p.get("note")
    s = p["layers"]["vulnerability_toy_coded"]["summary"]
    log("OUTPUT", json.dumps(s)[:400])
    assert set(s["classes"]) == {"1", "3", "5"}
    assert s["classes"]["5"]["cells"] == 16            # the middle 4 x 4 was 50
    _toy_pack(monkeypatch)
    t = assemble.assemble(pack="toy", place="Toy site", layers=["vulnerability_toy_coded"])
    assert t["status"] == "ok", t.get("note")
    assert t["layers"]["vulnerability_toy_coded"]["summary"]["classes"] == s["classes"]


# --------------------------------------------------------------------------- 6. the vector seam
class _KeepFirst:
    """A stand-in vector op registered for the test: keep the first N features."""
    NAME, STEP, KINDS = "keep_first", "convert", ("vector",)
    FIELDS = {"keep_first": "keep the first N features (a test op)"}

    @staticmethod
    def validate(m):
        return [] if isinstance(m.get("keep_first"), int) else ["keep_first must be a number"]

    @staticmethod
    def run(ctx):
        fc = json.loads(Path(ctx.path).read_text())
        fc["features"] = fc["features"][:ctx.manifest["keep_first"]]
        out = ctx.tmp(".geojson", "kept")
        Path(out).write_text(json.dumps(fc))
        return prepare.PreparedFile(out, [f"kept the first {ctx.manifest['keep_first']} "
                                          "features"], {"inputs": {"n": ctx.manifest["keep_first"]}})

    @staticmethod
    def suggest(situation):
        return None


def test_a_vector_op_runs_at_the_same_seam_and_its_steps_ride_the_landed_row(env, monkeypatch, log):
    monkeypatch.setattr(prepare, "_modules", lambda: (recode, _KeepFirst))
    feats = [{"type": "Feature", "properties": {"name": f"p{i}"},
              "geometry": {"type": "Polygon", "coordinates": [[[103 + i * .01, 13], [103.005 + i * .01, 13],
                                                               [103.005 + i * .01, 13.005], [103 + i * .01, 13]]]}}
             for i in range(5)]
    raw = json.dumps({"type": "FeatureCollection", "features": feats}).encode()
    url = env["host"].put("plots.geojson", raw)
    m = {"layer": "toy_plots", "url": url, "title": "plots", "description": "d", "source": "s",
         "license": "CC0-1.0", "vintage": "2026-10", "keep_first": 2}
    pv = _submit("vector", {**m, "preview": True})
    assert pv["status"] == "preview" and pv["would_land"]["features"] == 2
    assert pv["would_land"]["km2"] > 0 and not vectors.landed()
    out = _submit("vector", m)
    assert out["status"] == "approved", out
    row = vectors.landed()["toy_plots"]
    log("OUTPUT", json.dumps(row["fetched"])[:400])
    assert row["features"] == 2
    assert [st["op"] for st in row["fetched"]["steps"]] == ["fetch", "keep_first"]
    assert row["fetched"]["steps"][0]["sha256"] == _sha(raw)
    with open(vectors.master_path(row), "rb") as fh:
        assert row["fetched"]["sha256"] == _sha(fh.read())
    assert row["prepared"][0].startswith("kept the first 2 features")
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 7. budgets, safety
def test_an_input_over_the_preparation_budget_is_declined_with_its_size(env, monkeypatch):
    _land_1956(env, "landcover_gulf")
    y85 = _catie()[1]
    raw = _utm(y85, "int8", -128)
    url = env["host"].put("big.tif", raw)
    monkeypatch.setattr(get_settings(), "grp_prep_max_input_bytes", 1000)
    out = _submit("raster", _year("landcover_gulf_1985", url, recode={10: 1, 20: 2}, legend=LEGEND,
                                  outside_values=[3], declared=UINT8_3))
    assert out["status"] == "declined"
    assert out["problems"] == [f"{url} is {prepare.size_text(len(raw))}, over the 1000 bytes the "
                               "platform downloads to prepare one contribution — give a smaller "
                               "file (one tile, one year, one area)"]
    assert _no_leftovers(env)


def test_a_step_over_its_time_budget_is_declined(env, monkeypatch):
    _land_1956(env, "landcover_gulf")
    url = env["host"].put("y85.tif", _utm(_catie()[1], "int8", -128))
    monkeypatch.setattr(get_settings(), "grp_prep_op_seconds", 1e-9)
    out = _submit("raster", _year("landcover_gulf_1985", url, recode={10: 1, 20: 2}, legend=LEGEND,
                                  outside_values=[3], declared=UINT8_3))
    assert out["status"] == "declined"
    assert "step ran over its 1e-09 s budget" in out["problems"][0]
    assert _no_leftovers(env)


def _ctx(tmp_path, **over):
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="work-", dir=tmp_path))
    kw = dict(kind="raster", manifest={"url": "https://hub.test/x.zip"}, workdir=d, cap=1 << 20,
              max_input_bytes=1 << 30, op_seconds=60, max_cells=10 ** 9, max_vertices=10 ** 7)
    ctx = prepare.Context(**{**kw, **over})
    ctx.begin("test")
    return ctx


def _zip(members, compress=zipfile.ZIP_STORED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize("name", ["../evil.tif", "/etc/evil.tif", "C:/evil.tif", "a/../../evil.tif"])
def test_a_zip_naming_a_file_outside_itself_is_refused(tmp_path, name):
    p = tmp_path / "bad.zip"
    p.write_bytes(_zip({"ok.tif": b"x", name: b"y"}))
    with pytest.raises(prepare.PrepRefused, match="names a file outside itself"):
        _ctx(tmp_path).unzip(str(p))
    assert not (tmp_path / "evil.tif").exists()


def test_a_zip_bomb_and_a_zip_of_too_many_files_are_refused(tmp_path, monkeypatch):
    p = tmp_path / "bomb.zip"
    p.write_bytes(_zip({"bomb.tif": b"\0" * (8 << 20)}, zipfile.ZIP_DEFLATED))
    with pytest.raises(prepare.PrepRefused, match="refused as a zip bomb"):
        _ctx(tmp_path).unzip(str(p))
    monkeypatch.setattr(prepare, "ZIP_MEMBERS", 3)
    p.write_bytes(_zip({f"{i}.tif": b"x" for i in range(4)}))
    with pytest.raises(prepare.PrepRefused, match="holds 4 files"):
        _ctx(tmp_path).unzip(str(p))
    p.write_bytes(_zip({"a/one.tif": b"1", "two.tif": b"22"}))
    got = _ctx(tmp_path).unzip(str(p))
    assert sorted(got) == ["a/one.tif", "two.tif"] and Path(got["two.tif"]).read_bytes() == b"22"


def test_fetch_to_file_streams_to_disk_under_the_same_policy(env, tmp_path):
    host = env["host"]
    data = bytes(range(256)) * 4096                      # 1 MiB
    url = host.put("blob.bin", data)
    dest = tmp_path / "blob.part"
    n, name = fetch_policy.fetch_to_file(url, dest, max_bytes=2 << 20)
    assert n == len(data) and name == "blob.bin" and dest.read_bytes() == data
    with pytest.raises(fetch_policy.TooLarge) as exc:
        fetch_policy.fetch_to_file(url, tmp_path / "small.part", max_bytes=1000)
    assert exc.value.size == len(data) and not (tmp_path / "small.part").exists()
    get_settings().grp_contrib_allow_private_hosts = False
    try:
        with pytest.raises(fetch_policy.FetchRefused, match="private, loopback or link-local"):
            fetch_policy.fetch_to_file(url, tmp_path / "private.part")
    finally:
        get_settings().grp_contrib_allow_private_hosts = True
    assert not (tmp_path / "private.part").exists()


def test_a_zip_holding_the_raster_is_unpacked_and_recoded(env):
    """A recode on a zipped .img (the delivered folder zipped whole): the member named in
    the url is read, and the record names it."""
    _land_1956(env, "landcover_gulf")
    y85 = _catie()[1]
    z = _zip({"MANGLE_NICOYA_CI/MANGLE_1985_RCLSS.IMG": _utm(y85, "int8", -128, driver="HFA"),
              "MANGLE_NICOYA_CI/readme.txt": b"codes 10/20"})
    url = env["host"].put("mangle.zip", z)
    out = _submit("raster", _year("landcover_gulf_1985", url + "#MANGLE_NICOYA_CI/MANGLE_1985_RCLSS.IMG",
                                  recode={10: 1, 20: 2}, legend=LEGEND, outside_values=[3],
                                  declared=UINT8_3))
    assert out["status"] == "approved", out
    steps = tiffs.catalog()["landcover_gulf_1985"]["fetched"]["steps"]
    assert steps[0]["inputs"]["member"] == "MANGLE_NICOYA_CI/MANGLE_1985_RCLSS.IMG"
    assert [st["op"] for st in steps] == ["fetch", "recode"]
    assert _no_leftovers(env)
