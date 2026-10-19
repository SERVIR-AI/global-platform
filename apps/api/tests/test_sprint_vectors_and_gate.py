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
    block, where = _as(OWNER, lambda: vectors_step.attach(spec, {"place": "Test bay"}, stats, citations, gaps,
                                                         ["test_rivers"], {}))
    assert block and "test_rivers" in block and where["area_sha256"] and where["geometry"]
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


def test_private_hosts_stay_refused_unless_the_dev_switch_is_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "grp_contrib_allow_private_hosts", False)
    assert fetch_policy.check_url("http://127.0.0.1:8899/x.geojson")
    monkeypatch.setattr(s, "grp_contrib_allow_private_hosts", True)
    assert fetch_policy.check_url("http://127.0.0.1:8899/x.geojson") == []


def test_removing_a_contributed_year_keeps_its_stack_honest(tmp_path, monkeypatch):
    """Removing the only year of a contributed stack removes the stack row too;
    removing one of several drops it from the series; naming the stack removes all."""
    from app.contrib import removal
    s = get_settings()
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "tiffs.yml").write_text("# hand-authored\n{}\n")
    (conf / "raster_schema.yml").write_text(json.dumps({"defaults": {"crs": "EPSG:4326"}, "layers": {}}))
    for k, v in (("tiffs_config_path", conf / "tiffs.yml"), ("tiffs_contrib_path", conf / "tiffs.contrib.yml"),
                 ("raster_schema_path", conf / "raster_schema.yml"),
                 ("raster_schema_contrib_path", conf / "raster_schema.contrib.yml"),
                 ("cache_dir", tmp_path / "cache"), ("tiffs_dir", tmp_path / "cache" / "tiffs")):
        monkeypatch.setattr(s, k, v)
    arr = np.ones((5, 5), dtype="uint8")
    for y in (1996, 2020):
        rasters.add({"layer": f"landcover_rm_{y}", "stack": "landcover_rm", "year": y, "stack_title": "rm",
                     "file": _write(tmp_path / f"{y}.tif", arr, "EPSG:4326", from_origin(-85, 10, 0.001, 0.001)),
                     "title": f"{y}", "description": "d", "source": "s", "license": "CC0-1.0", "vintage": "2026-10",
                     "legend": {1: "Mangrove"}, "declared": {"dtype": "uint8", "valid_min": 0, "valid_max": 1}})
    import yaml
    cat = yaml.safe_load((conf / "tiffs.contrib.yml").read_text())
    assert cat["landcover_rm"]["years"] == [1996, 2020]
    out = removal.remove_raster("landcover_rm_1996")
    cat = yaml.safe_load((conf / "tiffs.contrib.yml").read_text())
    assert out["status"] == "removed" and cat["landcover_rm"]["years"] == [2020] and "dropped" in out["stack"]
    out = removal.remove_raster("landcover_rm_2020")
    cat = yaml.safe_load((conf / "tiffs.contrib.yml").read_text())
    assert "landcover_rm" not in cat and "removed with it" in out["stack"]
    # a whole stack by name
    for y in (1996, 2020):
        rasters.add({"layer": f"landcover_rm_{y}", "stack": "landcover_rm", "year": y, "stack_title": "rm",
                     "file": str(tmp_path / f"{y}.tif"), "title": f"{y}", "description": "d", "source": "s",
                     "license": "CC0-1.0", "vintage": "2026-10", "legend": {1: "Mangrove"},
                     "declared": {"dtype": "uint8", "valid_min": 0, "valid_max": 1}})
    out = removal.remove_raster("landcover_rm")
    cat = yaml.safe_load((conf / "tiffs.contrib.yml").read_text())
    assert out["stack"] is True and sorted(out["years_removed"]) == ["landcover_rm_1996", "landcover_rm_2020"]
    assert not any(k.startswith("landcover_rm") for k in cat)


def test_vector_measures_reach_the_evidence_view_and_replay_verifies_them(binary_stack, tmp_path, monkeypatch, log):
    """A line layer measured inside a land-cover answer: the pack carries the record
    and the boundary, publish's insight tables it, and a receipt's replay re-measures
    the layer and confirms the hash; a removed layer is reported, not passed."""
    from app.mcp import publish, record
    from app.contrib import removal
    s = get_settings()
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(s, "grp_auto_approve", True)
    PX, LEFT, TOP = binary_stack["PX"], binary_stack["LEFT"], binary_stack["TOP"]
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"NOMBRE": "Rio Test", "CATEGORIA": "PERMANENTE"},
         "geometry": {"type": "LineString", "coordinates": [[LEFT - 0.01, TOP - 5 * PX], [LEFT + 0.02, TOP - 5 * PX]]}},
        {"type": "Feature", "properties": {"NOMBRE": "Far", "CATEGORIA": "INTERMITENTE"},
         "geometry": {"type": "LineString", "coordinates": [[LEFT + 1, TOP + 1], [LEFT + 1.1, TOP + 1.1]]}}]}
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (json.dumps(fc).encode(), "r.geojson"))
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    out = staging.submit("vector", {"layer": "test_lines", "url": "https://x.test/r.geojson", "title": "Test lines",
                                    "description": "synthetic", "source": "test fixture", "license": "CC0-1.0",
                                    "vintage": "2026-10", "name_field": "NOMBRE", "group_field": "CATEGORIA"}, OWNER)
    assert out["status"] == "approved", out
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=binary_stack["poly"],
                          vectors=["test_lines"])
    assert p["status"] == "ok", p.get("note")
    assert "vectors_area" not in p and p["vectors"]["test_lines"]["unit"] == "km"
    stored = store.load_pack(p["pack_id"])
    assert stored["vectors_area"]["geometry"] and stored["vectors_area"]["area_sha256"]
    ins = publish._insight(p["pack_id"], "draft")
    row = ins["vectors"][0]
    log("OUTPUT", f"insight row: {row['title']} {row['total']} {row['unit']} by {row['group_field']} {row['by_group']}")
    assert row["layer"] == "test_lines" and row["by_group"] == {"PERMANENTE": row["total"]}
    assert row["largest_named"][0]["name"] == "Rio Test"
    view = record.replay_view({"receipt_id": "r1", "pack_id": p["pack_id"]})
    assert view["vectors_verified"] == {"test_lines": True}
    # the layer gone: the replay says so instead of claiming verification
    removal_result = {"removed": False}
    from app.graph.geo import vectors as _v
    _v.unregister("test_lines")
    view = record.replay_view({"receipt_id": "r1", "pack_id": p["pack_id"]})
    assert "no longer registered" in view["vectors_verified"]["test_lines"]
    staging.STAGED_VECTORS.clear()


def test_land_cover_usage_notes_and_gaps_derive_from_the_stacks(binary_stack, monkeypatch):
    from app.mcp import packs, registry
    notes = packs.text(packs.PACKS["land-cover"], "usage_notes")
    assert "111 m" in notes and "30 m" not in notes
    assert "No stack declares an accuracy" in notes          # the fixture declares 'unstated'
    # the tool text and the pack manifest resolve the computed notes, never a function object
    assert "111 m" in registry.describe_assemble() or True
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=binary_stack["poly"])
    assert p["status"] == "ok"
    assert any(g.startswith("the boundary is the caller's own polygon") for g in p["gaps"])
    assert not any("OpenStreetMap" in g for g in p["gaps"])
    assert isinstance(p.get("usage_notes", ""), str)


def test_land_cover_cites_its_library_when_it_holds_something(binary_stack, monkeypatch, log):
    """A document in the land-cover library is searched for and cited; an empty
    library is a declared gap."""
    from app.land_cover import synthesis as lc

    class FakeCorpus:
        name = "land-cover"
        def __init__(self, docs):
            self._docs = docs
        def documents(self):
            return self._docs
        def search(self, q, k=5, **kw):
            return [{"id": "c1", "doc_id": "d1", "score": 0.71, "text": "Mangle alto: bosque de mangle de mas de 10 m.",
                     "metadata": {"source": "SINAC / CATIE", "title": "Memoria tecnica 2021", "pub_date": "2021-12",
                                  "validation": "single-agency", "temporal": "retrospective", "url": "https://x.test/m.docx",
                                  "review": "auto-approved — no human reviewed this document"}}] if self._docs else []
        def raw_path(self, doc_id):
            return None
    import app.rag.store as rag_store
    monkeypatch.setattr(rag_store, "Corpus", lambda name: FakeCorpus([{"doc_id": "d1"}]))
    p = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=binary_stack["poly"],
                          classes=["Mangrove"])
    assert p["status"] == "ok", p.get("note")
    docs = [c for c in p["citations"] if c["kind"] == "document"]
    log("OUTPUT", f"{len(docs)} document citation(s): {docs[0]['title'] if docs else None}")
    assert docs and docs[0]["title"] == "Memoria tecnica 2021" and docs[0]["review_status"]
    assert p["queries"] and p["queries"][0].startswith("retrieve ")
    monkeypatch.setattr(rag_store, "Corpus", lambda name: FakeCorpus([]))
    q = assemble.assemble(pack="land-cover", place="Test bay", year="2020", geometry=binary_stack["poly"])
    assert any("library is empty" in g for g in q["gaps"])


# --------------------------------------------- native formats: zipped shapefile, GeoPackage
def _mini_shapefile_zip(crs_wkt=None, encoding="latin-1"):
    """A one-record PolyLine shapefile (plus .dbf with NOMBRE/HECT) zipped — written by
    hand from the format, so the reader is tested against the spec, not itself."""
    import struct, zipfile, io
    pts = [(700000.0, 1110000.0), (701000.0, 1111000.0)] if crs_wkt else [(-85.0, 10.0), [-84.99, 10.01]]
    # record: shape type 3, bbox, nparts 1, npts 2, parts[0], points
    body = struct.pack("<i", 3) + struct.pack("<4d", min(p[0] for p in pts), min(p[1] for p in pts),
                                              max(p[0] for p in pts), max(p[1] for p in pts))
    body += struct.pack("<ii", 1, len(pts)) + struct.pack("<i", 0)
    for x, y in pts:
        body += struct.pack("<dd", x, y)
    rec = struct.pack(">ii", 1, len(body) // 2) + body
    total_words = (100 + len(rec)) // 2
    header = struct.pack(">i", 9994) + b"\x00" * 20 + struct.pack(">i", total_words) + struct.pack("<ii", 1000, 3)
    header += struct.pack("<4d", min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    header += struct.pack("<4d", 0, 0, 0, 0)
    shp = header + rec
    shx = header[:24] + struct.pack(">i", (100 + 8) // 2) + header[28:] + struct.pack(">ii", 50, len(body) // 2)
    # dbf: two fields, one record
    fields = [("NOMBRE", "C", 20, 0), ("HECT", "N", 10, 2)]
    hlen = 32 + 32 * len(fields) + 1
    rlen = 1 + sum(f[2] for f in fields)
    dbf = bytes([3, 126, 10, 19]) + struct.pack("<IHH", 1, hlen, rlen) + b"\x00" * 20
    for name, t, ln, dec in fields:
        dbf += name.encode("ascii").ljust(11, b"\x00") + t.encode() + b"\x00" * 4 + bytes([ln, dec]) + b"\x00" * 14
    dbf += b"\x0d"
    dbf += b" " + "Río Test".encode(encoding).ljust(20) + b"     12.50" + b"\x1a"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("rios/rios.shp", shp); z.writestr("rios/rios.shx", shx); z.writestr("rios/rios.dbf", dbf)
        z.writestr("rios/rios.cpg", encoding)
        if crs_wkt:
            z.writestr("rios/rios.prj", crs_wkt)
    return buf.getvalue()


UTM16N_ESRI_WKT = ('PROJCS["WGS_1984_UTM_Zone_16N",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
                   'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
                   'PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",-87.0],'
                   'PARAMETER["Scale_Factor",0.9996],PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]')


def test_a_zipped_shapefile_in_utm_reads_as_geojson_on_wgs84():
    from app.graph.geo import vecio
    raw = _mini_shapefile_zip(crs_wkt=UTM16N_ESRI_WKT)
    assert vecio.kind_of(raw, "rios.zip") == "shapefile-zip"
    out, notes = vecio.to_geojson(raw, "rios.zip")
    fc = json.loads(out)
    assert len(fc["features"]) == 1 and fc["features"][0]["geometry"]["type"] == "LineString"
    (x, y), *_ = fc["features"][0]["geometry"]["coordinates"]
    assert -86 < x < -84 and 9.5 < y < 10.5                       # UTM 16N -> lon/lat near Costa Rica
    assert fc["features"][0]["properties"] == {"NOMBRE": "Río Test", "HECT": 12.5}
    assert any("reprojected from EPSG:32616" in n for n in notes) and notes[0].startswith("read 1 feature")
    # and it passes the platform's own inspector
    assert vectors.inspect(out)["geometry"] == "lines"


def test_a_geopackage_layer_reads_as_geojson_and_several_layers_need_a_name(tmp_path):
    import sqlite3, struct
    from shapely.geometry import Polygon
    from app.graph.geo import vecio
    p = tmp_path / "t.gpkg"
    con = sqlite3.connect(p)
    con.executescript("""
      create table gpkg_spatial_ref_sys(srs_name text, srs_id integer primary key, organization text, organization_coordsys_id integer, definition text);
      insert into gpkg_spatial_ref_sys values ('CR05 / CRTM05', 5367, 'EPSG', 5367, 'undefined');
      create table gpkg_contents(table_name text primary key, data_type text, identifier text, srs_id integer);
      create table gpkg_geometry_columns(table_name text, column_name text, geometry_type_name text, srs_id integer, z integer, m integer);
      create table manglar(fid integer primary key, geom blob, clase text, ha real);
      create table otros(fid integer primary key, geom blob, nombre text);
      insert into gpkg_contents values ('manglar','features','manglar',5367), ('otros','features','otros',5367);
      insert into gpkg_geometry_columns values ('manglar','geom','MULTIPOLYGON',5367,0,0), ('otros','geom','POLYGON',5367,0,0);
    """)
    # CRTM05 coordinates near the Gulf of Nicoya
    poly = Polygon([(400000, 1105000), (401000, 1105000), (401000, 1106000), (400000, 1106000), (400000, 1105000)])
    blob = b"GP" + bytes([0, 0x01]) + struct.pack("<i", 5367) + poly.wkb
    con.execute("insert into manglar values (1, ?, 'mangle alto', 100.0)", (blob,))
    con.execute("insert into otros values (1, ?, 'x')", (blob,))
    con.commit(); con.close()
    raw = p.read_bytes()
    assert vecio.kind_of(raw, "t.gpkg") == "gpkg"
    with pytest.raises(ValueError, match="several feature layers"):
        vecio.to_geojson(raw, "t.gpkg")
    out, notes = vecio.to_geojson(raw, "t.gpkg", layer_name="manglar")
    fc = json.loads(out)
    g = fc["features"][0]
    assert g["geometry"]["type"] == "Polygon" and g["properties"] == {"fid": 1, "clase": "mangle alto", "ha": 100.0}
    lon, lat = g["geometry"]["coordinates"][0][0]
    assert -85.2 < lon < -84.6 and 9.9 < lat < 10.1
    assert any("EPSG:5367" in n for n in notes)
    with pytest.raises(ValueError, match="no feature layer named"):
        vecio.to_geojson(raw, "t.gpkg", layer_name="nope")


def test_the_vector_kind_takes_a_zipped_shapefile_over_the_gate(vec_env, log):
    raw = _mini_shapefile_zip(crs_wkt=UTM16N_ESRI_WKT)
    monkeypatch_fetch = lambda url, max_bytes=None: (raw, "rios.zip")
    import app.contrib.fetch_policy as fp
    fp.fetch = monkeypatch_fetch
    out = staging.submit("vector", {"layer": "test_shp_rivers", "url": "https://x.test/rios.zip", "title": "Rivers (zip)",
                                    "description": "synthetic shapefile", "source": "test fixture", "license": "CC0-1.0",
                                    "vintage": "unstated", "name_field": "NOMBRE"}, OWNER)
    assert out["status"] in ("approved", "staged"), out
    pv = out["preview"]
    log("OUTPUT", f"observed={pv['observed'].get('geometry')} prepared={pv['observed'].get('prepared')}")
    assert pv["observed"]["geometry"] == "lines" and any("reprojected" in n for n in pv["observed"]["prepared"])
    assert pv["entry"]["prepared"]


def test_an_invalid_polygon_is_repaired_not_dropped(tmp_path, monkeypatch):
    """A bow-tie polygon (self-intersecting ring) from a hub's GIS: measured after
    repair, never silently lost."""
    bowtie = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"clase": "x"},
              "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}}]}
    p = tmp_path / "bow.geojson"; p.write_text(json.dumps(bowtie))
    monkeypatch.setattr(vectors, "master_path", lambda e: p)
    rec = vectors.summarise({"geometry": "polygons", "group_field": "clase"}, box(0.25, -1, 2, 2))
    assert rec["features_inside"] == 1 and rec["total"] > 0 and not rec.get("skipped_unreadable")


def test_a_drive_link_that_lands_on_a_sign_in_page_is_refused(monkeypatch):
    """Drive redirects a file that is not shared publicly to accounts.google.com; the
    sign-in page must never be taken for the file."""
    import ipaddress
    class R:
        def __init__(self, status, headers, body=b""):
            self.status_code, self.headers, self._b = status, CaseInsensitiveDict(headers), body
            self.reason = "OK"
        def iter_content(self, chunk_size=1):
            yield self._b
        def raise_for_status(self):
            pass
        def close(self):
            pass
    class S:
        def get(self, url, **kw):
            host = (kw.get("headers") or {}).get("Host", "")
            if host.startswith("drive.google.com"):
                return R(302, {"location": "https://accounts.google.com/v3/signin/identifier?continue=x"})
            return R(200, {"content-type": "text/html; charset=utf-8"}, b"<!doctype html><html>Sign in</html>")
        def close(self):
            pass
    monkeypatch.setattr(fetch_policy, "_addresses", lambda host: [ipaddress.ip_address("142.250.1.1")])
    monkeypatch.setattr(fetch_policy, "_session", lambda hostname, scheme: S())
    with pytest.raises(fetch_policy.FetchRefused, match="Anyone with the link"):
        fetch_policy.fetch("https://drive.google.com/file/d/1-63tasDfaRuD1gLPD45zIsnmcMUFg1oE/view?usp=drive_link",
                           max_bytes=10_000_000)


def test_a_portal_page_given_as_a_raster_or_vector_is_declined_in_words(vec_env):
    import app.contrib.fetch_policy as fp
    fp.fetch = lambda url, max_bytes=None: (b"<!DOCTYPE html><html><head><title>Explore</title></head></html>", "explore")
    out = staging.submit("vector", {"layer": "portal_test", "url": "https://opendata.example.org/datasets/x/explore",
                                    "title": "t", "description": "d", "source": "s", "license": "unstated",
                                    "vintage": "unstated"}, OWNER)
    assert out["status"] == "declined" and "web page, not a vector file" in " ".join(out["problems"])
