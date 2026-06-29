"""The drawn-AOI latency fix: fetch only the OSM asset layer a question needs, and
cache per-layer so a later question for another layer only fetches what's missing.
No network — the per-layer Overpass fetch is monkeypatched to record requests.
"""
from app.config import get_settings
from app.graph import graph as gm
from app.graph.geo import ingest


def test_needed_layers_maps_op_to_single_asset():
    """fetch requests only the asset layer the op reads — a roads question never pulls buildings."""
    assert gm._needed_layers({"operation": "roads_in_hazard"}) == ["roads"]
    assert gm._needed_layers({"operation": "count_in_hazard", "op_args": {"layer": "buildings"}}) == ["buildings"]
    assert gm._needed_layers({"operation": "count_features", "op_args": {"layer": "hospitals"}}) == ["hospitals"]
    assert gm._needed_layers({"operation": "something_else"}) is None


def test_ensure_aoi_fetches_only_requested_layers(tmp_path, monkeypatch):
    """A drawn AOI asking for roads fetches roads only; a later hospitals ask adds just hospitals."""
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    calls = []
    monkeypatch.setattr(ingest, "_fetch_layer", lambda layer, bbox, boundary: (calls.append(layer), [])[1])

    geom = [103.16, 13.07, 103.24, 13.15]            # a drawn bbox (no Nominatim needed)
    ingest.ensure_aoi(geometry=geom, layers=["roads"])
    assert calls == ["roads"]                         # not hospitals/schools/buildings

    ingest.ensure_aoi(geometry=geom, layers=["hospitals"])
    assert calls == ["roads", "hospitals"]            # roads cached -> only hospitals fetched

    ingest.ensure_aoi(geometry=geom, layers=["roads", "hospitals"])
    assert calls == ["roads", "hospitals"]            # both cached -> no new fetch
