"""Clip at the source, and the raster preparation gaps the sprint re-test found.

A raster over the size cap is declined as a missing `clip` field (the contributor's
agent asks for the area); with a clip, only the window is read over HTTP range
requests — from a GeoTIFF, from a member inside a zip — and the cap applies to the
stored clip, whose url, box, member and sha256 ride the record. A NetCDF on CF
latitude/longitude with no CRS is read as EPSG:4326; a zip says what it holds; a
picture says it is a picture; a VRT is never opened.

Everything is synthetic and local: a tiny threaded HTTP server in this file answers
Range requests (or refuses to, per file), and no test touches the network."""

import hashlib
import io
import re
import struct
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
import rasterio
import yaml
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds, from_origin
from rasterio.warp import transform_bounds
from shapely.geometry import box as shp_box

from app.config import get_settings
from app.contrib import fetch_policy, identity, rasters, remote_clip, staging
from app.graph.geo import ingest
from app.mcp import store

OWNER = identity.Caller("user_owner", "hydrologist@hub.test", False, "bound")
REVIEWER = identity.Caller("user_rev", "reviewer@hub.test", True, "bound")
CAP = 256 * 1024                       # the size cap, small so a synthetic file is "national"
WORLD = (-86.0, 9.0, -84.0, 11.0)      # every synthetic file's extent (degrees)
GULF = [-85.2, 10.0, -85.0, 10.2]      # a study area well inside it


# --------------------------------------------------------------------------- a host
class _Host:
    """Files by path; each answers Range requests unless listed in `whole`."""

    def __init__(self):
        self.files, self.whole, self.log = {}, set(), []
        self.port = None

    def put(self, name, data, ranges=True):
        self.files["/" + name] = data
        if not ranges:
            self.whole.add("/" + name)
        return f"http://127.0.0.1:{self.port}/{name}"

    def ranged_bytes(self, name):
        return sum(n for p, rng, n in self.log if p == "/" + name and rng)


def _handler(host):
    class H(BaseHTTPRequestHandler):
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
            if rng:
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                a = int(m.group(1))
                b = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                part, code = data[a:b + 1], 206
            else:
                part, code = data, 200
            host.log.append((path, rng, len(part) if rng else 0))
            self.send_response(code)
            if rng:
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
            ctype = "text/html" if part[:15].lower().startswith(b"<!doctype html") else \
                "application/octet-stream"
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(part)))
            self.end_headers()
            if body:
                try:
                    for i in range(0, len(part), 65536):
                        self.wfile.write(part[i:i + 65536])
                except (BrokenPipeError, ConnectionResetError):
                    pass                 # the reader had what it needed (a size, a header)

        def do_GET(self):
            self._send(True)

        def do_HEAD(self):
            self._send(False)
    return H


@pytest.fixture
def host():
    h = _Host()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(h))
    h.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield h
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A hermetic catalog and store; the real fetch policy, with the loopback host
    allowed (the server above) and a small cap."""
    s = get_settings()
    cat = tmp_path / "tiffs.yml"
    cat.write_text(yaml.safe_dump({}))
    sch = tmp_path / "raster_schema.yml"
    sch.write_text(yaml.safe_dump({"defaults": {"crs": "EPSG:4326", "float_tol": 0.0001},
                                   "layers": {}}))
    for k, v in {"cache_dir": tmp_path, "tiffs_dir": tmp_path / "tiffs",
                 "tiffs_config_path": cat, "raster_schema_path": sch,
                 "tiffs_contrib_path": tmp_path / "tiffs.contrib.yml",
                 "raster_schema_contrib_path": tmp_path / "raster_schema.contrib.yml",
                 "grp_mattermost_webhook": "", "grp_contrib_allow_private_hosts": True,
                 "grp_contrib_max_bytes": CAP, "grp_contrib_hourly_cap": 0}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(staging, "STAGED_RASTERS", {})
    monkeypatch.setattr(staging, "_STAGED_LOADED", False)
    return tmp_path


# --------------------------------------------------------------------------- files
def _tif(arr, bounds=WORLD, crs="EPSG:4326", nodata=None, transform=None):
    h, w = arr.shape
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=w, height=h, count=1, dtype=arr.dtype.name, crs=crs,
                     transform=transform or from_bounds(*bounds, w, h), nodata=nodata,
                     tiled=True, blockxsize=256, blockysize=256, compress="deflate") as d:
            d.write(arr, 1)
        return mf.read()


def _noise(n=1000, seed=0):
    """A 'national DEM': random metres, so it compresses badly and is far over CAP."""
    return np.random.default_rng(seed).uniform(0, 1800, (n, n)).astype("float32")


def _zip(members, stored=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _nc(lat, lon, data, lat_name="latitude", lon_name="longitude", units=True):
    """A classic (CDF-1) NetCDF shaped like an ERDDAP griddap subset: time, latitude
    (ascending), longitude, one double variable with a _FillValue, and NO grid_mapping."""
    def name(t):
        b = t.encode()
        return struct.pack(">i", len(b)) + b + b"\0" * (-len(b) % 4)

    def attr(k, v):
        if isinstance(v, str):
            b = v.encode()
            return name(k) + struct.pack(">ii", 2, len(b)) + b + b"\0" * (-len(b) % 4)
        return name(k) + struct.pack(">ii", 6, 1) + struct.pack(">d", v)

    def attrs(d):
        return (struct.pack(">ii", 12, len(d)) + b"".join(attr(k, v) for k, v in d.items())
                if d else struct.pack(">ii", 0, 0))
    lat_at = {"units": "degrees_north", "standard_name": "latitude"} if units else {}
    lon_at = {"units": "degrees_east", "standard_name": "longitude"} if units else {}
    dims = [("time", 1), (lat_name, len(lat)), (lon_name, len(lon))]
    vs = [("time", [0], {"units": "seconds since 1970-01-01T00:00:00Z"}, np.array([1.2623e9])),
          (lat_name, [1], lat_at, np.asarray(lat, float)),
          (lon_name, [2], lon_at, np.asarray(lon, float)),
          ("thetao_mean", [0, 1, 2], {"_FillValue": -9999.0, "units": "degree_C"},
           np.asarray(data, float))]
    head = b"CDF\x01" + struct.pack(">i", 0)
    head += struct.pack(">ii", 10, len(dims)) + b"".join(name(n) + struct.pack(">i", k) for n, k in dims)
    head += attrs({"Conventions": "CF-1.6, COARDS, ACDD-1.3"})

    def block(offsets):
        out = struct.pack(">ii", 11, len(vs))
        for (n, ids, at, arr), off in zip(vs, offsets):
            out += name(n) + struct.pack(">i", len(ids)) + b"".join(struct.pack(">i", i) for i in ids)
            out += attrs(at) + struct.pack(">iii", 6, arr.size * 8, off)
        return out
    pos, offsets = len(head) + len(block([0] * len(vs))), []
    for v in vs:
        offsets.append(pos)
        pos += v[3].size * 8
    return head + block(offsets) + b"".join(v[3].astype(">f8").tobytes() for v in vs)


def _measure(layer, url, **over):
    return {"layer": layer, "url": url, "title": f"{layer} (synthetic)",
            "description": "synthetic test layer", "source": "test fixture", "license": "CC0-1.0",
            "vintage": "2026-10", "countries": ["Testland"],
            "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 1800, "units": "m"},
            **over}


def _submit(m):
    tok = identity.bind(OWNER)
    try:
        return staging.submit("raster", m, OWNER)
    finally:
        identity.unbind(tok)


def _staged(out):
    return out["preview"]["staged_file"]


def _no_strays(env):
    return not list((env / "tiffs").glob("staged-tmp-*")) if (env / "tiffs").exists() else True


# --------------------------------------------------------------------------- 1. clip
def test_the_clip_field_says_it_is_required_over_the_cap_and_never_guessed():
    """The form a contributor's agent reads: clip is required over the cap, ask the
    contributor for the area, never guess one."""
    text = staging.RASTER_FIELDS["optional"]["clip"]
    assert "REQUIRED when the file is over the size cap" in text
    assert "ASK the contributor" in text and "never guess" in text
    assert "clip_member" in staging.RASTER_FIELDS["optional"]
    assert "clip" in staging.describe_submit()


def test_over_the_cap_with_no_clip_is_declined_as_a_missing_field(env, host, log):
    """A national raster with no clip: declined the way a missing field is, saying how
    big it is, asking for the area — and nothing is staged or left behind."""
    url = host.put("cr_dem.tif", _tif(_noise()))
    out = _submit(_measure("measure_cr_dem", url))
    log("OUTPUT", out["problems"][0])
    assert out["status"] == "declined"
    said = out["problems"][0]
    assert said.startswith("missing required field 'clip': ")
    size = remote_clip.size_text(len(host.files["/cr_dem.tif"]))
    assert f"{url} is {size}, over the 256 KB cap" in said
    assert "ask the contributor which area to keep" in said and "never guess one" in said
    assert "[minLon, minLat, maxLon, maxLat]" in said and "place or contributed area name" in said
    assert store.list_contributions() == [] and _no_strays(env)


def test_a_clip_reads_only_the_window_over_range_requests_and_lands_with_its_provenance(env, host, log):
    """The gulf out of the 'national DEM': only the window is fetched, the stored file
    holds exactly the source cells (no resampling), and the url, box and sha256 ride
    the staged entry, the reply's note and — after approval — the landed row."""
    arr = _noise()
    data = _tif(arr)
    url = host.put("cr_dem.tif", data)
    out = _submit(_measure("measure_cr_dem_gulf", url, clip=GULF))
    assert out["status"] == "staged", out
    log("OUTPUT", out["preview"]["note"][:300])
    assert host.ranged_bytes("cr_dem.tif") < len(data) / 3            # a window, not the file
    with rasterio.open(_staged(out)) as ds:
        got, b = ds.read(1), ds.bounds
        assert ds.crs.to_epsg() == 4326
    cell = (WORLD[2] - WORLD[0]) / 1000
    c0, r0 = round((GULF[0] - WORLD[0]) / cell), round((WORLD[3] - GULF[3]) / cell)
    assert np.array_equal(got, arr[r0:r0 + got.shape[0], c0:c0 + got.shape[1]])
    assert b.left <= GULF[0] and b.right >= GULF[2] and b.bottom <= GULF[1] and b.top >= GULF[3]
    assert b.right - b.left < (GULF[2] - GULF[0]) + 2 * cell
    entry = out["preview"]["entry"]
    sha = hashlib.sha256(open(_staged(out), "rb").read()).hexdigest()
    assert entry["fetched"]["url"] == url and entry["fetched"]["clip"] == GULF
    assert entry["fetched"]["sha256"] == sha and entry["fetched"]["of"] == [1000, 1000]
    assert entry["fetched"]["read"].startswith("range requests")
    assert entry["prepared"][0].startswith("clipped at the source to lon -85.2 to -85")
    assert sha in entry["prepared"][0] and "Clipped at the source" in out["preview"]["note"]
    ok = staging.approve(out["contribution_id"], REVIEWER)
    assert ok["status"] == "approved", ok
    row = yaml.safe_load((env / "tiffs.contrib.yml").read_text())["measure_cr_dem_gulf"]
    landed = env / "tiffs" / "measure_cr_dem_gulf.tif"
    assert row["fetched"]["sha256"] == hashlib.sha256(landed.read_bytes()).hexdigest() == sha
    assert row["prepared"][0].startswith("clipped at the source") and _no_strays(env)


def test_a_projected_file_is_cut_in_its_own_crs_then_reprojected(env, host, log):
    """A UTM raster: the degree box is reprojected to the file's CRS to find the
    window; the clip then goes through the usual reprojection, and both are said."""
    arr = _noise(800, seed=3)
    tr = from_origin(300000, 1130000, 30, 30)
    url = host.put("utm.tif", _tif(arr, crs="EPSG:32616", transform=tr))
    w, s, e, n = transform_bounds("EPSG:32616", "EPSG:4326", 300000, 1106000, 324000, 1130000)
    box = [w + (e - w) * 0.4, s + (n - s) * 0.4, w + (e - w) * 0.6, s + (n - s) * 0.6]
    out = _submit(_measure("measure_utm_clip", url, clip=box))
    assert out["status"] == "staged", out
    prepared = out["preview"]["entry"]["prepared"]
    log("OUTPUT", " | ".join(prepared))
    assert prepared[0].startswith("clipped at the source")
    assert any("reprojected at ingest from EPSG:32616" in p for p in prepared)
    with rasterio.open(_staged(out)) as ds:
        b = ds.bounds
        assert ds.crs.to_epsg() == 4326
    assert b.left <= box[0] and b.right >= box[2] and b.bottom <= box[1] and b.top >= box[3]


def test_every_raster_role_takes_a_clip(env, host, log):
    """Every namespace in the schema's table — derived, never typed — takes a clip: a
    class map, a land-cover stack year, a count grid, a measure."""
    roles = rasters.namespaces()
    assert roles
    for prefix, rule in roles.items():
        name = prefix + "clipt"
        if rule.get("stack"):
            arr = np.where(np.arange(600)[None, :] % 2, 1, 2).repeat(600, 0).astype("uint8")
            extra = {"layer": f"{name}_2020", "stack": name, "year": 2020,
                     "legend": {1: "Mangrove", 2: "Other"},
                     "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 2}}
        elif rule.get("values") == "classes":
            arr = (np.arange(600 * 600) % 5 + 1).reshape(600, 600).astype("uint8")
            extra = {"layer": name, "legend": {i: f"class {i}" for i in range(1, 6)},
                     "declared": {"dtype": "uint8", "valid_min": 0, "valid_max": 5}}
        elif rule.get("values") in ("count", "amount"):
            arr = np.random.default_rng(1).uniform(0, 50, (600, 600)).astype("float32")
            extra = {"layer": name, "declared": {"dtype": "float32", "valid_min": 0,
                                                 "valid_max": 50, "units": "people per pixel"}}
        else:
            pytest.fail(f"namespace {prefix} holds {rule.get('values')!r}: teach this test "
                        "what a file of that kind looks like")
        url = host.put(f"{name}.tif", _tif(arr))
        out = _submit({**_measure(name, url, clip=GULF), **extra})
        log("OUTPUT", f"{prefix}: {out['status']}")
        assert out["status"] == "staged", (prefix, out)
        fetched = out["preview"]["entry"]["fetched"]
        assert fetched["clip"] == GULF and fetched["window"][2] < 600, prefix
        with rasterio.open(_staged(out)) as ds:
            assert ds.width < 600 and ds.height < 600, prefix


def test_a_fixed_file_resubmitted_at_the_same_link_is_read_afresh(env, host):
    """GDAL caches remote files in-process; a contributor who corrects a file and
    resubmits the same link gets the corrected cells, not the cached ones."""
    url = host.put("fixme.tif", _tif(np.full((400, 400), 5, "float32")))
    first = _submit(_measure("measure_first", url, clip=GULF))
    host.put("fixme.tif", _tif(np.full((500, 500), 9, "float32")))
    second = _submit(_measure("measure_second", url, clip=GULF))
    assert first["status"] == second["status"] == "staged", (first, second)
    with rasterio.open(_staged(first)) as a, rasterio.open(_staged(second)) as b:
        assert set(np.unique(a.read(1))) == {5} and set(np.unique(b.read(1))) == {9}
    assert second["preview"]["entry"]["fetched"]["of"] == [500, 500]


def test_a_clip_that_is_still_over_the_cap_says_give_a_smaller_box(env, host):
    """The cap holds on the stored clip — checked before reading when the window is
    plainly too big, and on the compressed file when it is not."""
    url = host.put("dem.tif", _tif(_noise()))
    huge = _submit(_measure("measure_huge", url, clip=list(WORLD)))
    assert huge["status"] == "declined" and "before compression" in huge["problems"][0]
    assert "give a smaller box" in huge["problems"][0]
    big = _submit(_measure("measure_big", url, clip=[-85.8, 9.6, -84.8, 10.4]))
    assert big["status"] == "declined", big
    assert "stored (compressed), over the 256 KB cap" in big["problems"][0]
    assert "give a smaller box" in big["problems"][0] and _no_strays(env)


def test_a_clip_is_checked_before_anything_is_fetched(env, host):
    """A box in the wrong order is named at once — no request is made."""
    url = host.put("dem.tif", _tif(_noise(200)))
    out = _submit(_measure("measure_badbox", url, clip=[-84.0, 9.0, -86.0, 11.0]))
    assert out["status"] == "declined" and "west to east" in " ".join(out["problems"])
    assert host.log == []
    far = _submit(_measure("measure_far", url, clip=[10.0, 10.0, 11.0, 11.0]))
    assert far["status"] == "declined" and "does not overlap" in far["problems"][0]
    assert "lon -86.000 to -84.000" in far["problems"][0]


def test_a_clip_may_name_a_place_but_never_a_guessed_box(env, host, monkeypatch):
    """A place resolves the way every pack resolves one; a name that resolves only to a
    point is refused rather than boxed by guesswork."""
    url = host.put("dem.tif", _tif(_noise(400)))
    area = {"geometry": shp_box(*GULF), "name": "Golfo de Nicoya", "approximate": False,
            "how": "contributed boundary: Golfo de Nicoya from study areas"}
    monkeypatch.setattr(ingest, "_resolve", lambda place, cap=None: area)
    out = _submit(_measure("measure_place", url, clip="Golfo de Nicoya"))
    assert out["status"] == "staged", out
    fetched = out["preview"]["entry"]["fetched"]
    assert fetched["clip_place"] == "Golfo de Nicoya" and fetched["clip"] == GULF
    assert "the bounding box of Golfo de Nicoya" in out["preview"]["entry"]["prepared"][0]
    monkeypatch.setattr(ingest, "_resolve", lambda place, cap=None: {
        **area, "approximate": True, "how": "10 km radius box around Puntarenas"})
    guess = _submit(_measure("measure_guess", url, clip="Puntarenas"))
    assert guess["status"] == "declined" and "will not guess an area" in guess["problems"][0]


def test_a_host_that_sends_only_whole_files(env, host):
    """No range requests: a file over the cap cannot be clipped here and the decline
    says what to do; one under the cap is downloaded whole and cut locally."""
    big = host.put("big.tif", _tif(_noise()), ranges=False)
    out = _submit(_measure("measure_whole_big", big, clip=GULF))
    assert out["status"] == "declined" and "sends it only whole" in out["problems"][0]
    small = host.put("small.tif", _tif(_noise(250)), ranges=False)
    ok = _submit(_measure("measure_whole_small", small, clip=GULF))
    assert ok["status"] == "staged", ok
    assert ok["preview"]["entry"]["fetched"]["read"] == "downloaded whole"


def test_a_private_host_is_refused_before_gdal_reads_it(env, host, monkeypatch):
    """GDAL's own client does the windowed reads, so the fetch policy runs first: a
    loopback host is refused unless the development switch allows it."""
    url = host.put("dem.tif", _tif(_noise(300)))
    monkeypatch.setattr(get_settings(), "grp_contrib_allow_private_hosts", False)
    out = _submit(_measure("measure_private", url, clip=GULF))
    assert out["status"] == "declined" and "private, loopback" in " ".join(out["problems"])
    assert host.log == []


def test_an_unshared_drive_file_is_said_to_be_unshared(env, host, monkeypatch):
    """The probe reads a Drive link through Drive's download host; a web page there
    means the file is not shared with Anyone with the link."""
    page = host.put("drive-page", b"<!DOCTYPE html><html><body>Sign in</body></html>")
    monkeypatch.setattr(remote_clip, "source_url", lambda url: page)
    with pytest.raises(fetch_policy.FetchRefused, match="not shared with 'Anyone with the link'"):
        remote_clip.probe("https://drive.google.com/file/d/1h931ygoVe8Gf6haGm_Xk36OdLX9R4pTX/view")


# --------------------------------------------------------------------------- 3. zips
def test_a_zip_over_the_cap_asks_for_the_area_and_the_member(env, host, log):
    """A producer zip of several rasters, over the cap: the decline asks for the clip
    and names the rasters inside (listed from the zip's directory over ranges)."""
    url = host.put("wc_bio.zip", _zip({"wc/bio_1.tif": _tif(_noise()),
                                       "wc/bio_12.tif": _tif(_noise(seed=2)),
                                       "wc/readme.txt": b"WorldClim-like"}))
    out = _submit(_measure("measure_bio1", url))
    log("OUTPUT", out["problems"][0])
    said = out["problems"][0]
    assert said.startswith("missing required field 'clip'")
    assert "It is a zip of 2 rasters (wc/bio_1.tif, wc/bio_12.tif)" in said and "clip_member" in said


def test_a_member_of_a_remote_zip_is_clipped_in_place(env, host, log):
    """clip + clip_member (or the url's #member): the window is read from inside the
    zip over range requests; the record names the member."""
    data = _zip({"wc/bio_1.tif": _tif(_noise()), "wc/bio_12.tif": _tif(_noise(seed=2))})
    url = host.put("wc_bio.zip", data)
    out = _submit(_measure("measure_bio1_gulf", url, clip=GULF, clip_member="wc/bio_1.tif"))
    assert out["status"] == "staged", out
    log("OUTPUT", out["preview"]["entry"]["prepared"][0][:200])
    fetched = out["preview"]["entry"]["fetched"]
    assert fetched["member"] == "wc/bio_1.tif" and fetched["clip"] == GULF
    assert "from 'wc/bio_1.tif' inside the zip" in out["preview"]["entry"]["prepared"][0]
    assert host.ranged_bytes("wc_bio.zip") < len(data) / 3
    frag = _submit(_measure("measure_bio12_gulf", url + "#wc/bio_12.tif", clip=GULF))
    assert frag["status"] == "staged" and frag["preview"]["entry"]["fetched"]["member"] == "wc/bio_12.tif"
    unnamed = _submit(_measure("measure_bio_any", url, clip=GULF))
    assert unnamed["status"] == "declined"
    assert "zip of 2 rasters (wc/bio_1.tif, wc/bio_12.tif)" in unnamed["problems"][0]
    wrong = _submit(_measure("measure_bio_wrong", url, clip=GULF, clip_member="bio_7.tif"))
    assert "'bio_7.tif' is not in the zip" in wrong["problems"][0]


def test_a_small_zip_is_read_by_member_and_never_shows_a_server_path(env, host, log):
    """Under the cap: a zip of one raster is read whole from inside; a zip of several
    says so and lists them; a zipped shapefile is sent to the vector kind — and no
    decline ever quotes the server's temporary path."""
    one = host.put("one.zip", _zip({"maps/gulf.tif": _tif(_noise(200)), "maps/gulf.tfw": b""},
                                   stored=False))
    out = _submit(_measure("measure_one", one))
    assert out["status"] == "staged", out
    entry = out["preview"]["entry"]
    assert entry["fetched"]["member"] == "maps/gulf.tif" and "clip" not in entry["fetched"]
    assert any("the one raster in the zip" in p for p in entry["prepared"])
    two = host.put("two.zip", _zip({"a.tif": _tif(_noise(100)), "b.tif": _tif(_noise(100))}))
    many = _submit(_measure("measure_two", two))
    log("OUTPUT", many["problems"][0])
    assert "is a zip of 2 rasters (a.tif, b.tif)" in many["problems"][0]
    assert "clip_member" in many["problems"][0]
    shp = host.put("rivers.zip", _zip({"rios.shp": b"x", "rios.dbf": b"x", "rios.prj": b"x"}))
    vec = _submit(_measure("measure_rivers", shp))
    assert "contribute it as kind 'vector'" in vec["problems"][0]
    for said in (many["problems"][0], vec["problems"][0]):
        assert "staged-tmp" not in said and str(env) not in said
    assert _no_strays(env)


def test_a_zip_reaching_the_gate_directly_is_named_not_a_gdal_error(tmp_path):
    """The CLI path (rasters.add on a local file): a zip is said to be one, with its
    files, and the path never appears."""
    p = tmp_path / "staged-tmp-abc.tif"
    p.write_bytes(_zip({"x/a.tif": b"II*\x00", "x/b.tif": b"II*\x00"}))
    said = rasters.unreadable(str(p), Exception(f"'{p}' not recognized as being in a supported file format."))
    assert said.startswith("the file is a ZIP archive of 2 files (x/a.tif, x/b.tif)")
    assert str(p) not in said and "staged-tmp" not in said


# --------------------------------------------------------------------------- 2. NetCDF
def test_an_erddap_netcdf_on_cf_lat_lon_is_read_as_wgs84(env, host, log):
    """An ERDDAP subset carries latitude/longitude in degrees and no CRS: read as
    EPSG:4326, values unchanged, and the note says so."""
    lat = 8.025 + 0.05 * np.arange(6)            # ascending, as ERDDAP serves it
    lon = -85.975 + 0.05 * np.arange(8)
    sst = (27.5 + 0.05 * np.arange(48)).reshape(1, 6, 8)
    sst[0, 0, 0] = -9999.0
    url = host.put("thetao.nc", _nc(lat, lon, sst))
    out = _submit(_measure("measure_sst", url, declared={
        "dtype": "float64", "valid_min": 27.5, "valid_max": 30.0, "nodata": -9999,
        "units": "degC"}))
    assert out["status"] == "staged", out
    note = " ".join(out["preview"]["entry"]["prepared"])
    log("OUTPUT", note)
    assert "CF coordinates 'latitude' and 'longitude'" in note and "EPSG:4326" in note
    with rasterio.open(_staged(out)) as ds:
        assert ds.crs.to_epsg() == 4326
        assert ds.bounds.left == pytest.approx(-86.0) and ds.bounds.top == pytest.approx(8.3)
        got = ds.read(1)
    assert np.array_equal(got, sst[0][::-1])     # north-up, every value as stored


def test_a_netcdf_with_no_coordinates_is_still_refused(env, host):
    """Genuinely unreferenced: no degree units on the axes -> still no CRS."""
    url = host.put("plain.nc", _nc(np.arange(6.0), np.arange(8.0), np.zeros((1, 6, 8)) + 1,
                                   lat_name="y", lon_name="x", units=False))
    out = _submit(_measure("measure_plain", url, declared={
        "dtype": "float64", "valid_min": 0, "valid_max": 5, "units": "m"}))
    assert out["status"] == "declined"
    assert "carries no coordinate reference system" in out["problems"][0]


# --------------------------------------------------------------------------- 4. pictures
def test_a_map_picture_is_said_to_be_a_picture(env, host, log):
    """A print-layout PNG offered as a raster: a picture of a map, not 'assign its CRS'."""
    rgba = np.zeros((4, 120, 160), dtype="uint8")
    rgba[3] = 255
    with MemoryFile(ext=".png") as mf:
        with mf.open(driver="PNG", width=160, height=120, count=4, dtype="uint8") as d:
            d.write(rgba)
        png = mf.read()
    url = host.put("mapa_ecosistemas_mangle.png", png)
    out = _submit({**_measure("landcover_cr_mapa_png_2021", url), "stack": "landcover_cr_mapa_png",
                   "year": 2021, "legend": {1: "Mangle"},
                   "declared": {"dtype": "uint8", "valid_min": 0, "valid_max": 255}})
    log("OUTPUT", out["problems"][0])
    said = out["problems"][0]
    assert said.startswith("the file is a picture (PNG image, 160 x 120 pixels, RGBA)")
    assert "not a georeferenced raster" in said and "assign its CRS" not in said


# --------------------------------------------------------------------------- security
def test_a_vrt_is_refused_and_what_it_names_is_never_read(env, host):
    """A VRT names other files; GDAL would read them on the server. It is refused from
    its first bytes, whole or clipped, and the file it names is never requested."""
    secret = host.put("secret.tif", _tif(_noise(64)))
    vrt = (f'<VRTDataset rasterXSize="64" rasterYSize="64"><SRS>EPSG:4326</SRS>'
           f'<GeoTransform>-86, 0.03, 0, 11, 0, -0.03</GeoTransform>'
           f'<VRTRasterBand dataType="Float32" band="1"><SimpleSource>'
           f'<SourceFilename relativeToVRT="0">/vsicurl/{secret}</SourceFilename>'
           f'<SourceBand>1</SourceBand></SimpleSource></VRTRasterBand></VRTDataset>').encode()
    url = host.put("mosaic.vrt", vrt)
    for clip in (None, GULF):
        out = _submit(_measure("measure_vrt", url, **({"clip": clip} if clip else {})))
        assert out["status"] == "declined" and "virtual raster (VRT)" in out["problems"][0]
    assert not [p for p, _, _ in host.log if p == "/secret.tif"]
    assert rasters.INDIRECT >= {"VRT", "WMS"}
