"""BYOD-S2: the per-thread registry. A verified upload is stored under its layer name in the
tiff cache so ingest.source_raster resolves it locally (no Drive), it surfaces in the
per-thread menu, threads are isolated, a failing tif is never stored, and the curated
conf/tiffs.yml is never touched.
"""
import os

import numpy as np

from app.config import get_settings
from app.graph.geo import byod_registry, ingest

_CLASS = np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16")


def test_register_passes_and_source_raster_resolves_locally(tif_writer, byod_env, log):
    """A verified tif registers, lands in the tiff cache, and source_raster returns it with no
    Drive lookup (the file already exists on disk)."""
    src = tif_writer(_CLASS)
    layer, report = byod_registry.register("t1", hazard_label="flood", severity_scale="0-5",
                                           src_path=src, filename="my_flood.tif")
    log("REGISTER", f"{layer}  ok={report.ok}")
    assert report.ok is True
    assert layer and layer.startswith("byod_flood_")
    dest = os.path.join(str(byod_env), f"{layer}.tif")
    assert os.path.exists(dest)
    assert not os.path.exists(src)                      # moved, not copied

    resolved = ingest.source_raster(layer)
    log("SOURCE", resolved)
    assert os.path.realpath(resolved) == os.path.realpath(dest)


def test_descriptions_and_per_thread_isolation(tif_writer, byod_env, log):
    """The layer surfaces in its own thread's menu and is invisible to other threads."""
    layer, _ = byod_registry.register("t1", hazard_label="flood", severity_scale="0-5",
                                      src_path=tif_writer(_CLASS))
    desc = byod_registry.descriptions_for("t1")
    log("DESCRIPTIONS t1", desc)
    assert layer in desc and "flood" in desc[layer].lower()
    assert layer in byod_registry.entries_for("t1")
    assert byod_registry.entries_for("t2") == {}        # thread isolation


def test_failing_tif_is_never_registered(tif_writer, byod_env, log):
    """A multiband tif fails verification, so nothing is stored or registered."""
    bad = tif_writer(_CLASS, count=2)
    layer, report = byod_registry.register("t1", hazard_label="flood", severity_scale="0-5", src_path=bad)
    log("REGISTER", f"layer={layer} ok={report.ok}")
    assert layer is None and report.ok is False
    assert byod_registry.entries_for("t1") == {}
    assert os.path.exists(bad)                           # not moved


def test_curated_catalog_is_untouched(tif_writer, byod_env):
    """Registering a BYOD layer never mutates conf/tiffs.yml."""
    cat_path = get_settings().tiffs_config_path
    before = open(cat_path, "rb").read()
    byod_registry.register("t1", hazard_label="flood", severity_scale="0-5", src_path=tif_writer(_CLASS))
    assert open(cat_path, "rb").read() == before
