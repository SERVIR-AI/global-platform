"""rasterise: a polygon map landed as a class raster, on the server.

The sprint case: MAGA Guatemala publishes its land use / land cover maps (2003, 2010,
2020, 2025) as WFS polygon layers, each year with its own code list (2003 an older one).
Comparing them year to year needs a land-cover stack — rasters on one grid with one
legend — so each year was rasterised by hand (30 m, pixel centre, 13 grouped classes).
Here each year goes in from its own WFS link: the as-delivered decline proposes
rasterise; a mapping that misses values is declined naming every value with its label
and area; the full mapping lands, the stored cells equal an independent rasterisation of
the same polygons cell for cell, mangrove keeps its polygons' area, the WFS was asked
only for the clip's box, page by page, and every step is on the record. A preview stores
nothing. Beyond the asking pack: a vulnerability map drawn as polygons in UTM (a plain
GeoJSON, not a service) is rasterised and read by the risk pack and by a contributed
extension pack.

Everything is synthetic and local: a loopback HTTP server in this file plays the WFS
(GeoServer's paging, bbox and GeoJSON shapes) and serves plain files; no test touches
the network."""

import hashlib
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import numpy as np
import pytest
import rasterio
import yaml
from rasterio import features as rfeatures
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import transform_geom
from shapely.geometry import box as sbox, mapping, shape

from app.config import get_settings
from app.contrib import identity, prepare, staging
from app.contrib.prepare import _layer_reader as _features, rasterise, recode
from app.graph.geo import ingest, schema, tiffs, vectors
from app.mcp import assemble, packs, registry, store
from app.risk import synthesis as risk

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
B = (103.0, 13.0, 103.02, 13.02)              # the risk world: 20 x 20 cells of 0.001 deg
RISK_POLY = {"type": "Polygon", "coordinates": [[[103.005, 13.005], [103.015, 13.005],
             [103.015, 13.015], [103.005, 13.015], [103.005, 13.005]]]}

# --------------------------------------------------------------------------- the MAGA shape
CLIP = [-91.2, 13.8, -91.0, 13.95]            # the coast box asked for
RES = 0.001                                   # 200 x 150 cells
LEGEND = {1: "Built-up and artificial areas", 2: "Annual crops", 4: "Pasture",
          6: "Mangrove forest", 11: "Water (rivers, lakes, sea)", 12: "Estuary",
          13: "Aquaculture and salt pans"}
DECL = {"dtype": "uint8", "valid_min": 0, "valid_max": 13, "units": "class"}
# each year's own codes, as MAGA codes them, and what they group to (the hand grouping)
CODES = {2025: {314: ("Bosque manglar", 6), 521: ("Estero", 12), 232: ("Pasto natural", 4),
                2111: ("Maíz y frijol", 2), 1213: ("Producción hidrobiológica (camaroneras, "
                                                   "piscícolas)", 13),
                112: ("Tejido urbano discontinuo", 1), 522: ("Mar y/u océano", 11)},
         2003: {411: ("Manglar", 6), 51: ("Cuerpos de agua", 11), 311: ("Pastos", 4),
                211: ("Granos básicos", 2), 1311: ("Camaroneras y salineras", 13),
                111: ("Centros poblados", 1)}}


def _ring(cx, cy, r, n=23, wob=0.0):
    """A closed lon/lat ring round (cx, cy) — irregular when wob > 0."""
    pts = []
    for i in range(n):
        a = 2 * np.pi * i / n
        rr = r * (1 + wob * np.sin(3 * a))
        pts.append([round(cx + rr * np.cos(a), 7), round(cy + rr * np.sin(a) * 0.8, 7)])
    return pts + [pts[0]]


def _maga(year):
    """A year of the coast as polygons: a grid of rectangles with irregular mangrove and
    estuary patches cut out, some features outside the box (they must not be asked for),
    one feature with no geometry."""
    codes = list(CODES[year])
    mangrove, estuary = codes[0], codes[1] if year == 2025 else codes[1]
    feats, gid = [], 0

    def add(geom, code):
        nonlocal gid
        gid += 1
        feats.append({"type": "Feature", "id": f"cobertura_{year}.{gid}", "geometry_name": "geom",
                      "geometry": geom, "properties": {
                          "gid": gid, "C_USO": code, "usot": CODES[year][code][0],
                          "depto": "Escuintla", "area_ha": 1.0}})
    holes = {(1, 1): (mangrove, 0.012 if year == 2025 else 0.010),
             (2, 1): (estuary, 0.008), (0, 1): (mangrove, 0.007)}
    others = [c for c in codes if c not in (mangrove, estuary)]
    for i in range(4):
        for j in range(3):
            x0, y0 = CLIP[0] + i * 0.05, CLIP[1] + j * 0.05
            cell = sbox(x0, y0, x0 + 0.05, y0 + 0.05)
            if (i, j) in holes:
                code, r = holes[(i, j)]
                patch = shape({"type": "Polygon", "coordinates": [_ring(x0 + 0.025, y0 + 0.025, r,
                                                                        wob=0.15)]})
                add(mapping(patch), code)
                cell = cell.difference(patch)
            add(mapping(cell), others[(i + j) % len(others)])
    # beyond the box: across the border, and out at sea
    add(mapping(sbox(-91.5, 13.85, -91.3, 13.9)), others[0])
    add(mapping(sbox(-90.8, 13.5, -90.7, 13.6)), mangrove)
    feats.append({"type": "Feature", "id": f"cobertura_{year}.{gid + 1}", "geometry": None,
                  "properties": {"gid": gid + 1, "C_USO": others[0], "usot": "sin geometría"}})
    return feats


def _grouped(year):
    return {code: grp for code, (_, grp) in CODES[year].items()}


# --------------------------------------------------------------------------- a host
class _Host:
    """Plain files by path, and a WFS at /geoserver/ows (GeoServer's GeoJSON: count +
    startIndex paging, bbox in lat/lon with the EPSG urn, numberMatched, a crs member)."""

    def __init__(self):
        self.files, self.layers, self.log, self.port = {}, {}, [], None
        self.dupes = self.no_key = False
        self.crs = "urn:ogc:def:crs:EPSG::4326"

    def put(self, name, data):
        self.files["/" + name] = data
        return f"http://127.0.0.1:{self.port}/{name}"

    def wfs(self, type_name, feats):
        self.layers[type_name] = feats
        return (f"http://127.0.0.1:{self.port}/geoserver/ows?service=WFS&version=2.0.0&"
                f"request=GetFeature&typeNames={type_name}")

    def asked(self, path):
        return [q for p, q in self.log if p == path]

    def answer(self, path, query):
        """(status, body). A layer with no primary key (`no_key`) pages only sorted, as
        GeoServer answers MAGA's 2010 layer."""
        q = {k.lower(): v for k, v in parse_qsl(query)}
        if self.no_key and "startindex" in q and "sortby" not in q:
            return 400, (b'<?xml version="1.0" encoding="UTF-8"?><ows:ExceptionReport '
                         b'xmlns:ows="http://www.opengis.net/ows/1.1"><ows:Exception><ows:'
                         b'ExceptionText>java.lang.RuntimeExceptionCannot do natural order '
                         b'without a primary key, please add it or specify a manual sort over '
                         b'existing attributes</ows:ExceptionText></ows:Exception>'
                         b'</ows:ExceptionReport>')
        feats = self.layers[q["typenames"]]
        if "sortby" in q:
            feats = sorted(feats, key=lambda f: f["properties"][q["sortby"].split()[0]])
        if "bbox" in q:
            s, w, n, e = (float(v) for v in q["bbox"].split(",")[:4])
            area = sbox(w, s, e, n)
            feats = [f for f in feats if f["geometry"] and shape(f.get("lonlat") or f["geometry"])
                     .intersects(area)]
        start = int(q.get("startindex", 0))
        count = int(q.get("count", 10 ** 9))
        page = feats[start:start + count]
        if self.dupes and start:
            page = feats[start - 1:start - 1 + count]
        body = {"type": "FeatureCollection", "features": page, "totalFeatures": len(feats),
                "numberMatched": len(feats), "numberReturned": len(page),
                "crs": {"type": "name", "properties": {"name": self.crs}}}
        return 200, json.dumps(body).encode()


def _handler(host):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            parts = urlsplit(self.path)
            host.log.append((parts.path, parts.query))
            code = 200
            if parts.path == "/geoserver/ows":
                (code, data), ctype, name = host.answer(parts.path, parts.query), "application/json", None
            else:
                data, ctype, name = host.files.get(parts.path), "application/octet-stream", parts.path[1:]
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if name:
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
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
def _gtiff(arr, dtype, nodata=None, crs="EPSG:4326", transform=None, bounds=B):
    h, w = arr.shape
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=w, height=h, count=1, dtype=dtype, crs=crs,
                     transform=transform or from_bounds(*bounds, w, h), nodata=nodata) as d:
            d.write(arr.astype(dtype), 1)
        return mf.read()


@pytest.fixture
def env(tmp_path, monkeypatch, host):
    """A hermetic platform: an empty land-cover catalog, the risk pack's toy world (a
    flood hazard and a 1-5 vulnerability layer near 103 E, 13 N), auto-approve, the real
    fetch policy with the loopback host allowed, a hermetic store, and WFS pages of 4."""
    s = get_settings()
    cache = tmp_path / "cache"
    tdir = cache / "tiffs"
    tdir.mkdir(parents=True)
    haz = np.zeros((20, 20))
    haz[:, :10] = 3
    (tdir / "hazard_toyflood.tif").write_bytes(_gtiff(haz, "int16"))
    vul = np.ones((20, 20))
    vul[5:15, 5:15] = 4
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
                 "grp_contrib_allow_private_hosts": True, "grp_contrib_hourly_cap": 0}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(ingest, "_fetch_layer", lambda layer, bbox, boundary: [])
    monkeypatch.setattr(risk, "_pack_feeds", lambda *a, **k: [])
    monkeypatch.setattr(risk, "_document_hits", lambda *a, **k: (None, []))
    monkeypatch.setattr(_features, "PAGE", 4)
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
    strays = list((env["cache"] / "tiffs").glob("staged-tmp-*"))
    return (not prep.exists() or not list(prep.iterdir())) and not strays


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _year(year, url, **over):
    return {"layer": f"landcover_gtcoast_{year}", "stack": "landcover_gtcoast", "year": year,
            "url": url, "title": f"MAGA land use / land cover {year}, coast (synthetic)",
            "description": "grouped classes from the MAGA polygons", "license": "unstated",
            "source": "test fixture shaped like the MAGA WFS layers", "vintage": "unstated",
            "legend": LEGEND, "declared": DECL, "outside_values": [0], "countries": ["Guatemala"],
            "stack_title": "Synthetic Guatemala coast 2003-2025", **over}


def _rasterise(year, **over):
    return {"field": "C_USO", "resolution": RES,
            "classes": {str(k): v for k, v in _grouped(year).items()}, **over}


def _poly(box):
    w, s, e, n = box
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _by_hand(feats, grouped, box=CLIP, res=RES):
    """The hand script's rasterisation: every mapped polygon, pixel centre, on the box's
    grid from its north-west corner."""
    w, s, e, n = box
    width, height = round((e - w) / res), round((n - s) / res)
    shapes = [(f["geometry"], grouped[f["properties"]["C_USO"]]) for f in feats
              if f["geometry"] and shape(f["geometry"]).intersects(sbox(*box))]
    return rfeatures.rasterize(shapes, out_shape=(height, width),
                               transform=from_origin(w, n, res, res), fill=0,
                               all_touched=False, dtype="uint8")


def _class_km2(sid, year, cls):
    p = assemble.assemble(pack="land-cover", place="Coast (synthetic)", year=str(year),
                          geometry=_poly(CLIP), stack=sid, classes=[cls])
    assert p["status"] == "ok", p.get("note")
    return p["stats"]["classes_km2"][cls]


# --------------------------------------------------------------------------- 1. the registry
def test_rasterise_is_registered_and_its_field_reaches_the_form(env):
    """The op is a module of the package; its form text reaches the raster form and the
    contribute_submit description; it is no vector field."""
    assert "rasterise" in [op.NAME for op in prepare.ops("raster")]
    assert rasterise.STEP in prepare.ORDER and not rasterise.NEEDS_INPUT
    assert staging.RASTER_FIELDS["optional"]["rasterise"] == rasterise.FIELDS["rasterise"]
    assert "rasterise" in staging._RASTER_KNOWN and "rasterise" not in staging.VECTOR_FIELDS["optional"]
    assert staging.form("raster")["fields"]["optional"]["rasterise"].startswith("for a POLYGON map")
    assert "declined" in prepare.SITUATIONS


def test_a_rasterise_that_cannot_work_is_declined_before_any_fetch(env, log):
    """Wrong kind, a measure layer, a resolution that is not a cell size, a code outside
    the legend, a fill that is a class, a clip too big to rasterise: words, no request."""
    host = env["host"]
    url = host.wfs("lc:cobertura_2025", _maga(2025))
    out = _submit("vector", {"layer": "gt_lc", "url": url, "title": "t", "description": "d",
                             "source": "s", "license": "CC0-1.0", "vintage": "2026-10",
                             "rasterise": _rasterise(2025)})
    assert ("rasterise prepares a raster contribution (rasterise); it does not apply to a "
            "vector — drop rasterise") in out["problems"]
    out = _submit("raster", {"layer": "measure_gt_height", "url": url, "title": "t",
                             "description": "d", "source": "s", "license": "CC0-1.0",
                             "vintage": "2026-10", "rasterise": _rasterise(2025),
                             "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 50,
                                          "units": "m"}})
    assert ("rasterise makes a class map; a measure_* layer holds amount, not classes — drop "
            "rasterise") in out["problems"]
    for bad, said in ((30, "reads as degrees (30° is about 3,340 km)"),
                      ("thirty", "must be the cell size"),
                      ("0.01 m", "a cell of 0.01 m is not a map grid")):
        out = _submit("raster", _year(2025, url, clip=CLIP,
                                      rasterise=_rasterise(2025, resolution=bad)))
        log("OUTPUT", out["problems"])
        assert out["status"] == "declined" and any(said in p for p in out["problems"])
    out = _submit("raster", _year(2025, url, rasterise=_rasterise(2025, classes={314: 66})))
    assert any("rasterise.classes writes 66, which is no class of the legend" in p
               for p in out["problems"])
    out = _submit("raster", _year(2025, url, outside_values=[6], rasterise=_rasterise(2025)))
    assert any("cells no polygon covers take 6, which the legend names 'Mangrove forest'" in p
               for p in out["problems"])
    out = _submit("raster", _year(2025, url, clip=[-92.0, 13.0, -88.0, 17.0],
                                  rasterise=_rasterise(2025, resolution=0.00001)))
    assert any("makes 160,000,000,000 cells" in p for p in out["problems"])
    out = _submit("raster", _year(2025, url, rasterise={"field": "C_USO", "resolution": RES,
                                                        "colour": "red"}))
    assert "rasterise takes field, resolution, classes, layer — not colour" in out["problems"]
    assert host.log == []


# --------------------------------------------------------------------------- 2. the real case
def test_the_maga_years_go_in_from_their_wfs_links_and_land_as_one_stack(env, log):
    """As delivered the WFS link is declined as no raster, proposing rasterise; a mapping
    that misses values is declined with every value, its label and its area; the full
    mapping lands — cell for cell the hand rasterisation, mangrove at its polygons' area,
    the WFS asked only for the box, page by page — and 2003 joins with its own codes."""
    host = env["host"]
    f25, f03 = _maga(2025), _maga(2003)
    url25 = host.wfs("lc:cobertura_2025", f25)
    url03 = host.wfs("lc:cobertura_2003", f03)

    # as delivered: the WFS link given as the raster
    out = _submit("raster", _year(2025, url25))
    said = out["problems"][0]
    log("OUTPUT", said)
    assert out["status"] == "declined" and "not in a raster format" in said
    assert ("or, if it is a POLYGON map of classes (land cover or land use, zoning, a hazard map "
            "drawn as polygons), the platform makes the class raster itself: resubmit the same "
            "link with rasterise: {field: '<the attribute naming each polygon's class>'") in said
    assert "ASK the contributor which attribute holds the class" in said and ", clip: <the area>" in said

    # the agent fills rasterise but maps only mangrove: every other value is named
    host.log.clear()
    out = _submit("raster", _year(2025, url25, clip=CLIP,
                                  rasterise=_rasterise(2025, classes={"314": 6})))
    said = out["problems"][0]
    log("OUTPUT", said)
    assert out["status"] == "declined"
    assert "rasterise needs a code for every value of C_USO; 6 of the 7 value(s) the polygons hold have none" in said
    assert "521 'Estero' (1 polygon(s)," in said and "1213 'Producción hidrobiológica" in said
    assert "rasterise.classes: {112: <code>, 232: <code>, 521: <code>" in said
    assert "ASK the contributor which class of the legend" in said
    assert _no_leftovers(env)

    # the full mapping (the hand grouping) lands
    host.log.clear()
    out = _submit("raster", _year(2025, url25, clip=CLIP, rasterise=_rasterise(2025)))
    log("OUTPUT", out.get("preview", {}).get("note") or out)
    assert out["status"] == "approved", out
    probe, *pages = host.asked("/geoserver/ows")
    assert "count=1" in probe and "startIndex" not in probe      # a look at the attributes
    assert len(pages) > 1 and all("startIndex=" in q and "count=4" in q and "sortBy=gid" in q
                                  for q in pages)
    assert all("bbox=13.8%2C-91.2%2C13.95%2C-91.0%2Curn%3Aogc%3Adef%3Acrs%3AEPSG%3A%3A4326" in q
               or "bbox=13.8,-91.2,13.95,-91.0,urn:ogc:def:crs:EPSG::4326" in q for q in pages)
    row = tiffs.catalog()["landcover_gtcoast_2025"]
    path = get_settings().cache_dir / row["local_path"]
    with rasterio.open(path) as src:
        got, tr, crs = src.read(1), src.transform, src.crs
    hand = _by_hand(f25, _grouped(2025))
    assert crs.to_epsg() == 4326 and got.shape == hand.shape == (150, 200)
    assert tr.almost_equals(from_origin(CLIP[0], CLIP[3], RES, RES))
    assert np.array_equal(got, hand)                            # cell for cell
    # mangrove keeps its polygons' area, within the cells along its edge
    mangrove = sum(vectors.area_km2(shape(f["geometry"]).intersection(sbox(*CLIP)))
                   for f in f25 if f["geometry"] and f["properties"]["C_USO"] == 314)
    km2 = _class_km2("landcover_gtcoast", 2025, "Mangrove forest")
    log("OUTPUT", f"mangrove 2025: raster {km2} km2, polygons {mangrove:.3f} km2")
    assert abs(km2 - mangrove) / mangrove < 0.02

    # the record: the paged fetch as ONE step, the rasterise step, the stored file's sha256
    f = row["fetched"]
    assert [st["op"] for st in f["steps"]] == ["fetch", "rasterise"]
    fetch, rast = f["steps"]
    inside = [x for x in f25 if x["geometry"] and shape(x["geometry"]).intersects(sbox(*CLIP))]
    assert fetch["inputs"]["features"] == len(inside)
    assert fetch["inputs"]["paged"] == {"by": "startIndex", "size_param": "count",
                                        "pages": len(pages), "sort_by": "gid",
                                        "asked": [[i * 4, 4] for i in range(len(pages))]}
    assert len(fetch["inputs"]["page_sha256"]) == len(pages) and fetch["inputs"]["box"] == CLIP
    assert "outputFormat=application" in fetch["inputs"]["url"]
    assert rast["inputs"]["field"] == "C_USO" and rast["inputs"]["all_touched"] is False
    assert rast["inputs"]["resolution"] == {"given": "0.001°", "degrees": RES}
    assert rast["inputs"]["classes"] == {str(k): v for k, v in _grouped(2025).items()}
    assert rast["inputs"]["grid"] == {"crs": "EPSG:4326", "width": 200, "height": 150,
                                      "transform": [RES, 0.0, CLIP[0], 0.0, -RES, CLIP[3]]}
    assert rast["inputs"]["cells"]["6"] == int((hand == 6).sum())
    with open(path, "rb") as fh:
        assert f["sha256"] == _sha(fh.read())
    assert f["prepared"] == "on the server: fetch -> rasterise" and f["clip"] == CLIP
    assert f["read"].startswith("WFS GetFeature, paged:") and "only the clip's box" in f["read"]
    notes = " | ".join(row["prepared"])
    assert "rasterised at ingest from" in notes and "(all-touched off)" in notes
    assert "differs slightly from its polygons'" in notes
    assert "area per class, polygons / raster (km², ellipsoid): " in notes
    assert "1 feature(s) without geometry were dropped" not in notes   # outside the box: never asked

    # 2003 joins with its own (older) codes, mapped onto the stack's
    out = _submit("raster", _year(2003, url03, clip=CLIP, rasterise=_rasterise(2003)))
    assert out["status"] == "approved", out
    with rasterio.open(get_settings().cache_dir
                       / tiffs.catalog()["landcover_gtcoast_2003"]["local_path"]) as src:
        assert np.array_equal(src.read(1), _by_hand(f03, _grouped(2003)))
    assert tiffs.catalog()["landcover_gtcoast"]["years"] == [2003, 2025]
    p = assemble.assemble(pack="land-cover", place="Coast (synthetic)", year="2003",
                          year_to="2025", geometry=_poly(CLIP), stack="landcover_gtcoast",
                          classes=["Mangrove forest"])
    assert p["status"] == "ok", p.get("note")
    log("OUTPUT", json.dumps({k: v for k, v in p["stats"].items() if "km2" in k})[:600])
    assert p["stats"]["classes_km2"]["Mangrove forest"] > 0
    assert _no_leftovers(env)


def test_without_classes_the_legend_maps_the_values_or_the_decline_offers_one_code_each(env, log):
    """No `classes`: values that are the legend's labels land as its codes; values that are
    not are named, with the two ways on: map them onto the legend, or one code per value."""
    url = env["host"].wfs("lc:cobertura_2025", _maga(2025))
    out = _submit("raster", _year(2025, url, clip=CLIP, rasterise={"field": "C_USO",
                                                                   "resolution": RES}))
    said = out["problems"][0]
    log("OUTPUT", said)
    assert "7 of the 7 value(s) the polygons hold are no label or code of the legend" in said
    assert "Give each the code it lands as — rasterise.classes: {112: <code>," in said
    assert ("or, for a new map (a layer of its own, or the first year of a new stack), one code "
            "per value: classes {112: 1, 232: 2, 314: 3, 521: 4, 522: 5, 1213: 6, 2111: 7} with "
            "legend {1: Tejido urbano discontinuo, 2: Pasto natural, 3: Bosque manglar") in said
    out = _submit("raster", _year(2025, url, clip=CLIP, rasterise={"field": "usot", "resolution": RES},
                                  legend={c: lab for c, (lab, _) in CODES[2025].items()},
                                  declared={**DECL, "valid_max": 2111, "dtype": "uint16"}))
    assert out["status"] == "approved", out
    st = tiffs.catalog()["landcover_gtcoast_2025"]["fetched"]["steps"][-1]["inputs"]
    assert st["classes"]["Bosque manglar"] == 314 and st["field"] == "usot"
    assert _no_leftovers(env)


def test_a_layer_without_the_field_is_named_with_its_attributes(env):
    url = env["host"].wfs("lc:cobertura_2025", _maga(2025))
    out = _submit("raster", _year(2025, url, clip=CLIP,
                                  rasterise=_rasterise(2025, field="CODIGO")))
    assert out["status"] == "declined"
    assert out["problems"][0].startswith(f"missing required field 'rasterise.field': no "
                                         f"feature of the layer at {url} has an attribute "
                                         "'CODIGO' — its attributes are C_USO, area_ha, depto, gid, usot")
    assert len(env["host"].asked("/geoserver/ows")) == 2          # said on the first page
    assert _no_leftovers(env)


def test_a_layer_without_a_primary_key_is_paged_sorted_by_its_id_attribute(env, log):
    """MAGA's 2010 layer answers 400 to startIndex without sortBy (no primary key): the
    platform pages it sorted by the id attribute it sees on one feature, and says so."""
    env["host"].no_key = True
    f10 = _maga(2003)
    url = env["host"].wfs("lc:cobertura_2010", f10)
    out = _submit("raster", _year(2003, url, clip=CLIP, rasterise=_rasterise(2003)))
    assert out["status"] == "approved", out
    f = tiffs.catalog()["landcover_gtcoast_2003"]["fetched"]
    assert f["steps"][0]["inputs"]["paged"]["sort_by"] == "gid"
    assert "sortBy=gid" in f["steps"][0]["inputs"]["url"]
    with rasterio.open(get_settings().cache_dir
                       / tiffs.catalog()["landcover_gtcoast_2003"]["local_path"]) as src:
        assert np.array_equal(src.read(1), _by_hand(f10, _grouped(2003)))
    # a layer with no attribute to sort by: the service's refusal, and what to do
    bare = [{**x, "properties": {k: v for k, v in x["properties"].items() if k != "gid"}}
            for x in _maga(2025)]
    url = env["host"].wfs("lc:cobertura_bare", bare)
    out = _submit("raster", _year(2025, url, clip=CLIP, rasterise=_rasterise(2025)))
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined" and "answered HTTP 400" in out["problems"][0]
    assert ("the platform pages a WFS with startIndex; if the service cannot page this layer, "
            "add sortBy=<an attribute naming each feature once> to the link") in out["problems"][0]
    assert _no_leftovers(env)


def test_a_layer_of_huge_polygons_comes_a_few_at_a_time(env, monkeypatch):
    """Pages weigh about PAGE_BYTES, not a fixed count: MAGA's 2010 layer is 75 dissolved
    polygons, 187 MB in one page. Fewer features a page, the same cells, every window on
    the record."""
    monkeypatch.setattr(_features, "PAGE_BYTES", 900)
    feats = _maga(2025)
    url = env["host"].wfs("lc:cobertura_2025", feats)
    out = _submit("raster", _year(2025, url, clip=CLIP, rasterise=_rasterise(2025)))
    assert out["status"] == "approved", out
    f = tiffs.catalog()["landcover_gtcoast_2025"]["fetched"]
    asked = f["steps"][0]["inputs"]["paged"]["asked"]
    assert max(c for _, c in asked) < 4 and len(asked) > 4
    assert all(asked[i + 1][0] > asked[i][0] for i in range(len(asked) - 1))
    with rasterio.open(get_settings().cache_dir
                       / tiffs.catalog()["landcover_gtcoast_2025"]["local_path"]) as src:
        assert np.array_equal(src.read(1), _by_hand(feats, _grouped(2025)))
    assert _no_leftovers(env)


@pytest.mark.parametrize("chunk", [5, 64, 1 << 20])
def test_a_page_is_parsed_from_disk_one_feature_at_a_time(tmp_path, monkeypatch, chunk):
    """The page reader reads the features of a FeatureCollection in order whatever the
    read size (numbers and strings cut at a chunk's end), with the members after them."""
    monkeypatch.setattr(_features, "_CHUNK", chunk)
    feats = _maga(2025)[:5]
    body = {"type": "FeatureCollection", "features": feats, "totalFeatures": 123456789,
            "numberMatched": 5, "crs": {"type": "name", "properties": {"name": "EPSG:32616"}}}
    p = tmp_path / "page.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps(body, ensure_ascii=False).encode())
    members: dict = {}
    assert list(_features.stream(str(p), members)) == json.loads(json.dumps(feats))
    assert members == {"type": "FeatureCollection", "features": True, "totalFeatures": 123456789,
                       "numberMatched": 5, "crs": body["crs"]}
    p.write_text('{"type": "FeatureCollection", "features": [{"a": 1}, {"b": ')
    with pytest.raises(ValueError):
        list(_features.stream(str(p), {}))


def test_a_service_on_a_projected_crs_is_read_on_it_and_reprojected(env):
    """A WFS that answers in UTM names it in a crs member AFTER the features (GeoServer
    writes it last): the look at one feature learns it, every page is reprojected."""
    feats = []
    for f in _maga(2025):
        g = f["geometry"]
        feats.append({**f, "lonlat": g,
                      "geometry": transform_geom("EPSG:4326", "EPSG:32615", g) if g else None})
    env["host"].crs = "urn:ogc:def:crs:EPSG::32615"
    url = env["host"].wfs("lc:cobertura_utm", feats) + "&srsName=EPSG:32615"
    out = _submit("raster", _year(2025, url, clip=CLIP, rasterise=_rasterise(2025)))
    assert out["status"] == "approved", out
    row = tiffs.catalog()["landcover_gtcoast_2025"]
    assert any("reprojected from EPSG:32615 (the service's crs member)" in n for n in row["prepared"])
    with rasterio.open(get_settings().cache_dir / row["local_path"]) as src:
        agree = (src.read(1) == _by_hand(_maga(2025), _grouped(2025))).mean()
    assert agree > 0.999
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 3. preview
def test_preview_says_what_would_land_and_stages_nothing(env, log):
    url = env["host"].wfs("lc:cobertura_2025", _maga(2025))
    m = _year(2025, url, clip=CLIP, rasterise=_rasterise(2025))
    before = store.list_contributions()
    out = _submit("raster", {**m, "preview": True})
    log("OUTPUT", json.dumps(out)[:700])
    assert out["status"] == "preview"
    wl = out["would_land"]
    assert wl["classes"]["Mangrove forest"]["code"] == 6 and wl["classes"]["Mangrove forest"]["km2"] > 0
    assert wl["classes"]["Estuary"]["cells"] > 0 and wl["crs"] == "EPSG:4326"
    assert [st["op"] for st in wl["fetched"]["steps"]] == ["fetch", "rasterise"]
    assert any(n.startswith("rasterised at ingest") for n in wl["prepared"])
    assert store.list_contributions() == before
    assert "landcover_gtcoast_2025" not in tiffs.catalog() and not staging.STAGED_RASTERS
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 4. other packs
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


def test_a_vulnerability_map_drawn_as_polygons_in_utm_is_read_by_the_risk_pack(
        env, monkeypatch, log):
    """Beyond the asking pack: a plain GeoJSON (no service) in UTM, its classes named by
    label — no `classes`, the legend's labels map them — lands as a 1-5 class raster
    that the risk pack and a contributed extension pack read; the grid snaps to the
    cell size over the polygons."""
    def utm(geom):
        return transform_geom("EPSG:4326", "EPSG:32648", geom)
    feats = [{"type": "Feature", "properties": {"nivel": "Bajo"},
              "geometry": utm(mapping(sbox(103.0, 13.0, 103.02, 13.02).difference(
                  sbox(103.008, 13.008, 103.012, 13.012))))},
             {"type": "Feature", "properties": {"nivel": "Muy alto"},
              "geometry": utm(mapping(sbox(103.008, 13.008, 103.012, 13.012)))}]
    raw = json.dumps({"type": "FeatureCollection", "features": feats, "crs": {
        "type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32648"}}}).encode()
    url = env["host"].put("vulnerabilidad.geojson", raw)
    m = {"layer": "vulnerability_toy_poly", "url": url, "title": "Polygon vulnerability",
         "description": "five classes drawn as polygons", "source": "s", "license": "CC0-1.0",
         "vintage": "2026-10", "legend": {1: "Bajo", 3: "Medio", 5: "Muy alto"},
         "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 5, "nodata": 0},
         "rasterise": {"field": "nivel", "resolution": "100 m"}}
    out = _submit("raster", m)
    assert out["status"] == "approved", out
    row = tiffs.catalog()["vulnerability_toy_poly"]
    st = row["fetched"]["steps"]
    assert [s["op"] for s in st] == ["fetch", "rasterise"] and st[0]["sha256"] == _sha(raw)
    assert st[1]["inputs"]["classes"] == {"Bajo": 1, "Muy alto": 5}
    assert st[1]["inputs"]["resolution"] == {"given": "100 m", "degrees": 0.0008983}
    x0, y0 = st[1]["inputs"]["grid"]["transform"][2], st[1]["inputs"]["grid"]["transform"][5]
    assert abs(x0 / 0.0008983 - round(x0 / 0.0008983)) < 1e-6         # on the lattice
    assert any("reprojected from EPSG:32648" in n for n in row["prepared"])
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY,
                          layers=["vulnerability_toy_poly"])
    assert p["status"] == "ok", p.get("note")
    s = p["layers"]["vulnerability_toy_poly"]["summary"]
    log("OUTPUT", json.dumps(s)[:400])
    assert set(s["classes"]) == {"1", "5"} and s["classes"]["5"]["cells"] > 0
    _toy_pack(monkeypatch)
    t = assemble.assemble(pack="toy", place="Toy site", layers=["vulnerability_toy_poly"])
    assert t["status"] == "ok", t.get("note")
    assert t["layers"]["vulnerability_toy_poly"]["summary"]["classes"] == s["classes"]
    assert _no_leftovers(env)


def test_a_zip_of_a_shapefile_given_as_a_raster_proposes_rasterise(env):
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("uso/uso.shp", b"\x00\x00\x27\x0a" + b"\0" * 96)
        zf.writestr("uso/uso.dbf", b"x")
    url = env["host"].put("uso_suelo.zip", z.getvalue())
    out = _submit("raster", _year(2025, url))
    assert out["status"] == "declined"
    assert "it holds vector data" in out["problems"][0]
    assert "— or, if it is a POLYGON map of classes" in out["problems"][0]
    arr = np.full((20, 20), 6, "uint8")
    tif = env["host"].put("lc_2025.tif", _gtiff(arr, "uint8", bounds=(-91.2, 13.8, -91.0, 13.95)))
    out = _submit("raster", {**_year(2025, tif), "declared": {**DECL, "dtype": "int16"}})
    assert out["status"] == "declined" and "dtype" in out["problems"][0]
    assert "POLYGON map" not in " ".join(out["problems"])        # a raster: nothing proposed


# --------------------------------------------------------------------------- 5. budgets, safety
def test_a_layer_over_the_vertex_budget_is_declined(env, monkeypatch):
    url = env["host"].wfs("lc:cobertura_2025", _maga(2025))
    monkeypatch.setattr(get_settings(), "grp_prep_max_vertices", 50)
    out = _submit("raster", _year(2025, url, clip=CLIP, rasterise=_rasterise(2025)))
    assert out["status"] == "declined"
    assert "vertices, over the 50 one rasterise step may touch" in out["problems"][0]
    assert _no_leftovers(env)


def test_a_service_that_pages_inconsistently_is_declined(env):
    env["host"].dupes = True
    url = env["host"].wfs("lc:cobertura_2025", _maga(2025))
    out = _submit("raster", _year(2025, url, clip=CLIP, rasterise=_rasterise(2025)))
    assert out["status"] == "declined"
    assert "twice while being paged — it does not page in a stable order" in out["problems"][0]
    assert _no_leftovers(env)


def test_a_link_that_pages_itself_and_an_identifier_field_are_declined_in_words(env):
    url = env["host"].wfs("lc:cobertura_2025", _maga(2025))
    out = _submit("raster", _year(2025, url + "&count=10", clip=CLIP, rasterise=_rasterise(2025)))
    assert out["problems"] == ["the link sets count — drop it: the platform pages the service "
                               "itself and reads every feature it sends"]
    big = [{"type": "Feature", "properties": {"gid": i, "C_USO": 314},
            "geometry": mapping(sbox(CLIP[0] + i * 0.002, 13.81, CLIP[0] + i * 0.002 + 0.001, 13.82))}
           for i in range(60)]
    url = env["host"].put("parcels.geojson", json.dumps({"type": "FeatureCollection",
                                                        "features": big}).encode())
    out = _submit("raster", _year(2025, url, rasterise={"field": "gid", "resolution": RES}))
    assert out["status"] == "declined"
    assert "gid looks like an identifier (60 values over 60 polygons), not a class" in out["problems"][0]
    assert _no_leftovers(env)


def test_a_previous_step_s_file_is_read_in_place(env, monkeypatch, tmp_path):
    """An assemble step that made the polygon layer hands it on: rasterise reads ctx.path
    and fetches nothing itself."""
    feats = _maga(2025)
    raw = json.dumps({"type": "FeatureCollection", "features": feats}).encode()

    class _Made:
        NAME, STEP, KINDS, NEEDS_INPUT = "made_here", "assemble", ("raster",), False
        FIELDS = {"made_here": "a test op that makes the layer"}

        @staticmethod
        def validate(m):
            return []

        @staticmethod
        def run(ctx):
            out = ctx.tmp(".geojson", "made")
            Path(out).write_bytes(raw)
            return prepare.PreparedFile(out, ["made the layer"], {"inputs": {"n": len(feats)}})

        @staticmethod
        def suggest(s):
            return None
    monkeypatch.setattr(prepare, "_modules", lambda: (_Made, rasterise, recode))
    never = f"http://127.0.0.1:{env['host'].port}/never.zip"
    out = _submit("raster", _year(2025, never, clip=CLIP, made_here=True,
                                  rasterise=_rasterise(2025)))
    assert out["status"] == "approved", out
    f = tiffs.catalog()["landcover_gtcoast_2025"]["fetched"]
    assert [st["op"] for st in f["steps"]] == ["made_here", "rasterise"]
    with rasterio.open(get_settings().cache_dir
                       / tiffs.catalog()["landcover_gtcoast_2025"]["local_path"]) as src:
        assert np.array_equal(src.read(1), _by_hand(feats, _grouped(2025)))
    assert env["host"].log == [] and _no_leftovers(env)
