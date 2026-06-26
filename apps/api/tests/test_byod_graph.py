"""BYOD-S2b: a verified BYOD layer is wired into the graph — route offers it (per-thread),
and a question that selects it routes straight through (no exposure/L1/L2 ask) to a clipped,
counted answer. A second thread can't see it; the map legend degrades gracefully.
"""
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from app.config import get_settings
from app.graph import graph as gm
from app.graph.geo import byod_registry, viz


def _write_tif(path, data, *, dtype="int16"):
    h, w = data.shape
    t = from_bounds(103.0, 13.0, 103.1, 13.1, w, h)
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype=dtype, crs="EPSG:4326", transform=t) as dst:
        dst.write(data.astype(dtype), 1)
    return str(path)


@pytest.fixture(autouse=True)
def _clean_registry():
    byod_registry.clear()
    yield
    byod_registry.clear()


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiffs_dir", tmp_path / "tiffs")
    return tmp_path / "tiffs"


def _register_flood(tmp_path, thread="t1"):
    src = _write_tif(tmp_path / "up.tif", np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16"))
    layer, report = byod_registry.register(thread, hazard_label="flood", severity_scale="0-5",
                                           src_path=src, filename="my_flood.tif")
    assert report.ok
    return layer


def _cfg(client, thread):
    return {"configurable": {"thread_id": thread, "client": client, "model": "stub"}}


def test_route_menu_merges_byod_per_thread(tmp_path, tdir, log):
    """route's menu includes the thread's BYOD layer (so the model may pick it) but not others'."""
    layer = _register_flood(tmp_path, "t1")
    menu1, menu2 = gm._route_menu("t1"), gm._route_menu("t2")
    log("t1 has byod?", layer in menu1)
    log("t2 has byod?", layer in menu2)
    assert layer in menu1 and "flood" in menu1[layer].lower()
    assert layer not in menu2                          # per-thread: no leak
    assert "hazard_flood" in menu1                     # built-ins still present


def test_byod_layer_answered_without_the_3way_ask(aoi, make_client, monkeypatch, tmp_path, tdir, log):
    """[stub] a question that selects a registered BYOD layer routes straight through (no
    exposure/L1/L2 ask) → fetch clips it → operate produces a number."""
    monkeypatch.setattr(gm.ingest, "ensure_aoi", lambda *a, **k: aoi)
    monkeypatch.setattr(gm.ingest, "hazard_clip", lambda a, layer: a.get(layer) or a["hazard_flood"])
    layer = _register_flood(tmp_path, "t1")
    client = make_client(("tool", "roads_in_hazard", {"place": "Testville", "hazard_layers": [layer]}))

    out = gm._build_graph().invoke(
        {"messages": [{"role": "user", "content": "roads in my uploaded flood layer in Testville"}]},
        _cfg(client, "t1"))
    log("TRACE", out["trace"])
    log("RESULT", out.get("result"))
    assert out.get("awaiting_choice") is None          # BYOD = direct use, no 3-way ask
    assert out.get("result") and out["result"]["method"] == "roads_in_hazard"
    assert any(layer in step for step in out["trace"])  # the BYOD layer flowed through route + fetch


def test_byod_legend_degrades_gracefully(tmp_path, tdir):
    """A BYOD layer has no catalog legend, so tiffs.legend is empty and viz falls back to the
    generic severity ramp — the map still renders five colored classes."""
    layer = _register_flood(tmp_path, "t1")
    leg = viz.legend_colored(layer)
    assert set(leg) == {1, 2, 3, 4, 5}
    assert all(leg[s]["color"] for s in leg)
