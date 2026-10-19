"""Area cache safety under concurrency, and stated areas on the ellipsoid (mangrove
sprint re-test, 2026-10-19).

Two callers relabelling one drawn polygon wrote its meta.json at once; the file held
both objects and every later question about that polygon failed on "Extra data: line
21 column 2 (char 637)", for good. Cache files are now written whole (temp file,
fsync, rename), a polygon's label is the caller's and never written back, a garbled
meta.json rebuilds its area, and a clip cut short is clipped again. And the area a
boundary is stated at is the ellipsoidal one its measures add up to: the Gulf of
Nicoya box said ~2,399 km² beside measures over 2,384.0. Synthetic data throughout."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box, mapping, shape

from app.config import get_settings
from app.contrib import fetch_policy, identity, staging
from app.graph.geo import ingest, vectors
from app.mcp import packs, store

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
# The re-test's plain Gulf of Nicoya box, as an anticlockwise ring.
GULF = {"type": "Polygon", "coordinates": [[[-85.2977, 9.9337], [-84.7561, 9.9337], [-84.7561, 10.2968],
                                            [-85.2977, 10.2968], [-85.2977, 9.9337]]]}
CAP = 250_000            # the land-cover pack's cap: its own cache identity, as in the re-test
# An irregular basin, contributed through the gate as a named polygon layer.
BASIN = Polygon([(-83.75, 8.70), (-83.05, 8.78), (-82.98, 9.25), (-83.30, 9.42), (-83.80, 9.30)])


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tmp cache and an empty raster catalog (no stack covers anything), contributions
    auto-approved, the geocoder and Overpass stubbed. Nothing on the network."""
    s = get_settings()
    conf = tmp_path / "conf"; conf.mkdir()
    (conf / "tiffs.yml").write_text("{}")
    (conf / "raster_schema.yml").write_text(json.dumps({"defaults": {"crs": "EPSG:4326"},
                                                        "layers": {}, "stacks": {}}))
    feeds = tmp_path / "feeds"; feeds.mkdir()
    cache = tmp_path / "cache"
    for k, v in (("cache_dir", cache), ("tiffs_dir", cache / "tiffs"),
                 ("tiffs_config_path", conf / "tiffs.yml"), ("tiffs_contrib_path", conf / "tiffs.contrib.yml"),
                 ("raster_schema_path", conf / "raster_schema.yml"),
                 ("raster_schema_contrib_path", conf / "raster_schema.contrib.yml"),
                 ("feeds_conf_dir", feeds), ("grp_mattermost_webhook", ""), ("grp_auto_approve", True)):
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(ingest, "_search", lambda q: [])
    monkeypatch.setattr(ingest, "_GAZETTEER", {})
    fetched = []

    def fetch_layer(layer, bbox, boundary):
        fetched.append(layer)
        time.sleep(0.05)                   # long enough for every other caller to arrive
        c = boundary.centroid
        return [ingest._feature(c, {"name": "a road"})]
    monkeypatch.setattr(ingest, "_fetch_layer", fetch_layer)
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    yield {"cache": cache, "tmp": tmp_path, "fetched": fetched}
    staging.STAGED_VECTORS.clear()


def _together(n, fn):
    """Run fn(i) for i < n on n threads released at the same instant."""
    gate = threading.Barrier(n)

    def run(i):
        gate.wait()
        return fn(i)
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(run, range(n)))


def _adir(aoi):
    from pathlib import Path
    return Path(aoi["admin"]).parent


# ------------------------------------------------------- 1. concurrency
def test_ten_callers_naming_one_polygon_at_once_leave_one_valid_bundle(env, log):
    """The re-test's failure: callers asking about one polygon under different labels at
    once. Each gets its own label and the same bundle; meta.json stays one object, and a
    label is never written back, so a second round of new labels does not touch it."""
    results = _together(10, lambda i: ingest.ensure_aoi(geometry=GULF, name=f"Golfo label {i}",
                                                        layers=("schools",), area_cap_km2=CAP))
    adir = _adir(results[0])
    meta = adir / "meta.json"
    info = json.loads(meta.read_text())                     # one object, not two
    log("OUTPUT", f"{sorted(info)} fetched={env['fetched']}")
    assert [r["name"] for r in results] == [f"Golfo label {i}" for i in range(10)]
    assert {r["how"] for r in results} == {"supplied polygon"}
    assert {(r["area_km2"], r["admin"], json.dumps(r["counts"], sort_keys=True)) for r in results} \
        == {(info["area_km2"], str(adir / "admin.geojson"), '{"schools": 1}')}
    assert env["fetched"] == ["schools"]                     # one Overpass pull, not ten
    before = meta.read_bytes()
    again = _together(10, lambda i: ingest.ensure_aoi(geometry=GULF, name=f"Otra etiqueta {i}",
                                                      layers=("schools",), area_cap_km2=CAP))
    assert [r["name"] for r in again] == [f"Otra etiqueta {i}" for i in range(10)]
    assert meta.read_bytes() == before                       # labels are per call, never written back
    assert not list(adir.glob("*.tmp"))
    # unnamed, the same polygon is the caller's drawn area, whoever named it before
    drawn = ingest.ensure_aoi(geometry=GULF, layers=(), area_cap_km2=CAP)
    assert (drawn["name"], drawn["how"]) == ("drawn area", "drawn")


def test_callers_fetching_different_layers_of_one_area_keep_each_others_counts(env):
    """Each caller used to write back the copy of meta.json it read first, dropping the
    count another had just added. Under the asset budget, so every layer is fetched."""
    small = mapping(box(-85.0, 10.0, -84.8, 10.2))
    layers = ("roads", "hospitals", "schools", "buildings")
    _together(4, lambda i: ingest.ensure_aoi(geometry=small, name="Bahía", layers=(layers[i],)))
    aoi = ingest.ensure_aoi(geometry=small, name="Bahía", layers=())
    assert aoi["counts"] == {ly: 1 for ly in layers}
    assert sorted(env["fetched"]) == sorted(layers)


@pytest.mark.parametrize("garble", ["two writes interleaved", "cut short", "not an object"])
def test_a_garbled_meta_json_rebuilds_its_area_instead_of_failing_every_call(env, garble, capsys, log):
    aoi = ingest.ensure_aoi(geometry=GULF, name="Golfo", layers=("schools",), area_cap_km2=CAP)
    meta = _adir(aoi) / "meta.json"
    good = meta.read_text()
    meta.write_text({"two writes interleaved": good + good[-40:],      # "Extra data", as in the re-test
                     "cut short": good[: len(good) // 2],
                     "not an object": "[]"}[garble])
    again = ingest.ensure_aoi(geometry=GULF, name="Golfo", layers=("schools",), area_cap_km2=CAP)
    out = capsys.readouterr().out
    log("OUTPUT", out.strip().splitlines()[-2] if out.strip() else "")
    assert "meta.json is unreadable" in out and "rebuilding" in out
    assert json.loads(meta.read_text())["counts"] == {"schools": 1}
    assert again["area_km2"] == aoi["area_km2"] and again["counts"] == {"schools": 1}
    assert env["fetched"] == ["schools", "schools"]              # the whole area was rebuilt


def test_an_unreadable_boundary_file_rebuilds_its_area(env, capsys):
    aoi = ingest.ensure_aoi(geometry=GULF, name="Golfo", layers=(), area_cap_km2=CAP)
    admin = _adir(aoi) / "admin.geojson"
    admin.write_text(admin.read_text()[:30])
    again = ingest.ensure_aoi(geometry=GULF, name="Golfo", layers=(), area_cap_km2=CAP)
    assert "boundary file is unreadable" in capsys.readouterr().out
    assert shape(json.loads(admin.read_text())["features"][0]["geometry"]).equals(shape(GULF))
    assert again["area_km2"] == aoi["area_km2"]


def test_write_json_leaves_the_old_file_whole_when_a_write_fails(tmp_path):
    p = tmp_path / "meta.json"
    ingest.write_json(p, {"name": "kept"})
    with pytest.raises(TypeError):
        ingest.write_json(p, {"name": "lost", "bad": object()})   # fails part-way through
    assert json.loads(p.read_text()) == {"name": "kept"}
    assert [f.name for f in tmp_path.iterdir()] == ["meta.json"]


def _source_tif(tmp_path):
    arr = np.zeros((60, 80), dtype="uint8")
    arr[:, :40] = 3
    path = tmp_path / "hazard_toy.tif"
    with rasterio.open(path, "w", driver="GTiff", height=60, width=80, count=1, dtype="uint8",
                       crs="EPSG:4326", transform=from_origin(-85.4, 10.4, 0.01, 0.01)) as dst:
        dst.write(arr, 1)
    return str(path)


def test_callers_clipping_one_layer_at_once_get_one_whole_clip(env, monkeypatch):
    src = _source_tif(env["tmp"])
    opened = []
    monkeypatch.setattr(ingest, "source_raster", lambda layer: (opened.append(layer), src)[1])
    aoi = ingest.ensure_aoi(geometry=GULF, name="Golfo", layers=(), area_cap_km2=CAP)
    paths = _together(8, lambda i: ingest.hazard_clip(aoi, "hazard_toy"))
    assert len(set(paths)) == 1 and opened == ["hazard_toy"]   # clipped once; the rest read it
    with rasterio.open(paths[0]) as clip:
        assert clip.read(1).max() == 3
    assert not list(_adir(aoi).glob("*.tmp"))


def test_a_clip_cut_short_is_clipped_again_not_served(env, monkeypatch, capsys):
    src = _source_tif(env["tmp"])
    monkeypatch.setattr(ingest, "source_raster", lambda layer: src)
    aoi = ingest.ensure_aoi(geometry=GULF, name="Golfo", layers=(), area_cap_km2=CAP)
    clip = _adir(aoi) / "hazard_toy.tif"
    clip.write_bytes(b"II*\x00" + b"\x00" * 20)              # a TIFF header and nothing after it
    path = ingest.hazard_clip(aoi, "hazard_toy")
    assert "is unreadable" in capsys.readouterr().out
    with rasterio.open(path) as c:
        assert c.read(1).max() == 3


# ------------------------------------------------------- 2. stated areas on the ellipsoid
def test_a_drawn_polygon_is_stated_at_the_area_its_measures_add_up_to(env):
    aoi = ingest.ensure_aoi(geometry=GULF, name="Golfo de Nicoya (box)", layers=(), area_cap_km2=CAP)
    exact = vectors.area_km2(shape(GULF))
    assert round(exact, 1) == 2384.0
    assert aoi["area_km2"] == round(exact) == 2384               # was ~2,399 on the plane
    assert ingest.area_of(aoi)["area_km2"] == 2384
    assert json.loads((_adir(aoi) / "meta.json").read_text())["measure"] == vectors.MEASURE


def test_a_geocoded_boundary_is_stated_on_the_ellipsoid_in_its_area_and_its_how(env, monkeypatch):
    ring = [list(p) for p in BASIN.exterior.coords]
    monkeypatch.setattr(ingest, "_search", lambda q: [{
        "class": "boundary", "type": "administrative", "place_rank": 12, "display_name": "Cuenca Uno, Testland",
        "lon": "-83.4", "lat": "9.0", "geojson": {"type": "Polygon", "coordinates": [ring]}}])
    n = round(vectors.area_km2(BASIN))
    r = ingest.resolve_place("Cuenca Uno Province")
    assert r["status"] == "ok" and r["area_km2"] == n and f"~{n} km²" in r["how"]
    aoi = ingest.ensure_aoi(place="Cuenca Uno Province", layers=())
    assert aoi["area_km2"] == n and aoi["how"] == r["how"]


def test_an_area_cached_on_the_plane_is_restated_on_the_ellipsoid_and_keeps_its_layers(env):
    """A bundle cached before this fix carries the planar figure in area_km2 and in how.
    It is restated from its own boundary, without re-resolving or re-fetching."""
    d = env["cache"] / "golfo-cached"
    d.mkdir(parents=True)
    (d / "admin.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "Golfo"}, "geometry": GULF}]}))
    (d / "schools.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    (d / "meta.json").write_text(json.dumps({
        "name": "Golfo", "area_km2": 2399, "counts": {"schools": 0}, "resolver": ingest.RESOLVER_VERSION,
        "how": "admin boundary ~2399 km² — this is Golfo itself, not the wider area (~9000 km²)"}))
    aoi = ingest.ensure_aoi(place="Golfo cached", layers=("schools",))
    assert aoi["area_km2"] == 2384
    assert aoi["how"] == "admin boundary ~2384 km² — this is Golfo itself, not the wider area (~9000 km²)"
    assert env["fetched"] == [] and aoi["counts"] == {"schools": 0}
    assert json.loads((d / "meta.json").read_text())["measure"] == vectors.MEASURE


def test_a_freshly_contributed_basin_is_stated_at_its_measured_area_by_every_seam(env, monkeypatch, log):
    """A polygon layer contributed through the gate names a basin. The resolve bone, the
    risk pack's area (default cap) and the land-cover pack's area-only citation all state
    the area its own measure gives, not a planar one ~0.7% higher."""
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"NOMBRE": "Cuenca Prueba"}, "geometry": mapping(BASIN)}]}
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (json.dumps(fc).encode(), "c.geojson"))
    tok = identity.bind(OWNER)
    try:
        out = staging.submit("vector", {"layer": "t_basins", "url": "https://x.test/c.geojson",
                                        "title": "Test basins (synthetic)", "description": "synthetic",
                                        "source": "test fixture", "license": "CC0-1.0", "vintage": "2026",
                                        "name_field": "NOMBRE"}, OWNER)
        assert out["status"] in ("approved", "landed"), out
        entry = vectors.landed()["t_basins"]
        measured = vectors.summarise(entry, BASIN)["total"]
        n = round(measured)
        planar = BASIN.area * 111.32 ** 2 * np.cos(np.radians(BASIN.centroid.y))
        log("OUTPUT", f"measured {measured} km², planar {planar:.1f} km²")
        assert round(planar) != n

        r = ingest.resolve_place("Cuenca Prueba")
        assert r["status"] == "ok" and r["is_contributed_boundary"] and r["area_km2"] == n
        risk_aoi = ingest.ensure_aoi(place="Cuenca Prueba", layers=())          # the risk pack's cap
        assert risk_aoi["area_km2"] == n
        spec = packs.PACKS["land-cover"]
        extras = {"vectors": ["t_basins"]}
        target, why = packs.build_target("land-cover", {"place": "Cuenca Prueba", "year": "", **extras})
        assert why is None
        citations, _gaps, stats = spec["gather"](target, spec["default_focus"](target), [], extras)
        log("OUTPUT", citations[0]["text"][:160])
        assert citations[0]["kind"] == "area" and f"~{n:,} km²" in citations[0]["text"]
        assert stats["aoi"]["area_km2"] == n and stats["_area"]["area_km2"] == n
    finally:
        identity.unbind(tok)
