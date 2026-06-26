"""BYOD-S2: the per-thread registry. A verified upload is stored under its layer name in
the tiff cache so `ingest.source_raster` resolves it locally (no Drive), it surfaces in the
per-thread menu, threads are isolated, a failing tif is never stored, and the curated
conf/tiffs.yml is never touched.
"""
import os

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from app.config import get_settings
from app.graph.geo import byod_registry, ingest


def _write_tif(path, data, *, crs="EPSG:4326", count=1, dtype="int16"):
    h, w = data.shape
    transform = from_bounds(103.0, 13.0, 103.1, 13.1, w, h)
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=count,
                       dtype=dtype, crs=crs, transform=transform) as dst:
        for band in range(1, count + 1):
            dst.write(data.astype(dtype), band)
    return str(path)


@pytest.fixture(autouse=True)
def _clean_registry():
    byod_registry.clear()
    yield
    byod_registry.clear()


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    """Point the tiff cache at a tmp dir so register + source_raster share it."""
    d = tmp_path / "tiffs"
    monkeypatch.setattr(get_settings(), "tiffs_dir", d)
    return d


def _good(tmp_path, name="up.tif"):
    return _write_tif(tmp_path / name, np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16"))


def test_register_passes_and_source_raster_resolves_locally(tmp_path, tdir, log):
    """A verified tif registers, lands in the tiff cache, and source_raster returns it with
    no Drive lookup (the file already exists on disk)."""
    src = _good(tmp_path)
    layer, report = byod_registry.register("t1", hazard_label="flood", severity_scale="0-5",
                                           src_path=src, filename="my_flood.tif")
    log("REGISTER", f"{layer}  ok={report.ok}")
    assert report.ok is True
    assert layer and layer.startswith("byod_flood_")
    dest = os.path.join(str(tdir), f"{layer}.tif")
    assert os.path.exists(dest)
    assert not os.path.exists(src)                      # moved, not copied

    # No network: the file exists, so source_raster short-circuits to it.
    resolved = ingest.source_raster(layer)
    log("SOURCE", resolved)
    assert os.path.realpath(resolved) == os.path.realpath(dest)


def test_descriptions_and_per_thread_isolation(tmp_path, tdir, log):
    """The layer surfaces in its own thread's menu and is invisible to other threads."""
    layer, _ = byod_registry.register("t1", hazard_label="flood", severity_scale="0-5",
                                      src_path=_good(tmp_path))
    desc = byod_registry.descriptions_for("t1")
    log("DESCRIPTIONS t1", desc)
    assert layer in desc and "flood" in desc[layer].lower()
    assert byod_registry.entries_for("t2") == {}        # thread isolation
    assert byod_registry.is_byod("t1", layer) and not byod_registry.is_byod("t2", layer)


def test_failing_tif_is_never_registered(tmp_path, tdir, log):
    """A multiband tif fails verification, so nothing is stored or registered (the gate holds)."""
    bad = _write_tif(tmp_path / "bad.tif", np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16"), count=2)
    layer, report = byod_registry.register("t1", hazard_label="flood", severity_scale="0-5", src_path=bad)
    log("REGISTER", f"layer={layer} ok={report.ok} mismatches={report.mismatches}")
    assert layer is None and report.ok is False
    assert byod_registry.entries_for("t1") == {}
    assert os.path.exists(bad)                           # not moved


def test_curated_catalog_is_untouched(tmp_path, tdir):
    """Registering a BYOD layer never mutates conf/tiffs.yml."""
    cat_path = get_settings().tiffs_config_path
    before = open(cat_path, "rb").read()
    byod_registry.register("t1", hazard_label="flood", severity_scale="0-5", src_path=_good(tmp_path))
    assert open(cat_path, "rb").read() == before
