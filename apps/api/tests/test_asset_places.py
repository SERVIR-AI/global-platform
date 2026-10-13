"""A count is traceable to the places behind it, and a contributed point layer can
reach the map. Before this, "8 of 8 centres in the flood zone" came with no name,
no coordinate and a picture of two hospitals."""
import json

import pytest

from app.graph.geo import operations, vectors
from app.mcp import assemble, publish, store
from app.risk import synthesis as risk


def _write(tmp_path, name, feats):
    p = tmp_path / f"{name}.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    return str(p)


def _pt(lon, lat, **props):
    return {"type": "Feature", "properties": props,
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


@pytest.fixture
def hub_aoi(monkeypatch, aoi, tmp_path):
    """Testville plus a contributed layer `evac_centres` (name_field=centre_name):
    three centres in the flooded half, one dry, one outside the boundary."""
    aoi = dict(aoi)
    aoi["evac_centres"] = _write(tmp_path, "evac_centres", [
        _pt(0.001, 0.001, centre_name="Wat Nuea hall", capacity=550),
        _pt(0.002, 0.009, centre_name="Ban Klang school", capacity=200),
        _pt(0.004, 0.004, centre_name="Municipal gym", capacity=900),
        _pt(0.009, 0.009, centre_name="Dry-side temple", capacity=120),
        _pt(0.02, 0.02, centre_name="Outside the district", capacity=1),
    ])
    entry = {"title": "Evacuation centres (test)", "source": "hub list", "vintage": "2026-09",
             "name_field": "centre_name", "local_path": aoi["evac_centres"]}
    monkeypatch.setattr(vectors, "visible", lambda: {"evac_centres": entry})
    calls = []
    monkeypatch.setattr(risk.ingest, "ensure_aoi",
                        lambda place=None, geometry=None, layers=None, name=None: (calls.append(place), dict(aoi))[1])
    monkeypatch.setattr(risk.ingest, "hazard_clip", lambda a, layer: a["hazard_flood"])
    aoi["_ensure_calls"] = calls
    return aoi


def test_every_counted_point_layer_lists_its_places(hub_aoi, log):
    p = assemble.assemble(place="Testville", hazard="flood")
    assert p["status"] == "ok", p.get("note")
    a = p["stats"]["assets"]
    log("OUTPUT", f"assets layers: {sorted(a)}")
    # the contributed layer: names from ITS name_field, the set count_in_hazard counted
    ev = a["evac_centres"]
    assert ev["total"] == 4 and ev["exposed"] == 3          # the outside point is nobody's
    assert ev["name_field"] == "centre_name"
    assert [it["name"] for it in ev["items"]] == ["Ban Klang school", "Municipal gym", "Wat Nuea hall"]
    assert all(it["hazard_class"] == 3 for it in ev["items"])
    assert ev["items"][0]["lon"] == 0.002 and ev["items"][0]["lat"] == 0.009
    assert ev["truncated"] is False
    assert len(ev["items"]) == p["stats"]["counts"]["evac_centres"]["exposed"]
    # built-ins list theirs too; unnamed OSM points still carry coordinates and class
    assert len(a["hospitals"]["items"]) == 1 and a["hospitals"]["items"][0]["hazard_class"] == 3
    assert a["hospitals"]["items"][0]["name"] is None
    assert len(a["schools"]["items"]) == 1
    # building centroids: counts only, and the pack says so
    assert a["buildings"]["items"] == [] and "centroids" in a["buildings"]["items_omitted"]
    # the citation names the places and points at the full list
    cit = next(c for c in p["citations"] if c["title"].startswith("Evacuation centres (test) vs"))
    log("OUTPUT", cit["text"][-160:])
    assert "Named: Ban Klang school; Municipal gym; Wat Nuea hall" in cit["text"]
    assert cit["places_in"] == "stats.assets.evac_centres"


def test_a_contributed_layer_leads_the_map_when_nothing_built_in_is_exposed(hub_aoi, tmp_path, log):
    # move every hospital and school to the dry half
    hub_aoi["hospitals"] = _write(tmp_path, "h2", [_pt(0.008, 0.005), _pt(0.009, 0.005)])
    hub_aoi["schools"] = _write(tmp_path, "s2", [_pt(0.007, 0.007)])
    p = assemble.assemble(place="Testville", hazard="flood")
    viz = store.load_pack(p["pack_id"])["viz"]
    log("OUTPUT", f"drawn={viz['layer']} secondary={sorted(viz['point_layers'])}")
    assert viz["layer"] == "evac_centres"                   # not two grey hospitals
    assert viz["metric"]["value"] == 3 and viz["metric"]["total"] == 4   # the outside point is not drawn either
    names = sorted(f["properties"]["centre_name"] for f in viz["features"]["features"])
    assert names == ["Ban Klang school", "Dry-side temple", "Municipal gym", "Wat Nuea hall"]
    # every feature says which class `severity` is
    assert all("hazard_class" in f["properties"] for f in viz["features"]["features"])
    m = publish._panel_map(store.load_pack(p["pack_id"]))
    assert m["assets_layer"] == "evac_centres"
    assert m["point_layers"] is None or "evac_centres" not in m["point_layers"]   # drawn once, not twice


def test_map_layer_chooses_the_drawn_layer_and_the_others_ride_along(hub_aoi, log):
    p = assemble.assemble(place="Testville", hazard="flood", map_layer="evac_centres")
    assert p["status"] == "ok", p.get("note")
    pack = store.load_pack(p["pack_id"])
    viz = pack["viz"]
    assert viz["layer"] == "evac_centres"
    assert set(viz["point_layers"]) == {"hospitals", "schools"}   # every counted point layer, buildings aside
    assert viz["point_layers"]["hospitals"]["features"][0]["properties"]["hazard_class"] == 3
    m = publish._panel_map(pack)
    log("OUTPUT", f"panel: drawn={m['assets_layer']} layers={sorted(m['point_layers'])}")
    assert sorted(m["point_layers"]) == ["hospitals", "schools"]
    # and the published insight carries the places, bounded and labelled
    places = publish._insight_places(pack)
    assert places["evac_centres"]["items"][0]["name"] == "Ban Klang school"
    assert places["evac_centres"]["truncated"] is False
    assert "buildings" not in places                         # nothing to list


def test_an_unknown_map_layer_declines_by_name_before_any_fetch(hub_aoi, log):
    p = assemble.assemble(place="Testville", hazard="flood", map_layer="shelters")
    log("OUTPUT", p.get("note"))
    assert p["status"] == "declined"
    assert "shelters" in p["note"] and "evac_centres" in p["note"] and "hospitals" in p["note"]
    assert hub_aoi["_ensure_calls"] == []                    # declined before geocoding


def test_a_declined_map_layer_falls_back_and_says_so(hub_aoi, log):
    hub_aoi["assets_declined"] = {"layers": ["evac_centres"]}
    p = assemble.assemble(place="Testville", hazard="flood", map_layer="evac_centres")
    assert p["status"] == "ok"
    viz = store.load_pack(p["pack_id"])["viz"]
    assert viz["layer"] == "hospitals"
    assert any("map_layer 'evac_centres' was not counted" in g for g in p["gaps"])
    assert "evac_centres" not in p["stats"]["assets"]


def test_the_panel_map_sheds_building_centroids_before_dropping_the_map():
    """Bang Khu Rat: no hospital or school exposed, 468 building centroids drawn,
    the whole map dropped at the size cap — with a named civil-defence centre in
    the counts. Buildings go first; the named layer stays."""
    many = {"type": "FeatureCollection", "features": [
        _pt(100.35 + i * 1e-5, 13.91, severity=5) for i in range(3000)]}
    named = {"type": "FeatureCollection", "features": [
        _pt(100.35, 13.91, name="Civil defence post", severity=5)]}
    pack = {"viz": {"place": "Bang Khu Rat", "hazard": "hazard_flood", "layer": "buildings",
                    "features": many, "point_layers": {"civil_defence": named},
                    "hazard_grid": {"w": 56, "h": 56, "cells": "3" * 3136},
                    "aoi": {"type": "Feature", "geometry": {"type": "Point", "coordinates": [100.35, 13.91]}},
                    "bounds": [100.3, 13.9, 100.4, 13.95], "legend": {}}}
    m = publish._panel_map(pack)
    assert m is not None
    assert m["assets"] is None and "centroids" in m["assets_note"]
    assert m["point_layers"]["civil_defence"]["features"][0]["properties"]["name"] == "Civil defence post"


def test_the_web_chat_can_be_asked_about_a_contributed_layer(hub_aoi):
    tools = operations.schema(["hazard_flood"])
    enum = tools[1]["function"]["parameters"]["properties"]["layer"]["enum"]
    assert "evac_centres" in enum and "hospitals" in enum
    assert "contributed" in tools[1]["function"]["description"]
