"""Raising the land-cover cap must not cost every place its cached area (22 Oct: each
first question rebuilt boundary and clips, 35-60 s), and two questions on a fresh worker
must not build rival gazetteers (one then failed looking up an area by its id)."""
import json
import os
import threading

import pytest
from shapely.geometry import box

from app.config import get_settings
from app.graph.geo import ingest, vectors


@pytest.fixture
def cache(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(ingest, "_contributed",
                        lambda place: {"area": None, "aside": [], "similar": [], "similar_total": 0})
    monkeypatch.setattr(ingest, "_reverse_country", lambda x, y: "Costa Rica")
    monkeypatch.setattr(vectors, "visible", lambda: {})
    return tmp_path / "cache"


def _bundle(root, slug, **meta):
    adir = root / slug
    adir.mkdir()
    g = box(-85.0, 10.0, -84.9, 10.1)
    ingest._write(str(adir), "admin", [ingest._feature(g, {"name": meta.get("name", "Tiny Place")})])
    info = {"name": "Tiny Place", "area_km2": 120, "how": "admin boundary ~120 km²", "counts": {},
            "resolver": ingest.RESOLVER_VERSION, "measure": vectors.MEASURE, "country": "Costa Rica"}
    info.update(meta)
    ingest.write_json(str(adir / "meta.json"), info, indent=2)
    return adir


def test_a_bundle_cached_under_another_cap_is_reused(cache, monkeypatch):
    old = _bundle(cache, "tiny-place-cap250000")
    (old / "landcover_x_2020.tif").write_bytes(b"clip")          # a clip rides along
    monkeypatch.setattr(ingest, "_resolve", lambda *a, **k: pytest.fail("re-resolved a reusable bundle"))
    ingest.ensure_aoi(place="Tiny Place", layers=(), area_cap_km2=2_000_000)
    new = cache / "tiny-place-cap2000000"
    assert (new / "meta.json").exists() and (new / "landcover_x_2020.tif").exists()
    assert not old.exists()


def test_a_box_the_old_cap_forced_is_resolved_again(cache, monkeypatch):
    old = _bundle(cache, "big-place-cap250000", name="Big Place", area_km2=576, approximate=True,
                  how="12 km radius box at the centre of Big Place (~600000 km², over the 250000 km² cap)")
    called = []

    def resolve(place, cap, contributed):
        called.append(cap)
        return {"km2": 600000.0, "name": "Big Place", "geometry": box(-80, -5, -70, 5),
                "how": "admin boundary ~600000 km²", "approximate": False, "nearby": []}
    monkeypatch.setattr(ingest, "_resolve", resolve)
    ingest.ensure_aoi(place="Big Place", layers=(), area_cap_km2=2_000_000)
    assert called == [2_000_000.0]
    assert old.exists()                                     # left alone, not adopted
    assert json.loads((cache / "big-place-cap2000000" / "meta.json").read_text())["area_km2"] == 600000


def test_another_place_sharing_the_slug_start_is_not_adopted(cache, monkeypatch):
    _bundle(cache, "bangkok-capital-cap250000", name="Bangkok Capital")
    monkeypatch.setattr(ingest, "_resolve", lambda place, cap, c: {
        "km2": 1500.0, "name": "Bangkok", "geometry": box(100.3, 13.5, 100.9, 14.0),
        "how": "admin boundary ~1500 km²", "approximate": False, "nearby": []})
    ingest.ensure_aoi(place="Bangkok", layers=(), area_cap_km2=2_000_000)
    assert (cache / "bangkok-capital-cap250000").exists()
    assert json.loads((cache / "bangkok-cap2000000" / "meta.json").read_text())["name"] == "Bangkok"


def test_forms_never_misses_an_area_from_an_older_build(monkeypatch):
    def area():
        return {"layer": "lyr", "key": "chomes", "label": "Chomes", "attrs": {"canton": "Puntarenas"},
                "geoms": [], "bbox": (0, 0, 1, 1)}
    old = area()
    monkeypatch.setattr(ingest, "_gazetteer", lambda layer, entry: {"chomes": [area()]})   # a rebuilt copy
    forms = ingest._forms([old], {"lyr": {"name_field": "name"}})
    assert id(old) in forms and forms[id(old)][0].startswith("Chomes (lyr")


def test_the_gazetteer_is_built_once_under_concurrency(tmp_path, monkeypatch):
    path = tmp_path / "areas.geojson"
    feats = [{"type": "Feature", "properties": {"name": f"Area {i}"},
              "geometry": {"type": "Polygon", "coordinates": [[[i, 0], [i + 1, 0], [i + 1, 1], [i, 1], [i, 0]]]}}
             for i in range(3000)]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    monkeypatch.setattr(vectors, "master_path", lambda entry: str(path))
    monkeypatch.setattr(ingest, "_GAZETTEER", {})
    got, barrier = [], threading.Barrier(8)

    def ask():
        barrier.wait()
        got.append(ingest._gazetteer("lyr", {"name_field": "name"}))
    ts = [threading.Thread(target=ask) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(got) == 8 and all(g is got[0] for g in got)
