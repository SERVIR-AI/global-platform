"""api_points: a paged JSON API turned into a point layer on the server.

The sprint case: GBIF's occurrence search answers 300 records a page and no GeoJSON, so
the Costa Rica mangrove-species query (1,984 records) went in as one page of 300 and the
gulf's records were paged into a GeoJSON by hand; OBIS answers occurrences as JSON (its
/points endpoint is a bare MultiPoint), and each record's licence was joined by hand from
its dataset table. Here the query goes in as the layer's url: the as-delivered decline
names the field, the resubmission pages every record (not 300), each point keeps its own
licence, and the record keeps the query, every page's sha256 and the count against the
API's total. A preview stores nothing. Beyond the asking pack: a freshly contributed
station registry behind a generic paged API (page/per_page, records under data.items,
coordinates nested) lands through the generic form and the risk pack counts its points.

Everything is synthetic and local: a loopback HTTP server in this file answers as the
two APIs do (paging, totals, caps), the API hosts resolve to it, and no test touches the
network."""

import hashlib
import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import pytest
import yaml
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from app.config import get_settings
from app.contrib import fetch_policy, identity, prepare, staging
from app.contrib.prepare import api_points
from app.graph.geo import ingest, schema, vectors
from app.mcp import assemble, registry, store
from app.risk import synthesis as risk

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
GULF = (-85.2977, 9.9337, -84.7561, 10.2968)        # the gulf box the hand files were cut to
CR = (-85.95, 8.05, -82.6, 11.2)
SPECIES = ("Rhizophora mangle", "Rhizophora racemosa", "Avicennia germinans",
           "Avicennia bicolor", "Laguncularia racemosa", "Pelliciera rhizophorae",
           "Conocarpus erectus")
LICENCES = ("http://creativecommons.org/publicdomain/zero/1.0/legalcode",
            "http://creativecommons.org/licenses/by/4.0/legalcode",
            "http://creativecommons.org/licenses/by-nc/4.0/legalcode")
B = (103.0, 13.0, 103.02, 13.02)                     # the risk pack's toy world
RISK_POLY = {"type": "Polygon", "coordinates": [[[103.005, 13.005], [103.015, 13.005],
             [103.015, 13.015], [103.005, 13.015], [103.005, 13.005]]]}
QUERY = ("country=CR&taxonKey=3086528&taxonKey=3874097&taxonKey=2925400&taxonKey=4196610"
         "&taxonKey=3189382&taxonKey=2883101&taxonKey=5421045&hasCoordinate=true"
         "&hasGeospatialIssue=false&limit=300")
OBIS_Q = ("geometry=POLYGON((-85.2977%209.9337,-84.7561%209.9337,-84.7561%2010.2968,"
          "-85.2977%2010.2968,-85.2977%209.9337))")
API_HOSTS = ("api.gbif.org", "api.obis.org", "api.example.test")


# --------------------------------------------------------------------------- the APIs
def _inside(x, y, box):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _gbif_records():
    """1,984 records of the seven species in Costa Rica, 303 of them in the gulf box —
    the shape of the real query and of the hand-built gulf file."""
    rng = np.random.RandomState(7)
    pts = []
    while len(pts) < 303:
        pts.append((rng.uniform(GULF[0] + .002, GULF[2] - .002),
                    rng.uniform(GULF[1] + .002, GULF[3] - .002)))
    while len(pts) < 1984:
        x, y = rng.uniform(CR[0], CR[2]), rng.uniform(CR[1], CR[3])
        if not _inside(x, y, GULF):
            pts.append((x, y))
    out = []
    for i, (x, y) in enumerate(pts):
        key = 5_000_000_000 + i
        out.append({"key": key, "gbifID": str(key), "species": SPECIES[i % 7],
                    "scientificName": SPECIES[i % 7], "decimalLongitude": round(x, 6),
                    "decimalLatitude": round(y, 6), "eventDate": f"{1990 + i % 35}-05-01",
                    "year": 1990 + i % 35,
                    "basisOfRecord": ("HUMAN_OBSERVATION", "PRESERVED_SPECIMEN")[i % 2],
                    "license": LICENCES[i % 3], "rightsHolder": "Synthetic herbarium",
                    "datasetName": "Synthetic observations", "datasetKey": f"ds-{i % 4}",
                    "institutionCode": "SYN", "coordinateUncertaintyInMeters": 30.0,
                    "issues": ["COORDINATE_ROUNDED"], "extensions": {}, "media": []})
    return out


def _obis_records():
    """4,468 records in the gulf box from 26 datasets, ids in the API's own order."""
    rng = np.random.RandomState(11)
    classes = ("Teleostei", "Aves", "Gastropoda", "Bivalvia", "Crocodylia")
    return [{"id": f"{i:08x}-0000-4000-8000-{i:012x}", "scientificName": f"Taxon {i % 865}",
             "species": f"Taxon {i % 865}", "phylum": "Chordata", "class": classes[i % 5],
             "family": "Synthetidae", "eventDate": "1995-02-13T12:00:00Z",
             "date_year": 1995, "basisOfRecord": "HumanObservation",
             "dataset_id": f"dataset-{i % 26:02d}", "institutionCode": "SYN",
             "bibliographicCitation": "Synthetic collection (2026)",
             "decimalLongitude": round(rng.uniform(GULF[0], GULF[2]), 6),
             "decimalLatitude": round(rng.uniform(GULF[1], GULF[3]), 6),
             "flags": ["NO_DEPTH"], "dropped": False}
            for i in range(4468)]


RIGHTS = ("Creative Commons Attribution Non Commercial (CC-BY-NC 4.0)",
          "CC0: the publisher has waived all rights", "Unrestricted")


def _datasets():
    return [{"id": f"dataset-{d:02d}", "title": f"Synthetic dataset {d}",
             "intellectualrights": RIGHTS[d % 3], "abstract": "x" * 50} for d in range(26)]


def _stations():
    """120 stations of a hub's registry around the risk pack's toy world; two without a
    location; licence and attribution on each record."""
    out = []
    for i in range(120):
        rec = {"site_id": f"S-{i:03d}", "name": f"Station {i}", "kind": "shelter",
               "licence": "CC-BY-4.0", "attribution": "Synthetic hub registry",
               "tags": ["a", "b"], "meta": {"built": 2001}}
        if i < 6:                                 # inside RISK_POLY: 4 west (flood), 2 east
            x = 103.006 + i * 0.0006 if i < 4 else 103.012 + (i - 4) * 0.001
            rec["location"] = {"lon": x, "lat": 13.01}
        elif i not in (50, 51):
            rec["location"] = {"lon": 103.0005 + (i % 10) * 0.0001, "lat": 13.0005}
        out.append(rec)
    return out


class _Host:
    def __init__(self):
        self.log, self.port = [], None
        self.gbif, self.obis, self.ds, self.st = _gbif_records(), _obis_records(), _datasets(), _stations()
        self.gbif_count = None
        self.files = {}
        self.delay, self.fail_once = 0.0, set()     # seconds per answer; queries answered 503 once
        self.inflight = self.max_inflight = 0
        self.lock = threading.Lock()

    def url(self, host, path_query):
        return f"http://{host}:{self.port}{path_query}"

    def asked(self, path):
        return [q for p, q in self.log if p == path]


def _answer(host, path, q):
    one = lambda k, d=None: (q.get(k) or [d])[0]     # noqa: E731
    if path == "/v1/occurrence/search":
        off, lim = int(one("offset", 0)), min(int(one("limit", 20)), 300)
        recs = host.gbif[off:off + lim]
        count = host.gbif_count or len(host.gbif)
        return 200, {"offset": off, "limit": lim, "endOfRecords": off + len(recs) >= len(host.gbif),
                     "count": count, "results": recs, "facets": []}
    if path == "/v3/occurrence":
        size = int(one("size", 10))
        if size > 10000:
            return 400, {"message": f"Size {size} too large (should be 10000 or lower)"}
        after = one("after")
        recs = [r for r in host.obis if after is None or r["id"] > after][:size]
        return 200, {"total": len(host.obis), "results": recs}
    if path == "/v3/occurrence/points":
        return 200, {"type": "MultiPoint", "coordinates": [[r["decimalLongitude"], r["decimalLatitude"]]
                                                           for r in host.obis[:50]]}
    if path == "/v3/dataset":
        return 200, {"total": len(host.ds), "results": host.ds}
    if path == "/records":
        page, per = int(one("page", 1)), min(int(one("per_page", 50)), 50)
        items = host.st[(page - 1) * per: page * per]
        return 200, {"meta": {"total": len(host.st), "page": page}, "data": {"items": items}}
    if path == "/capped":                       # answers at most 30 records, whatever is asked
        off, lim = int(one("start", 0)), min(int(one("rows", 10)), 30)
        return 200, {"numFound": len(host.st), "docs": host.st[off:off + lim]}
    if path in host.files:
        return 200, host.files[path]
    return 404, {"message": "not found"}


def _handler(host):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            import time
            u = urlparse(self.path)
            with host.lock:
                host.log.append((u.path, u.query))
                host.inflight += 1
                host.max_inflight = max(host.max_inflight, host.inflight)
                failing = u.query in host.fail_once
                host.fail_once.discard(u.query)
            try:
                time.sleep(host.delay)
            finally:
                with host.lock:
                    host.inflight -= 1
            code, doc = (503, {"message": "busy"}) if failing else \
                _answer(host, u.path, parse_qs(u.query))
            body = doc if isinstance(doc, bytes) else json.dumps(doc).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/octet-stream" if isinstance(doc, bytes)
                             else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    return Handler


@pytest.fixture
def host(monkeypatch):
    h = _Host()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(h))
    h.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    real = fetch_policy._addresses
    monkeypatch.setattr(fetch_policy, "_addresses", lambda name: (
        [ipaddress.ip_address("127.0.0.1")] if name in API_HOSTS else real(name)))
    yield h
    srv.shutdown()
    srv.server_close()


# --------------------------------------------------------------------------- a platform
def _gtiff(arr, dtype, bounds=B):
    h, w = arr.shape
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=w, height=h, count=1, dtype=dtype, crs="EPSG:4326",
                     transform=from_bounds(*bounds, w, h)) as d:
            d.write(arr.astype(dtype), 1)
        return mf.read()


@pytest.fixture
def env(tmp_path, monkeypatch, host):
    """A hermetic platform: an empty land-cover catalog, the risk pack's toy world (a
    flood hazard over the west half near 103 E, 13 N), auto-approve, the real fetch
    policy with the loopback host allowed, and a hermetic store."""
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


def _no_leftovers(env):
    prep = env["cache"] / "prep"
    strays = list((env["cache"] / "tiffs").glob("staged-tmp-*")) + \
        list((env["cache"] / "vectors").glob("staged-tmp-*"))
    return (not prep.exists() or not list(prep.iterdir())) and not strays


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _poly(box):
    w, s, e, n = box
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def _layer(name, url, **over):
    return {"layer": name, "url": url, "title": f"{name} (synthetic)",
            "description": "one point per API record (synthetic, shaped like the real query)",
            "source": "test fixture shaped like the API", "license": "mixed per record",
            "vintage": "2026-10", "countries": ["Costa Rica"], **over}


def _stored(layer):
    with open(vectors.master_path(vectors.landed()[layer]), "rb") as f:
        raw = f.read()
    return raw, json.loads(raw)


# --------------------------------------------------------------------------- 1. the registry
def test_the_op_is_registered_from_its_module_and_its_field_reaches_the_vector_form():
    names = [op.NAME for op in prepare.ops("vector")]
    assert "api_points" in names and "api_points" not in [op.NAME for op in prepare.ops("raster")]
    text = staging.VECTOR_FIELDS["optional"]["api_points"]
    assert text == api_points.FIELDS["api_points"]
    for name, p in api_points.PRESETS.items():          # the presets' names derive from the data
        assert f"'{name}' ({p['host']}{p['path']})" in text
    assert "ask the contributor" in text and "api_points" not in staging._RASTER_KNOWN
    assert "not_features" in prepare.SITUATIONS
    assert staging.form("vector")["fields"]["optional"]["api_points"].startswith("for a POINT layer")


# --------------------------------------------------------------------------- 2. GBIF, the real case
def test_the_gbif_query_lands_every_record_not_one_page(env, monkeypatch, log):
    """The S2-GBIF case: the query url as delivered, with the platform's own preparation
    off (GRP_PREP_AUTO=0; on, it applies api_points itself — test_prepare_auto), is
    declined with the exact field; with api_points: gbif all 1,984 records land (7 pages
    of up to 300), each point with its own licence; the 303 in the gulf box are the
    hand-built file's 303."""
    monkeypatch.setattr(get_settings(), "grp_prep_auto", False)
    host = env["host"]
    url = host.url("api.gbif.org", f"/v1/occurrence/search?{QUERY}")
    m = _layer("cr_gbif_mangrove_occ", url, name_field="species", group_field="species")
    out = _submit("vector", m)
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined"
    assert "must be a GeoJSON FeatureCollection — resubmit the same link with api_points: gbif" \
        in out["problems"][0]
    assert "every page of up to 300, not only the first" in out["problems"][0]
    assert len(host.log) == 1                             # the gate's own read of the url
    host.log.clear()

    pv = _submit("vector", {**m, "api_points": "gbif", "preview": True})
    assert pv["status"] == "preview", pv
    wl = pv["would_land"]
    log("OUTPUT", json.dumps({k: wl[k] for k in ("features", "geometry", "extent", "bytes")}))
    assert wl["features"] == 1984 and wl["geometry"] == "points"
    assert wl["prepared"][0].startswith("built at ingest from GBIF's occurrence search: 1,984 "
                                        "points from every record of the 1,984 the query matched")
    assert not vectors.landed() and not staging.STAGED_VECTORS and _no_leftovers(env)
    host.log.clear()

    host.delay, host.max_inflight = 0.2, 0
    out = _submit("vector", {**m, "api_points": "gbif"})
    assert out["status"] == "approved", out
    offsets = [parse_qs(q)["offset"][0] for p, q in host.log]
    assert offsets[0] == "0" and sorted(offsets, key=int) == ["0", "300", "600", "900", "1200",
                                                              "1500", "1800"]
    assert 2 <= host.max_inflight <= api_points.AHEAD    # once the first page gave the total
    assert all(parse_qs(q)["taxonKey"] == parse_qs(QUERY)["taxonKey"] for _, q in host.log)
    assert all(parse_qs(q)["limit"] == ["300"] for _, q in host.log)     # the url's own, replaced

    row = vectors.landed()["cr_gbif_mangrove_occ"]
    raw, fc = _stored("cr_gbif_mangrove_occ")
    log("OUTPUT", json.dumps(row["fetched"])[:600])
    assert row["features"] == 1984
    steps = row["fetched"]["steps"]
    assert [st["op"] for st in steps] == ["api_points"]
    inp = steps[0]["inputs"]
    assert inp["url"] == url and inp["api"] == "gbif"
    assert (inp["total"], inp["records"], inp["points"], inp["part"]) == (1984, 1984, 1984, False)
    assert [p["records"] for p in inp["pages"]] == [300] * 6 + [184]
    first = json.dumps(_answer(host, "/v1/occurrence/search",
                               parse_qs(QUERY.replace("limit=300", "") + "&offset=0&limit=300"))[1]).encode()
    assert inp["pages"][0]["sha256"] == _sha(first)
    assert row["fetched"]["sha256"] == _sha(raw)
    assert row["fetched"]["read"].startswith("paged from GBIF's occurrence search on the server: 7 page(s)")
    assert any("each point keeps its own licence and attribution as the API gives them: license, "
               "rights_holder" in n for n in row["prepared"])
    assert "the url's own limit is set by the paging" in row["prepared"]
    props = fc["features"][0]["properties"]
    assert set(props) == set(api_points.PRESETS["gbif"]["keep"])   # issues, media... not kept
    assert {f["properties"]["license"] for f in fc["features"]} == set(LICENCES)
    by_licence = {lic: sum(f["properties"]["license"] == lic for f in fc["features"]) for lic in LICENCES}
    assert by_licence == {LICENCES[0]: 662, LICENCES[1]: 661, LICENCES[2]: 661}

    # the hand-built file held the gulf's 303; the land-cover pack counts the same 303
    want = {}
    for r in host.gbif:
        if _inside(r["decimalLongitude"], r["decimalLatitude"], GULF):
            want[r["species"]] = want.get(r["species"], 0) + 1
    assert sum(want.values()) == 303
    p = assemble.assemble(pack="land-cover", place="Gulf (synthetic)", geometry=_poly(GULF),
                          vectors=["cr_gbif_mangrove_occ"])
    assert p["status"] == "ok", p.get("note")
    v = p["vectors"]["cr_gbif_mangrove_occ"]
    log("OUTPUT", json.dumps(v)[:400])
    assert v["features_inside"] == 303
    assert _no_leftovers(env)


def test_max_records_keeps_a_declared_part_and_says_so(env):
    host = env["host"]
    url = host.url("api.gbif.org", f"/v1/occurrence/search?{QUERY}")
    out = _submit("vector", _layer("cr_gbif_part", url, api_points={"preset": "gbif", "max_records": 500}))
    assert out["status"] == "approved", out
    assert [parse_qs(q)["limit"][0] for _, q in host.log] == ["300", "200"]
    row = vectors.landed()["cr_gbif_part"]
    inp = row["fetched"]["steps"][0]["inputs"]
    assert (inp["records"], inp["total"], inp["part"]) == (500, 1984, True)
    assert row["features"] == 500
    assert row["prepared"][0].startswith("built at ingest from GBIF's occurrence search: 500 points "
                                         "from the first 500 of the 1,984 records the query matched "
                                         "(max_records)")
    assert any(n.startswith("a PART of the query: 500 of 1,984 records") for n in row["prepared"])


def test_a_page_that_fails_is_asked_again_and_nothing_is_lost(env):
    """A busy answer (503) to one page, fetched ahead or in order, is asked once more."""
    host = env["host"]
    url = host.url("api.gbif.org", f"/v1/occurrence/search?{QUERY}")
    host.fail_once = {f"{QUERY.replace('&limit=300', '')}&offset=600&limit=300",
                      f"{QUERY.replace('&limit=300', '')}&offset=0&limit=300"}
    out = _submit("vector", _layer("cr_gbif_retry", url, api_points="gbif"))
    assert out["status"] == "approved", out
    offsets = sorted(int(parse_qs(q)["offset"][0]) for _, q in host.log)
    assert offsets == [0, 0, 300, 600, 600, 900, 1200, 1500, 1800]
    assert vectors.landed()["cr_gbif_retry"]["features"] == 1984
    assert _no_leftovers(env)


def test_an_api_answering_fewer_records_than_asked_is_read_to_its_total(env):
    """An API that caps its pages below the page_size given (30 of 50): a short page does
    not end the query and nothing is fetched ahead on a wrong guess; every record lands."""
    host = env["host"]
    url = host.url("api.example.test", "/capped")
    form = {"records_path": "docs", "lon": "location.lon", "lat": "location.lat",
            "total_path": "numFound", "paging": {"offset": "start", "limit": "rows",
                                                 "page_size": 50}}
    out = _submit("vector", _layer("toy_capped", url, api_points=form))
    assert out["status"] == "approved", out
    starts = [int(parse_qs(q)["start"][0]) for _, q in host.log]
    assert starts == [0, 30, 60, 90]
    assert vectors.landed()["toy_capped"]["features"] == 118
    inp = vectors.landed()["toy_capped"]["fetched"]["steps"][0]["inputs"]
    assert (inp["records"], inp["total"]) == (120, 120)


def test_a_query_beyond_what_gbif_pages_is_declined_after_one_page(env):
    host = env["host"]
    host.gbif_count = 250_000
    url = host.url("api.gbif.org", f"/v1/occurrence/search?{QUERY}")
    out = _submit("vector", _layer("cr_gbif_big", url, api_points="gbif"))
    assert out["status"] == "declined"
    assert out["problems"] == [
        "the query matches 250,000 records; GBIF's search pages through the first 100,000 "
        "records only — narrow the query, or make a GBIF download of it (a free account; it "
        "gives a DOI to cite) and contribute that file; or set max_records: N to keep the first N"]
    assert len(host.log) == 1 and _no_leftovers(env)


def test_a_wrong_preset_or_a_bad_form_is_declined_before_any_request(env):
    host = env["host"]
    url = host.url("api.obis.org", f"/v3/occurrence?{OBIS_Q}")
    out = _submit("vector", _layer("cr_wrong", url, api_points="gbif"))
    assert out["status"] == "declined"
    assert out["problems"][0].startswith("api_points: gbif reads GBIF's occurrence search "
                                         "(api.gbif.org/v1/occurrence/search); the url is")
    out = _submit("vector", _layer("cr_wrong", url, api_points={"records_path": "results",
                                                                 "lon": "x", "paging": {"offset": "o"},
                                                                 "colour": 1}))
    assert out["problems"] == ["api_points does not take colour — its fields are preset, "
                               "records_path, lon, lat, total_path, end_path, id, paging, keep, "
                               "join, max_records"]
    out = _submit("vector", _layer("cr_wrong", url, api_points={"records_path": "results",
                                                                 "lon": "x", "lat": "y",
                                                                 "paging": {"offset": "o", "page": "p"}}))
    assert "api_points paging names ONE way the API pages" in out["problems"][0]
    out = _submit("raster", {"layer": "measure_x", "url": url, "api_points": "obis"})
    assert ("api_points prepares a vector contribution (api_points); it does not apply to a "
            "raster — drop api_points") in out["problems"]
    assert host.log == []


# --------------------------------------------------------------------------- 3. OBIS
def test_the_obis_points_link_is_redirected_to_the_records_and_each_point_gets_its_rights(
        env, monkeypatch, log):
    """The S2-OBIS case: /points (a bare MultiPoint) is declined with the records' url (the
    platform's own preparation off, GRP_PREP_AUTO=0 — on, it reads the records itself:
    test_prepare_auto); with api_points: obis every record lands (paged by `after`), each
    point carrying its dataset's title and rights joined from the dataset endpoint on the
    same query."""
    monkeypatch.setattr(get_settings(), "grp_prep_auto", False)
    host = env["host"]
    monkeypatch.setitem(api_points.PRESETS["obis"], "paging",
                        {"after": "after", "after_field": "id", "limit": "size", "page_size": 1000})
    points = host.url("api.obis.org", f"/v3/occurrence/points?{OBIS_Q}")
    m = _layer("cr_obis_occ", points, name_field="scientific_name", group_field="class")
    out = _submit("vector", m)
    log("OUTPUT", out["problems"][0])
    records = host.url("api.obis.org", f"/v3/occurrence?{OBIS_Q}")
    assert out["status"] == "declined"
    assert (f"that url answers the records' locations without their attributes; resubmit with "
            f"url: {records} (the same query) and api_points: obis") in out["problems"][0]
    out = _submit("vector", {**m, "api_points": "obis"})
    assert "answers the records' locations without their attributes" in out["problems"][0]
    host.log.clear()

    out = _submit("vector", {**m, "url": records, "api_points": "obis"})
    assert out["status"] == "approved", out
    occ = host.asked("/v3/occurrence")
    assert len(occ) == 5 and "after" not in parse_qs(occ[0])
    assert [parse_qs(q)["after"][0] for q in occ[1:]] == [host.obis[k * 1000 - 1]["id"] for k in (1, 2, 3, 4)]
    assert all(parse_qs(q)["geometry"] == parse_qs(OBIS_Q)["geometry"] for q in occ)
    ds = host.asked("/v3/dataset")
    assert len(ds) == 1 and parse_qs(ds[0])["geometry"] == parse_qs(OBIS_Q)["geometry"]
    assert "after" not in parse_qs(ds[0])

    row = vectors.landed()["cr_obis_occ"]
    _raw, fc = _stored("cr_obis_occ")
    inp = row["fetched"]["steps"][0]["inputs"]
    log("OUTPUT", json.dumps(inp["join"]))
    assert row["features"] == 4468 and (inp["total"], inp["points"]) == (4468, 4468)
    assert [p["records"] for p in inp["pages"]] == [1000, 1000, 1000, 1000, 468]
    assert inp["join"]["matched"] == inp["join"]["of"] == 26
    want = {r["id"]: RIGHTS[int(r["dataset_id"][-2:]) % 3] for r in host.obis}
    assert all(f["properties"]["dataset_rights"] == want[f["properties"]["obis_id"]] for f in fc["features"])
    assert all(f["properties"]["citation"] == "Synthetic collection (2026)" for f in fc["features"])
    assert any("dataset_title, dataset_rights joined from" in n and "26 of 26 dataset_id" in n
               for n in row["prepared"])
    p = assemble.assemble(pack="land-cover", place="Gulf (synthetic)", geometry=_poly(GULF),
                          vectors=["cr_obis_occ"])
    assert p["status"] == "ok", p.get("note")
    assert p["vectors"]["cr_obis_occ"]["features_inside"] == 4468
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 4. beyond the asking pack
def test_a_fresh_station_registry_lands_through_the_generic_form_and_the_risk_pack_counts_it(
        env, log):
    """A hub's station registry behind a generic API (records under data.items, the
    coordinates nested, page/per_page paging): the decline proposes the generic form read
    from the answer itself, the resubmission pages it, and the risk pack counts its points
    in the toy flood."""
    host = env["host"]
    url = host.url("api.example.test", "/records?kind=shelter")
    m = {"layer": "toy_stations", "url": url, "title": "Synthetic hub stations",
         "description": "one point per station", "source": "synthetic hub registry",
         "license": "CC-BY-4.0", "vintage": "2026-10", "name_field": "name"}
    out = _submit("vector", m)
    log("OUTPUT", out["problems"][0])
    assert ("resubmit the same link with api_points: {records_path: 'data.items', lon: "
            "'location.lon', lat: 'location.lat', total_path: 'meta.total'}") in out["problems"][0]
    assert "this answer holds 50 record(s) and says the query matches 120" in out["problems"][0]
    assert "never guess" in out["problems"][0]

    form = {"records_path": "data.items", "lon": "location.lon", "lat": "location.lat",
            "total_path": "meta.total", "id": "site_id",
            "paging": {"page": "page", "limit": "per_page", "page_size": 50}}
    out = _submit("vector", {**m, "api_points": form})
    assert out["status"] == "approved", out
    row = vectors.landed()["toy_stations"]
    inp = row["fetched"]["steps"][0]["inputs"]
    assert [p["params"]["page"] for p in inp["pages"]] == [1, 2, 3]
    assert (inp["total"], inp["records"], inp["points"]) == (120, 120, 118)
    assert inp["left_out"] == {"no coordinates": 2, "outside lon/lat": 0, "duplicates": 0}
    _raw, fc = _stored("toy_stations")
    props = fc["features"][0]["properties"]
    assert props == {"site_id": "S-000", "name": "Station 0", "kind": "shelter",
                     "licence": "CC-BY-4.0", "attribution": "Synthetic hub registry",
                     "tags": "a; b"}
    assert any(n == "2 record(s) without a longitude and latitude left out" for n in row["prepared"])
    assert any(n.startswith("nested fields not kept: meta") for n in row["prepared"])

    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY)
    assert p["status"] == "ok", p.get("note")
    cit = next(c for c in p["citations"] if c.get("title", "").startswith("Synthetic hub stations"))
    log("OUTPUT", cit["text"][:300])
    assert cit["text"].startswith("4 of 6 toy_stations")
    assert _no_leftovers(env)


# --------------------------------------------------------------------------- 5. budgets, safety
def test_an_api_that_ignores_the_paging_given_is_declined(env):
    host = env["host"]
    url = host.url("api.example.test", "/records")
    form = {"records_path": "data.items", "lon": "location.lon", "lat": "location.lat",
            "paging": {"offset": "start", "limit": "rows", "page_size": 50}}
    out = _submit("vector", _layer("toy_ignored", url, api_points=form))
    assert out["status"] == "declined"
    assert out["problems"] == [
        "the API at api.example.test answered page 2 exactly as page 1: it does not page by "
        "rows, start — take its paging parameters from its documentation (or ask the "
        "contributor) and resubmit"]
    assert _no_leftovers(env)


def test_a_query_over_the_record_budget_and_a_layer_over_the_stored_cap_are_declined(
        env, monkeypatch):
    host = env["host"]
    url = host.url("api.example.test", "/records")
    form = {"records_path": "data.items", "lon": "location.lon", "lat": "location.lat",
            "total_path": "meta.total", "paging": {"page": "page", "limit": "per_page",
                                                   "page_size": 50}}
    monkeypatch.setattr(api_points, "MAX_RECORDS", 100)
    out = _submit("vector", _layer("toy_over", url, api_points=form))
    assert out["problems"] == [
        "the query matches 120 records, over 100 (the most the platform pages into one layer) "
        "— narrow the query (an area, a year range, fewer taxa), or set max_records: N to keep "
        "the first N (the record then says it holds N of 120)"]
    monkeypatch.setattr(api_points, "MAX_RECORDS", 100_000)
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", 4000)
    out = _submit("vector", _layer("toy_over", url, api_points=form))
    assert out["status"] == "declined"
    assert "of GeoJSON, over the" in out["problems"][0] and "keep fewer fields" in out["problems"][0]
    monkeypatch.setattr(get_settings(), "grp_contrib_max_bytes", 50 << 20)
    monkeypatch.setattr(get_settings(), "grp_prep_op_seconds", 1e-9)
    out = _submit("vector", _layer("toy_over", url, api_points=form))
    assert out["status"] == "declined"
    assert out["problems"][0].startswith("the API at api.example.test answered too slowly: 0 "
                                         "page(s), 0 records were read when the step's 1e-09 s "
                                         "ran out — try again later")
    assert _no_leftovers(env)


def test_an_answer_that_is_not_json_or_has_no_records_there_is_declined_in_words(env):
    host = env["host"]
    host.files["/export.csv"] = b"lon,lat\n1,2\n"
    url = host.url("api.example.test", "/export.csv")
    out = _submit("vector", _layer("toy_csv", url, api_points={"records_path": "", "lon": "lon",
                                                               "lat": "lat"}))
    assert out["problems"] == [f"page 1 of {fetch_policy.shown(url)} is not JSON (it starts "
                               "'lon,lat 1,2') — api_points reads an API that answers JSON records"]
    url = host.url("api.example.test", "/records")
    out = _submit("vector", _layer("toy_path", url, api_points={"records_path": "items",
                                                                "lon": "lon", "lat": "lat"}))
    assert out["problems"] == ["the API at api.example.test's answer has no list of records at "
                               "records_path 'items' — its answer holds meta, data; give the path "
                               "to the list (e.g. results, data.items)"]
    out = _submit("vector", _layer("toy_path", url, api_points={"records_path": "data.items",
                                                                "lon": "x", "lat": "y"}))
    assert out["problems"][0].startswith("no record of the API at api.example.test's first page "
                                         "has a longitude at 'x' — a record holds site_id, name")
    assert _no_leftovers(env)


def test_a_private_host_is_refused_before_any_request(env, monkeypatch):
    host = env["host"]
    monkeypatch.setattr(get_settings(), "grp_contrib_allow_private_hosts", False)
    url = host.url("api.gbif.org", f"/v1/occurrence/search?{QUERY}")
    out = _submit("vector", _layer("cr_private", url, api_points="gbif"))
    assert out["status"] == "declined"
    assert "private, loopback or link-local" in out["problems"][0]
    assert host.log == []
