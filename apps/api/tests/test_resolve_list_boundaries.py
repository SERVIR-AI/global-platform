"""resolve_place_time(list=...) lists every contributed boundary layer with its names
and sizes — the menu a question picks an area from, derived from the live registry."""
import json

import pytest

from app.graph.geo import ingest, vectors
from app.mcp import resolve


def _poly(name, x0, y0, w, h, **props):
    return {"type": "Feature", "properties": {"name": name, **props},
            "geometry": {"type": "Polygon", "coordinates": [[[x0, y0], [x0 + w, y0], [x0 + w, y0 + h],
                                                              [x0, y0 + h], [x0, y0]]]}}


@pytest.fixture
def sites(monkeypatch, tmp_path):
    p = tmp_path / "sites.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": [
        _poly("Estero", -84.9, 10.0, 0.05, 0.05, country="Costa Rica"),      # ~30 km²
        _poly("Gulf box", -85.3, 9.9, 0.5, 0.4, country="Costa Rica"),        # ~2,400 km²
        _poly("Gulf box", -80.3, -3.0, 0.1, 0.1, country="Ecuador"),          # same name, other country
    ]}))
    entry = {"title": "Sprint sites (test)", "source": "sprint", "vintage": "2026-10",
             "name_field": "name", "group_field": "country", "local_path": str(p),
             "geometry": "polygons", "features": 3}
    monkeypatch.setattr(vectors, "visible", lambda: {"sprint_sites": entry})
    monkeypatch.setattr(vectors, "master_path", lambda e: e["local_path"])
    ingest._GAZETTEER.clear()
    return entry


def test_list_names_every_area_with_its_size_and_class(sites):
    out = resolve.place_time(list="all")
    assert out["status"] == "ok"
    (layer,) = out["layers"]
    assert layer["layer"] == "sprint_sites" and layer["areas"] == 3
    by = {(r["name"], r.get("qualifier")): r for r in layer["names"]}
    estero = by[("Estero", "Costa Rica")]
    assert 25 < estero["km2"] < 35 and estero["size"] == "small"
    gulf = by[("Gulf box", "Costa Rica")]
    assert 2300 < gulf["km2"] < 2500 and gulf["size"] == "large"
    assert [r["km2"] for r in layer["names"]] == sorted(r["km2"] for r in layer["names"])
    assert "small" in out["sizes"] and "place" in out["how_to_use"]


def test_list_one_layer_by_id_and_decline_an_unknown_one(sites):
    assert resolve.place_time(list="sprint_sites")["layers"][0]["areas"] == 3
    out = resolve.place_time(list="no_such_layer")
    assert out["status"] == "declined" and "sprint_sites" in out["note"]


def test_a_long_layer_is_abridged_unless_named(sites, monkeypatch):
    out = ingest.list_boundaries(names_cap=2)
    layer = out["layers"][0]
    assert len(layer["names"]) == 2 and "pass layer='sprint_sites'" in layer["note"]
    assert len(ingest.list_boundaries(layer="sprint_sites", names_cap=2)["layers"][0]["names"]) == 3


def test_list_does_not_disturb_the_place_half(sites):
    out = resolve.place_time(place="Estero")
    assert out["status"] == "ok" and out["place"]["is_contributed_boundary"]
