"""from_folder: a vector layer delivered as loose files in a shared Drive folder, assembled
on the server.

The sprint cases: the Costa Rica hydrology, coastline and protected-area layers and the
CATIE change maps were all shared as Google Drive FOLDERS of loose shapefile parts (.shp,
.shx, .dbf, .prj beside .sbn/.sbx indexes, .xml metadata and an editor's lock file), some
in subfolders ('CENTROAMERICA', 'Costa Rica/cuencas50mil_costeras2'), and two CATIE copies
without a .prj. Each was zipped by hand. Here the folder link goes in as it is: the
decline proposes from_folder, the server lists the folder (subfolders too), picks the
layer, fetches its parts and zips them, and what lands is what the hand-zipped file lands
as — with the listing, every part and the zip on the record. Beyond the asking pack: the
land-cover pack and a contributed pack both measure the assembled layer; loose part links
on any host go in as shapefile_parts.

Everything is synthetic and local: a loopback HTTP server in this file serves a Drive-like
folder listing and the files; no test touches the network."""

import hashlib
import io
import json
import re
import struct
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from rasterio.crs import CRS
from shapely.geometry import shape

from app.config import get_settings
from app.contrib import fetch_policy, identity, prepare, staging
from app.contrib.prepare import _drive, from_folder
from app.graph.geo import ingest, schema, vecio, vectors
from app.mcp import assemble, packs, registry, store
from app.risk import synthesis as risk

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
WGS84 = CRS.from_epsg(4326).to_wkt(version="WKT1_ESRI")
UTM16N = CRS.from_epsg(32616).to_wkt(version="WKT1_ESRI")
QGIS_UTM16N = CRS.from_epsg(32616).to_wkt()                  # what a .qpj holds
NICOYA = (-85.30, 9.93, -84.75, 10.30)
NICOYA_POLY = {"type": "Polygon", "coordinates": [[[NICOYA[0], NICOYA[1]], [NICOYA[2], NICOYA[1]],
               [NICOYA[2], NICOYA[3]], [NICOYA[0], NICOYA[3]], [NICOYA[0], NICOYA[1]]]]}


# --------------------------------------------------------------------------- a shapefile
def _shapefile(records, stype=5, rows=(), fields=(("NAME", "C", 40, 0),), encoding="utf-8"):
    """{'.shp', '.shx', '.dbf'} written from the format: each record a list of parts,
    each a list of (x, y)."""
    body, index, every = b"", b"", []
    off = 50
    for k, rec in enumerate(records):
        pts = [p for part in rec for p in part]
        every += pts
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        content = struct.pack("<i4d2i", stype, min(xs), min(ys), max(xs), max(ys), len(rec), len(pts))
        at = 0
        for part in rec:
            content += struct.pack("<i", at)
            at += len(part)
        for x, y in pts:
            content += struct.pack("<dd", x, y)
        body += struct.pack(">ii", k + 1, len(content) // 2) + content
        index += struct.pack(">ii", off, len(content) // 2)
        off += 4 + len(content) // 2
    xs, ys = [p[0] for p in every], [p[1] for p in every]

    def head(n):
        return (struct.pack(">i", 9994) + b"\x00" * 20 + struct.pack(">i", n // 2)
                + struct.pack("<ii", 1000, stype)
                + struct.pack("<4d", min(xs), min(ys), max(xs), max(ys)) + b"\x00" * 32)
    hlen, rlen = 32 + 32 * len(fields) + 1, 1 + sum(f[2] for f in fields)
    dbf = bytes([3, 126, 10, 20]) + struct.pack("<IHH", len(records), hlen, rlen) + b"\x00" * 20
    for name, t, ln, dec in fields:
        dbf += name.encode().ljust(11, b"\x00") + t.encode() + b"\x00" * 4 + bytes([ln, dec]) + b"\x00" * 14
    dbf += b"\x0d"
    for k in range(len(records)):
        row = rows[k] if k < len(rows) else {}
        dbf += b" "
        for name, t, ln, _ in fields:
            cell = str(row.get(name, "")).encode(encoding)[:ln]
            dbf += cell.rjust(ln) if t in "NF" else cell.ljust(ln)
    dbf += b"\x1a"
    return {".shp": head(100 + len(body)) + body, ".shx": head(100 + len(index)) + index, ".dbf": dbf}


def _box(x0, y0, x1, y1):
    return [[(x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0)]]          # clockwise: an outer ring


def _basins(n=3, x0=-85.2, y0=10.0, d=0.05):
    recs = [_box(x0 + i * d, y0, x0 + (i + 1) * d - 0.005, y0 + d) for i in range(n)]
    return _shapefile(recs, 5, [{"NAME": f"Cuenca {i}"} for i in range(n)])


def _rivers():
    recs = [[[(-85.2 + i * 0.03, 10.0), (-85.18 + i * 0.03, 10.08), (-85.15 + i * 0.03, 10.1)]]
            for i in range(4)]
    return _shapefile(recs, 3, [{"NAME": n} for n in ("RIO TEMPISQUE", "RIO BEBEDERO",
                                                      "QUEBRADA X", "SIN NOMBRE")])


def _catie(n=6):
    """UTM 16N polygons on the CATIE grid's corner (30 m cells)."""
    x0, y0 = 686625.0, 1098915.0
    recs = [_box(x0 + i * 900, y0, x0 + i * 900 + 600, y0 + 600) for i in range(n)]
    return _shapefile(recs, 5, [{"GRIDCODE": str(i % 3 + 1)} for i in range(n)],
                      fields=(("GRIDCODE", "N", 10, 0),))


def _with(parts, stem, **extra):
    """{name: bytes} of a shapefile's files as a folder holds them."""
    return {f"{stem}{ext}": data for ext, data in {**parts, **extra}.items()}


def _hand_zip(files: dict) -> bytes:
    """What a person made by hand: the parts the reader needs, zipped flat."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            if re.search(r"\.(shp|shx|dbf|prj|cpg)$", name, re.I):
                z.writestr(name, data)
    return buf.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- a Drive
class _Drive:
    """Folders and files with Drive-like ids, a listing page per folder shaped like
    Drive's embedded folder view, and the files by id."""

    def __init__(self):
        self.pages, self.files, self.log, self.port, self.n = {}, {}, [], None, 0
        self.stall = set()             # file names that stall once before answering

    def _id(self, kind):
        self.n += 1
        return f"{kind}{self.n:03d}" + "x" * 20

    def folder(self, tree: dict) -> str:
        """A folder from {name: bytes | {subtree}}; its share link. 'x#2' is a second file
        named x (Drive keeps both)."""
        return f"https://drive.google.com/drive/folders/{self._add(tree)}?usp=sharing"

    def _add(self, tree):
        fid = self._id("F")
        rows = []
        for name, v in tree.items():
            name = name.split("#")[0]
            if isinstance(v, dict):
                sub = self._add(v)
                rows.append((sub, name, f"https://drive.google.com/drive/folders/{sub}"))
            else:
                eid = self._id("f")
                self.files[eid] = (name, v)
                rows.append((eid, name, f"https://drive.google.com/file/d/{eid}/view?usp=drive_web"))
        entries = "".join(
            f'<div class="flip-entry" id="entry-{i}" tabindex="0" role="link"><div class="flip-entry-info">'
            f'<a href="{href}" target="_blank"><div class="flip-entry-visual"></div>'
            f'<div class="flip-entry-title">{name.replace("&", "&amp;")}</div></a></div>'
            f'<div class="flip-entry-last-modified"><div>4/17/24</div></div></div>'
            for i, name, href in rows)
        self.pages[fid] = (f'<!DOCTYPE html><html><head><title>folder</title></head><body class="flip-embedded">'
                           f'<div class="flip-entries">{entries}</div></body></html>').encode()
        return fid

    def asked(self):
        return [p for p in self.log]


def _handler(d):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            d.log.append(path)
            kind, _, key = path.strip("/").partition("/")
            if kind == "listing" and key in d.pages:
                body, ctype, name = d.pages[key], "text/html; charset=utf-8", None
            elif kind == "file" and key in d.files:
                name, body = d.files[key]
                ctype = "application/octet-stream"
                if name in d.stall:
                    d.stall.discard(name)
                    time.sleep(1.0)
            elif kind == "plain" and key in d.files:          # a host that is not Drive
                name, body = key, d.files[key]
                ctype = "application/octet-stream"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            if name:
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):   # the client gave up on a stall
                pass
    return Handler


@pytest.fixture
def drive(monkeypatch):
    d = _Drive()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(d))
    d.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(_drive, "LISTING", f"http://127.0.0.1:{d.port}/listing/{{id}}")
    monkeypatch.setattr(_drive, "FILE", f"http://127.0.0.1:{d.port}/file/{{id}}")
    yield d
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def env(tmp_path, monkeypatch, drive):
    """A hermetic platform: an empty land-cover catalog, auto-approve, the real fetch
    policy with the loopback host allowed, and a hermetic store."""
    s = get_settings()
    cache = tmp_path / "cache"
    tdir = cache / "tiffs"
    tdir.mkdir(parents=True)
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "tiffs.yml").write_text(yaml.safe_dump({}))
    (conf / "raster_schema.yml").write_text(yaml.safe_dump({
        "defaults": {"crs": "EPSG:4326", "float_tol": 0.0001},
        "roles": schema._shipped()["roles"], "layers": {}}))
    (conf / "risk_l2.yml").write_text(yaml.safe_dump({"class_max": 5, "weights": {}}))
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
    yield {"tmp": tmp_path, "cache": cache, "drive": drive}
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
    strays = [p for d in (env["cache"] / "tiffs", vectors._dir()) for p in d.glob("staged-tmp-*")]
    return (not prep.exists() or not list(prep.iterdir())) and not strays


def _layer(layer, url, **over):
    return {"layer": layer, "url": url, "title": f"{layer} (synthetic)",
            "description": "shaped like the sprint's Drive folder", "source": "test fixture",
            "license": "unstated", "vintage": "unstated", "countries": ["Costa Rica"], **over}


def _stored(layer) -> bytes:
    with open(vectors.master_path(vectors.landed()[layer]), "rb") as f:
        return f.read()


def _km2(raw: bytes) -> float:
    return sum(vectors.area_km2(shape(f["geometry"])) for f in json.loads(raw)["features"])


# --------------------------------------------------------------------------- sprint shapes
def _hydrosheds_folder():
    """L20: a root of two subfolders; 'Costa Rica' holds two shapefiles, each in its own
    subfolder (one with a space in its name), 'CENTROAMERICA' the basins and a PDF."""
    ca, coast, national = _basins(5, -86.0, 9.5, 0.2), _basins(3), _basins(2, -85.0, 10.1, 0.08)
    ca_files = _with(ca, "ca_bas_30s_beta_ud", **{".prj": WGS84.encode(), ".sbn": b"sbn",
                                                  ".sbx": b"sbx", ".shp.xml": b"<xml/>"})
    coast_files = _with(coast, "cuencas50mil_costeras2", **{".prj": WGS84.encode(), ".sbn": b"s",
                                                            ".sbx": b"s", ".shp.xml": b"<x/>"})
    nat_files = _with(national, "cuencas 50mil_geograf", **{".prj": WGS84.encode(), ".shp.xml": b"<x/>"})
    tree = {"CENTROAMERICA": {"ca_bas_30s_beta.shp.htm": b"<html/>", **ca_files,
                              "HydroSHEDS_TechDoc_v10.pdf": b"%PDF-1.4"},
            "Costa Rica": {"cuencas 50mil_geograf": nat_files,
                           "cuencas50mil_costeras2": coast_files}}
    return tree, coast_files, nat_files


def _catie_folder():
    """L01: capas/MANGLE_NICOYA_CI holds cambios_8514 (with its .prj, UTM 16N), and its
    copy c8514_copy without one; rasters, a GeoPackage and layouts beside them."""
    cambios, copy = _catie(6), _catie(4)
    tree = {"capas": {"MANGLE_NICOYA_CI": {
        **_with(cambios, "cambios_8514", **{".prj": UTM16N.encode(), ".sbn": b"s", ".sbx": b"s",
                                             ".shp.xml": b"<x/>"}),
        **_with(copy, "c8514_copy", **{".sbn": b"s", ".sbx": b"s"}),
        "c8514_dis.shp.BRENES-PC.5124.6708.sr.lock": b"",
        "CAMBIOS8514.img": b"HFA", "mangrove_56.rrd": b"rrd"}},
        "MANGLE_analisis_cambios_layouts": {"CAMBIOS_85_14.jpg": b"\xff\xd8"},
        "Dinamica.pdf": b"%PDF-1.4"}
    return tree, cambios, copy


# --------------------------------------------------------------------------- 1. the registry
def test_the_op_is_derived_from_its_module_and_its_fields_reach_the_vector_form():
    """No list names the op: the registry, the vector form and contribute_submit's
    description derive from the module; its fields are not a raster's."""
    assert from_folder in prepare.ops("vector") and from_folder not in prepare.ops("raster")
    assert from_folder.STEP == "assemble" and from_folder.NEEDS_INPUT is False
    for f, text in from_folder.FIELDS.items():
        assert staging.VECTOR_FIELDS["optional"][f] == text
        assert f not in staging._RASTER_KNOWN
    assert "ASK them, never guess" in from_folder.FIELDS["shapefile_crs"]
    assert "from_folder (true when `url` is a Google Drive FOLDER link" in staging.describe_submit()


# --------------------------------------------------------------------------- 2. the decline
def test_a_folder_link_as_delivered_proposes_from_folder_and_fetches_nothing(env, log):
    """The sheet's folder link as a participant gives it: declined before any fetch, with
    the field that resolves it. A raster folder hears from_folder is for vectors."""
    tree, _, _ = _hydrosheds_folder()
    url = env["drive"].folder(tree)
    out = _submit("vector", _layer("cr_cuencas_costeras", url))
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and len(out["problems"]) == 1
    assert out["problems"][0].startswith("this is a Google Drive FOLDER link")
    assert "or resubmit the same folder link with from_folder: true" in out["problems"][0]
    assert "add layer_name: '<the shapefile's name without .shp>'" in out["problems"][0]
    assert env["drive"].log == []
    ras = _submit("raster", {"layer": "landcover_x_2014", "url": url, "title": "t"})
    assert any(p.startswith("this is a Google Drive FOLDER link") for p in ras["problems"])
    assert not any("from_folder" in p for p in ras["problems"])
    ras = _submit("raster", {"layer": "landcover_x_2014", "url": url, "from_folder": True})
    assert ("from_folder prepares a vector contribution (from_folder); it does not apply to a "
            "raster — drop from_folder") in ras["problems"]
    assert env["drive"].log == []


# --------------------------------------------------------------------------- 3. the real path
def test_from_folder_walks_subfolders_and_lands_what_the_hand_zip_lands(env, monkeypatch, log):
    """L20, the coastal watersheds two subfolders down: preview stores nothing; the
    submission lands the same GeoJSON, byte for byte, as the hand-zipped parts; the
    listing, each part and the zip are on the landed row; the index, metadata and htm
    files are left out; land-cover and a contributed pack measure it."""
    tree, coast_files, _ = _hydrosheds_folder()
    url = env["drive"].folder(tree)
    m = _layer("cr_cuencas_costeras", url, from_folder=True, layer_name="cuencas50mil_costeras2")
    pv = _submit("vector", {**m, "preview": True})
    log("OUTPUT", json.dumps({k: pv["would_land"][k] for k in ("features", "km2", "extent")}))
    assert pv["status"] == "preview", pv
    assert pv["would_land"]["features"] == 3 and pv["would_land"]["km2"] > 0
    assert not vectors.landed() and not store.list_contributions() and _no_leftovers(env)
    asked = len(env["drive"].log)
    out = _submit("vector", m)
    assert out["status"] == "approved", out
    hand, _ = vecio.to_geojson(_hand_zip(coast_files), "cuencas50mil_costeras2.zip")
    assert _stored("cr_cuencas_costeras") == hand
    row = vectors.landed()["cr_cuencas_costeras"]
    steps = row["fetched"]["steps"]
    log("OUTPUT", json.dumps(steps)[:600])
    assert [s["op"] for s in steps] == ["list_folder", "fetch", "fetch", "fetch", "fetch", "from_folder"]
    assert steps[0]["inputs"]["folders"] == 5 and steps[0]["inputs"]["url"] == url
    fetched = {s["inputs"]["filename"]: s["sha256"] for s in steps if s["op"] == "fetch"}
    assert fetched == {n: _sha(coast_files[n]) for n in fetched}
    assert sorted(fetched) == sorted(f"cuencas50mil_costeras2{e}" for e in (".shp", ".shx", ".dbf", ".prj"))
    last = steps[-1]
    assert last["inputs"]["picked"] == "Costa Rica/cuencas50mil_costeras2/cuencas50mil_costeras2.shp"
    assert last["inputs"]["left_out"] == ["cuencas50mil_costeras2.sbn", "cuencas50mil_costeras2.sbx",
                                          "cuencas50mil_costeras2.shp.xml"]
    assert last["sha256"] == pv["would_land"]["fetched"]["steps"][-1]["sha256"]   # same parts, same zip
    assert row["fetched"]["sha256"] == _sha(_stored("cr_cuencas_costeras"))
    assert row["fetched"]["prepared"].startswith("on the server: list_folder -> fetch")
    assert row["prepared"][0].startswith("assembled on the server from the Google Drive folder")
    assert "left out: cuencas50mil_costeras2.sbn" in row["prepared"][0]
    assert len(env["drive"].log) > asked and _no_leftovers(env)
    # beyond the asking pack: land-cover and a contributed pack both measure it
    want = round(_km2(hand), 2)
    p = assemble.assemble(pack="land-cover", place="Gulf of Nicoya", geometry=NICOYA_POLY,
                          vectors=["cr_cuencas_costeras"])
    assert p["status"] == "ok", p.get("note")
    got = p["vectors"]["cr_cuencas_costeras"]
    log("OUTPUT", json.dumps(got)[:300])
    assert round(got["total"], 2) == want and got["unit"] == "km2"
    _toy_pack(monkeypatch)
    t = assemble.assemble(pack="toy", place="Toy gulf", vectors=["cr_cuencas_costeras"])
    assert t["status"] == "ok", t.get("note")
    assert round(t["vectors"]["cr_cuencas_costeras"]["total"], 2) == want
    cit = next(c for c in t["citations"] if c.get("layer") == "cr_cuencas_costeras")
    assert cit["fetched"]["steps"][-1]["op"] == "from_folder"


def _toy_pack(monkeypatch):
    """A contributed pack: its own area, nothing about vectors in it."""
    def gather(target, focus, trace, extras):
        stats = {"queries": None, "_area": {"name": "Toy gulf", "how": "test polygon",
                                            "geometry": NICOYA_POLY}}
        return ([{"n": 1, "kind": "measurement", "retrieval": "computed-at-pack-time",
                  "source": "toy", "title": "reading", "text": "42 units."}], [], stats)
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy", "version": "v0", "target_keys": ("place",),
        "target_doc": {"place": "site"}, "gather": gather,
        "sections": lambda: ["## Reading"], "corpus": None,
        "default_focus": lambda t: t.get("place", "")})


def test_several_layers_are_named_and_a_path_inside_the_folder_picks_one(env, log):
    """With no layer_name the decline lists the folder's layers (never the PDF) and asks
    which; a subfolder path picks the national watersheds, whose name has a space."""
    tree, _, nat_files = _hydrosheds_folder()
    url = env["drive"].folder(tree)
    out = _submit("vector", _layer("cr_cuencas", url, from_folder=True))
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined"
    assert re.search(r"holds 3 layers: CENTROAMERICA/ca_bas_30s_beta_ud \(shapefile\), Costa Rica/"
                     r"cuencas 50mil_geograf/cuencas 50mil_geograf \(shapefile\), Costa Rica/"
                     r"cuencas50mil_costeras2/cuencas50mil_costeras2 \(shapefile\) — ASK the "
                     r"contributor which one", out["problems"][0])
    assert "TechDoc" not in out["problems"][0] and _no_leftovers(env)
    out = _submit("vector", _layer("cr_cuencas", url, layer_name="cuencas"))
    assert out["status"] == "declined"
    out = _submit("vector", _layer("cr_cuencas", url, from_folder=True, layer_name="cuencas"))
    assert "no shapefile named 'cuencas' in the folder" in out["problems"][0]
    assert "it holds the shapefiles ca_bas_30s_beta_ud, cuencas 50mil_geograf" in out["problems"][0]
    out = _submit("vector", _layer("cr_cuencas", url, from_folder="Costa Rica/nowhere"))
    assert "holds no subfolder or file 'Costa Rica/nowhere'; at its top it holds: CENTROAMERICA, Costa Rica" \
        in out["problems"][0]
    out = _submit("vector", _layer("cr_cuencas", url, from_folder="Costa Rica/cuencas 50mil_geograf"))
    assert out["status"] == "approved", out
    hand, _ = vecio.to_geojson(_hand_zip(nat_files), "x.zip")
    assert _stored("cr_cuencas") == hand
    assert vectors.landed()["cr_cuencas"]["features"] == 2 and _no_leftovers(env)


def test_a_shapefile_without_its_prj_is_declined_with_its_neighbours_crs_then_lands(env, log):
    """CATIE's c8514_copy came without a .prj (the hand zip borrowed cambios_8514's).
    The decline names what the .prj beside it says and asks; with shapefile_crs the
    layer lands as the hand zip with the borrowed .prj does. A shapefile_crs that
    contradicts the layer's own .prj is declined."""
    tree, cambios, copy = _catie_folder()
    url = env["drive"].folder(tree)
    m = _layer("cr_catie_c8514_copy", url, from_folder=True, layer_name="c8514_copy")
    out = _submit("vector", m)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined"
    p = out["problems"][0]
    assert p.startswith("capas/MANGLE_NICOYA_CI/c8514_copy.shp came without a .prj, so its "
                        "coordinate system is unknown — ASK the contributor")
    assert ("(the folder's other .prj files: capas/MANGLE_NICOYA_CI/cambios_8514.prj names WGS 84 / "
            "UTM zone 16N (EPSG:32616) — if this layer is in the same, that is its code)") in p
    assert _no_leftovers(env)
    out = _submit("vector", {**m, "shapefile_crs": "EPSG:32616"})
    assert out["status"] == "approved", out
    hand, _ = vecio.to_geojson(_hand_zip(_with(copy, "c8514_copy", **{".prj": UTM16N.encode()})), "x.zip")
    got = _stored("cr_catie_c8514_copy")
    assert json.loads(got)["features"] == json.loads(hand)["features"]
    row = vectors.landed()["cr_catie_c8514_copy"]
    assert row["fetched"]["steps"][-1]["inputs"]["written"] == ["c8514_copy.prj"]
    assert row["fetched"]["steps"][-1]["inputs"]["shapefile_crs"] == "EPSG:32616"
    assert any("the contributor's shapefile_crs" in n for n in row["prepared"])
    out = _submit("vector", _layer("cr_catie_8514", url, from_folder=True, layer_name="cambios_8514",
                                   shapefile_crs="EPSG:5367"))
    assert out["status"] == "declined"
    assert "cambios_8514.prj names" in out["problems"][0] and "shapefile_crs says" in out["problems"][0]
    out = _submit("vector", _layer("cr_catie_8514", url, from_folder=True, layer_name="cambios_8514"))
    assert out["status"] == "approved", out
    hand, _ = vecio.to_geojson(_hand_zip(_with(cambios, "cambios_8514", **{".prj": UTM16N.encode()})), "x.zip")
    assert _stored("cr_catie_8514") == hand and _no_leftovers(env)


def test_a_qpj_stands_in_for_a_missing_prj_and_a_cpg_rides_along(env):
    """Life zones (QGIS export: .qpj beside .prj) and WDPA (.cpg UTF-8, .qmd): the .cpg is
    zipped, the .qmd and a .qpj next to a .prj left out; with no .prj, the .qpj is read."""
    parts = _shapefile([_box(-85.2, 10.0, -85.1, 10.1)], 5, [{"NAME": "Área Ñ"}])
    tree = {"asp_costa_rica.cpg": b"UTF-8", **_with(parts, "asp_costa_rica",
                                                     **{".prj": WGS84.encode(), ".qmd": b"<q/>",
                                                        ".qpj": QGIS_UTM16N.encode()})}
    out = _submit("vector", _layer("cr_asp", env["drive"].folder(tree), from_folder=True))
    assert out["status"] == "approved", out
    row = vectors.landed()["cr_asp"]
    assert sorted(row["fetched"]["steps"][-1]["inputs"]["members"]) == [
        "asp_costa_rica.cpg", "asp_costa_rica.dbf", "asp_costa_rica.prj", "asp_costa_rica.shp",
        "asp_costa_rica.shx"]
    assert row["fetched"]["steps"][-1]["inputs"]["left_out"] == ["asp_costa_rica.qmd", "asp_costa_rica.qpj"]
    assert json.loads(_stored("cr_asp"))["features"][0]["properties"]["NAME"] == "Área Ñ"
    utm = _catie(2)
    tree = _with(utm, "zonasdevida", **{".qpj": QGIS_UTM16N.encode()})
    out = _submit("vector", _layer("cr_zonas", env["drive"].folder(tree), from_folder=True))
    assert out["status"] == "approved", out
    row = vectors.landed()["cr_zonas"]
    assert any("its .qpj (QGIS's record of the CRS, WGS 84 / UTM zone 16N (EPSG:32616)) was "
               "written as zonasdevida.prj" in n for n in row["prepared"])
    hand, _ = vecio.to_geojson(_hand_zip(_with(utm, "zonasdevida", **{".prj": UTM16N.encode()})), "x.zip")
    assert json.loads(_stored("cr_zonas"))["features"] == json.loads(hand)["features"]
    assert _no_leftovers(env)


def test_a_lone_vector_file_in_a_folder_is_fetched_as_it_is(env):
    """L02's shape: a folder of one GeoJSON among a picture and a report — the file goes in."""
    fc = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"c": 1},
          "geometry": {"type": "Polygon", "coordinates": [[[-85.2, 10.0], [-85.1, 10.0],
                                                           [-85.1, 10.1], [-85.2, 10.0]]]}}]}
    tree = {"mapa_ecosistemas_mangle.png": b"\x89PNG", "Memoria.docx": b"PK",
            "base topografica": {"mapa_manglar_2021.geojson": json.dumps(fc).encode()}}
    out = _submit("vector", _layer("cr_manglar", env["drive"].folder(tree), from_folder=True))
    assert out["status"] == "approved", out
    row = vectors.landed()["cr_manglar"]
    assert [s["op"] for s in row["fetched"]["steps"]] == ["list_folder", "fetch", "from_folder"]
    assert row["fetched"]["steps"][-1]["inputs"]["picked"] == "base topografica/mapa_manglar_2021.geojson"
    assert row["features"] == 1 and _no_leftovers(env)


# --------------------------------------------------------------------------- 4. any host
def test_shapefile_parts_assemble_loose_part_links_on_any_host(env, log):
    """CR_rios's parts as separate links on a host that is not Drive: the .shp alone is
    declined with shapefile_parts proposed (no folder to list); listing the parts lands
    the hand zip's layer; a link that is no part is declined."""
    d = env["drive"]
    parts = _with(_rivers(), "CR_rios", **{".prj": WGS84.encode(), ".sbn": b"sbn"})
    for name, data in parts.items():
        d.files[name] = data
    link = {n: f"http://127.0.0.1:{d.port}/plain/{n}" for n in parts}
    out = _submit("vector", _layer("cr_rios", link["CR_rios.shp"]))
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and "one part of a shapefile" in out["problems"][0]
    assert ("or resubmit with shapefile_parts: [<the .shp link>, <the .shx link>, <the .dbf "
            "link>, <the .prj link>]") in out["problems"][0]
    assert "from_folder" not in out["problems"][0]
    others = [link[n] for n in ("CR_rios.shx", "CR_rios.dbf", "CR_rios.prj", "CR_rios.sbn")]
    out = _submit("vector", _layer("cr_rios", link["CR_rios.shp"], shapefile_parts=others))
    assert out["status"] == "approved", out
    hand, _ = vecio.to_geojson(_hand_zip(parts), "CR_rios.zip")
    assert _stored("cr_rios") == hand
    row = vectors.landed()["cr_rios"]
    assert row["fetched"]["steps"][-1]["inputs"]["left_out"] == ["CR_rios.sbn"]
    assert row["fetched"]["steps"][-1]["inputs"]["links"][0] == link["CR_rios.shp"]
    d.files["LEAME.txt"] = b"readme"
    out = _submit("vector", _layer("cr_rios_2", link["CR_rios.shp"],
                                   shapefile_parts=[*others, f"http://127.0.0.1:{d.port}/plain/LEAME.txt"]))
    assert "is 'LEAME.txt', not a part of a shapefile" in out["problems"][0]
    out = _submit("vector", _layer("cr_rios_2", link["CR_rios.shp"], shapefile_parts=[link["CR_rios.shx"]]))
    assert "holds CR_rios.shp but no CR_rios.dbf" in out["problems"][0]
    out = _submit("vector", _layer("cr_rios_2", link["CR_rios.shp"],
                                   shapefile_parts=[link["CR_rios.shx"], link["CR_rios.dbf"]]))
    assert out["problems"][0].startswith("CR_rios.shp came without a .prj")
    assert out["problems"][0].endswith("Or add the link of the layer's .prj to shapefile_parts")
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 5. declines
def test_what_cannot_be_assembled_is_declined_in_words(env, monkeypatch):
    d = env["drive"]
    rivers = _rivers()
    m = lambda url, **k: _layer("cr_x", url, from_folder=True, **k)          # noqa: E731
    out = _submit("vector", m("https://drive.google.com/drive/folders/Fnotthere00000000000"))
    assert out["problems"] == [_drive.NOT_SHARED.format(
        url="https://drive.google.com/drive/folders/Fnotthere00000000000")]
    no_dbf = {k: v for k, v in _with(rivers, "CR_rios", **{".prj": WGS84.encode()}).items()
              if not k.endswith(".dbf")}
    out = _submit("vector", m(d.folder(no_dbf)))
    assert "holds CR_rios.shp but no CR_rios.dbf" in out["problems"][0]
    twice = _with(rivers, "CR_rios", **{".prj": WGS84.encode()})
    out = _submit("vector", m(d.folder({**twice, "CR_rios.shp#2": twice["CR_rios.shp"]})))
    assert "holds two files named 'CR_rios.shp' — Drive keeps both" in out["problems"][0]
    out = _submit("vector", m(d.folder({**twice, "CR_rios.dbf#2": twice["CR_rios.dbf"]})))
    assert "holds two files named 'CR_rios.dbf' — Drive keeps both" in out["problems"][0]
    out = _submit("vector", m(d.folder({"notes.txt": b"x", "plot.jpg": b"y"})))
    assert "holds no shapefile (.shp) and no GeoPackage, GeoJSON or zip" in out["problems"][0]
    out = _submit("vector", _layer("cr_x", "https://hub.test/rios.zip", from_folder=True))
    assert any("url must be the folder's link" in p for p in out["problems"])
    out = _submit("vector", _layer("cr_x", "https://drive.google.com/drive/folders/Fabc00000000000000000",
                                   from_folder=True, shapefile_crs="CRTM05 please"))
    assert any("shapefile_crs 'CRTM05 please' is no coordinate system" in p for p in out["problems"])
    out = _submit("vector", _layer("cr_x", "https://hub.test/rios.zip", shapefile_crs="EPSG:5367"))
    assert any("drop shapefile_crs" in p for p in out["problems"])
    assert _no_leftovers(env)


def test_the_listing_and_the_parts_go_through_the_fetch_policy_and_the_budgets(env, monkeypatch):
    """The listing is a fetch like any other (a private address is refused when the
    policy says so); the input budget, the vertex budget and the folder depth hold."""
    d = env["drive"]
    url = d.folder(_with(_rivers(), "CR_rios", **{".prj": WGS84.encode()}))
    s = get_settings()
    monkeypatch.setattr(s, "grp_contrib_allow_private_hosts", False)
    out = _submit("vector", _layer("cr_rios", url, from_folder=True))
    assert "private, loopback or link-local" in out["problems"][0] and d.log == []
    monkeypatch.setattr(s, "grp_contrib_allow_private_hosts", True)
    monkeypatch.setattr(s, "grp_prep_max_vertices", 5)
    out = _submit("vector", _layer("cr_rios", url, from_folder=True))
    assert "the shapefile CR_rios has 12 vertices, over the 5" in out["problems"][0]
    monkeypatch.setattr(s, "grp_prep_max_vertices", 10 ** 6)
    monkeypatch.setattr(s, "grp_prep_max_input_bytes", 300)
    out = _submit("vector", _layer("cr_rios", url, from_folder=True))
    assert "the platform downloads to prepare one contribution" in out["problems"][0]
    monkeypatch.setattr(s, "grp_prep_max_input_bytes", 1 << 30)
    monkeypatch.setattr(_drive, "MAX_DEPTH", 1)
    deep = d.folder({"a": {"b": _with(_rivers(), "CR_rios", **{".prj": WGS84.encode()})}})
    out = _submit("vector", _layer("cr_rios", deep, from_folder=True))
    assert "nests subfolders deeper than 1 levels (a/b)" in out["problems"][0]
    assert _no_leftovers(env)


def test_a_part_that_stalls_once_is_fetched_again(env, monkeypatch):
    """Drive stalled on 2 of ~40 part downloads in the live run: one stall is retried."""
    d = env["drive"]
    url = d.folder(_with(_rivers(), "CR_rios", **{".prj": WGS84.encode()}))
    monkeypatch.setattr(fetch_policy, "TIMEOUT", (2, 0.4))
    d.stall = {"CR_rios.dbf"}
    out = _submit("vector", _layer("cr_rios", url, from_folder=True))
    assert out["status"] == "approved", out
    dbf = [s for s in vectors.landed()["cr_rios"]["fetched"]["steps"]
           if s["op"] == "fetch" and s["inputs"]["filename"] == "CR_rios.dbf"]
    assert len(dbf) == 1 and sum(p.startswith("/file/") for p in d.log) == 5
    assert _no_leftovers(env)


def test_the_listing_page_is_read_as_drive_writes_it():
    """The embedded view as Drive served it on 2026-10-19 (CR_rios's folder, trimmed)."""
    page = ('<div class="flip-entries"><div class="flip-entry" id="entry-1xf_vsvnAxzlPsDpkKDaoBZKj6EdK69lB" '
            'tabindex="0" role="link"><div class="flip-entry-info"><a href="https://drive.google.com/drive/'
            'folders/1xf_vsvnAxzlPsDpkKDaoBZKj6EdK69lB" target="_blank"><div class="flip-entry-visual">'
            '<div class="flip-entry-visual-card"><div class="flip-entry-icon"><div aria-label="Folder" '
            'class="icon-color-1 drive-sprite-folder-grid-shared-icon"></div></div></div></div>'
            '<div class="flip-entry-title">CENTROAMERICA</div></a></div></div>'
            '<div class="flip-entry" id="entry-1SESDZp_7wceoUkfcvHNbHexcQfN_DBBT" tabindex="0" role="link">'
            '<div class="flip-entry-info"><a href="https://drive.google.com/file/d/1SESDZp_7wceoUkfcvHNbHexcQfN_DBBT/'
            'view?usp=drive_web" target="_blank"><div class="flip-entry-title">R&iacute;os &amp; CR_rios.dbf</div>'
            '</a></div></div></div>')
    assert _drive.entries(page) == [("1xf_vsvnAxzlPsDpkKDaoBZKj6EdK69lB", "CENTROAMERICA", True),
                                    ("1SESDZp_7wceoUkfcvHNbHexcQfN_DBBT", "Ríos & CR_rios.dbf", False)]
    for link in ("https://drive.google.com/drive/folders/18PDQMN2EoOqZKceZGx894uSIv5vGoG4l?usp=sharing",
                 "https://drive.google.com/drive/u/0/folders/18PDQMN2EoOqZKceZGx894uSIv5vGoG4l",
                 "https://drive.google.com/embeddedfolderview?id=18PDQMN2EoOqZKceZGx894uSIv5vGoG4l"):
        assert _drive.folder_id(link) == "18PDQMN2EoOqZKceZGx894uSIv5vGoG4l"
    assert _drive.folder_id("https://drive.google.com/file/d/1SESDZp_7wceoUkfcvHNbHexcQfN_DBBT/view") is None
    assert _drive.folder_id("https://example.org/drive/folders/18PDQMN2EoOqZKceZGx894uSIv5vGoG4l") is None
