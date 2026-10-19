"""Sprint dry run (2026-10-19): what a hub hits contributing a mangrove use case.

Google Drive share links fetch the file, not the page; a projected ERDAS/UTM class
raster is reprojected to EPSG:4326 at ingest and the passport says so; a binary
extent stack (0 = not mangrove) shows loss and gain; line and polygon layers are
accepted, measured inside any pack's area, and a named polygon resolves as a place.
All on synthetic data, never presented as real."""
import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from requests.structures import CaseInsensitiveDict
from shapely.geometry import box, mapping

from app.config import get_settings
from app.contrib import fetch_policy, identity, rasters, staging
from app.graph.geo import ingest, stacks, vectors
from app.mcp import assemble, store, vectors as vectors_step

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")


# ----------------------------------------------------------------- Drive links
def test_drive_share_links_become_direct_downloads_and_folders_are_refused():
    fid = "1ngkTwhAP11MJRPGmVdtIPdj2snrJdqTA"
    for url in (f"https://drive.google.com/file/d/{fid}/view?usp=sharing",
                f"https://drive.google.com/open?id={fid}",
                f"https://drive.google.com/uc?id={fid}&export=download"):
        assert fetch_policy.drive_file_id(url) == fid
        assert fetch_policy.direct_url(url) == f"https://drive.google.com/uc?export=download&id={fid}"
    assert fetch_policy.direct_url("https://example.org/a.tif") == "https://example.org/a.tif"
    folder = fetch_policy.check_url("https://drive.google.com/drive/folders/1dUNKBDyfIP14y2Mx8keZwQxdAlJuhFe9")
    assert folder and "FOLDER" in folder[0]
    doc = fetch_policy.check_url("https://docs.google.com/document/d/1abcdefghijklmnop/edit")
    assert doc and "Google Docs" in doc[0]


def test_drive_confirm_page_is_followed_once_and_a_sign_in_page_is_refused(monkeypatch):
    """Drive answers HTML in two cases: the virus-scan confirm page (follow its form)
    and a not-shared/sign-in page (refuse in words). The filename comes from the
    response, not the URL, so a Drive file is not archived as 'uc'."""
    fid = "1ngkTwhAP11MJRPGmVdtIPdj2snrJdqTA"
    html = (f'<html><form id="download-form" action="https://drive.usercontent.google.com/download" method="get">'
            f'<input type="hidden" name="id" value="{fid}"><input type="hidden" name="export" value="download">'
            f'<input type="hidden" name="confirm" value="t"><input type="hidden" name="uuid" value="abc"></form></html>')
    calls = []

    class R:
        def __init__(self, status, headers, body):
            self.status_code, self.headers, self._body = status, CaseInsensitiveDict(headers), body
        def iter_content(self, chunk_size=1):
            yield self._body
        def raise_for_status(self):
            pass
        def close(self):
            pass

    class S:
        def get(self, url, **kw):
            # the policy pins the connection to an IP and carries the name in Host
            host = (kw.get("headers") or {}).get("Host", "")
            calls.append(f"{host}{url[url.index('/', 8):]}")
            if "usercontent" in host:
                return R(200, {"content-type": "image/tiff",
                               "content-disposition": 'attachment; filename="MANGLE_2014.tif"'}, b"II*\x00data")
            return R(200, {"content-type": "text/html; charset=utf-8"}, html.encode())
        def close(self):
            pass
    monkeypatch.setattr(fetch_policy, "_addresses", lambda host: [__import__("ipaddress").ip_address("142.250.1.1")])
    monkeypatch.setattr(fetch_policy, "_session", lambda hostname, scheme: S())
    data, name = fetch_policy.fetch(f"https://drive.google.com/file/d/{fid}/view", max_bytes=10_000)
    assert data == b"II*\x00data" and name == "MANGLE_2014.tif"
    assert "uc?export=download" in calls[0] and "confirm=t" in calls[1]

    class S2(S):
        def get(self, url, **kw):
            return R(200, {"content-type": "text/html"}, b"<html>Sign in</html>")
    monkeypatch.setattr(fetch_policy, "_session", lambda hostname, scheme: S2())
    with pytest.raises(fetch_policy.FetchRefused, match="Anyone with the link"):
        fetch_policy.fetch(f"https://drive.google.com/file/d/{fid}/view", max_bytes=10_000)


# ----------------------------------------------------------------- raster gate
def _write(path, arr, crs, transform, nodata=None, driver="GTiff"):
    with rasterio.open(path, "w", driver=driver, width=arr.shape[1], height=arr.shape[0], count=1,
                       dtype=arr.dtype, crs=crs, transform=transform, nodata=nodata) as dst:
        dst.write(arr, 1)
    return str(path)


def test_projected_class_raster_is_reprojected_at_ingest_and_said_so(tmp_path):
    arr = np.full((20, 20), 2, dtype="uint8")
    arr[:, :10] = 1
    # UTM 16N, 30 m cells, somewhere on the Pacific coast of Costa Rica
    p = _write(tmp_path / "utm.tif", arr, "EPSG:32616", from_origin(700000, 1120000, 30, 30), nodata=255)
    out, notes = rasters.normalise(p, "landcover")
    assert out.endswith(".norm.tif") and notes and "reprojected at ingest from EPSG:32616" in notes[0]
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326 and ds.driver == "GTiff" and ds.nodata == 255
        vals = set(np.unique(ds.read(1)).tolist())
    assert vals <= {1, 2, 255} and {1, 2} <= vals          # classes unchanged, fill is nodata
    # an EPSG:4326 GeoTIFF passes through untouched
    q = _write(tmp_path / "geo.tif", arr, "EPSG:4326", from_origin(-85.0, 10.0, 0.001, 0.001))
    assert rasters.normalise(q, "landcover") == (q, [])
    # a count grid cannot be re-gridded: refused in words
    with pytest.raises(ValueError, match="count grid"):
        rasters.normalise(p, "population")
    # a file with no CRS is refused in words
    r = _write(tmp_path / "nocrs.tif", arr, None, from_origin(0, 0, 1, 1))
    with pytest.raises(ValueError, match="no coordinate reference system"):
        rasters.normalise(r, "landcover")


def test_one_class_legend_gets_the_binary_map_hint():
    m = {"layer": "landcover_gmw_test_2020", "legend": {1: "Mangrove"}}
    assert rasters.hints(m) and "declare it as a class" in rasters.hints(m)[0]
    assert rasters.hints({**m, "legend": {0: "Not mangrove", 1: "Mangrove"}, "outside_values": []}) == []
    assert rasters.hints({**m, "outside_values": [255]}) == []


# ----------------------------------------------------- binary stack: loss and gain
@pytest.fixture
def binary_stack(tmp_path, monkeypatch):
    """A two-year 0/1 extent stack (0 = not mangrove, a CLASS; nothing is outside)."""
    s = get_settings()
    W = H = 10
    PX = 0.001
    LEFT, TOP = -85.0, 10.0
    a = np.zeros((H, W), dtype="uint8"); a[:, :4] = 1            # 1996: four columns mangrove
    b = np.zeros((H, W), dtype="uint8"); b[:, 1:3] = 1; b[:, 6] = 1  # 2020: lost cols 0,3; gained col 6
    data = tmp_path / "data"; data.mkdir()
    for y, arr in ((1996, a), (2020, b)):
        _write(data / f"gmw-{y}.tif", arr, "EPSG:4326", from_origin(LEFT, TOP, PX, PX), nodata=0)
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "tiffs.yml").write_text(json.dumps({"landcover_bin": {
        "kind": "stack", "role": "landcover", "internal": True, "title": "Binary test extent (synthetic)",
        "description": "synthetic", "source": "test fixture", "license": "CC0-1.0", "pub_date": "2026-01-01",
        "authority": "test", "accuracy": "unstated", "legend": {0: "Not mangrove", 1: "Mangrove"},
        "outside_values": [], "years": [1996, 2020], "countries": ["Testland"],
        "extent": {"lon": [LEFT, LEFT + W * PX], "lat": [TOP - H * PX, TOP]},
        "layer_pattern": "landcover_bin_{year}", "file_pattern": "gmw-{year}.tif", "data_root": str(data)}}))
    (conf / "raster_schema.yml").write_text(json.dumps({
        "defaults": {"crs": "EPSG:4326", "float_tol": 0.0001}, "layers": {},
        "stacks": {"landcover_bin": {"role": "landcover", "dtype": "uint8", "valid_min": 0, "valid_max": 1,
                                     "nodata": 0, "pixel_size_deg": PX}}}))
    for k, v in (("tiffs_config_path", conf / "tiffs.yml"), ("tiffs_contrib_path", conf / "tiffs.contrib.yml"),
                 ("raster_schema_path", conf / "raster_schema.yml"),
                 ("raster_schema_contrib_path", conf / "raster_schema.contrib.yml"),
                 ("cache_dir", tmp_path / "cache"), ("tiffs_dir", tmp_path / "cache" / "tiffs")):
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    poly = box(LEFT + 1 * PX, TOP - 9 * PX, LEFT + 9 * PX, TOP - 1 * PX)   # columns 1-8, rows 1-8 inside
    return {"poly": mapping(poly), "PX": PX, "LEFT": LEFT, "TOP": TOP}


def test_binary_extent_stack_reports_loss_and_gain(binary_stack, log):
    p = assemble.assemble(pack="land-cover", place="Test bay", year="1996", year_to="2020",
                          geometry=binary_stack["poly"], classes=["Mangrove"])
    assert p["status"] == "ok", p.get("note")
    ch = p["stats"]["change"]["by_class"]["Mangrove"]
    log("OUTPUT", f"mangrove from {ch['km2_from']:.4f} to {ch['km2_to']:.4f} km2; loss {ch['loss_km2']:.4f} gain {ch['gain_km2']:.4f}")
    # inside: columns 1-8. 1996 mangrove = cols 1,2,3 (3 cols); 2020 = cols 1,2,6 (3 cols)
    # loss = col 3, gain = col 6, stable = cols 1,2  -> loss == gain == one column, both > 0
    assert ch["loss_km2"] > 0 and ch["gain_km2"] > 0
    assert abs(ch["loss_km2"] - ch["gain_km2"]) < 1e-9
    assert abs(ch["km2_from"] - ch["km2_to"]) < 1e-9
    assert p["stats"]["change"]["km2_one_year_only"] == 0           # nothing is "outside": 0 is a class
    texts = " ".join(c["text"] for c in p["citations"])
    assert "Not mangrove" in texts and "Mangrove" in texts
    assert "Testland" in " ".join(str(v) for v in __import__("app.mcp.packs", fromlist=["x"]).PACKS["land-cover"]["where"]())


# ------------------------------------------------------------- vector layers
RIVERS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"NOMBRE": "Rio Tempisque", "CATEGORIA": "principal"},
     "geometry": {"type": "LineString", "coordinates": [[-85.2, 10.3], [-85.1, 10.2], [-85.0, 10.1]]}},
    {"type": "Feature", "properties": {"NOMBRE": "Quebrada Seca", "CATEGORIA": "secundario"},
     "geometry": {"type": "LineString", "coordinates": [[-85.05, 10.15], [-84.95, 10.05]]}},
    {"type": "Feature", "properties": {"NOMBRE": "Far away", "CATEGORIA": "principal"},
     "geometry": {"type": "LineString", "coordinates": [[-83.0, 9.0], [-82.9, 9.1]]}}]}
AREAS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"NAME": "Humedal Nacional Térraba-Sierpe", "DESIG": "Humedal"},
     "geometry": {"type": "Polygon", "coordinates": [[[-85.15, 10.05], [-85.05, 10.05], [-85.05, 10.25], [-85.15, 10.25], [-85.15, 10.05]]]}},
    {"type": "Feature", "properties": {"NAME": "Reserva Playa Hermosa", "DESIG": "Reserva"},
     "geometry": {"type": "Polygon", "coordinates": [[[-85.0, 10.0], [-84.9, 10.0], [-84.9, 10.1], [-85.0, 10.1], [-85.0, 10.0]]]}}]}


def test_inspect_accepts_lines_and_polygons_and_refuses_mixed_or_projected():
    assert vectors.inspect(json.dumps(RIVERS).encode())["geometry"] == "lines"
    out = vectors.inspect(json.dumps(AREAS).encode())
    assert out["geometry"] == "polygons" and "NAME" in out["properties"]
    mixed = {"type": "FeatureCollection", "features": RIVERS["features"][:1] + AREAS["features"][:1]}
    with pytest.raises(ValueError, match="one geometry family"):
        vectors.inspect(json.dumps(mixed).encode())
    utm = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {
        "type": "LineString", "coordinates": [[700000, 1100000], [700100, 1100100]]}}]}
    with pytest.raises(ValueError, match="reprojected"):
        vectors.inspect(json.dumps(utm).encode())


def test_measures_on_the_sphere():
    one_deg = {"type": "LineString", "coordinates": [[0, 0], [1, 0]]}
    from shapely.geometry import shape
    assert abs(vectors.length_km(shape(one_deg)) - 111.19) < 0.2
    sq = shape({"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]})
    assert abs(vectors.area_km2(sq) - 12364) < 60               # one square degree at the equator


@pytest.fixture
def vec_env(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "tiffs_dir", tmp_path / "cache" / "tiffs")
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    bodies = {"https://x.test/rivers.geojson": json.dumps(RIVERS).encode(),
              "https://x.test/areas.geojson": json.dumps(AREAS).encode()}
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (bodies[url], url.rsplit("/", 1)[-1]))
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    monkeypatch.setenv("GRP_AUTO_APPROVE", "1")
    monkeypatch.setattr(s, "grp_auto_approve", True)
    yield tmp_path
    staging.STAGED_VECTORS.clear()


def _as(caller, fn):
    tok = identity.bind(caller)
    try:
        return fn()
    finally:
        identity.unbind(tok)


def test_line_and_polygon_layers_land_measure_inside_an_area_and_name_places(vec_env, log):
    r = staging.submit("vector", {"layer": "test_rivers", "url": "https://x.test/rivers.geojson",
                                  "title": "Test rivers (synthetic)", "description": "synthetic lines",
                                  "source": "test fixture", "license": "CC0-1.0", "vintage": "2026-10",
                                  "name_field": "NOMBRE", "group_field": "CATEGORIA", "countries": ["Testland"]}, OWNER)
    assert r["status"] in ("staged", "landed", "approved"), r
    log("OUTPUT", r.get("preview", {}).get("how_to_test") or r.get("how_to_test"))
    a = staging.submit("vector", {"layer": "test_areas", "url": "https://x.test/areas.geojson",
                                  "title": "Test protected areas (synthetic)", "description": "synthetic polygons",
                                  "source": "test fixture", "license": "CC0-1.0", "vintage": "2026-10",
                                  "name_field": "NAME", "group_field": "DESIG"}, OWNER)
    assert a["status"] in ("staged", "landed", "approved"), a
    reg = _as(OWNER, vectors.visible)
    assert vectors.family(reg["test_rivers"]) == "lines" and vectors.family(reg["test_areas"]) == "polygons"

    # measured inside a boundary: the 'Far away' river is outside; the two others are clipped
    bnd = box(-85.3, 10.0, -84.9, 10.4)
    rec = _as(OWNER, lambda: vectors.summarise(reg["test_rivers"], bnd))
    log("OUTPUT", json.dumps(rec)[:300])
    assert rec["unit"] == "km" and rec["features_inside"] == 2 and rec["total"] > 20
    assert set(rec["by_group"]) == {"principal", "secundario"}
    assert rec["largest_named"][0]["name"] == "Rio Tempisque"
    pa = _as(OWNER, lambda: vectors.summarise(reg["test_areas"], box(-85.2, 10.0, -85.0, 10.3)))
    assert pa["unit"] == "km2" and pa["features_inside"] == 1 and 200 < pa["total"] < 260   # 0.1 x 0.2 deg

    # a contributed polygon's name resolves as a place: accent-insensitive, with the hub's `how`
    hit = _as(OWNER, lambda: vectors.named_area("humedal nacional terraba-sierpe"))
    assert hit and not hit["ambiguous"] and hit["label"].startswith("Humedal") and "contributed boundary" in hit["how"]
    partial = _as(OWNER, lambda: vectors.named_area("Térraba-Sierpe"))
    assert partial and partial["label"].startswith("Humedal")
    assert _as(OWNER, lambda: vectors.named_area("Nowhere Bay")) is None
    # ...and through the resolver and the AOI machinery, cached under its own key
    res = _as(OWNER, lambda: ingest.resolve_place("Humedal Nacional Terraba-Sierpe"))
    assert res["status"] == "ok" and res["is_contributed_boundary"] and 200 < res["area_km2"] < 260
    aoi = _as(OWNER, lambda: ingest.ensure_aoi(place="Humedal Nacional Terraba-Sierpe", layers=()))
    assert aoi["how"].startswith("contributed boundary") and aoi["name"].startswith("Humedal")


def test_seam_measures_named_vectors_for_any_pack_and_declines_unknown_names(vec_env, log):
    staging.submit("vector", {"layer": "test_rivers", "url": "https://x.test/rivers.geojson",
                              "title": "Test rivers (synthetic)", "description": "synthetic lines",
                              "source": "test fixture", "license": "CC0-1.0", "vintage": "2026-10",
                              "name_field": "NOMBRE", "group_field": "CATEGORIA"}, OWNER)
    spec = {"id": "demo"}
    stats = {"_area": {"name": "Test bay", "how": "supplied polygon",
                       "geometry": mapping(box(-85.3, 10.0, -84.9, 10.4))}}
    citations, gaps = [{"n": 1, "text": "x"}], []
    block = _as(OWNER, lambda: vectors_step.attach(spec, {"place": "Test bay"}, stats, citations, gaps,
                                                  ["test_rivers"], {}))
    assert block and "test_rivers" in block
    cit = citations[-1]
    log("OUTPUT", cit["text"][:400])
    assert cit["n"] == 2 and cit["kind"] == "vector_summary" and cit["features_sha256"] == block["test_rivers"]["features_sha256"]
    assert "km of line" in cit["text"] and "By CATEGORIA" in cit["text"] and "Rio Tempisque" in cit["text"]
    assert _as(OWNER, lambda: vectors_step.check(["nope"])) and "unknown vector layer" in _as(OWNER, lambda: vectors_step.check(["nope"]))
    assert vectors_step.check(None) is None
    # the risk pack's counted assets stay point layers: a line layer is not an "asset"
    from app.risk import synthesis as risk
    assert "test_rivers" not in _as(OWNER, risk._asset_layers)


# --------------------------------------------- a freshly contributed stack, end to end
def test_a_contributed_extent_stack_answers_the_land_cover_pack(tmp_path, monkeypatch, log):
    """Two years of a 0/1 extent map contributed over the gate (auto-approve on), then
    the land-cover pack counts area, loss and gain over a polygon — no code change."""
    s = get_settings()
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "tiffs.yml").write_text("# hand-authored\n{}\n")
    (conf / "raster_schema.yml").write_text(json.dumps({"defaults": {"crs": "EPSG:4326", "float_tol": 0.0001}, "layers": {}}))
    for k, v in (("tiffs_config_path", conf / "tiffs.yml"), ("tiffs_contrib_path", conf / "tiffs.contrib.yml"),
                 ("raster_schema_path", conf / "raster_schema.yml"),
                 ("raster_schema_contrib_path", conf / "raster_schema.contrib.yml"),
                 ("cache_dir", tmp_path / "cache"), ("tiffs_dir", tmp_path / "cache" / "tiffs"),
                 ("grp_mattermost_webhook", ""), ("grp_auto_approve", True)):
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    staging.STAGED_RASTERS.clear()
    monkeypatch.setattr(staging, "_STAGED_LOADED", True)
    W = H = 10; PX = 0.001; LEFT, TOP = -85.0, 10.0
    a = np.zeros((H, W), dtype="uint8"); a[:, :4] = 1
    b = np.zeros((H, W), dtype="uint8"); b[:, 1:3] = 1; b[:, 6] = 1
    files = {}
    for y, arr in ((1996, a), (2020, b)):
        files[f"https://x.test/gmw-{y}.tif"] = open(_write(tmp_path / f"src-{y}.tif", arr, "EPSG:4326",
                                                           from_origin(LEFT, TOP, PX, PX), nodata=0), "rb").read()
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (files[url], url.rsplit("/", 1)[-1]))
    for y in (1996, 2020):
        out = staging.submit("raster", {
            "layer": f"landcover_bintest_{y}", "stack": "landcover_bintest", "year": y,
            "stack_title": "Binary extent test (synthetic)", "url": f"https://x.test/gmw-{y}.tif",
            "title": f"Extent {y} (synthetic)", "description": "synthetic 0/1 extent", "source": "test fixture",
            "license": "CC0-1.0", "vintage": "2026-10", "legend": {0: "Not mangrove", 1: "Mangrove"},
            "outside_values": [], "countries": ["Testland"],
            "declared": {"dtype": "uint8", "valid_min": 0, "valid_max": 1, "nodata": 0}}, OWNER)
        assert out["status"] == "approved", out
        log("OUTPUT", out["preview"]["how_to_test"][:160])
        assert "land-cover" in out["preview"]["how_to_test"] and "only you" not in out["preview"]["how_to_test"]
    poly = mapping(box(LEFT + 1 * PX, TOP - 9 * PX, LEFT + 9 * PX, TOP - 1 * PX))
    p = assemble.assemble(pack="land-cover", place="Test bay", year="1996", year_to="2020",
                          geometry=poly, classes=["Mangrove"])
    assert p["status"] == "ok", p.get("note")
    ch = p["stats"]["change"]["by_class"]["Mangrove"]
    assert ch["loss_km2"] > 0 and ch["gain_km2"] > 0 and p["stats"]["change"]["km2_one_year_only"] == 0
    assert any("Testland" in w for w in __import__("app.mcp.packs", fromlist=["x"]).PACKS["land-cover"]["where"]())
    staging.STAGED_RASTERS.clear()


def test_an_upstream_refusal_names_the_site_and_what_to_do(monkeypatch):
    class R:
        status_code, reason, headers = 403, "Forbidden", CaseInsensitiveDict({"content-type": "text/html"})
        def iter_content(self, chunk_size=1):
            yield b""
        def close(self):
            pass
    class S:
        def get(self, url, **kw):
            return R()
        def close(self):
            pass
    monkeypatch.setattr(fetch_policy, "_addresses", lambda host: [__import__("ipaddress").ip_address("23.62.46.184")])
    monkeypatch.setattr(fetch_policy, "_session", lambda hostname, scheme: S())
    with pytest.raises(fetch_policy.FetchRefused) as exc:
        fetch_policy.fetch("https://www.mdpi.com/2072-4292/14/15/3657/pdf", max_bytes=1000)
    msg = str(exc.value)
    assert "www.mdpi.com" in msg and "23.62.46.184" not in msg and "Google Drive" in msg


def test_binary_stack_values_count_both_classes_and_gaps_derive_from_the_stack(binary_stack, log):
    """The nodata tag (0) is also the legend class 'Not mangrove': the values block
    reads both classes, no false 'one value' gap; the standing gaps state the
    declared accuracy and the real cell size instead of typed sentences."""
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020",
                          geometry=binary_stack["poly"], classes=["Mangrove"])
    assert p["status"] == "ok", p.get("note")
    rec = p["layers"]["landcover_bin_2020"]
    log("OUTPUT", f"distinct={rec['summary'].get('distinct')} nodata rule={rec['nodata']['rule'][:120]}")
    assert rec["summary"]["distinct"] == 2 and not rec["summary"].get("uniform")
    assert "legend class" in rec["nodata"]["rule"]
    gaps = " | ".join(p["gaps"])
    assert "holds ONE value" not in gaps
    assert "not documented" in gaps                 # the fixture declares accuracy 'unstated'
    assert "111 m pixel" in gaps and "30 m pixel" not in gaps


def test_the_stack_that_has_the_year_is_read_when_several_cover_the_area(binary_stack, tmp_path, monkeypatch, log):
    """Two stacks cover the same bay; the first registered holds only 2014. A 1996
    question reads the second, names the first as an alternative, and a year no
    stack holds declines naming each stack's years."""
    s = get_settings()
    conf = tmp_path / "conf"
    data = tmp_path / "data"
    PX, LEFT, TOP = binary_stack["PX"], binary_stack["LEFT"], binary_stack["TOP"]
    _write(data / "other-2014.tif", np.ones((10, 10), dtype="uint8"), "EPSG:4326", from_origin(LEFT, TOP, PX, PX), nodata=0)
    cat = json.loads((conf / "tiffs.yml").read_text())
    cat = {"landcover_first": {**cat["landcover_bin"], "title": "First stack (synthetic, 2014 only)",
                               "years": [2014], "layer_pattern": "landcover_first_{year}",
                               "file_pattern": "other-{year}.tif", "accuracy": "90% (synthetic)"}, **cat}
    (conf / "tiffs.yml").write_text(json.dumps(cat))
    sch = json.loads((conf / "raster_schema.yml").read_text())
    sch["stacks"]["landcover_first"] = sch["stacks"]["landcover_bin"]
    (conf / "raster_schema.yml").write_text(json.dumps(sch))
    p = assemble.assemble(pack="land-cover", place="Test bay", year="1996", year_to="2020",
                          geometry=binary_stack["poly"], classes=["Mangrove"])
    assert p["status"] == "ok", p.get("note")
    assert p["stats"]["stack"] == "landcover_bin"
    assert any("First stack" in g for g in p["gaps"])
    q = assemble.assemble(pack="land-cover", place="Test bay", year="2014", geometry=binary_stack["poly"])
    assert q["status"] == "ok" and q["stats"]["stack"] == "landcover_first"
    assert any("accuracy as declared" in g and "90%" in g for g in q["gaps"])
    r = assemble.assemble(pack="land-cover", place="Test bay", year="1999", geometry=binary_stack["poly"])
    log("OUTPUT", r.get("note"))
    assert r["status"] == "declined" and "has 2014" in r["note"] and "has 1996, 2020" in r["note"]


# ------------------------------------------------------------- factor tables
IPCC_CSV = """Class,Factor,Unit,Pool,Basis,Low,High
Mangrove,386,t C/ha,soil organic carbon 0-1 m (aggregated organic and mineral soils),IPCC 2013 Wetlands Supplement Table 4.11 (n=119),351,424
Mangrove,86.6,t C/ha,above-ground biomass carbon (192 t d.m./ha tropical wet x carbon fraction 0.451),IPCC 2013 Wetlands Supplement Tables 4.2 and 4.3,,
Seagrass meadow,108,t C/ha,soil organic carbon mineral soils,IPCC 2013 Wetlands Supplement Table 4.11,,
"""


def test_a_factor_table_bound_to_land_cover_multiplies_class_areas_and_change(binary_stack, tmp_path, monkeypatch, log):
    """A contributed table (class, factor per hectare) bound to the land-cover pack
    is applied to the areas the pack counted — stock, loss, gain — and the
    arithmetic is in the citation text; a class the stack lacks is skipped and said."""
    s = get_settings()
    feeds_dir = tmp_path / "feeds"; feeds_dir.mkdir()
    monkeypatch.setattr(s, "feeds_conf_dir", feeds_dir)
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(s, "grp_auto_approve", True)
    from app.mcp import registry
    registry.reload_declarative_feeds()
    staging.STAGED_FEEDS.clear()
    monkeypatch.setattr(staging, "_STAGED_LOADED", True)
    out = staging.submit("table", {
        "dataset": "ipcc_mangrove_tier1_test", "pack": "land-cover",
        "title": "IPCC 2013 Wetlands Supplement Tier 1 mangrove carbon defaults (test copy)",
        "description": "Per-hectare carbon stock defaults for mangroves from the IPCC 2013 Wetlands Supplement, Chapter 4; the above-ground row is the contributor's arithmetic (192 x 0.451).",
        "source": "IPCC 2013 Wetlands Supplement, Chapter 4 (Coastal Wetlands)", "validation": "multi-agency-consensus",
        "license": "unstated", "vintage": "2014-02", "cadence": "irregular",
        "columns": {"class": "Class", "factor": "Factor", "unit": "Unit", "pool": "Pool", "basis": "Basis", "low": "Low", "high": "High"},
        "units": "tonnes of carbon per hectare", "csv_text": IPCC_CSV,
        "usage_notes": "Tier 1 global defaults; a site measurement supersedes them."}, OWNER)
    assert out["status"] == "approved", out
    p = assemble.assemble(pack="land-cover", place="Test bay", year="1996", year_to="2020",
                          geometry=binary_stack["poly"], classes=["Mangrove"])
    assert p["status"] == "ok", p.get("note")
    cits = [c for c in p["citations"] if c["kind"] == "class_factor"]
    assert len(cits) == 1
    text = cits[0]["text"]
    log("OUTPUT", text[:600])
    f = p["stats"]["factors"]["ipcc_mangrove_tier1_test"]["Mangrove"]
    soil = next(e for e in f if e["factor"] == 386)
    ha = p["stats"]["classes_km2"]["Mangrove"] * 100          # stats round to 0.1 km2 (10 ha)
    # stats round ha to 0.1 and value is computed from the unrounded area
    assert abs(soil["ha"] - ha) <= 10 and abs(soil["value"] - soil["ha"] * 386) < 386 * 0.1
    assert f"× 386 t C/ha" in text and "= " in text and "95% CI 351-424" in text
    assert "loss" in text and "gain" in text and "not an emission estimate" in text
    assert "1 row(s) named classes not in this legend" in text      # the seagrass row
    assert soil["value_lost"] > 0 and soil["value_gained"] > 0
    # every number the brief may quote is in the citation text
    for v in (soil["value"], soil["value_lost"]):
        assert f"{v:,.0f}" in text
    registry.reload_declarative_feeds()
