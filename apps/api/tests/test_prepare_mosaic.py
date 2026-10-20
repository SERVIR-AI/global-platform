"""Preparation on the server: mosaic — a raster delivered as tiles, joined into one layer.

The sprint cases: Global Mangrove Watch v3.0 is a zip of 1,521 one-degree tiles (named
by their top-left corner, nodata 0, only where mangroves are), GWL_FCS30D a 2.5 GB zip of
five-degree tiles whose grids sit a fraction of a cell apart, ETH canopy height 400 MB
three-degree tiles behind separate links (named by their bottom-left corner). Costa Rica,
the Gulf of Nicoya and the Gulf of Guayaquil each need several; they were mosaicked by
hand. Here the decline on the zip proposes `mosaic: 'auto'`, the server reads the
headers of the tiles named near the clip, reads only their cells under it (range
requests; never the whole zip) and stores one file equal to the hand mosaic. Beyond the
asking pack: a vulnerability map in two tiles is read by the risk pack and a contributed
extension pack.

Everything is synthetic and local: a loopback HTTP server in this file serves the files
(Range requests too), and no test touches the network."""

import hashlib
import io
import json
import re
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
import rasterio
import yaml
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds, from_origin

from app.config import get_settings
from app.contrib import identity, prepare, remote_clip, staging
from app.contrib.prepare import mosaic
from app.graph.geo import ingest, schema, tiffs
from app.mcp import assemble, packs, store
from app.risk import synthesis as risk

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
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


class _Server(ThreadingHTTPServer):
    request_queue_size = 64            # the tiles' pieces are asked several at once


@pytest.fixture
def host():
    h = _Host()
    srv = _Server(("127.0.0.1", 0), _handler(h))
    h.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield h
    srv.shutdown()
    srv.server_close()


# --------------------------------------------------------------------------- a world
def _gtiff(arr, dtype, nodata=None, crs="EPSG:4326", transform=None, bounds=B, tiled=False):
    h, w = arr.shape
    extra = {"tiled": True, "blockxsize": 256, "blockysize": 256, "compress": "deflate"} if tiled else {}
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=w, height=h, count=1, dtype=dtype, crs=crs,
                     transform=transform or from_bounds(*bounds, w, h), nodata=nodata, **extra) as d:
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
    from app.mcp import registry
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


def _no_leftovers(env):
    prep = env["cache"] / "prep"
    strays = list((env["cache"] / "tiffs").glob("staged-tmp-*"))
    return (not prep.exists() or not list(prep.iterdir())) and not strays


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _poly(box):
    w, s, e, n = box
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _zip(members, compress=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _stored(layer):
    row = tiffs.catalog()[layer]
    path = get_settings().cache_dir / row["local_path"]
    with rasterio.open(path) as src:
        return row, src.read(1), src.profile, path


# --------------------------------------------------------------------------- the GMW shape
GMW_PX = 60                                    # cells per degree (the real tiles: 4,500)
GMW_BOX = [-86.0, 8.0, -82.5, 11.3]             # Costa Rica, as cut by hand
# the tiles over Costa Rica that exist (only where there are mangroves), by top-left corner
GMW_HERE = [(9, -83), (9, -84), (10, -83), (10, -84), (10, -85), (10, -86), (11, -85),
            (11, -86), (12, -84), (12, -86)]
# named near the box but lying outside it (touching an edge), and far away
GMW_EDGE = [(9, -87), (12, -82), (8, -84)]
GMW_FAR = [(0, 8), (0, 9), (-38, 145), (-38, 146), (-38, 177)]


def _gmw_name(top, left):
    ns = f"{'N' if top >= 0 else 'S'}{abs(top):02d}"
    ew = f"{'E' if left >= 0 else 'W'}{abs(left):03d}"
    return f"gmw_v3_2020/GMW_{ns}{ew}_2020_v3.tif"


def _gmw_tile(top, left, seed):
    rng = np.random.default_rng(seed)
    if (top, left) in GMW_FAR:          # the rest of the world: most of the zip's bytes
        return rng.integers(0, 256, (6 * GMW_PX, 6 * GMW_PX), dtype="uint8")
    return (rng.random((GMW_PX, GMW_PX)) < 0.04).astype("uint8")     # 1 = mangrove


def _gmw():
    """The zip as Zenodo serves it, and the mosaic as it was cut by hand: the box's grid,
    every existing tile pasted, 0 where no tile is (no mangrove there), no nodata tag."""
    members, hand = {}, np.zeros((round(3.3 * GMW_PX), round(3.5 * GMW_PX)), "uint8")
    under = np.zeros(hand.shape, bool)
    for i, (top, left) in enumerate(GMW_HERE + GMW_EDGE + GMW_FAR):
        a = _gmw_tile(top, left, i)
        members[_gmw_name(top, left)] = _gtiff(a, "uint8", 0, bounds=(left, top - 1, left + 1, top))
        if (top, left) in GMW_HERE:
            r0 = round((GMW_BOX[3] - top) * GMW_PX)
            c0 = round((left - GMW_BOX[0]) * GMW_PX)
            rs, cs = max(0, r0), max(0, c0)
            re_, ce = min(hand.shape[0], r0 + GMW_PX), min(hand.shape[1], c0 + GMW_PX)
            hand[rs:re_, cs:ce] = a[rs - r0:re_ - r0, cs - c0:ce - c0]
            under[rs:re_, cs:ce] = True
    _gmw.uncovered = int((~under).sum())
    return _zip(dict(sorted(members.items()))), hand


GMW_LEGEND = {0: "Not mangrove", 1: "Mangrove"}


def _gmw_year(layer_year, url, **over):
    stack, year = layer_year.rsplit("_", 1)
    return {"layer": layer_year, "stack": stack, "year": int(year), "url": url,
            "title": f"Global Mangrove Watch v3.0 mangrove / not mangrove {year} (synthetic)",
            "description": "1 = mangrove, 0 = not mangrove; a mosaic of the producer's tiles",
            "source": "test fixture shaped like GMW v3.0 (Zenodo 6894273)",
            "license": "CC-BY-4.0", "vintage": "2022-07", "legend": GMW_LEGEND,
            "declared": {"dtype": "uint8", "valid_min": 0, "valid_max": 1, "units": "class"},
            "outside_values": [], "countries": ["Costa Rica"],
            "stack_title": "GMW v3.0 two-class, Costa Rica (synthetic)", **over}


# --------------------------------------------------------------------------- 1. the registry
def test_mosaic_is_registered_and_its_field_reaches_the_form():
    """The op is a module in the package: its field is on the raster form and in the
    contribute_submit description, and not on the vector form."""
    assert "mosaic" in [op.NAME for op in prepare.ops("raster")]
    assert prepare.ORDER[mosaic.STEP] < prepare.ORDER["recode"] < prepare.ORDER["clip"]
    text = staging.RASTER_FIELDS["optional"]["mosaic"]
    assert text == mosaic.FIELDS["mosaic"] and "'auto'" in text and "ASK the contributor" in text
    assert "mosaic" in staging._RASTER_KNOWN and "mosaic" not in staging.VECTOR_FIELDS["optional"]
    assert "mosaic (for a raster delivered as TILES" in staging.describe_submit()
    assert "zip_of_rasters" in prepare.SITUATIONS


@pytest.mark.parametrize("name, where", [
    ("gmw_v3_2020/GMW_N09W083_2020_v3.tif", (-83, 9)),
    ("2020/GWL_FCS30D_2020Maps_W85N10.tif", (-85, 10)),
    ("2020/GWL_FCS30D_2020Maps_W80N5.tif", (-80, 5)),
    ("ETH_GlobalCanopyHeight_10m_2020_S03W081_Map.tif", (-81, -3)),
    ("Hansen_GFC-2019-v1.7_treecover2000_10N_080W.tif", (-80, 10)),
    ("GMW_S38E145_2020_v3.tif", (145, -38)),
    ("FCS30D_2020Maps.tif", None),                 # 'S30' inside a word is no position
    ("mangle_2014.tif", None),
])
def test_a_tile_name_places_it_only_as_a_guide(name, where):
    assert mosaic.position(name) == where


# --------------------------------------------------------------------------- 2. GMW
def test_the_gmw_zip_lands_as_one_costa_rica_mosaic_from_its_own_link(env, monkeypatch, log):
    """The zip of tiles as delivered is declined with the mosaic proposal (the platform's
    own preparation off, GRP_PREP_AUTO=0 — on, it applies mosaic itself: test_prepare_auto);
    the manifest with mosaic: 'auto' and the clip lands; only the headers named near the
    box and the tiles under it are read; the stored file equals the hand mosaic cell for
    cell, the pack's mangrove km² are the hand layer's, and every step is on the record."""
    monkeypatch.setattr(get_settings(), "grp_prep_auto", False)
    host = env["host"]
    raw, hand = _gmw()
    url = host.put("gmw_v3_2020_gtiff.zip", raw)

    # as delivered, with the area: a zip of rasters — name one, or join the tiles
    out = _submit("raster", _gmw_year("landcover_crgmw_2020", url, clip=GMW_BOX))
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined"
    said = out["problems"][0]
    assert f"is a zip of {len(GMW_HERE) + len(GMW_EDGE) + len(GMW_FAR)} rasters" in said
    assert "resubmit the same link with mosaic: 'auto' and clip" in said
    assert "nothing to do by hand" in said and "ASK the contributor" in said

    # as delivered with no area, over the cap: the area is asked for, and the mosaic named
    s = get_settings()
    cap = s.grp_contrib_max_bytes
    s.grp_contrib_max_bytes = len(raw) // 2
    try:
        out = _submit("raster", _gmw_year("landcover_crgmw_2020", url))
    finally:
        s.grp_contrib_max_bytes = cap
    log("OUTPUT", out["problems"][0])
    assert out["problems"][0].startswith("missing required field 'clip'")
    assert "mosaic: 'auto'" in out["problems"][0]

    # the agent fills the proposed field
    host.log.clear()
    m = _gmw_year("landcover_crgmw_2020", url, clip=GMW_BOX, mosaic="auto")
    out = _submit("raster", m)
    log("OUTPUT", out.get("preview", {}).get("note", out))
    assert out["status"] == "approved", out
    # every request a range, for the directory and each tile's own bytes: never the whole zip
    assert host.log and all(rng for _, rng in host.log)
    got = sum(min(int(b), len(raw) - 1) - int(a) + 1
              for a, b in (re.match(r"bytes=(\d+)-(\d+)", g).groups() for _, g in host.log))
    log("OUTPUT", f"{len(host.log)} range requests, {got:,} of the zip's {len(raw):,} bytes")
    assert got < len(raw) / 2
    row, arr, prof, path = _stored("landcover_crgmw_2020")
    assert arr.shape == hand.shape and np.array_equal(arr, hand)     # cell for cell
    assert prof["nodata"] is None and prof["crs"].to_epsg() == 4326
    assert tuple(round(v, 9) for v in rasterio.open(path).bounds) == tuple(GMW_BOX[i] for i in (0, 1, 2, 3))

    f = row["fetched"]
    log("OUTPUT", json.dumps({k: v for k, v in f.items() if k != "steps"}))
    assert [st["op"] for st in f["steps"]] == ["mosaic"]
    step = f["steps"][0]
    inp = step["inputs"]
    assert inp["mosaic"] == "auto" and inp["picked"]["by"] == "header bounds"
    assert sorted(t["member"] for t in inp["tiles"]) == sorted(_gmw_name(*t) for t in GMW_HERE)
    read = set(inp["picked"]["headers_read"])
    # the first header read shows the names give the top-left corner of a 1-degree tile:
    # only the tiles those names put under the box are read, never the edge or far ones
    assert read == {_gmw_name(*t) for t in GMW_HERE}
    assert all(t["read"] == "range requests, the member only" and t["grid"] == "own"
               for t in inp["tiles"])
    with zipfile.ZipFile(io.BytesIO(raw)) as z:                 # each tile's sha256 is its own
        assert all(t["sha256"] == _sha(z.read(t["member"])) for t in inp["tiles"])
    assert inp["clip"] == GMW_BOX and inp["uncovered_cells"] == _gmw.uncovered > 0
    assert inp["nodata"] is None and inp["fill"] == 0
    assert f"{_gmw.uncovered:,} cells of the mosaic lie under no tile and were written as 0 " \
           "(the legend's class Not mangrove)" in row["prepared"]
    with open(path, "rb") as fh:
        stored_sha = _sha(fh.read())
    assert f["sha256"] == stored_sha == step["sha256"]
    assert f["prepared"] == "on the server: mosaic" and f["read"] == "range requests, only the tiles"
    assert f["clip"] == GMW_BOX
    assert row["prepared"][0].startswith(f"mosaicked {len(GMW_HERE)} tiles of the zip at {url}")
    assert any("the legend names 0 a class (Not mangrove)" in n for n in row["prepared"])
    assert any(n.startswith("the tiles were picked by their headers") for n in row["prepared"])

    # the hand mosaic, contributed as it was on the sprint, reads the same
    hand_url = host.put("cr_gmw_v3_2020_2class.tif", _gtiff(hand, "uint8", None, bounds=GMW_BOX))
    out = _submit("raster", _gmw_year("landcover_crgmwhand_2020", hand_url))
    assert out["status"] == "approved", out
    for sid in ("landcover_crgmw", "landcover_crgmwhand"):
        p = assemble.assemble(pack="land-cover", place="Gulf of Nicoya (synthetic)", year="2020",
                              geometry=_poly([-85.3, 9.9, -84.7, 10.3]), stack=sid,
                              classes=["Mangrove"])
        assert p["status"] == "ok", p.get("note")
        log("OUTPUT", f"{sid}: mangrove {p['stats']['classes_km2']['Mangrove']} km2")
    km2 = [assemble.assemble(pack="land-cover", place="Costa Rica box", year="2020",
                             geometry=_poly(GMW_BOX), stack=sid)["stats"]["classes_km2"]
           for sid in ("landcover_crgmw", "landcover_crgmwhand")]
    assert km2[0] == km2[1]
    assert _no_leftovers(env)


def test_preview_says_what_the_mosaic_would_land_and_stages_nothing(env, monkeypatch, log):
    """preview: true runs the mosaic and every gate check and says what would land —
    nothing staged; each member fetched in many small pieces here, joined back exactly."""
    monkeypatch.setattr(mosaic, "CHUNK", 777)
    raw, hand = _gmw()
    url = env["host"].put("gmw.zip", raw)
    m = _gmw_year("landcover_crgmw_2020", url, clip=GMW_BOX, mosaic="auto")
    before = store.list_contributions()
    out = _submit("raster", {**m, "preview": True})
    log("OUTPUT", json.dumps(out)[:700])
    assert out["status"] == "preview"
    wl = out["would_land"]
    assert wl["classes"]["Mangrove"]["cells"] == int((hand == 1).sum())
    assert wl["classes"]["Not mangrove"]["cells"] == int((hand == 0).sum())
    assert wl["cells"] == [hand.shape[1], hand.shape[0]] and wl["bytes"] > 0
    assert wl["extent"] == GMW_BOX
    assert [st["op"] for st in wl["fetched"]["steps"]] == ["mosaic"]
    assert store.list_contributions() == before and not staging.STAGED_RASTERS
    assert "landcover_crgmw_2020" not in tiffs.catalog()
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 3. GWL
GWL_PX = 100                                   # cells per tile (the real tiles: 18,554)
GWL_RES = 5.01 / GWL_PX                        # a tile spans a bit over 5 degrees: grids
                                               # of neighbours sit 0.2 cell apart
GWL_CLASS = {(-90, 15): 181, (-85, 15): 185, (-90, 10): 187, (-85, 10): 0}
GWL_BOX = [-86.6, 8.6, -84.6, 11.9]            # mostly in W90N15


def _gwl_name(left, top):
    return f"2020/GWL_FCS30D_2020Maps_W{abs(left)}N{top}.tif"


def _gwl():
    members = {}
    for (left, top), cls in GWL_CLASS.items():
        a = np.full((GWL_PX, GWL_PX), cls, "uint8")
        if cls == 0:
            a[-20:, -20:] = 255                                 # open sea, not mapped
        members[_gwl_name(left, top)] = _gtiff(
            a, "uint8", None, transform=from_origin(left, top, GWL_RES, GWL_RES))
    for left, top in ((-80, 50), (-80, 5)):                     # elsewhere
        members[_gwl_name(left, top)] = _gtiff(
            np.full((GWL_PX, GWL_PX), 182, "uint8"), "uint8", None,
            transform=from_origin(left, top, GWL_RES, GWL_RES))
    return _zip(members)


GWL_LEGEND = {0: "Non-wetland", 181: "Swamp", 182: "Marsh", 185: "Mangrove forest",
              187: "Tidal flat"}


def test_gwl_tiles_on_offset_grids_join_by_nearest_from_a_zip_sent_only_whole(env, log):
    """A host without range requests: the zip comes whole (streamed to disk, its sha256
    on the record), the tiles under the clip are picked by their headers, the ones on
    another grid are resampled by nearest neighbour onto the grid of the tile that
    covers most of the clip — every class value unchanged, each quadrant its tile's."""
    raw = _gwl()
    url = env["host"].put("GWL_FCS30_2020.zip", raw, ranges=False)
    out = _submit("raster", {
        "layer": "landcover_crgwl_2020", "stack": "landcover_crgwl", "year": 2020, "url": url,
        "title": "GWL_FCS30D 2020 (synthetic)", "description": "wetland classes, 30 m",
        "source": "test fixture shaped like GWL_FCS30D", "license": "CC-BY-4.0",
        "vintage": "2023-11", "legend": GWL_LEGEND, "outside_values": [255],
        "declared": {"dtype": "uint8", "valid_min": 0, "valid_max": 187, "units": "class"},
        "stack_title": "GWL_FCS30D (synthetic)", "clip": GWL_BOX, "mosaic": "auto"})
    assert out["status"] == "approved", out
    row, arr, prof, path = _stored("landcover_crgwl_2020")
    f = row["fetched"]
    log("OUTPUT", row["prepared"][0])
    assert [st["op"] for st in f["steps"]] == ["fetch", "mosaic"]
    assert f["steps"][0]["sha256"] == _sha(raw) and f["read"] == "downloaded whole"
    inp = f["steps"][1]["inputs"]
    assert inp["grid"]["of"] == _gwl_name(-90, 15)              # covers most of the clip
    assert inp["resampling"] == "nearest" and inp["uncovered_cells"] == 0
    assert {t["member"]: t["grid"] for t in inp["tiles"]} == {
        _gwl_name(-90, 15): "own", _gwl_name(-85, 15): "nearest",
        _gwl_name(-90, 10): "nearest", _gwl_name(-85, 10): "nearest"}
    assert _gwl_name(-80, 50) not in inp["picked"]["headers_read"]
    assert set(np.unique(arr).tolist()) <= {0, 181, 185, 187, 255}
    with rasterio.open(path) as src:
        for (left, top), cls in GWL_CLASS.items():             # each quadrant its tile's
            x = (max(left, GWL_BOX[0]) + min(left + 5, GWL_BOX[2])) / 2
            y = (max(top - 5, GWL_BOX[1]) + min(top, GWL_BOX[3])) / 2
            r, c = src.index(x, y)
            assert arr[r, c] == cls, (left, top)
        assert abs(src.transform.a - GWL_RES) < 1e-12           # the tiles' own cells
    assert prof["nodata"] is None                               # the tiles carry none
    assert "resampled onto it by nearest neighbour" in row["prepared"][0]
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 4. ETH
ETH_PX = 600                                   # cells per degree (the real tiles: 12,000)
ETH_BOX = [-80.6, -3.6, -79.7, -2.0]           # the Gulf of Guayaquil


def _eth(bottom):
    rng = np.random.default_rng(abs(bottom))
    a = rng.integers(0, 50, (3 * ETH_PX, 3 * ETH_PX), dtype="uint8")
    a[: ETH_PX // 2, : ETH_PX // 2] = 255                       # no data
    return a


def test_eth_tiles_behind_two_links_join_reading_only_their_windows(env, log):
    """Two 3-degree tiles behind their own links (named by their bottom-left corner):
    the clip straddling their edge is read from each over range requests — a small part
    of each file — and stored joined, cell for cell the two windows stacked."""
    host = env["host"]
    t03, t06 = _eth(-3), _eth(-6)
    urls = [host.put(f"ETH_GlobalCanopyHeight_10m_2020_{n}W081_Map.tif",
                     _gtiff(a, "uint8", 255, transform=from_origin(-81, top, 1 / ETH_PX, 1 / ETH_PX),
                            tiled=True))
            for n, a, top in (("S03", t03, 0.0), ("S06", t06, -3.0))]
    out = _submit("raster", {
        "layer": "measure_ec_eth_height_gulf", "url": urls[0], "mosaic": urls,
        "clip": ETH_BOX, "title": "ETH canopy height 2020, Gulf of Guayaquil (synthetic)",
        "description": "canopy top height, m", "source": "test fixture shaped like ETH",
        "license": "CC-BY-4.0", "vintage": "2022-04", "countries": ["Ecuador"],
        "declared": {"dtype": "uint8", "valid_min": 0, "valid_max": 60, "nodata": 255,
                     "units": "m"}})
    assert out["status"] == "approved", out
    row, arr, prof, path = _stored("measure_ec_eth_height_gulf")
    full = np.vstack([t03, t06])                                # lat 0 .. -6 at -81 .. -78
    r0, r1 = round((0 - ETH_BOX[3]) * ETH_PX), round((0 - ETH_BOX[1]) * ETH_PX)
    c0, c1 = round((ETH_BOX[0] + 81) * ETH_PX), round((ETH_BOX[2] + 81) * ETH_PX)
    assert np.array_equal(arr, full[r0:r1, c0:c1]) and prof["nodata"] == 255
    for u in urls:
        # every read is a range (GDAL's size probe, a GET it drops after the headers,
        # is the one request without one)
        name = u.rsplit("/", 1)[1]
        asked = [g for g in host.asked(name) if g]
        got = sum(int(b) - int(a) + 1 for a, b in (re.match(r"bytes=(\d+)-(\d+)", g).groups()
                                                   for g in asked))
        log("OUTPUT", f"{name}: {got:,} of {len(host.files['/' + name]):,} bytes read")
        assert got < len(host.files["/" + name]) / 2
    f = row["fetched"]
    assert [st["op"] for st in f["steps"]] == ["mosaic"]
    inp = f["steps"][0]["inputs"]
    assert [t["url"] for t in inp["tiles"]] == urls and inp["mosaic"] == urls
    assert all(t["grid"] == "own" for t in inp["tiles"]) and "resampling" not in inp
    assert "from 2 links" in row["prepared"][0]
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


def test_a_vulnerability_map_in_two_tiles_lands_joined_and_the_risk_pack_reads_it(
        env, monkeypatch, log):
    """Beyond the asking pack: a freshly contributed vulnerability map delivered as a
    west and an east tile, in one zip, joins by listing the members (no clip: each tile
    whole) and is read by the risk pack and a contributed extension pack."""
    west = np.full((20, 10), 2, "uint8")
    west[8:12, 8:10] = 5
    east = np.full((20, 10), 3, "uint8")
    east[8:12, 0:2] = 5
    z = _zip({"vuln/west.tif": _gtiff(west, "uint8", 0, bounds=(103.0, 13.0, 103.01, 13.02)),
              "vuln/east.tif": _gtiff(east, "uint8", 0, bounds=(103.01, 13.0, 103.02, 13.02)),
              "vuln/readme.txt": b"two tiles"})
    url = env["host"].put("vuln_tiles.zip", z)
    out = _submit("raster", {"layer": "vulnerability_toy_tiles", "url": url,
                             "mosaic": ["vuln/west.tif", "east.tif"],
                             "title": "Tiled vulnerability", "description": "classes 1-5",
                             "source": "s", "license": "CC0-1.0", "vintage": "2026-10",
                             "legend": {1: "Very Low", 3: "Moderate", 5: "Very High"},
                             "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 5,
                                          "nodata": 0}})
    assert out["status"] == "approved", out
    row, arr, prof, _ = _stored("vulnerability_toy_tiles")
    assert np.array_equal(arr, np.hstack([west, east])) and prof["nodata"] == 0
    assert [t["member"] for t in row["fetched"]["steps"][0]["inputs"]["tiles"]] == [
        "vuln/west.tif", "vuln/east.tif"] or [
        t["member"] for t in row["fetched"]["steps"][0]["inputs"]["tiles"]] == [
        "vuln/east.tif", "vuln/west.tif"]
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY,
                          layers=["vulnerability_toy_tiles"])
    assert p["status"] == "ok", p.get("note")
    s = p["layers"]["vulnerability_toy_tiles"]["summary"]
    log("OUTPUT", json.dumps(s)[:400])
    assert s["classes"]["5"]["cells"] == 16                    # the 4 x 4 across the seam
    _toy_pack(monkeypatch)
    t = assemble.assemble(pack="toy", place="Toy site", layers=["vulnerability_toy_tiles"])
    assert t["status"] == "ok", t.get("note")
    assert t["layers"]["vulnerability_toy_tiles"]["summary"]["classes"] == s["classes"]
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 6. declines
def _bare(**over):
    return {"layer": "landcover_x_2020", "stack": "landcover_x", "year": 2020,
            "title": "t", "description": "d", "source": "s", "license": "CC0-1.0",
            "vintage": "2026-10", "legend": {1: "A", 2: "B"}, "stack_title": "x",
            "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 2}, **over}


def test_a_mosaic_that_cannot_run_says_why_before_anything_is_fetched(env):
    url = env["host"].put("tiles.zip", b"never fetched")
    for over, said in (
            ({"mosaic": "auto"}, "mosaic: 'auto' takes the tiles of the zip at url that the clip "
                                 "touches — give clip"),
            ({"mosaic": ["a.tif"]}, "a mosaic joins two or more tiles"),
            ({"mosaic": ["a.tif", "a.tif"]}, "mosaic lists a.tif more than once"),
            ({"mosaic": 7}, "mosaic must be 'auto'"),
            ({"mosaic": ["a.tif", "b.tif"], "clip_member": "a.tif"}, "drop clip_member"),
            ({"mosaic": ["a.tif", "b.tif"], "url": url + "#a.tif"}, "without '#a.tif'"),
            ({"mosaic": ["ftp://x/a.tif", "b.tif"]}, "mosaic tile ftp://x/a.tif: url scheme")):
        out = _submit("raster", {**_bare(url=url), **over})
        assert out["status"] == "declined", over
        assert any(said in p for p in out["problems"]), (over, out["problems"])
    assert env["host"].log == []


def test_a_clip_no_tile_lies_under_says_so(env):
    raw, _ = _gmw()
    url = env["host"].put("gmw.zip", raw)
    out = _submit("raster", _gmw_year("landcover_crgmw_2020", url, clip=[10.0, 50.0, 11.0, 51.0],
                                      mosaic="auto"))
    assert out["status"] == "declined"
    assert "is named near the clip" in out["problems"][0]
    out = _submit("raster", _gmw_year("landcover_crgmw_2020", url,
                                      clip=[-87.5, 12.2, -86.2, 12.6], mosaic="auto"))
    assert out["status"] == "declined"
    assert "were read and lie outside it" in out["problems"][0]
    assert _no_leftovers(env)


def test_cells_no_tile_covers_with_no_nodata_ask_for_declared_nodata(env):
    a = np.ones((10, 10), "uint8")
    z = _zip({"t/N01E001.tif": _gtiff(a, "uint8", None, bounds=(1.0, 0.0, 2.0, 1.0)),
              "t/N02E002.tif": _gtiff(a * 2, "uint8", None, bounds=(2.0, 1.0, 3.0, 2.0))})
    url = env["host"].put("diag.zip", z)
    out = _submit("raster", _bare(url=url, mosaic=["t/N01E001.tif", "t/N02E002.tif"]))
    assert out["status"] == "declined"
    assert "lie under no tile, and the tiles carry no nodata — declare declared.nodata" \
        in out["problems"][0]
    out = _submit("raster", _bare(url=url, mosaic=["t/N01E001.tif", "t/N02E002.tif"],
                                  outside_values=[0],
                                  declared={"dtype": "uint8", "valid_min": 1, "valid_max": 2,
                                            "nodata": 0}))
    assert out["status"] == "approved", out
    _, arr, prof, _ = _stored("landcover_x_2020")
    assert prof["nodata"] == 0 and (arr == 0).sum() == 200 and arr.shape == (20, 20)
    assert _no_leftovers(env)


def test_a_count_grid_on_two_grids_is_refused(env):
    a = np.ones((10, 10), "uint16")
    url1 = env["host"].put("p1.tif", _gtiff(a, "uint16", 0, bounds=(1.0, 0.0, 2.0, 1.0)))
    url2 = env["host"].put("p2.tif", _gtiff(a, "uint16", 0, bounds=(2.05, 0.0, 3.05, 1.0)))
    out = _submit("raster", {"layer": "population_toy", "url": url1, "mosaic": [url1, url2],
                             "title": "t", "description": "people per cell", "source": "s",
                             "license": "CC0-1.0", "vintage": "2026-10",
                             "declared": {"dtype": "uint16", "valid_min": 0, "valid_max": 9,
                                          "nodata": 0, "units": "people per pixel"}})
    assert out["status"] == "declined"
    assert "count grid, which the platform never re-grids" in out["problems"][0]
    assert _no_leftovers(env)


def test_a_zip_member_reaching_outside_or_a_bomb_is_refused(env):
    a = np.ones((10, 10), "uint8")
    z = _zip({"ok/N01E001.tif": _gtiff(a, "uint8", 0, bounds=(1.0, 0.0, 2.0, 1.0)),
              "../evil.tif": _gtiff(a, "uint8", 0, bounds=(2.0, 0.0, 3.0, 1.0)),
              "bomb.tif": b"\0" * (8 << 20)})
    url = env["host"].put("bad.zip", z)
    out = _submit("raster", _bare(url=url, mosaic=["ok/N01E001.tif", "../evil.tif"]))
    assert out["status"] == "declined" and "names a file outside itself" in out["problems"][0]
    out = _submit("raster", _bare(url=url, mosaic=["ok/N01E001.tif", "bomb.tif"]))
    assert out["status"] == "declined" and "refused as a zip bomb" in out["problems"][0]
    assert _no_leftovers(env)


def test_a_mosaic_over_the_cell_budget_is_declined(env, monkeypatch):
    raw, _ = _gmw()
    url = env["host"].put("gmw.zip", raw)
    monkeypatch.setattr(get_settings(), "grp_prep_max_cells", 1000)
    out = _submit("raster", _gmw_year("landcover_crgmw_2020", url, clip=GMW_BOX, mosaic="auto"))
    assert out["status"] == "declined"
    assert "the mosaic has 41,580 cells, over the 1,000 one mosaic step may touch" \
        in out["problems"][0]
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 7. the proposal
def test_the_zip_of_rasters_proposal_names_what_fits():
    tiles = [_gmw_name(9, -83), _gmw_name(10, -83)]
    said = remote_clip._proposed(tiles, "https://hub.test/x.zip")
    assert said.startswith("; if the area needs more than one of its tiles, resubmit the same "
                           "link with mosaic: 'auto'")
    said = remote_clip._proposed(["a/west.tif", "a/east.tif"], "https://hub.test/x.zip")
    assert "mosaic: [<member>, <member>, ...]" in said and "ASK the contributor" in said
    assert remote_clip._proposed(["one.tif"], "https://hub.test/x.zip") == ""
    assert mosaic.suggest({"event": "zip_of_rasters", "manifest": {"mosaic": "auto"},
                           "rasters": tiles}) is None


# --------------------------------------------------------------------------- 8. more shapes
def test_tiles_whose_names_place_them_nowhere_are_picked_by_their_headers_alone(env):
    """'auto' over a zip of rasters named a.tif, b.tif, ...: every header is read (no name
    guides it), and only the tiles under the clip are joined."""
    t = {f"t/{n}.tif": (np.full((10, 10), v, "uint8"), b)
         for n, v, b in (("a", 1, (1.0, 0.0, 2.0, 1.0)), ("b", 2, (2.0, 0.0, 3.0, 1.0)),
                         ("c", 1, (40.0, 0.0, 41.0, 1.0)))}
    url = env["host"].put("plain.zip", _zip({k: _gtiff(a, "uint8", 0, bounds=b)
                                             for k, (a, b) in t.items()}))
    out = _submit("raster", _bare(url=url, mosaic="auto", clip=[1.5, 0.2, 2.5, 0.8]))
    assert out["status"] == "approved", out
    row, arr, _, _ = _stored("landcover_x_2020")
    inp = row["fetched"]["steps"][0]["inputs"]
    assert sorted(inp["picked"]["headers_read"]) == ["t/a.tif", "t/b.tif", "t/c.tif"]
    assert sorted(x["member"] for x in inp["tiles"]) == ["t/a.tif", "t/b.tif"]
    assert arr.shape == (6, 10) and set(np.unique(arr).tolist()) == {1, 2}
    assert any("3 with no position in their names were read, and 2 lie under it" in n
               for n in row["prepared"])
    assert _no_leftovers(env)


def test_a_member_that_does_not_unpack_to_its_directory_entry_is_refused(env):
    a = np.arange(400, dtype="uint16").reshape(20, 20).astype("uint8")
    good = _gtiff(a, "uint8", 0, bounds=(1.0, 0.0, 2.0, 1.0))
    z = bytearray(_zip({"t/N01E001.tif": good,
                        "t/N01E002.tif": _gtiff(a, "uint8", 0, bounds=(2.0, 0.0, 3.0, 1.0))},
                       compress=zipfile.ZIP_STORED))
    at = z.index(good[200:216])                       # a byte inside the first member's data
    z[at] ^= 0xFF
    url = env["host"].put("damaged.zip", bytes(z))
    out = _submit("raster", _bare(url=url, mosaic=["t/N01E001.tif", "t/N01E002.tif"],
                                  legend={1: "A"}, outside_values=[0]))
    assert out["status"] == "declined"
    assert "does not unpack to what the zip's directory says" in out["problems"][0]
    assert _no_leftovers(env)


def test_mosaic_then_recode_in_one_contribution(env):
    """Two tiles coded 10-50 join, then are recoded to the 1-5 contract: the steps run in
    the registry's order (mosaic, then recode), each on the record."""
    west = np.full((20, 10), 10, "uint8")
    east = np.full((20, 10), 50, "uint8")
    urls = [env["host"].put(f"v_{n}.tif", _gtiff(a, "uint8", 0, bounds=b))
            for n, a, b in (("west", west, (103.0, 13.0, 103.01, 13.02)),
                            ("east", east, (103.01, 13.0, 103.02, 13.02)))]
    out = _submit("raster", {"layer": "vulnerability_toy_coded_tiles", "url": urls[0],
                             "mosaic": urls, "recode": {10: 1, 50: 5},
                             "title": "Coded tiles", "description": "classes coded 10-50",
                             "source": "s", "license": "CC0-1.0", "vintage": "2026-10",
                             "legend": {1: "Very Low", 5: "Very High"},
                             "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 5,
                                          "nodata": 0}})
    assert out["status"] == "approved", out
    row, arr, _, _ = _stored("vulnerability_toy_coded_tiles")
    steps = row["fetched"]["steps"]
    assert [st["op"] for st in steps] == ["fetch", "fetch", "mosaic", "recode"]
    assert [st["inputs"]["url"] for st in steps[:2]] == urls         # no clip: each tile whole
    assert [st["sha256"] for st in steps[:2]] == [
        _sha(env["host"].files["/" + u.rsplit("/", 1)[1]]) for u in urls]
    assert np.array_equal(arr, np.hstack([west * 0 + 1, east * 0 + 5]))
    assert all(t["read"] == "downloaded whole" for t in steps[2]["inputs"]["tiles"])
    assert _no_leftovers(env)


def test_a_tile_its_name_misplaces_makes_every_named_candidate_read(env):
    """The names guide, the headers decide: a tile named N10W085 that lies elsewhere
    breaks the naming rule the first header showed, so every candidate's header is read
    and the tile the rule would have skipped (named N09W083, lying at N10W085's place)
    joins."""
    a = np.ones((GMW_PX, GMW_PX), "uint8")
    members = {
        _gmw_name(10, -84): _gtiff(a, "uint8", 0, bounds=(-84, 9, -83, 10)),
        _gmw_name(10, -85): _gtiff(a * 0, "uint8", 0, bounds=(40, 9, 41, 10)),    # misnamed
        _gmw_name(9, -83): _gtiff(a, "uint8", 0, bounds=(-85, 9, -84, 10)),       # misnamed
        _gmw_name(10, -83): _gtiff(a, "uint8", 0, bounds=(-83, 9, -82, 10))}
    url = env["host"].put("misnamed.zip", _zip(members))
    out = _submit("raster", _gmw_year("landcover_crgmw_2020", url, clip=[-84.8, 9.2, -83.2, 9.8],
                                      mosaic="auto"))
    assert out["status"] == "approved", out
    row, arr, _, _ = _stored("landcover_crgmw_2020")
    inp = row["fetched"]["steps"][0]["inputs"]
    assert set(inp["picked"]["headers_read"]) == set(members)
    assert sorted(t["member"] for t in inp["tiles"]) == sorted(
        [_gmw_name(10, -84), _gmw_name(9, -83)])
    assert arr.shape == (36, 96) and (arr == 1).all()            # both halves are mangrove
    assert _no_leftovers(env)


def test_a_range_the_host_drops_is_asked_again_and_one_it_keeps_dropping_is_declined(
        env, monkeypatch):
    import requests
    raw, hand = _gmw()
    url = env["host"].put("gmw.zip", raw)
    real, dropped = mosaic._get_range_once, []

    def flaky(ctx, src, first, last, dest):            # a tile's piece: dropped twice, then sent
        if len(dropped) < 2:
            dropped.append(first)
            raise requests.ConnectionError("connection reset by peer")
        return real(ctx, src, first, last, dest)
    monkeypatch.setattr(mosaic, "_get_range_once", flaky)
    monkeypatch.setattr(mosaic, "FETCHERS", 1)
    out = _submit("raster", _gmw_year("landcover_crgmw_2020", url, clip=GMW_BOX, mosaic="auto"))
    assert out["status"] == "approved", out
    assert len(dropped) == 2 and np.array_equal(_stored("landcover_crgmw_2020")[1], hand)

    def dead(ctx, src, first, last, dest):
        raise requests.ConnectionError("connection reset by peer")
    monkeypatch.setattr(mosaic, "_get_range_once", dead)
    out = _submit("raster", _gmw_year("landcover_crgmw_2021", url, clip=GMW_BOX, mosaic="auto"))
    assert out["status"] == "declined"
    assert "did not answer (ConnectionError) while a tile was read — try again" in out["problems"][0]
    assert _no_leftovers(env)
