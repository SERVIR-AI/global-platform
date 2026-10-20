"""The vector clip op: a layer too big or too wide for the cap, cut to the features of
an area — and a service's layer read whole, page by page.

The sprint cases, in synthetic form: HDX's boundaries come as one zip of every admin
level (one level was zipped on its own by hand); Global Mangrove Watch v4.1 is one
gzipped GeoPackage of the world (filtered to Costa Rica by hand); the soils
FeatureServer stops at 1,000 of its 1,088 features (exceededTransferLimit); the MAATE
GeoServer serves its land cover for a box built by hand. Each goes in from its own link
with `clip` or `whole: true`; the decline for a truncated export proposes the field;
what lands equals what the hand-prepared file gives; every step (fetch, gunzip, clip,
the pages) and sha256 is on the record. Beyond the asking pack: a point layer clipped out
of a big GeoJSON is counted by the risk pack, and a polygon layer is measured by it.

Everything is local: a loopback HTTP server in this file serves the files and plays a
FeatureServer and a GeoServer WFS; no test touches the network."""

import gzip
import hashlib
import io
import json
import sqlite3
import struct
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import numpy as np
import pytest
import yaml
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.warp import transform_geom
from shapely import wkb
from shapely.geometry import Point, box, mapping, shape

from app.config import get_settings
from app.contrib import identity, prepare, staging
from app.contrib.prepare import _features, clip
from app.graph.geo import ingest, schema, vecio, vectors
from app.mcp import assemble, registry, store
from app.risk import synthesis as risk

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
B = (103.0, 13.0, 103.02, 13.02)              # the risk world: 20 x 20 cells of 0.001 deg
RISK_POLY = {"type": "Polygon", "coordinates": [[[103.005, 13.005], [103.015, 13.005],
             [103.015, 13.015], [103.005, 13.015], [103.005, 13.005]]]}
GULF = [-85.3, 9.9, -84.7, 10.3]               # a Gulf of Nicoya box
WGS84_PRJ = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,'
             '298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')


# --------------------------------------------------------------------------- a host
class _Host:
    """Static files, and apps answering a path (a FeatureServer, a WFS)."""

    def __init__(self):
        self.files, self.apps, self.log, self.port = {}, {}, [], None

    def put(self, name, data):
        self.files["/" + name] = data
        return f"http://127.0.0.1:{self.port}/{name}"

    def app(self, prefix, fn):
        self.apps[prefix] = fn
        return f"http://127.0.0.1:{self.port}{prefix}"

    def asked(self, name):
        return [q for path, q in self.log if path == "/" + name.lstrip("/")]


def _handler(host):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body):
            u = urlparse(self.path)
            q = dict(parse_qsl(u.query, keep_blank_values=True))
            host.log.append((u.path, q))
            ctype, data, code = "application/octet-stream", None, 200
            for prefix, fn in host.apps.items():
                if u.path.startswith(prefix):
                    code, ctype, data = fn(u.path, q)
                    break
            else:
                data = host.files.get(u.path)
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if ctype == "application/octet-stream":
                self.send_header("Content-Disposition", f'attachment; filename="{u.path.rsplit("/", 1)[-1]}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if body:
                try:
                    self.wfile.write(data)
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


def _gtiff(arr, dtype, nodata=None, bounds=B):
    h, w = arr.shape
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=w, height=h, count=1, dtype=dtype, crs="EPSG:4326",
                     transform=from_bounds(*bounds, w, h), nodata=nodata) as d:
            d.write(arr.astype(dtype), 1)
        return mf.read()


@pytest.fixture
def env(tmp_path, monkeypatch, host):
    """A hermetic platform: the risk pack's toy world (a flood hazard and a 1-5
    vulnerability layer near 103 E, 13 N), auto-approve, the real fetch policy with the
    loopback host allowed, a 2 MiB stored cap, and a hermetic store."""
    s = get_settings()
    cache = tmp_path / "cache"
    tdir = cache / "tiffs"
    tdir.mkdir(parents=True)
    haz = np.zeros((20, 20))
    haz[:, :10] = 3
    (tdir / "hazard_toyflood.tif").write_bytes(_gtiff(haz, "int16"))
    vul = np.ones((20, 20))
    (tdir / "vulnerability_toy.tif").write_bytes(_gtiff(vul, "uint8"))
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
                 "grp_contrib_allow_private_hosts": True, "grp_contrib_hourly_cap": 0,
                 "grp_contrib_max_bytes": 2 << 20}.items():
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


def _no_leftovers(env):
    prep = env["cache"] / "prep"
    strays = [*(env["cache"] / "tiffs").glob("staged-tmp-*"),
              *(env["cache"] / "vectors").glob("staged-tmp-*")]
    return (not prep.exists() or not list(prep.iterdir())) and not strays


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(layer, url, **over):
    return {"layer": layer, "url": url, "title": f"{layer} (synthetic)",
            "description": "shaped like a sprint source", "source": "test fixture",
            "license": "CC-BY-4.0", "vintage": "2026-08", **over}


def _stored(layer):
    row = vectors.landed()[layer]
    with open(vectors.master_path(row), "rb") as f:
        raw = f.read()
    assert row["fetched"]["sha256"] == _sha(raw)
    return row, json.loads(raw)


# --------------------------------------------------------------------------- synthetic files
def _square(lon, lat, d=0.02):
    return box(lon, lat, lon + d, lat + d)


def _shapefile(stem, geoms, props, prj=WGS84_PRJ) -> dict:
    """{name: bytes} of a polygon (or point) shapefile with .shp/.shx/.dbf/.prj/.cpg."""
    points = geoms and geoms[0].geom_type == "Point"
    stype = 1 if points else 5
    recs = []
    for g in geoms:
        if points:
            recs.append(struct.pack("<idd", 1, g.x, g.y))
            continue
        ring = list(g.exterior.coords)
        if sum((x2 - x1) * (y2 + y1) for (x1, y1), (x2, y2) in zip(ring, ring[1:])) < 0:
            ring = ring[::-1]                    # outer rings clockwise
        recs.append(struct.pack("<i4d", 5, *g.bounds) + struct.pack("<iii", 1, len(ring), 0)
                    + b"".join(struct.pack("<dd", x, y) for x, y in ring))
    xs = [g.bounds for g in geoms]
    env = (min(b[0] for b in xs), min(b[1] for b in xs), max(b[2] for b in xs), max(b[3] for b in xs))

    def header(words):
        return (struct.pack(">7i", 9994, 0, 0, 0, 0, 0, words) + struct.pack("<2i", 1000, stype)
                + struct.pack("<4d", *env) + struct.pack("<4d", 0, 0, 0, 0))
    body, index, off = b"", b"", 50
    for i, r in enumerate(recs):
        body += struct.pack(">ii", i + 1, len(r) // 2) + r
        index += struct.pack(">ii", off, len(r) // 2)
        off += 4 + len(r) // 2
    names = list(props[0])
    widths = {n: max(10, max(len(str(p[n]).encode()) for p in props)) for n in names}
    hlen = 32 + 32 * len(names) + 1
    rlen = 1 + sum(widths.values())
    dbf = struct.pack("<4BIHH20x", 3, 126, 8, 1, len(props), hlen, rlen)
    for n in names:
        dbf += n.encode()[:10].ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([widths[n], 0]) + b"\0" * 14
    dbf += b"\r"
    for p in props:
        dbf += b" " + b"".join(str(p[n]).encode().ljust(widths[n]) for n in names)
    dbf += b"\x1a"
    return {f"{stem}.shp": header(off) + body, f"{stem}.shx": header(50 + 4 * len(recs)) + index,
            f"{stem}.dbf": dbf, f"{stem}.prj": prj.encode(), f"{stem}.cpg": b"UTF-8"}


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _admin():
    """Every level of a toy Costa Rica: provinces, cantons and districts (a grid of
    squares over the country's box), each level its own shapefile, as HDX ships them."""
    levels = {}
    for lvl, n in ((1, 3), (2, 8), (3, 24)):
        w, s, e, nn = -86.0, 8.0, -82.6, 11.2
        step_x, step_y = (e - w) / n, (nn - s) / 3
        geoms, props = [], []
        for i in range(n):
            for j in range(3):
                g = box(w + i * step_x, s + j * step_y, w + (i + 1) * step_x, s + (j + 1) * step_y)
                g = g.buffer(0.3 * step_x, quad_segs=24).intersection(box(w, s, e, nn))  # many vertices
                geoms.append(g)
                props.append({f"adm{lvl}_name": f"Lugar {lvl}-{i}-{j} ñ", f"adm{lvl}_pcode": f"CR{lvl}{i:02d}{j}"})
        levels[f"cri_admin{lvl}"] = (geoms, props)
    return levels


def _gpkg(path, table, geoms, props, rtree=True):
    con = sqlite3.connect(path)
    con.execute("create table gpkg_spatial_ref_sys (srs_name text not null, srs_id integer "
                "primary key, organization text not null, organization_coordsys_id integer not "
                "null, definition text not null, description text)")
    con.executemany("insert into gpkg_spatial_ref_sys values (?,?,?,?,?,?)", [
        ("Undefined cartesian SRS", -1, "NONE", -1, "undefined", None),
        ("Undefined geographic SRS", 0, "NONE", 0, "undefined", None),
        ("WGS 84 geodetic", 4326, "EPSG", 4326, CRS.from_epsg(4326).to_wkt(), None)])
    con.execute("create table gpkg_contents (table_name text not null primary key, data_type "
                "text not null, identifier text unique, description text default '', "
                "last_change datetime, min_x double, min_y double, max_x double, max_y double, "
                "srs_id integer)")
    con.execute("insert into gpkg_contents values (?, 'features', ?, '', '2026-01-01', -180, -90, "
                "180, 90, 4326)", (table, table))
    con.execute("create table gpkg_geometry_columns (table_name text not null, column_name text "
                "not null, geometry_type_name text not null, srs_id integer not null, z tinyint "
                "not null, m tinyint not null, primary key (table_name, column_name))")
    con.execute("insert into gpkg_geometry_columns values (?, 'geom', 'MULTIPOLYGON', 4326, 0, 0)",
                (table,))
    con.execute(f'create table "{table}" (fid integer primary key autoincrement not null, '
                "geom MULTIPOLYGON, cntry_iso3 text, area_ha real)")
    if rtree:
        con.execute(f'create virtual table "rtree_{table}_geom" using rtree(id, minx, maxx, miny, maxy)')
    for i, (g, p) in enumerate(zip(geoms, props), start=1):
        minx, miny, maxx, maxy = g.bounds
        blob = b"GP" + bytes([0, 0b011]) + struct.pack("<i", 4326) + \
            struct.pack("<4d", minx, maxx, miny, maxy) + wkb.dumps(g)
        con.execute(f'insert into "{table}" values (?, ?, ?, ?)', (i, blob, p["cntry_iso3"], p["area_ha"]))
        if rtree:
            con.execute(f'insert into "rtree_{table}_geom" values (?, ?, ?, ?, ?)',
                        (i, minx, maxx, miny, maxy))
    con.commit()
    con.close()
    return Path(path).read_bytes()


def _mangroves():
    """GMW-shaped polygons along three countries' coasts, one table, cntry_iso3 per row."""
    rng = np.random.default_rng(7)
    geoms, props = [], []
    for iso, (w, s, e, n) in (("CRI", (-85.9, 8.0, -82.6, 11.2)), ("NIC", (-87.6, 11.3, -83.2, 14.9)),
                              ("PAN", (-82.9, 7.2, -77.2, 9.6)), ("IDN", (95.0, -10.0, 141.0, 5.0))):
        for _ in range(150):
            lon, lat = rng.uniform(w, e), rng.uniform(s, n)
            geoms.append(_square(lon, lat, 0.01).buffer(0.002))
            props.append({"cntry_iso3": iso, "area_ha": 100.0})
    return geoms, props


def _hits(geoms, bbox):
    b = box(*bbox)
    return [i for i, g in enumerate(geoms) if g.intersects(b)]


# --------------------------------------------------------------------------- 1. the registry
def test_clip_is_a_vector_op_whose_fields_reach_the_vector_form_and_spare_the_raster_one():
    """The op is derived from its module; its fields are the vector form's; a raster's
    own clip is not taken for it, and whole on another kind says what it is for."""
    assert "clip" in [op.NAME for op in prepare.ops("vector")]
    assert "clip" not in [op.NAME for op in prepare.ops("raster")]
    for f in ("clip", "whole"):
        assert staging.VECTOR_FIELDS["optional"][f] == clip.FIELDS[f]
    assert "ASK the contributor for the area" in clip.FIELDS["clip"]
    assert "exceededTransferLimit" in clip.FIELDS["whole"]
    assert staging.RASTER_FIELDS["optional"]["clip"] != clip.FIELDS["clip"]   # the raster's own
    assert prepare.validate("raster", {"clip": GULF}) == []
    assert prepare.validate("raster", {"whole": True}) == [
        "whole prepares a vector contribution (clip); it does not apply to a raster — drop whole"]
    assert prepare.validate("table", {"clip": GULF}) == [
        "clip prepares a vector contribution (clip); it does not apply to a table — drop clip"]
    assert "vector: clip, whole" in staging.describe_submit()


def test_a_bad_clip_or_whole_or_wfs_url_is_declined_before_any_fetch(env):
    host = env["host"]
    wfs = f"http://127.0.0.1:{host.port}/geoserver/wfs?service=WFS&request=GetCapabilities"
    out = _submit("vector", _manifest("cr_bad", wfs, clip=[-82, 9, -85, 10], whole="yes"))
    assert out["status"] == "declined"
    p = " | ".join(out["problems"])
    assert "clip longitudes must run west to east" in p
    assert "whole must be true (or left out) — got 'yes'" in p
    assert "the WFS url asks for GetCapabilities" in p and "names no layer" in p
    assert host.log == []


# --------------------------------------------------------------------------- 2. HDX: one zip, every level
def _hdx(env):
    levels = _admin()
    members = {}
    for stem, (g, p) in levels.items():
        members.update({f"cri_admin_boundaries/{k}": v for k, v in _shapefile(stem, g, p).items()})
    members["cri_admin_boundaries/readme.txt"] = b"COD-AB levels 0-3"
    raw = _zip(members)
    url = env["host"].put("cri_admin_boundaries.shp.zip", raw)
    return levels, raw, url


def test_the_hdx_all_levels_zip_lands_one_level_whole_and_one_clipped(env, monkeypatch, log):
    """Over the cap as delivered (an archive is fetched whole); with whole: true and
    layer_name the level lands as the hand-split zip of that level would; with clip the
    districts intersecting the box land, whole, and the record says how."""
    levels, raw, url = _hdx(env)
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", len(raw) // 2)
    host = env["host"]
    delivered = _manifest("cr_cod_admin2_cantons", url, layer_name="cri_admin2",
                          name_field="adm2_name")
    out = _submit("vector", delivered)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and "clip it to your area" in out["problems"][0]
    # the stored cap is on the layer, not the archive
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", 2 << 20)
    out = _submit("vector", {**delivered, "whole": True})
    assert out["status"] == "approved", out
    row, fc = _stored("cr_cod_admin2_cantons")
    hand_zip = _zip(_shapefile("cri_admin2", *levels["cri_admin2"]))   # the hand-split level
    hand = json.loads(vecio.to_geojson(hand_zip, "cri_admin2.zip", "cri_admin2")[0])
    assert row["features"] == len(levels["cri_admin2"][0]) == 24
    assert fc["features"] == hand["features"]
    steps = row["fetched"]["steps"]
    log("OUTPUT", json.dumps(steps)[:500])
    assert [st["op"] for st in steps] == ["fetch", "clip"]
    assert steps[0]["sha256"] == _sha(raw) and steps[0]["inputs"]["url"] == url
    assert steps[1]["inputs"]["layer"] == "cri_admin_boundaries/cri_admin2.shp"
    assert steps[1]["inputs"]["whole"] is True and steps[1]["inputs"]["features"]["kept"] == 24
    assert row["prepared"][0].startswith("took shapefile cri_admin2 whole (24 features)")
    assert any("from shapefile cri_admin2" in n for n in row["prepared"])
    assert row["fetched"]["read"] == "downloaded whole (streamed to disk)"
    # districts, clipped: only those touching the gulf box, kept whole
    g3, p3 = levels["cri_admin3"]
    want = _hits(g3, GULF)
    m3 = _manifest("cr_cod_admin3_districts", url, layer_name="cri_admin3", clip=GULF)
    pv = _submit("vector", {**m3, "preview": True})
    assert pv["status"] == "preview", pv
    assert pv["would_land"]["features"] == len(want) and "cr_cod_admin3_districts" not in vectors.landed()
    out = _submit("vector", m3)
    assert out["status"] == "approved", out
    row, fc = _stored("cr_cod_admin3_districts")
    assert sorted(f["properties"]["adm3_pcode"] for f in fc["features"]) == sorted(p3[i]["adm3_pcode"] for i in want)
    assert 0 < len(want) < len(g3)
    for f in fc["features"]:                     # whole features, never cut at the box
        i = next(k for k in want if p3[k]["adm3_pcode"] == f["properties"]["adm3_pcode"])
        assert shape(f["geometry"]).area == pytest.approx(g3[i].area, rel=1e-5)   # 7 decimals
    assert row["fetched"]["clip"] == GULF and row["fetched"]["read"].endswith("filtered to the clip box")
    clip_step = row["fetched"]["steps"][-1]
    assert clip_step["inputs"]["features"] == {"read": len(g3), "kept": len(want)}
    assert row["prepared"][0].startswith(f"kept the {len(want)} of {len(g3)} features of shapefile "
                                         "cri_admin3 that intersect the clip box [-85.3, 9.9, -84.7, 10.3]")
    assert host.asked("cri_admin_boundaries.shp.zip")
    assert _no_leftovers(env)


def test_a_projected_shapefile_is_clipped_where_its_features_lie(env):
    """A shapefile in UTM 17N (as MAATE serves its maps): the lon/lat box is tested
    where the features lie, and they land reprojected."""
    utm = CRS.from_epsg(32617)
    lonlat = [_square(-80.6 + 0.05 * i, -3.0, 0.03) for i in range(20)]
    geoms = [shape(transform_geom("EPSG:4326", utm, mapping(g))) for g in lonlat]
    props = [{"ctn2": "MANGLAR" if i % 2 else "AGUA", "id": str(i)} for i in range(20)]
    url = env["host"].put("cobertura.zip", _zip(_shapefile("cobertura", geoms, props, utm.to_wkt())))
    bbox = [-80.4, -3.1, -80.1, -2.9]
    out = _submit("vector", _manifest("ec_gulf_luc", url, clip=bbox, group_field="ctn2"))
    assert out["status"] == "approved", out
    row, fc = _stored("ec_gulf_luc")
    assert sorted(f["properties"]["id"] for f in fc["features"]) == sorted(str(i) for i in _hits(lonlat, bbox))
    assert any("reprojected from EPSG:32617" in n for n in row["prepared"])
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 3. GMW: a gzipped GeoPackage of the world
@pytest.mark.parametrize("rtree", [True, False])
def test_the_gmw_geopackage_gz_is_decompressed_and_filtered_to_the_box(env, tmp_path, rtree, log):
    """The world's mangroves in one gzipped GeoPackage: decompressed to disk, read
    through its spatial index (or row by row without one), the polygons intersecting the
    box kept as they are — gunzip and clip both on the record."""
    geoms, props = _mangroves()
    gpkg = _gpkg(tmp_path / "w.gpkg", "gmw_v4112_2020_mng_ext_cntry_info_vec", geoms, props, rtree)
    gz = gzip.compress(gpkg)
    url = env["host"].put("gmw_v4112_2020_mng_ext_cntry_info_vec.gpkg.gz", gz)
    cr = [-85.95, 8.03, -82.55, 11.22]
    want = _hits(geoms, cr)
    out = _submit("vector", _manifest("cr_gmw_v41_2020_mangrove", url, clip=cr,
                                      group_field="cntry_iso3", countries=["Costa Rica"]))
    assert out["status"] == "approved", out
    row, fc = _stored("cr_gmw_v41_2020_mangrove")
    log("OUTPUT", row["prepared"][:3])
    assert row["features"] == len(want) and len(want) < len(geoms)
    assert {f["properties"]["cntry_iso3"] for f in fc["features"]} <= {"CRI", "NIC", "PAN"}
    assert sum(f["properties"]["cntry_iso3"] == "CRI" for f in fc["features"]) == sum(
        props[i]["cntry_iso3"] == "CRI" for i in want) > 140
    steps = row["fetched"]["steps"]
    assert [st["op"] for st in steps] == ["fetch", "gunzip", "clip"]
    assert steps[0]["sha256"] == _sha(gz) and steps[1]["sha256"] == _sha(gpkg)
    assert steps[2]["inputs"]["layer"] == "gmw_v4112_2020_mng_ext_cntry_info_vec"
    assert steps[2]["inputs"]["searched"] == ("its spatial index" if rtree else "every row")
    assert row["prepared"][0].startswith(f"kept the {len(want)} of {len(geoms)} features of "
                                         "GeoPackage table gmw_v4112_2020_mng_ext_cntry_info_vec")
    assert _no_leftovers(env)


def test_a_gzip_bomb_and_an_input_over_the_budget_are_declined(env, monkeypatch):
    bomb = gzip.compress(b"\0" * (64 << 20))
    url = env["host"].put("bomb.gpkg.gz", bomb)
    out = _submit("vector", _manifest("test_bomb", url, clip=GULF))
    assert out["status"] == "declined"
    assert "refused as a gzip bomb" in out["problems"][0]
    monkeypatch.setattr(get_settings(), "grp_prep_max_input_bytes", 1000)
    out = _submit("vector", _manifest("test_bomb", url, clip=GULF))
    assert out["problems"] == [f"{url} is {prepare.size_text(len(bomb))}, over the 1000 bytes the "
                               "platform downloads to prepare one contribution — give a smaller "
                               "file (one tile, one year, one area)"]
    from app.contrib import fetch_policy               # a host too slow for the fetch's time

    def slow(*a, **k):
        raise fetch_policy.FetchRefused("fetch exceeded the 600s deadline")
    monkeypatch.setattr(fetch_policy, "fetch_to_file", slow)
    out = _submit("vector", _manifest("test_slow", url, clip=GULF))
    assert out["problems"][0].startswith(f"{url} did not finish downloading in the 600 s")
    assert "Google Drive" in out["problems"][0]
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 4. a FeatureServer that stops at 1,000
def _soils(n=1088):
    feats = []
    for i in range(n):
        lon, lat = -85.9 + (i % 40) * 0.08, 8.1 + (i // 40) * 0.11
        feats.append({"type": "Feature", "id": i + 1,
                      "properties": {"OBJECTID": i + 1, "ORDENES": ["Ultisoles", "Entisoles", "Inceptisoles"][i % 3]},
                      "geometry": mapping(_square(lon, lat, 0.05))})
    return feats


class _FeatureServer:
    """An ArcGIS FeatureServer layer: maxRecordCount, returnCountOnly, an envelope
    filter, resultOffset/resultRecordCount ordered by OBJECTID, f=geojson."""

    def __init__(self, feats, max_records=1000, paging=True, overlap=False):
        self.feats, self.max, self.paging, self.overlap = feats, max_records, paging, overlap

    def __call__(self, path, q):
        js = lambda doc: (200, "application/json", json.dumps(doc).encode())
        if path.endswith("/FeatureServer/0"):
            return js({"type": "Feature Layer", "name": "Suelos", "objectIdField": "OBJECTID",
                       "maxRecordCount": self.max,
                       "advancedQueryCapabilities": {"supportsPagination": self.paging}})
        sel = self.feats
        if q.get("geometry"):
            assert q["geometryType"] == "esriGeometryEnvelope" and q["inSR"] == "4326"
            env = box(*map(float, q["geometry"].split(",")))
            sel = [f for f in sel if shape(f["geometry"]).intersects(env)]
        if q.get("returnCountOnly") == "true":
            return js({"count": len(sel)})
        if q.get("returnIdsOnly") == "true":
            return js({"objectIdFieldName": "OBJECTID", "objectIds": [f["id"] for f in sel]})
        if q.get("objectIds"):
            ids = {int(x) for x in q["objectIds"].split(",")}
            sel = [f for f in sel if f["id"] in ids]
        off = int(q.get("resultOffset", 0))
        if self.overlap and off:
            off -= 1
        n = min(int(q.get("resultRecordCount", self.max)), self.max)
        page = sel[off:off + n]
        doc = {"type": "FeatureCollection", "features": page}
        if off + n < len(sel):
            doc["properties"] = {"exceededTransferLimit": True}
        return js(doc)


def test_a_truncated_featureserver_export_proposes_whole_and_pages_to_every_feature(env, log):
    """As delivered the query stops at 1,000 of 1,088 (exceededTransferLimit) and is
    declined; the decline proposes whole: true; the resubmission pages the query in two
    requests ordered by OBJECTID and lands all 1,088, each page's sha256 on the record."""
    feats = _soils()
    host = env["host"]
    layer_url = host.app("/arcgis/rest/services/Suelos_de_Costa_Rica_2015/FeatureServer/0",
                         _FeatureServer(feats))
    url = layer_url + "/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"
    m = _manifest("cr_suelos_2015_subordenes", url, group_field="ORDENES", countries=["Costa Rica"])
    out = _submit("vector", m)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined"
    assert out["problems"][0] == (
        f"{url}: the server truncated this export at 1,000 features (exceededTransferLimit) — use "
        "the portal's full download (e.g. the Hub 'Download' GeoJSON/shapefile) or page the query"
        " — resubmit the same link with whole: true — the platform pages the query "
        "(resultOffset/resultRecordCount; a WFS by startIndex/count) until every feature is in, "
        "nothing to do by hand; or with clip: <the area> to keep only the features in an area "
        "(ASK the contributor for it — never guess one)")
    assert len(host.log) == 1
    out = _submit("vector", {**m, "whole": True})
    assert out["status"] == "approved", out
    row, fc = _stored("cr_suelos_2015_subordenes")
    assert row["features"] == 1088
    assert sorted(f["properties"]["OBJECTID"] for f in fc["features"]) == list(range(1, 1089))
    steps = row["fetched"]["steps"]
    log("OUTPUT", json.dumps(steps[0]["inputs"])[:600])
    assert [st["op"] for st in steps] == ["clip"]
    inp = steps[0]["inputs"]
    assert inp["counted"] == 1088 and inp["features"] == 1088 and inp["page_size"] == 1000
    assert [(p["offset"], p["features"]) for p in inp["pages"]] == [(0, 1000), (1000, 88)]
    assert all(len(p["sha256"]) == 64 for p in inp["pages"])
    asked = [q for path, q in host.log if path.endswith("/query") and q.get("f") == "geojson"]
    assert [q["orderByFields"] for q in asked[-2:]] == ["OBJECTID ASC"] * 2
    assert row["fetched"]["read"] == "paged: 2 request(s) of up to 1,000 features"
    assert row["prepared"][0].startswith("read 1,088 features from the ArcGIS layer")
    assert _no_leftovers(env)


def test_a_featureserver_with_a_clip_is_asked_for_the_box_and_one_without_paging_by_ids(env):
    """With clip the envelope goes into the query (the server filters); a layer that
    does not page is read by its object ids, in chunks of its record limit."""
    feats = _soils()
    host = env["host"]
    want = _hits([shape(f["geometry"]) for f in feats], GULF)
    url = host.app("/arcgis/rest/services/A/FeatureServer/0", _FeatureServer(feats)) + "/query?f=geojson"
    out = _submit("vector", _manifest("test_soils_gulf", url, clip=GULF))
    assert out["status"] == "approved", out
    row, fc = _stored("test_soils_gulf")
    assert row["features"] == len(want) and 0 < len(want) < 1088
    inp = row["fetched"]["steps"][-1]["inputs"]
    assert "geometry=-85.30000000%2C9.90000000%2C-84.70000000%2C10.30000000" in inp["query"]
    url = host.app("/arcgis/rest/services/B/FeatureServer/0",
                   _FeatureServer(feats, max_records=500, paging=False)) + "/query?f=geojson"
    out = _submit("vector", _manifest("test_soils_ids", url, whole=True))
    assert out["status"] == "approved", out
    inp = vectors.landed()["test_soils_ids"]["fetched"]["steps"][-1]["inputs"]
    assert inp["paged_by"] == "objectIds" and len(inp["pages"]) == 3 and inp["features"] == 1088
    assert _no_leftovers(env)


def test_pages_that_overlap_are_refused_rather_than_landed_incomplete(env):
    host = env["host"]
    url = host.app("/arcgis/rest/services/C/FeatureServer/0", _FeatureServer(_soils(), overlap=True))
    out = _submit("vector", _manifest("test_soils_bad", url, whole=True))
    assert out["status"] == "declined"
    assert "the service's pages overlap (feature 1000 came twice)" in out["problems"][0]
    assert "test_soils_bad" not in vectors.landed() and _no_leftovers(env)


# --------------------------------------------------------------------------- 5. a GeoServer WFS
class _GeoServer:
    """A WFS 2.0 GetFeature: bbox in lat,lon order (urn:ogc:def:crs:EPSG::4326),
    resultType=hits, startIndex/count capped at the server's own maximum, GeoJSON only."""

    def __init__(self, feats, server_max=1500):
        self.feats, self.max = feats, server_max

    def __call__(self, path, q):
        q = {k.lower(): v for k, v in q.items()}
        assert q["service"] == "WFS" and q["version"] == "2.0.0" and q["request"] == "GetFeature"
        assert q["typenames"] == "mae_ide:v_ff010_cobertura_vegetal_2022_a"
        sel = self.feats
        if q.get("bbox"):
            s, w, n, e, crs = q["bbox"].split(",")
            assert crs == "urn:ogc:def:crs:EPSG::4326"
            b = box(float(w), float(s), float(e), float(n))
            sel = [f for f in sel if shape(f["geometry"]).intersects(b)]
        if q.get("resulttype") == "hits":
            return 200, "text/xml", (f'<?xml version="1.0"?><wfs:FeatureCollection '
                                     f'numberMatched="{len(sel)}" numberReturned="0"/>').encode()
        if q.get("outputformat") != "application/json":
            return 200, "text/xml", b"<wfs:FeatureCollection><gml:featureMember/></wfs:FeatureCollection>"
        start, count = int(q.get("startindex", 0)), min(int(q.get("count", 10 ** 9)), self.max)
        page = sel[start:start + count]
        return 200, "application/json", json.dumps({
            "type": "FeatureCollection", "features": page, "totalFeatures": len(sel),
            "numberMatched": len(sel), "numberReturned": len(page),
            "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}}}).encode()


def test_a_wfs_shape_zip_request_with_a_clip_is_paged_as_geojson(env, log):
    """The MAATE request as a hub writes it (SHAPE-ZIP, no box) with the gulf as clip:
    the box goes in as WFS 2.0 wants it, the pages are GeoJSON, a short page from a
    server capped below the page size is not taken for the end, and every feature the
    server counts lands."""
    feats = []
    for i in range(4200):
        lon, lat = -81.0 + (i % 70) * 0.02, -3.9 + (i // 70) * 0.03
        feats.append({"type": "Feature", "id": f"v_ff010.{i}",
                      "properties": {"ctn2": "MANGLAR" if i % 4 == 0 else "MOSAICO AGROPECUARIO"},
                      "geometry": mapping(_square(lon, lat, 0.015))})
    gulf = [-80.6, -3.6, -79.7, -2.0]
    want = _hits([shape(f["geometry"]) for f in feats], gulf)
    base = env["host"].app("/geoserver/mae_ide/wfs", _GeoServer(feats))
    url = (base + "?service=WFS&version=2.0.0&request=GetFeature&typeNames="
           "mae_ide:v_ff010_cobertura_vegetal_2022_a&outputFormat=SHAPE-ZIP")
    out = _submit("vector", _manifest("ec_gulf_luc_2022", url, clip=gulf, group_field="ctn2",
                                      countries=["Ecuador"]))
    assert out["status"] == "approved", out
    row, fc = _stored("ec_gulf_luc_2022")
    assert row["features"] == len(want) and len(want) > 2000
    inp = row["fetched"]["steps"][-1]["inputs"]
    log("OUTPUT", json.dumps({k: v for k, v in inp.items() if k != "pages"})[:600])
    assert inp["counted"] == len(want) == 2300
    assert [p["features"] for p in inp["pages"]] == [1500, 800]      # 1,500 < 2,000: not the end
    assert "bbox=-3.60000000%2C-80.60000000%2C-2.00000000%2C-79.70000000%2Curn%3Aogc%3Adef%3Acrs%3AEPSG%3A%3A4326" in inp["query"]
    assert any("instead of the url's outputFormat SHAPE-ZIP" in n for n in row["prepared"])
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 6. GeoJSON files
def test_a_geojson_with_a_crs_member_and_a_truncated_one(env):
    """An ArcGIS Hub GeoJSON in EPSG:3857 (a crs member) is tested where its features
    lie; a truncated export saved as a file cannot be completed by a clip, and says so."""
    lonlat = [_square(-85.93 + 0.1 * i, 9.95, 0.05) for i in range(30)]
    merc = [transform_geom("EPSG:4326", "EPSG:3857", mapping(g)) for g in lonlat]
    fc = {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:3857"}},
          "features": [{"type": "Feature", "properties": {"i": i}, "geometry": g} for i, g in enumerate(merc)]}
    url = env["host"].put("hub.geojson", json.dumps(fc).encode())
    out = _submit("vector", _manifest("test_hub_merc", url, clip=GULF))
    assert out["status"] == "approved", out
    row, stored = _stored("test_hub_merc")
    assert sorted(f["properties"]["i"] for f in stored["features"]) == _hits(lonlat, GULF)
    cut = {"type": "FeatureCollection", "properties": {"exceededTransferLimit": True},
           "features": [{"type": "Feature", "properties": {}, "geometry": mapping(g)} for g in lonlat]}
    url = env["host"].put("cut.geojson", json.dumps(cut).encode())
    out = _submit("vector", _manifest("test_cut", url, clip=GULF))
    assert out["status"] == "declined"
    assert "is a server export cut at its record limit (exceededTransferLimit)" in out["problems"][0]
    url = env["host"].put("none.geojson", json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": mapping(_square(10, 10))}]}).encode())
    out = _submit("vector", _manifest("test_none", url, clip=GULF))
    assert out["problems"] == [f"none of the 1 features of the GeoJSON in {url} intersects the clip "
                               "box [-85.3, 9.9, -84.7, 10.3] — check the area with the contributor "
                               "([minLon, minLat, maxLon, maxLat], in degrees)"]
    assert _no_leftovers(env)


def test_the_streaming_reader_keeps_each_features_text_and_reads_past_its_chunk(tmp_path):
    """A feature longer than the read chunk, members before and after the features, a
    BOM: each feature's text comes back exactly as the file has it."""
    big = mapping(Point(1, 2).buffer(1, quad_segs=4000))
    fc = {"name": "x", "features": [{"type": "Feature", "properties": {"n": "ñ"}, "geometry": big},
                                    {"type": "Feature", "properties": {"n": 2}, "geometry": None}],
          "type": "FeatureCollection", "after": [1, 2]}
    p = tmp_path / "f.geojson"
    text = json.dumps(fc, ensure_ascii=False)
    p.write_bytes(b"\xef\xbb\xbf" + text.encode())
    got, members = [], {}
    assert _features.walk_geojson(str(p), members.__setitem__, lambda f, t: got.append((f, t)),
                                  chunk=1000)
    assert [t for _, t in got] == [json.dumps(f, ensure_ascii=False) for f in fc["features"]]
    assert members == {"name": "x", "type": "FeatureCollection", "after": [1, 2]}


# --------------------------------------------------------------------------- 7. beyond the asking pack
def test_points_clipped_out_of_a_big_geojson_are_counted_by_the_risk_pack(env, log):
    """Beyond the land-cover sprint: a hub's shelters for a whole region (a GeoJSON
    over the cap) clipped to its province land as a point layer the risk pack counts
    against the flood; a polygon layer clipped the same way is measured by the pack."""
    rng = np.random.default_rng(3)
    pts = [Point(103.001 + 0.0005 * i, 13.01) for i in range(10)]          # in the toy world
    pts += [Point(rng.uniform(90, 110), rng.uniform(-10, 25)) for _ in range(20000)]
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": f"Shelter {i}", "pad": "x" * 80},
         "geometry": mapping(p)} for i, p in enumerate(pts)]}
    raw = json.dumps(fc).encode()
    assert len(raw) > get_settings().grp_contrib_max_bytes
    url = env["host"].put("shelters_region.geojson", raw)
    clip_box = [102.99, 12.99, 103.03, 13.03]
    out = _submit("vector", _manifest("toy_shelters", url, clip=clip_box, name_field="name"))
    assert out["status"] == "approved", out
    row, stored = _stored("toy_shelters")
    assert row["features"] == len(_hits(pts, clip_box)) >= 10
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY)
    assert p["status"] == "ok", p.get("note")
    exp = next(c for c in p["citations"] if c.get("title", "").startswith("toy_shelters (synthetic) vs"))
    log("OUTPUT", exp["text"][:300])
    assert "contributed layer" in exp["text"]
    polys = [_square(103.004, 13.004, 0.004), _square(50, 50)]
    url = env["host"].put("plots.geojson", json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"k": i}, "geometry": mapping(g)} for i, g in enumerate(polys)]}).encode())
    out = _submit("vector", _manifest("toy_plots", url, clip=clip_box))
    assert out["status"] == "approved", out
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY,
                          vectors=["toy_plots"])
    assert p["status"] == "ok", p.get("note")
    assert p["vectors"]["toy_plots"]["total"] > 0
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 8. after another op
class _Parts:
    """A stand-in assemble op (as a from-folder op would be): fetches loose shapefile
    parts and zips them; the clip then reads the zip it made."""
    NAME, STEP, KINDS, NEEDS_INPUT = "parts_test", "assemble", ("vector",), False
    FIELDS = {"parts": "the loose parts' urls (a test op)"}

    @staticmethod
    def validate(m):
        return []

    @staticmethod
    def run(ctx):
        out = ctx.tmp(".zip", "assembled")
        with zipfile.ZipFile(out, "w") as z:
            for u in ctx.manifest["parts"]:
                z.write(ctx.fetch(u), u.rsplit("/", 1)[-1])
        return prepare.PreparedFile(out, ["zipped the parts"],
                                    {"inputs": {"parts": ctx.manifest["parts"]}})

    @staticmethod
    def suggest(situation):
        return None


def test_the_clip_reads_the_file_the_step_before_made(env, monkeypatch):
    """Ops compose: an assemble step's zip is clipped without a second download, and the
    record says the file came from the step before."""
    monkeypatch.setattr(prepare, "_modules", lambda: (_Parts, clip))
    g, p = _admin()["cri_admin3"]
    parts = [env["host"].put(f"loose/{k}", v) for k, v in _shapefile("cri_admin3", g, p).items()]
    out = _submit("vector", _manifest("test_loose_districts", parts[0], parts=parts, clip=GULF))
    assert out["status"] == "approved", out
    row, fc = _stored("test_loose_districts")
    assert row["features"] == len(_hits(g, GULF))
    assert [st["op"] for st in row["fetched"]["steps"]] == ["fetch"] * 5 + ["parts_test", "clip"]
    assert "read from the file the step before made" in row["prepared"][1]
    assert row["fetched"]["read"] == "prepared before, filtered to the clip box"
    assert _no_leftovers(env)
