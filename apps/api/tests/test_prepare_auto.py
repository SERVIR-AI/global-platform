"""The platform preparing a contribution BY ITSELF when no person's choice is involved
(contrib/prepare: detect before anything is fetched, auto after a decline) — and asking,
in the "missing required field ... ask the contributor ... never guess" form, when one is.

The sprint cases, from PLAIN manifests (no preparation field), as a participant's Claude
sends them: the CR_rios Drive FOLDER link assembles itself; the CR soils FeatureServer
query (1,088 features behind a 1,000-record limit) and a WFS GetFeature page themselves
whole; the GBIF search and the OBIS /points link become point layers; the GMW zip of tiles
given with a clip mosaics itself; a gzipped GeoPackage decompresses itself; the CATIE 1985
year whose labels are the stack's under other codes recodes itself. What stays a question:
the area of a layer over the cap (now with cut: true, the features cut at its edge, and a
raster instead), which layer of several, what codes mean when the labels differ (a mapping
to confirm), a CRS when the .prj is missing. Never: a field the contributor gave
overridden, a retry loop, two submissions sharing what they raised; a plain GeoJSON,
zipped shapefile, GeoTIFF, table, document or feed takes exactly the path it took before.
Beyond the asking pack: the risk pack counts a point layer that paged itself in.

Everything is local: loopback HTTP servers play the hosts; no test touches the network."""

import gzip
import ipaddress
import json
import re
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlparse

import numpy as np
import pytest
import shapely
import yaml
from shapely.geometry import box as _bbox, mapping, shape

from app.config import get_settings
from app.contrib import fetch_policy, identity, prepare, staging
from app.contrib.prepare import api_points, clip, from_folder
from app.graph.geo import ingest, schema, tiffs, vecio, vectors
from app.mcp import assemble, registry, store
from app.risk import synthesis as risk

from test_prepare_api_points import API_HOSTS, OBIS_Q, QUERY, _Host as _ApiHost, _handler as _api_handler
from test_prepare_clip import (GULF, RISK_POLY, _FeatureServer, _GeoServer, _admin, _gpkg,
                               _hits, _mangroves, _shapefile, _soils, _square, _zip)
from test_prepare_from_folder import (NICOYA_POLY, WGS84, _hand_zip, _hydrosheds_folder,
                                      _catie_folder, _rivers, _with, drive)  # noqa: F401  (fixture reuse)
from test_prepare_mosaic import GMW_BOX, _gmw, _gmw_name, _gmw_year, _gtiff
from test_prepare_recode import _catie, _land_1956, _utm, _year as _lc_year

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")


# --------------------------------------------------------------------------- a host
class _Host:
    """Files (answering range requests) and apps answering a path prefix."""

    def __init__(self):
        self.files, self.apps, self.log, self.port = {}, {}, [], None

    def put(self, name, data, ranges=True):
        self.files["/" + name] = data
        return f"http://127.0.0.1:{self.port}/{name}"

    def app(self, prefix, fn):
        self.apps[prefix] = fn
        return f"http://127.0.0.1:{self.port}{prefix}"

    def asked(self, path):
        return [q for p, q in self.log if p == path]


def _handler(host):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body):
            u = urlparse(self.path)
            q = dict(parse_qsl(u.query, keep_blank_values=True))
            host.log.append((u.path, q))
            rng, code = None, 200
            for prefix, fn in host.apps.items():
                if u.path.startswith(prefix):
                    code, ctype, data = fn(u.path, q)
                    break
            else:
                ctype, data = "application/octet-stream", host.files.get(u.path)
                rng = self.headers.get("Range")
            if data is None:
                self.send_response(404)
                self.end_headers()
                return
            if rng:
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                a = int(m.group(1))
                b = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                data, code = data[a:b + 1], 206
            self.send_response(code)
            if rng:
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(host.files[u.path])}")
            self.send_header("Content-Type", ctype)
            if ctype == "application/octet-stream":
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{u.path.rsplit("/", 1)[-1]}"')
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


def _serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def env(tmp_path, monkeypatch, drive):
    """A hermetic platform: an empty land-cover catalog, the risk pack's toy world (a
    flood hazard and a 1-5 vulnerability layer near 103 E, 13 N), auto-approve, the
    platform's own preparation on, the real fetch policy with the loopback hosts allowed
    (a file host, a Drive, the GBIF and OBIS APIs), and a hermetic store."""
    host, apis = _Host(), _ApiHost()
    srv, api_srv = _serve(_handler(host)), _serve(_api_handler(apis))
    host.port, apis.port = srv.server_address[1], api_srv.server_address[1]
    real = fetch_policy._addresses
    monkeypatch.setattr(fetch_policy, "_addresses", lambda name: (
        [ipaddress.ip_address("127.0.0.1")] if name in API_HOSTS else real(name)))
    s = get_settings()
    cache = tmp_path / "cache"
    tdir = cache / "tiffs"
    tdir.mkdir(parents=True)
    haz = np.zeros((20, 20))
    haz[:, :10] = 3
    (tdir / "hazard_toyflood.tif").write_bytes(_gtiff(haz, "int16", None, bounds=(103.0, 13.0, 103.02, 13.02)))
    (tdir / "vulnerability_toy.tif").write_bytes(_gtiff(np.ones((20, 20)), "uint8", None,
                                                        bounds=(103.0, 13.0, 103.02, 13.02)))
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
                 "grp_mattermost_webhook": "", "grp_auto_approve": True, "grp_prep_auto": True,
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
    yield {"tmp": tmp_path, "cache": cache, "host": host, "apis": apis, "drive": drive}
    monkeypatch.undo()
    registry.reload_declarative_feeds()
    for x in (srv, api_srv):
        x.shutdown()
        x.server_close()


def _submit(kind, m):
    tok = identity.bind(OWNER)
    try:
        return staging.submit(kind, m, OWNER)
    finally:
        identity.unbind(tok)


def _layer(name, url, **over):
    return {"layer": name, "url": url, "title": f"{name} (synthetic)",
            "description": "shaped like a sprint source", "source": "test fixture",
            "license": "unstated", "vintage": "unstated", "countries": ["Costa Rica"], **over}


def _raw(layer) -> bytes:
    with open(vectors.master_path(vectors.landed()[layer]), "rb") as f:
        return f.read()


def _record(out) -> dict:
    return store.load_contribution(out["contribution_id"])


def _fields(out) -> list:
    return [p["fields"] for p in out.get("prepared_automatically") or []]


def _no_leftovers(env):
    prep = env["cache"] / "prep"
    strays = [*(env["cache"] / "tiffs").glob("staged-tmp-*"),
              *(env["cache"] / "vectors").glob("staged-tmp-*")]
    return (not prep.exists() or not list(prep.iterdir())) and not strays


def _cr_rios():
    """The CR_rios folder as shared: the line shapefile's parts beside .sbn/.sbx indexes,
    .xml metadata and an editor's lock file."""
    files = _with(_rivers(), "CR_rios", **{".prj": WGS84.encode(), ".sbn": b"s", ".sbx": b"s",
                                            ".shp.xml": b"<x/>"})
    return files, {**files, "CR_rios.shp.HUB-PC.3416.2992.sr.lock": b""}


# --------------------------------------------------------------------------- 1. the framework
def test_the_hooks_are_found_on_the_op_modules_like_the_rest():
    """detect and auto are optional per op, derived from the modules; cut is a vector
    field the form lists; the new situation is declared."""
    has = {op.NAME: (callable(getattr(op, "detect", None)), callable(getattr(op, "auto", None)))
           for op in prepare.ops()}
    assert has["from_folder"] == (True, False) and has["clip"] == (True, True)
    assert has["api_points"] == (True, False) and has["mosaic"] == (False, True)
    assert has["recode"] == (False, True) and has["rasterise"] == (False, False)
    assert "vector_too_large" in prepare.SITUATIONS
    assert staging.VECTOR_FIELDS["optional"]["cut"] == clip.FIELDS["cut"]
    assert "ASK them, never set it unasked" in clip.FIELDS["cut"]
    assert "cut" not in staging._RASTER_KNOWN
    said = staging.describe_submit()
    assert ("When nothing in it is a person's choice (api_points, from_folder, mosaic, recode, "
            "clip: a Drive FOLDER link") in said and "prepared_automatically" in said


def _fake(name, kinds=("vector",), detect=None, auto=None, fields=None):
    return SimpleNamespace(NAME=name, STEP="assemble", KINDS=kinds,
                           FIELDS=fields or {name: "a test field"}, validate=lambda m: [],
                           run=lambda ctx: None, suggest=lambda s: None,
                           **({"detect": detect} if detect else {}), **({"auto": auto} if auto else {}))


def test_a_contributors_field_is_never_overridden_unless_restated_and_kept(monkeypatch):
    """A proposal touching a field the contributor gave is dropped; one that RESTATES it
    (the same content as the file lands) is applied and the given value kept."""
    a = _fake("a", detect=lambda k, m: {"fields": {"a": True, "title": "theirs?"}, "why": "w"})
    b = _fake("b", detect=lambda k, m: {"fields": {"b": True, "url": "https://x.test/records"},
                                        "restates": ["url"], "why": "the same query's records"})
    monkeypatch.setattr(prepare, "_modules", lambda: (a, b))
    m = {"url": "https://x.test/points", "title": "mine"}
    auto = prepare.Auto("vector", m)
    assert auto.manifest == {**m, "b": True, "url": "https://x.test/records"}
    assert auto.shown() == [{"fields": {"b": True, "url": "https://x.test/records"},
                             "why": "the same query's records",
                             "given": {"url": "https://x.test/points"}}]
    assert auto.record() == {"prepared_automatically": auto.shown()}
    # the op's own field given: its detect is not even asked
    asked = []
    c = _fake("c", detect=lambda k, m: asked.append(m) or {"fields": {"c": 2}, "why": "w"})
    monkeypatch.setattr(prepare, "_modules", lambda: (c,))
    assert prepare.Auto("vector", {"url": "u", "c": 1}).manifest == {"url": "u", "c": 1}
    assert asked == []
    # off, or a kind no op prepares: nothing is asked
    assert prepare.Auto("vector", {"url": "u"}, on=False).manifest == {"url": "u"} and asked == []
    assert prepare.Auto("document", {"url": "u"}).on is False and asked == []


def test_no_retry_loop_never_the_same_fields_twice_and_at_most_three_rounds(monkeypatch):
    """again() retries only for fields not tried yet, at most MAX_ROUNDS times; an op
    proposing the same fields again stops it."""
    same = _fake("same", auto=lambda s: {"fields": {"same": True}, "why": "w"})
    monkeypatch.setattr(prepare, "_modules", lambda: (same,))
    auto = prepare.Auto("vector", {"url": "u"})
    seen = [{"event": "declined", "kind": "vector", "problem": "p"}]
    assert auto.again([]) is False                       # nothing raised: nothing to do
    assert auto.again(seen) is True and auto.manifest["same"] is True
    assert auto.again(seen) is False and auto.rounds == 1
    # each round's decline resolved by another op: three rounds, then the decline stands
    chain = [_fake(f"op{i}", auto=(lambda i: lambda s: {"fields": {f"op{i}": True}, "why": "w"}
                                   if s.get("problem") == f"p{i}" else None)(i)) for i in range(5)]
    monkeypatch.setattr(prepare, "_modules", lambda: tuple(chain))
    auto = prepare.Auto("vector", {"url": "u"})
    rounds = [auto.again([{"event": "declined", "kind": "vector", "problem": f"p{i}"}])
              for i in range(5)]
    assert rounds == [True, True, True, False, False] and prepare.MAX_ROUNDS == 3
    assert [p["fields"] for p in auto.applied] == [{"op0": True}, {"op1": True}, {"op2": True}]


def test_concurrent_attempts_never_share_what_they_raised(env, log):
    """Each attempt's situations are its own (a context variable), across threads; and two
    whole submissions at once — a Drive folder, a plain GeoJSON — each say only their own."""
    barrier = threading.Barrier(2)
    seen = {}

    def run(name):
        with prepare.attempt() as mine:
            barrier.wait()
            prepare.suggest({"event": "declined", "kind": "vector", "manifest": {},
                             "problem": name})
            barrier.wait()
            seen[name] = [x["problem"] for x in mine]
    ts = [threading.Thread(target=run, args=(n,)) for n in ("a", "b")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert seen == {"a": ["a"], "b": ["b"]} and prepare._SEEN.get() is None

    _, folder = _cr_rios()
    url = env["drive"].folder(folder)
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"k": 1}, "geometry": mapping(_square(-85.1, 10.0))}]}
    plain = env["host"].put("plots.geojson", json.dumps(fc).encode())
    store.init()                       # as the server does at boot: the ledger exists
    with ThreadPoolExecutor(2) as ex:
        a, b = ex.map(lambda m: _submit("vector", m),
                      [_layer("cr_rios_t", url), _layer("cr_plots_t", plain)])
    log("OUTPUT", f"{a['status']} {_fields(a)} | {b['status']} {_fields(b)}")
    assert a["status"] == b["status"] == "approved", (a, b)
    assert _fields(a) == [{"from_folder": True}] and "prepared_automatically" not in b
    assert "prepared_automatically" not in _record(b) and "from_folder" not in _record(b)["manifest"]


# --------------------------------------------------------------------------- 2. by itself
def test_the_cr_rios_folder_link_assembles_itself_and_the_record_says_the_platform_chose_it(env, log):
    """The CR_rios case: the folder link as shared lands as the hand zip lands; the reply
    says what the platform did in words the client relays; the stored manifest holds
    from_folder (marked the platform's), so replaying it lands the same bytes; the
    land-cover pack measures it."""
    files, folder = _cr_rios()
    url = env["drive"].folder(folder)
    m = _layer("cr_rios", url, group_field="NAME")
    out = _submit("vector", m)
    log("OUTPUT", out.get("message"))
    assert out["status"] == "approved", out
    why = from_folder.detect("vector", m)["why"]
    assert out["prepared_automatically"] == [{"fields": {"from_folder": True}, "why": why}]
    assert out["message"] == (
        "The platform prepared this contribution by itself — the url is a Google Drive FOLDER, so "
        "the platform lists it and assembles the layer from the folder's own files (a shapefile's "
        "parts zipped on the server): from_folder: true. No choice of the contributor's was "
        "involved; tell them what was done. The stored manifest holds these fields, marked as "
        "the platform's")
    hand, _ = vecio.to_geojson(_hand_zip(files), "CR_rios.zip")
    assert _raw("cr_rios") == hand
    rec = _record(out)
    assert rec["manifest"] == {**m, "from_folder": True}
    assert rec["prepared_automatically"] == out["prepared_automatically"]
    row = vectors.landed()["cr_rios"]
    assert row["fetched"]["prepared_automatically"] == out["prepared_automatically"]
    assert any(n.startswith("prepared by the platform itself, no person's choice involved — the "
                            "url is a Google Drive FOLDER") for n in row["prepared"])
    assert "left out: CR_rios.sbn, CR_rios.sbx, CR_rios.shp.HUB-PC.3416.2992.sr.lock, CR_rios.shp.xml" \
        in row["prepared"][0]
    # a receipt replays exactly: the stored manifest lands the same, with nothing left to choose
    again = _submit("vector", {**rec["manifest"], "layer": "cr_rios_replay"})
    assert again["status"] == "approved" and "prepared_automatically" not in again
    assert _raw("cr_rios_replay") == hand
    p = assemble.assemble(pack="land-cover", place="Tempisque (synthetic)", geometry=NICOYA_POLY,
                          vectors=["cr_rios"])
    assert p["status"] == "ok", p.get("note")
    assert p["vectors"]["cr_rios"]["total"] > 0
    assert _no_leftovers(env)


def test_preview_applies_the_same_and_stages_nothing(env):
    _, folder = _cr_rios()
    url = env["drive"].folder(folder)
    pv = _submit("vector", {**_layer("cr_rios_pv", url), "preview": True})
    assert pv["status"] == "preview", pv
    assert _fields(pv) == [{"from_folder": True}] and "by itself" in pv["message"]
    assert pv["would_land"]["features"] == 4 and pv["would_land"]["geometry"] == "lines"
    assert pv["would_land"]["fetched"]["prepared_automatically"][0]["fields"] == {"from_folder": True}
    assert "cr_rios_pv" not in vectors.landed() and not staging.STAGED_VECTORS
    assert store.list_contributions() == [] and _no_leftovers(env)


def test_a_featureserver_query_pages_itself_whole_and_the_risk_pack_counts_a_fresh_one(env, log):
    """The CR soils case: the query url as given (1,000 of 1,088 per request) pages itself
    whole. Beyond the asking pack: a hub's shelters behind a FeatureServer land the same
    way and the risk pack counts them."""
    host = env["host"]
    layer = host.app("/arcgis/rest/services/Suelos_de_Costa_Rica_2015/FeatureServer/0",
                     _FeatureServer(_soils()))
    url = layer + "/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"
    out = _submit("vector", _layer("cr_suelos", url, group_field="ORDENES"))
    log("OUTPUT", out.get("message"))
    assert out["status"] == "approved", out
    assert _fields(out) == [{"whole": True}]
    assert "an ArcGIS FeatureServer/MapServer layer, so the platform pages it until every " \
           "feature the server counts is in" in out["message"]
    row = vectors.landed()["cr_suelos"]
    assert row["features"] == 1088
    inp = row["fetched"]["steps"][0]["inputs"]
    assert inp["whole"] is True and [p["features"] for p in inp["pages"]] == [1000, 88]
    assert _record(out)["manifest"]["whole"] is True
    pages = [q for p, q in host.log if p.endswith("/query") and q.get("f") == "geojson"]
    assert len(pages) == 2                                    # read once, never again

    shelters = [{"type": "Feature", "id": i + 1,
                 "properties": {"OBJECTID": i + 1, "name": f"Shelter {i}"},
                 "geometry": {"type": "Point", "coordinates": [103.006 + 0.0007 * i, 13.01]}}
                for i in range(12)]
    url = host.app("/arcgis/rest/services/Shelters/FeatureServer/0",
                   _FeatureServer(shelters, max_records=5)) + "/query?where=1%3D1&f=geojson"
    out = _submit("vector", _layer("toy_shelters", url, name_field="name", countries=["Toyland"]))
    assert out["status"] == "approved", out
    assert _fields(out) == [{"whole": True}] and vectors.landed()["toy_shelters"]["features"] == 12
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY)
    assert p["status"] == "ok", p.get("note")
    exp = next(c for c in p["citations"] if c.get("title", "").startswith("toy_shelters (synthetic) vs"))
    log("OUTPUT", exp["text"][:300])
    assert "contributed layer" in exp["text"]
    assert _no_leftovers(env)


def test_a_wfs_getfeature_pages_itself_and_a_capabilities_url_is_left_alone(env):
    feats = [{"type": "Feature", "id": f"v.{i}", "properties": {"ctn2": "MANGLAR"},
              "geometry": mapping(_square(-80.6 + (i % 30) * 0.02, -3.0 + (i // 30) * 0.03, 0.015))}
             for i in range(300)]
    base = env["host"].app("/geoserver/mae_ide/wfs", _GeoServer(feats, server_max=120))
    url = (base + "?service=WFS&version=2.0.0&request=GetFeature&typeNames="
           "mae_ide:v_ff010_cobertura_vegetal_2022_a&outputFormat=SHAPE-ZIP")
    out = _submit("vector", _layer("ec_luc_2022", url, countries=["Ecuador"]))
    assert out["status"] == "approved", out
    assert _fields(out) == [{"whole": True}] and "a WFS GetFeature request" in out["message"]
    row = vectors.landed()["ec_luc_2022"]
    assert row["features"] == 300
    assert [p["features"] for p in row["fetched"]["steps"][0]["inputs"]["pages"]] == [120, 120, 60]
    # a WFS 1.1 request with its own bbox: the clip op would decline it (axis order), so it
    # is never applied unasked — the url takes the path it took before
    v11 = (base + "?service=WFS&version=1.1.0&request=GetFeature&typeName="
           "mae_ide:v_ff010_cobertura_vegetal_2022_a&bbox=-3,-81,-2,-80")
    assert clip.detect("vector", {"url": v11}) == {"fields": {"whole": True}, "why": clip._WHOLE_SERVICE.format(
        what="a WFS GetFeature request")}
    assert prepare.detect("vector", {"url": v11}) == []
    caps = env["host"].app("/geoserver/caps/wfs", lambda p, q: (200, "text/xml", (
        b'<?xml version="1.0"?><wfs:WFS_Capabilities version="2.0.0"></wfs:WFS_Capabilities>')))
    url = caps + "?service=WFS&request=GetCapabilities"
    assert clip.detect("vector", {"url": url}) is None
    out = _submit("vector", _layer("ec_caps", url))
    assert out["status"] == "declined" and "prepared_automatically" not in out
    assert "answered with a WFS service description (XML)" in out["problems"][0]


def test_the_gbif_search_and_the_obis_points_link_become_point_layers_by_themselves(
        env, monkeypatch, log):
    """S2-GBIF and S2-OBIS as delivered: the search pages itself; the /points link is read
    as the same query's records (said in why, the given url kept); the contributor's own
    api_points with the /points url stays their words, declined as before."""
    apis = env["apis"]
    url = apis.url("api.gbif.org", f"/v1/occurrence/search?{QUERY}")
    out = _submit("vector", _layer("cr_gbif", url, name_field="species"))
    assert out["status"] == "approved", out
    assert _fields(out) == [{"api_points": "gbif"}] and "a query of GBIF's occurrence search" in out["message"]
    assert vectors.landed()["cr_gbif"]["features"] == 1984

    monkeypatch.setitem(api_points.PRESETS["obis"], "paging",
                        {"after": "after", "after_field": "id", "limit": "size", "page_size": 1000})
    points = apis.url("api.obis.org", f"/v3/occurrence/points?{OBIS_Q}")
    records = apis.url("api.obis.org", f"/v3/occurrence?{OBIS_Q}")
    out = _submit("vector", _layer("cr_obis", points))
    log("OUTPUT", out.get("message"))
    assert out["status"] == "approved", out
    pa = out["prepared_automatically"]
    assert pa[0]["fields"] == {"api_points": "obis", "url": records}
    assert pa[0]["given"] == {"url": points}
    assert "answering the records' locations without their attributes, so the platform reads " \
           "the same query's records at api.obis.org/v3/occurrence instead" in pa[0]["why"]
    assert _record(out)["manifest"]["url"] == records
    assert vectors.landed()["cr_obis"]["features"] == 4468
    out = _submit("vector", _layer("cr_obis_2", points, api_points="obis"))
    assert out["status"] == "declined" and "prepared_automatically" not in out
    assert "answers the records' locations without their attributes" in out["problems"][0]


def test_a_zip_of_named_tiles_with_a_clip_mosaics_itself_and_other_rasters_stay_a_question(env, log):
    """The GMW case: the zip of tiles with the area joins the tiles it touches by itself
    (after the gate names the zip's rasters) and lands what the hand mosaic is; a zip of
    rasters that are not tiles (two climate variables) asks which one."""
    raw, hand = _gmw()
    url = env["host"].put("gmw_v3_2020_gtiff.zip", raw)
    out = _submit("raster", _gmw_year("landcover_crgmw_2020", url, clip=GMW_BOX))
    log("OUTPUT", out.get("message"))
    assert out["status"] == "approved", out
    assert _fields(out) == [{"mosaic": "auto"}]
    assert "a zip of 18 raster tiles named by where they lie and a clip is given" in out["message"]
    row = tiffs.catalog()["landcover_crgmw_2020"]
    import rasterio
    with rasterio.open(get_settings().cache_dir / row["local_path"]) as src:
        assert np.array_equal(src.read(1), hand)                    # cell for cell
    assert row["fetched"]["prepared_automatically"][0]["fields"] == {"mosaic": "auto"}
    assert _record(out)["manifest"]["mosaic"] == "auto"

    bio = _zip({f"wc2.1_30s_bio_{k}.tif": _gtiff(np.full((30, 30), k, "uint8"), "uint8", None,
                                                   bounds=(-86, 8, -82, 12)) for k in (1, 12)})
    url = env["host"].put("wc2.1_30s_bio.zip", bio)
    out = _submit("raster", _gmw_year("landcover_crbio_2020", url, clip=GMW_BOX))
    assert out["status"] == "declined" and "prepared_automatically" not in out
    assert "name the one to contribute with clip_member" in out["problems"][0]
    assert "ASK the contributor which" in out["problems"][0]
    assert _no_leftovers(env)


def test_a_gzipped_geopackage_decompresses_itself_whether_or_not_its_link_says_so(env, tmp_path):
    """GMW v4.1 as a .gpkg.gz: read whole by itself before anything is fetched; a gzipped
    GeoJSON behind a link that does not say so is read once by the gate, then whole."""
    geoms, props = _mangroves()
    gz = gzip.compress(_gpkg(tmp_path / "gmw.gpkg", "gmw_v4_2020", geoms, props))
    url = env["host"].put("gmw_v4_2020_vec.gpkg.gz", gz)
    out = _submit("vector", _layer("gl_gmw_v4_2020", url))
    assert out["status"] == "approved", out
    assert _fields(out) == [{"whole": True}] and "the url is a gzipped layer" in out["message"]
    assert vectors.landed()["gl_gmw_v4_2020"]["features"] == len(geoms)
    assert len(env["host"].asked("/gmw_v4_2020_vec.gpkg.gz")) == 1
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"k": i}, "geometry": mapping(_square(-85 + i * .01, 10, .005))}
        for i in range(40)]}
    url = env["host"].put("download", gzip.compress(json.dumps(fc).encode()))
    assert clip.detect("vector", {"url": url}) is None
    out = _submit("vector", _layer("cr_squares", url))
    assert out["status"] == "approved", out
    assert _fields(out) == [{"whole": True}] and vectors.landed()["cr_squares"]["features"] == 40
    assert len(env["host"].asked("/download")) == 2               # the gate's read, then whole
    assert _no_leftovers(env)


def test_a_year_whose_labels_are_the_stacks_under_other_codes_recodes_itself(env, log):
    """CATIE 1985 as delivered — 10/20, labelled 'mangrove' / 'Non mangrove' — joins the 1956
    stack (1 Mangrove / 2 Non-mangrove) by itself: the recode, the stack's legend and the
    declared range as it lands; the given legend and declared are kept on the record."""
    _land_1956(env, "landcover_gulf")
    _, y85, _, hand85, _ = _catie()
    url = env["host"].put("MANGLE_1985_RCLSS.IMG", _utm(y85, "int8", -128, driver="HFA"))
    legend = {10: "mangrove", 20: "Non mangrove"}
    decl = {"dtype": "int8", "valid_min": 10, "valid_max": 20, "nodata": -128}
    out = _submit("raster", _lc_year("landcover_gulf_1985", url, legend=legend,
                                     outside_values=[-128], declared=decl))
    log("OUTPUT", out.get("message"))
    assert out["status"] == "approved", out
    pa = out["prepared_automatically"][0]
    # the stack's nodata (3) and outside values ([3]) as the 1956 year carries them
    assert pa["fields"] == {"recode": {10: 1, 20: 2}, "legend": {1: "Mangrove", 2: "Non-mangrove"},
                            "declared": {**decl, "valid_min": 1, "valid_max": 2, "nodata": 3},
                            "outside_values": [3]}
    assert pa["given"] == {"legend": legend, "declared": decl, "outside_values": [-128]}
    assert "names the classes of the stack 'landcover_gulf', {1: Mangrove, 2: Non-mangrove}, " \
           "under other codes or spellings" in pa["why"]
    cat = tiffs.catalog()
    assert cat["landcover_gulf"]["years"] == [1956, 1985]
    rec = _record(out)
    assert rec["manifest"]["recode"] == {"10": 1, "20": 2}
    assert rec["manifest"]["legend"] == {"1": "Mangrove", "2": "Non-mangrove"}
    assert [st["op"] for st in cat["landcover_gulf_1985"]["fetched"]["steps"]] == ["fetch", "recode"]
    # what lands is what the manifest the agent would have filled from the proposal lands
    _land_1956(env, "landcover_twin")
    twin = _submit("raster", {**rec["manifest"], "layer": "landcover_twin_1985",
                              "stack": "landcover_twin"})
    assert twin["status"] == "approved" and "prepared_automatically" not in twin
    import rasterio
    cat = tiffs.catalog()
    mine, theirs = (get_settings().cache_dir / cat[f"{sid}_1985"]["local_path"]
                    for sid in ("landcover_gulf", "landcover_twin"))
    with rasterio.open(mine) as a, rasterio.open(theirs) as b:
        assert np.array_equal(a.read(1), b.read(1)) and a.nodata == b.nodata == 3
        assert set(np.unique(a.read(1))) == {3, 1, 2}
    for y in (1956, 1985):                   # the year lands as the stack's years do
        assert cat[f"landcover_gulf_{y}"]["outside_values"] == [3]
        assert schema.schema_for(f"landcover_gulf_{y}")["nodata"] == 3
    assert int((hand85 == 1).sum()) > 0
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 3. asked
def test_what_codes_mean_when_the_labels_differ_is_asked_with_a_mapping_to_confirm(env):
    _land_1956(env, "landcover_bay")
    _, y85, _, _, _ = _catie()
    url = env["host"].put("bay_1985.img", _utm(y85, "int8", -128, driver="HFA"))
    out = _submit("raster", _lc_year("landcover_bay_1985", url, legend={10: "Manglar", 20: "Otro"},
                                     outside_values=[-128],
                                     declared={"dtype": "int8", "valid_min": 10, "valid_max": 20,
                                               "nodata": -128}))
    assert out["status"] == "declined" and "prepared_automatically" not in out
    said = out["problems"][0]
    assert ("the proposed mapping, to CONFIRM with the contributor before it is used: 10 "
            "'Manglar' -> 1 'Mangrove', 20 'Otro' -> 2 'Non-mangrove' — if they confirm it, "
            "resubmit the same link with recode: {10: 1, 20: 2}") in said
    assert "never guess" in said and env["host"].asked("/bay_1985.img") == []


def test_which_layer_of_several_and_a_missing_crs_are_asked_and_nothing_loops(env, log):
    """from_folder applies itself, then its own listing asks which layer; a shapefile with
    no .prj asks for its CRS with the neighbours' hint. Each folder is listed once."""
    tree, _, _ = _hydrosheds_folder()
    url = env["drive"].folder(tree)
    out = _submit("vector", _layer("cr_cuencas", url))
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined"
    assert out["problems"][0].startswith(f"missing required field 'layer_name': the folder {url} "
                                         "holds 3 layers")
    assert "ASK the contributor which one (never guess)" in out["problems"][0]
    assert _fields(out) == [{"from_folder": True}]
    assert out["message"].startswith("The platform applied by itself — the url is a Google Drive "
                                     "FOLDER") and "still declined" in out["message"]
    listed = [p for p in env["drive"].log if p.startswith("/listing/")]
    assert len(listed) == len(set(listed)) == 5                  # never a second round
    out = _submit("vector", _layer("cr_cuencas", url, layer_name="cuencas50mil_costeras2"))
    assert out["status"] == "approved", out
    assert _fields(out) == [{"from_folder": True}]

    tree, _, _ = _catie_folder()
    url = env["drive"].folder(tree)
    out = _submit("vector", _layer("cr_c8514_copy", url, layer_name="c8514_copy"))
    p = out["problems"][0]
    assert p.startswith("missing required field 'shapefile_crs': capas/MANGLE_NICOYA_CI/"
                        "c8514_copy.shp came without a .prj")
    assert "cambios_8514.prj names WGS 84 / UTM zone 16N (EPSG:32616)" in p and "never guess" in p
    assert _fields(out) == [{"from_folder": True}]
    assert _no_leftovers(env)


def test_a_layer_over_the_cap_asks_for_the_area_and_offers_cut_and_a_raster(env, monkeypatch, log):
    """The HDX admin3 / MAATE case: a layer that paged itself in is over the stored cap —
    the question is the area (never guessed), with cut: true and a raster contribution
    (rasterise) proposed; given an area and cut, the features crossing its edge are cut
    there, areas inside exact, and the record says which were cut."""
    host = env["host"]
    feats = _soils()
    layer = host.app("/arcgis/rest/services/Suelos_de_Costa_Rica_2015/FeatureServer/0",
                     _FeatureServer(feats))
    url = layer + "/query?where=1%3D1&outFields=*&outSR=4326&f=geojson"
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", 150_000)
    out = _submit("vector", _layer("cr_suelos_big", url))
    p = out["problems"][0]
    log("OUTPUT", p)
    assert out["status"] == "declined" and _fields(out) == [{"whole": True}]
    assert p.startswith(f"missing required field 'clip': the layer prepared from {url} is ")
    assert "as GeoJSON, over the" in p and "ask the contributor which area to keep" in p
    assert "never guess one" in p
    assert "with cut: true they are cut at the area's edge" in p and "ASK the contributor whether to cut" in p
    assert "contribute it as kind 'raster'" in p and "rasterise: {field:" in p
    assert len([q for path, q in host.log if path.endswith("/query") and q.get("f") == "geojson"]) == 2
    # far over (a paged GeoJSON over 3x the cap): asked the same, never parsed into memory
    real = vecio.to_geojson
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", 60_000)
    monkeypatch.setattr(vecio, "to_geojson", lambda *a, **k: pytest.fail("parsed"))
    out = _submit("vector", _layer("cr_suelos_far", url))
    assert out["problems"][0].startswith(f"missing required field 'clip': the layer prepared "
                                         f"from {url} is ")
    assert "as GeoJSON" not in out["problems"][0] and _fields(out) == [{"whole": True}]
    monkeypatch.setattr(vecio, "to_geojson", real)
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", 150_000)

    out = _submit("vector", _layer("cr_suelos_gulf", url, clip=GULF, cut=True))
    log("OUTPUT", json.dumps(out.get("preview", {}).get("entry", {}).get("prepared"))[:600])
    assert out["status"] == "approved", out
    assert "prepared_automatically" not in out                   # every field the contributor's
    row = vectors.landed()["cr_suelos_gulf"]
    fc = json.loads(_raw("cr_suelos_gulf"))
    gulf = _bbox(*GULF)
    want = [shape(f["geometry"]).intersection(gulf) for f in feats
            if shape(f["geometry"]).intersects(gulf)]
    want = [g for g in want if g.area > 0]
    assert row["features"] == len(want)
    got = sum(vectors.area_km2(shape(f["geometry"])) for f in fc["features"])
    assert got == pytest.approx(sum(vectors.area_km2(g) for g in want), rel=1e-6)
    assert all(gulf.buffer(1e-9).contains(shape(f["geometry"])) for f in fc["features"])
    inp = row["fetched"]["steps"][-1]["inputs"]
    assert inp["cut"]["features_cut"] > 0 and inp["clip"] == GULF
    assert any(n.startswith(f"cut {inp['cut']['features_cut']:,} of the") and
               "areas and lengths inside the box are exact; a cut feature's own attributes" in n
               for n in row["prepared"])
    assert clip.NEIGHBOURS in row["prepared"]
    assert row["fetched"]["cut"] is True
    assert _no_leftovers(env)


def test_an_archive_over_the_cap_is_read_whole_by_itself_and_a_level_too_big_asks(
        env, monkeypatch, log):
    """HDX's all-levels zip, over the cap as a download: with the level named it is read
    whole by itself (no hand split) and the cantons land as the hand-split zip would; the
    districts, over the stored cap as a layer, ask for the area; with neither a name nor
    an area, which level is asked. Over the vertex budget, the area is asked too."""
    levels = _admin()
    members = {}
    for stem, (g, p) in levels.items():
        members.update({f"cri_admin_boundaries/{k}": v for k, v in _shapefile(stem, g, p).items()})
    members["cri_admin_boundaries/cod-ab-cri.pdf"] = np.random.default_rng(1).bytes(300_000)
    raw = _zip(members)
    url = env["host"].put("cri_admin_boundaries.shp.zip", raw)
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", 100_000)
    assert len(raw) > 100_000
    out = _submit("vector", _layer("cr_admin2", url, layer_name="cri_admin2", name_field="adm2_name"))
    log("OUTPUT", out.get("message"))
    assert out["status"] == "approved", out
    assert _fields(out) == [{"whole": True}]
    assert "the file is over the size cap as a download, so the platform downloads it whole" \
        in out["message"]
    hand, _ = vecio.to_geojson(_zip(_shapefile("cri_admin2", *levels["cri_admin2"])),
                               "cri_admin2.zip", "cri_admin2")
    assert json.loads(_raw("cr_admin2"))["features"] == json.loads(hand)["features"]
    assert vectors.landed()["cr_admin2"]["features"] == 24
    out = _submit("vector", _layer("cr_admin3", url, layer_name="cri_admin3"))
    assert out["status"] == "declined" and _fields(out) == [{"whole": True}]
    assert out["problems"][0].startswith(f"missing required field 'clip': the layer prepared from "
                                         f"{url} is ")
    out = _submit("vector", _layer("cr_admin_x", url))
    assert out["status"] == "declined" and _fields(out) == [{"whole": True}]
    assert out["problems"][0].startswith(f"missing required field 'layer_name': the zip at {url} "
                                         "holds several shapefiles")
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", 50 << 20)
    monkeypatch.setattr(get_settings(), "grp_prep_max_vertices", 100)
    out = _submit("vector", _layer("cr_admin3_v", url, layer_name="cri_admin3", clip=GULF))
    assert "vertices, over the 100 one clip step may touch — ask the contributor for the area " \
           "that matters (clip) and resubmit with it, never guess one" in out["problems"][0]
    assert _no_leftovers(env)


def test_cut_cuts_a_file_layer_at_the_area_and_is_only_ever_the_contributors(env):
    """HDX's all-levels zip: districts cut at the gulf box land with exactly the area of
    their parts inside it; cut needs an area, is a vector field, and is never applied
    unasked."""
    levels = _admin()
    members = {}
    for stem, (g, p) in levels.items():
        members.update({f"cri_admin_boundaries/{k}": v for k, v in _shapefile(stem, g, p).items()})
    url = env["host"].put("cri_admin_boundaries.shp.zip", _zip(members))
    g3, _ = levels["cri_admin3"]
    out = _submit("vector", _layer("cr_admin3_gulf", url, layer_name="cri_admin3", clip=GULF, cut=True))
    assert out["status"] == "approved", out
    fc = json.loads(_raw("cr_admin3_gulf"))
    gulf = _bbox(*GULF)
    want = [g.intersection(gulf) for i, g in enumerate(g3) if i in _hits(g3, GULF)]
    got = sum(vectors.area_km2(shape(f["geometry"])) for f in fc["features"])
    assert got == pytest.approx(sum(vectors.area_km2(g) for g in want if g.area > 0), rel=1e-6)
    assert all(shapely.get_dimensions(shape(f["geometry"])) == 2 for f in fc["features"])
    assert _submit("vector", _layer("x_cut", url, cut=True))["problems"] == [
        "cut cuts the features at the clip's edge — give clip: the area (ASK the contributor "
        "for it; never guess one), or drop cut"]
    assert "cut must be true" in _submit("vector", _layer("x_cut", url, clip=GULF, cut="yes"))["problems"][0]
    out = _submit("vector", _layer("x_levels", url, whole=True))
    assert out["problems"][0].startswith(
        f"missing required field 'layer_name': the zip at {url} holds several shapefiles "
        "(cri_admin1, cri_admin2, cri_admin3) — ASK the contributor which one to contribute "
        "(never guess)")
    ras = _submit("raster", _gmw_year("landcover_x_2020", url, clip=GMW_BOX, cut=True))
    assert any("cut prepares a vector contribution (clip)" in p for p in ras["problems"])
    for name in ("vector_too_large", "declined", "vector_unreadable"):
        assert clip.auto({"event": name, "kind": "vector", "manifest": {"url": url}}) is None
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 4. unchanged
def _canon(out: dict) -> str:
    """A result as JSON, its ids and times taken out."""
    text = json.dumps(out, sort_keys=True, default=str)
    for k in ("contribution_id", "created_at", "updated_at"):
        if out.get(k):
            text = text.replace(str(out[k]), f"<{k}>")
    return re.sub(r"staged-[0-9a-f]{16}", "staged-<id>", text)


def test_plain_paths_take_exactly_the_path_they_took_before(env, monkeypatch, tmp_path):
    """A GeoJSON file, a zipped shapefile, a plain GeoTIFF: what would land is the same
    byte for byte with the platform's own preparation on and off, and no op's detect or
    auto proposes anything; a table, a document and a feed have no op to ask at all."""
    asked = []
    for op in prepare.ops():
        for hook in ("detect", "auto"):
            fn = getattr(op, hook, None)
            if callable(fn):
                monkeypatch.setattr(op, hook, (lambda f, n: lambda *a: (
                    lambda r: asked.append((n, r)) or r)(f(*a)))(fn, f"{op.NAME}.{hook}"))
    host = env["host"]
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"k": i}, "geometry": mapping(_square(-85 + i * .05, 10))}
        for i in range(5)]}
    g3, p3 = _admin()["cri_admin3"]
    shp = _zip(_shapefile("cri_admin3", g3, p3))
    raw, _ = _gmw()
    with zipfile.ZipFile(__import__("io").BytesIO(raw)) as z:
        tif = z.read(_gmw_name(10, -84))                      # a tile of 0 / 1 cells
    cases = [("vector", _layer("plain_geojson", host.put("plain.geojson", json.dumps(fc).encode()))),
             ("vector", _layer("plain_shp", host.put("cri_admin3.zip", shp))),
             ("raster", _gmw_year("landcover_plain_2020", host.put("plain.tif", tif)))]
    for kind, m in cases:
        assert prepare.detect(kind, m) == []
        on = _submit(kind, {**m, "preview": True})
        monkeypatch.setattr(get_settings(), "grp_prep_auto", False)
        off = _submit(kind, {**m, "preview": True})
        monkeypatch.setattr(get_settings(), "grp_prep_auto", True)
        assert on["status"] == "preview", on
        assert json.dumps(on, sort_keys=True) == json.dumps(off, sort_keys=True)
        out = _submit(kind, m)
        assert out["status"] == "approved", out
        assert "prepared_automatically" not in out and "message" not in out
        assert "prepared_automatically" not in _record(out)
        assert _record(out)["manifest"] == json.loads(json.dumps(m))
    assert asked and all(r is None for _, r in asked)
    csv = "Month,Stage_m\n2026-06,3.9\n2026-07,4.8\n"
    table = {"dataset": "x_river_stage", "title": "River stage (SYNTHETIC)",
             "description": "SYNTHETIC series.", "source": "test fixture",
             "validation": "unvalidated", "license": "CC0-1.0", "vintage": "2026-09",
             "cadence": "monthly", "columns": {"month": "Month", "stage_m": "Stage_m"},
             "units": "metres", "as_of_field": "month", "csv_text": csv}
    on = _submit("table", table)
    assert on["status"] == "approved", on
    assert staging.withdraw(on["contribution_id"], OWNER)["status"] == "withdrawn"
    monkeypatch.setattr(get_settings(), "grp_prep_auto", False)
    off = _submit("table", table)
    assert _canon(on) == _canon(off)
    for kind, m in (("document", {"url": "https://example.org/report.pdf"}),
                    ("feed", {"dataset": "x_feed"})):
        assert not prepare.ops(kind) and prepare.Auto(kind, m).on is False
        monkeypatch.setattr(get_settings(), "grp_prep_auto", True)
        on = _submit(kind, m)
        monkeypatch.setattr(get_settings(), "grp_prep_auto", False)
        assert on == _submit(kind, m) and on["status"] == "declined"


# --------------------------------------------------------------------------- 5. never worse
class _NoKeyGeoServer:
    """A GeoServer layer without a primary key (gis.asc-aqua.org's certified farms): it
    counts (resultType=hits) and answers the url as given, but any startIndex without a
    sortBy is HTTP 400 ("Cannot do natural order without a primary key"); `broken`: it
    answers nothing but the count."""

    def __init__(self, feats, broken=False):
        self.feats, self.broken = feats, broken

    def __call__(self, path, q):
        q = {k.lower(): v for k, v in q.items()}
        if q.get("resulttype") == "hits":
            return 200, "text/xml", (f'<wfs:FeatureCollection numberMatched="{len(self.feats)}" '
                                     'numberReturned="0"/>').encode()
        if self.broken or ("startindex" in q and "sortby" not in q):
            return 400, "application/xml", (
                b'<?xml version="1.0"?><ows:ExceptionReport><ows:Exception><ows:ExceptionText>'
                b'Cannot do natural order without a primary key, please add it or specify a '
                b'manual sort</ows:ExceptionText></ows:Exception></ows:ExceptionReport>')
        n = len(self.feats)
        return 200, "application/json", json.dumps({
            "type": "FeatureCollection", "features": self.feats, "totalFeatures": n,
            "numberMatched": n, "numberReturned": n}).encode()


def _farms(n=50):
    return [{"type": "Feature", "id": f"farms.fid--{i:04x}",
             "properties": {"farm_name": f"Farm {i}", "species__c": "Shrimp" if i % 3 else "Tilapia"},
             "geometry": {"type": "Point", "coordinates": [-85.0 + i * 0.01, 10.0 + i * 0.005]}}
            for i in range(n)]


def test_a_wfs_the_platform_cannot_page_lands_as_given_exactly_as_before(env, monkeypatch, log):
    """The ASC farms case: a WFS 1.0 GetFeature url that landed before the platform paged
    anything by itself. Its server refuses paging (no primary key); what detect applied
    (whole) is set aside and the url as given lands — byte for byte what it lands with the
    platform's own preparation off, the stored manifest the contributor's own, and the
    reply saying what was tried and why it was refused. Refused both ways, the answer is
    the platform's own attempt, as before."""
    host = env["host"]
    base = host.app("/geoserver/asc/wfs", _NoKeyGeoServer(_farms()))
    url = (base + "?service=WFS&version=1.0.0&request=GetFeature&typeName=asc:farms"
           "&outputFormat=application/json&CQL_FILTER=status%3D%27Certified%27")
    m = _layer("global_farms", url, name_field="farm_name", group_field="species__c")
    monkeypatch.setattr(get_settings(), "grp_prep_auto", False)
    off = _submit("vector", {**m, "preview": True})
    before = _submit("vector", {**m, "layer": "global_farms_before"})
    monkeypatch.setattr(get_settings(), "grp_prep_auto", True)
    assert before["status"] == "approved", before
    on = _submit("vector", {**m, "preview": True})
    assert {k: v for k, v in on.items()
            if k not in ("message", "prepared_automatically_refused")} == off
    n0 = len(host.log)
    out = _submit("vector", m)
    log("OUTPUT", out.get("message"))
    assert out["status"] == "approved", out
    assert "prepared_automatically" not in out
    why = clip._WHOLE_SERVICE.format(what="a WFS GetFeature request")
    refused = out["prepared_automatically_refused"]
    assert [{k: v for k, v in r.items() if k != "problems"} for r in refused] == [
        {"fields": {"whole": True}, "why": why}]
    assert "startIndex=0" in refused[0]["problems"][0]
    assert "answered HTTP 400" in refused[0]["problems"][0]
    assert out["message"].startswith(f"The platform first prepared this by itself — {why}: "
                                     "whole: true — and that was refused (")
    assert "it then read the url exactly as given" in out["message"]
    asked = [q for _, q in host.log[n0:]]
    assert [asked[0].get("resultType"), asked[1].get("startIndex")] == ["hits", "0"]
    assert len(asked) == 3 and asked[-1] == dict(parse_qsl(urlparse(url).query))   # as given
    rec = _record(out)
    assert rec["manifest"] == json.loads(json.dumps(m)) and "prepared_automatically" not in rec
    assert _raw("global_farms") == _raw("global_farms_before")
    assert vectors.landed()["global_farms"]["features"] == 50
    assert "fetched" not in vectors.landed()["global_farms"]           # nothing was prepared

    # refused both ways: the platform's own attempt is the answer, as it was
    base = host.app("/geoserver/down/wfs", _NoKeyGeoServer(_farms(), broken=True))
    url = base + "?service=WFS&version=1.0.0&request=GetFeature&typeName=asc:farms"
    out = _submit("vector", _layer("global_farms_down", url))
    assert out["status"] == "declined" and _fields(out) == [{"whole": True}]
    assert "startIndex=0" in out["problems"][0] and "answered HTTP 400" in out["problems"][0]
    assert "still declined" in out["message"] and "must decide" not in out["message"]
    assert "prepared_automatically_refused" not in out
    assert _no_leftovers(env)


def test_a_service_that_will_not_count_lands_as_given_and_the_risk_pack_counts_it(env, log):
    """Beyond the asking pack: a hub's shelters behind a FeatureServer that will not count
    its features (so a paged read could never be checked complete) land from the query
    url as given, as they did before — and the risk pack counts the fresh layer."""
    fs = _FeatureServer([{"type": "Feature", "id": i + 1,
                          "properties": {"OBJECTID": i + 1, "name": f"Shelter {i}"},
                          "geometry": {"type": "Point",
                                       "coordinates": [103.006 + 0.0007 * i, 13.01]}}
                         for i in range(12)])

    def no_count(path, q):
        if q.get("returnCountOnly") == "true":
            return 200, "application/json", json.dumps(
                {"error": {"code": 400, "message": "Unable to complete operation."}}).encode()
        return fs(path, q)
    url = env["host"].app("/arcgis/rest/services/Refugios/FeatureServer/0", no_count) + \
        "/query?where=1%3D1&outFields=*&f=geojson"
    out = _submit("vector", _layer("toy_refugios", url, name_field="name", countries=["Toyland"]))
    log("OUTPUT", out.get("message"))
    assert out["status"] == "approved", out
    refused = out["prepared_automatically_refused"]
    assert refused[0]["fields"] == {"whole": True}
    assert "answered an error for its feature count" in refused[0]["problems"][0]
    assert vectors.landed()["toy_refugios"]["features"] == 12
    assert _record(out)["manifest"].get("whole") is None
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY)
    assert p["status"] == "ok", p.get("note")
    exp = next(c for c in p["citations"]
               if c.get("title", "").startswith("toy_refugios (synthetic) vs"))
    assert "contributed layer" in exp["text"]
    assert _no_leftovers(env)


def test_a_prepared_layer_over_the_cap_is_declined_before_it_is_parsed(env, monkeypatch, tmp_path):
    """A GeoPackage over the cap as a download is read whole by itself. Its table, over
    1.5x the cap, is declined from its size — never read into memory; under that but
    over the cap as GeoJSON, declined before it is parsed again (inspect), the geometry
    family read from the text (no raster proposed for points). Read whole, the vertex
    budget counts every feature, of a GeoPackage and of a shapefile."""
    from shapely.geometry import Point
    polys = [Point(-85.0 + i * 0.003, 10.0).buffer(0.001, quad_segs=16) for i in range(400)]
    path = tmp_path / "parcels.gpkg"
    _gpkg(str(path), "parcels", polys, [{"cntry_iso3": "CRI", "area_ha": float(i)} for i in range(len(polys))])
    data = path.read_bytes()
    url = env["host"].put("parcels.gpkg", data)
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", len(data) // 3)
    real_read, real_inspect = vecio.to_geojson, vectors.inspect
    monkeypatch.setattr(vecio, "to_geojson", lambda *a, **k: pytest.fail("parsed"))
    out = _submit("vector", _layer("cr_parcels", url))
    p = out["problems"][0]
    assert out["status"] == "declined" and _fields(out) == [{"whole": True}], out
    assert p.startswith(f"missing required field 'clip': the layer prepared from {url} is ")
    assert "as GeoJSON" not in p and "ask the contributor which area to keep" in p

    monkeypatch.setattr(vecio, "to_geojson", real_read)
    monkeypatch.setattr(vectors, "inspect", lambda *a, **k: pytest.fail("inspected"))
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", int(len(data) * 0.9))
    out = _submit("vector", _layer("cr_parcels", url))
    p = out["problems"][0]
    assert out["status"] == "declined" and _fields(out) == [{"whole": True}], out
    assert "as GeoJSON, over the" in p and "contribute it as kind 'raster'" in p
    pts = [Point(-85.0 + i * 1e-4, 10.0) for i in range(3000)]
    _gpkg(str(tmp_path / "wells.gpkg"), "wells", pts,
          [{"cntry_iso3": "CRI", "area_ha": float(i)} for i in range(len(pts))])
    wells = (tmp_path / "wells.gpkg").read_bytes()
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", int(len(wells) * 0.9))
    out = _submit("vector", _layer("cr_wells", env["host"].put("wells.gpkg", wells)))
    assert "as GeoJSON, over the" in out["problems"][0], out
    assert "contribute it as kind 'raster'" not in out["problems"][0]   # points: no raster
    monkeypatch.setattr(vectors, "inspect", real_inspect)

    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", 50 << 20)
    monkeypatch.setattr(get_settings(), "grp_prep_max_vertices", 1000)
    verts = sum(len(g.exterior.coords) for g in polys)
    out = _submit("vector", _layer("cr_parcels_v", url, whole=True))
    assert out["problems"][0] == (
        f"the layer has {verts:,} vertices, over the 1,000 one clip step may touch — ask the "
        "contributor for the area that matters (clip) and resubmit with it, never guess one; "
        "or give a smaller or simplified layer")
    shp = env["host"].put("parcels.zip", _zip(_shapefile(
        "parcels", polys, [{"n": i} for i in range(len(polys))])))
    out = _submit("vector", _layer("cr_parcels_s", shp, whole=True))
    assert out["problems"][0].startswith(f"the layer has {verts:,} vertices, over the 1,000 one")
    monkeypatch.setattr(get_settings(), "grp_prep_max_vertices", 50_000_000)
    out = _submit("vector", _layer("cr_parcels_s", shp, whole=True))
    assert out["status"] == "approved", out
    assert vectors.landed()["cr_parcels_s"]["features"] == len(polys)
    assert _no_leftovers(env)
