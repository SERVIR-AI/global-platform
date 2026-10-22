"""X2b: the raster gate — declared contract verified against the file, nothing
written on mismatch."""

import numpy as np
import pytest
import yaml

from app.config import get_settings
from app.contrib import rasters


def _manifest(path, **over):
    d = {"layer": "hazard_toyheat", "file": path,
         "title": "Toy heat hazard", "description": "toy",
         "source": "toy institute, derived from toy reanalysis",
         "license": "CC-BY-4.0", "vintage": "2026-08",
         "legend": {1: "Low", 2: "High"},
         "declared": {"dtype": "int16", "valid_min": 0, "valid_max": 5}}
    d.update(over)
    return d


@pytest.fixture
def conf_env(monkeypatch, tmp_path):
    """Isolated hand-authored conf + EMPTY contrib files + isolated tiffs dir.
    Contributions must only ever touch the contrib files."""
    cat = tmp_path / "tiffs.yml"
    cat.write_text("# HAND-AUTHORED — a comment the gate must never destroy\n"
                   + yaml.safe_dump({"hazard_flood": {"local_path": "tiffs/hazard_flood.tif"}}))
    sch = tmp_path / "raster_schema.yml"
    sch.write_text(yaml.safe_dump({"defaults": {"crs": "EPSG:4326", "float_tol": 0.0001},
                                   "layers": {}}))
    monkeypatch.setattr(get_settings(), "tiffs_config_path", cat)
    monkeypatch.setattr(get_settings(), "raster_schema_path", sch)
    monkeypatch.setattr(get_settings(), "tiffs_contrib_path", tmp_path / "tiffs.contrib.yml")
    monkeypatch.setattr(get_settings(), "raster_schema_contrib_path",
                        tmp_path / "raster_schema.contrib.yml")
    monkeypatch.setattr(get_settings(), "tiffs_dir", str(tmp_path / "tiffs"))
    return tmp_path


def test_manifest_failures_all_named(log):
    fails = rasters.validate_manifest({"layer": "heat"})
    log("OUTPUT", "; ".join(fails)[:140])
    # the namespaces are derived from the schema's table, so the decline names every one
    ns = next(f for f in fails if "namespaced" in f)
    assert all(f"{p}<name>" in ns for p in rasters.namespaces())
    assert "measure_<name>" in ns and "risk_<name> (a risk map in classes 1-5)" in ns
    named = {f.split("'")[1] for f in fails if "missing required" in f}
    assert {"file", "title", "source", "license", "vintage", "legend"} <= named


def test_declared_contract_is_required_not_observed(log):
    fails = rasters.validate_manifest(
        {"layer": "hazard_x", "file": "/nope.tif", "title": "t", "description": "d",
         "source": "s", "license": "unstated", "vintage": "2026",
         "legend": {1: "a"}, "declared": {"dtype": "int8"}})
    log("OUTPUT", [f for f in fails if "declared" in f][0][:110])
    assert any("observation cannot write it" in f for f in fails)


def test_a_lying_declaration_refuses_and_writes_nothing(conf_env, tif_writer, log):
    """The file is float32 0..99; the declaration says int16 0..5. The gate must
    catch the lie and leave both conf files untouched."""
    path = tif_writer(np.random.rand(20, 20).astype("float32") * 99,
                      dtype="float32", name="liar.tif")
    out = rasters.add(_manifest(path))
    log("OUTPUT", out["failures"][0][:120])
    assert out["status"] == "declined" and out.get("verified") is False
    assert not (conf_env / "tiffs.contrib.yml").exists()     # nothing landed


def test_a_truthful_layer_lands_catalog_row_and_contract(conf_env, tif_writer, log):
    data = np.random.randint(0, 6, (20, 20)).astype("int16")
    path = tif_writer(data, dtype="int16", name="truth.tif")
    out = rasters.add(_manifest(path))
    log("OUTPUT", f"{out['status']} verified={out['verified']}")
    assert out["status"] == "landed" and out["verified"] is True
    # the landing goes to MACHINE-OWNED contrib files; the hand-authored file
    # keeps its comments untouched (the X2b yaml-round-trip regression)
    assert "HAND-AUTHORED" in (conf_env / "tiffs.yml").read_text()
    cat = yaml.safe_load((conf_env / "tiffs.contrib.yml").read_text())
    row = cat["hazard_toyheat"]
    assert row["license"] == "CC-BY-4.0" and row["vintage"] == "2026-08"
    assert row["contributed"] is True
    sch = yaml.safe_load((conf_env / "raster_schema.contrib.yml").read_text())
    assert sch["layers"]["hazard_toyheat"]["dtype"] == "int16"
    assert (conf_env / "tiffs" / "hazard_toyheat.tif").is_file()
    from app.graph.geo import tiffs as tiffs_mod
    assert "hazard_toyheat" in tiffs_mod.catalog()           # merged view serves it


def test_existing_layers_cannot_be_overwritten(conf_env, tif_writer, log):
    data = np.zeros((5, 5)).astype("int16")
    path = tif_writer(data, dtype="int16", name="z.tif")
    out = rasters.add(_manifest(path, layer="hazard_flood"))
    log("OUTPUT", out["failures"][0][:80])
    assert out["status"] == "declined"
    assert "do not overwrite" in out["failures"][0]


def test_dry_run_verifies_but_lands_nothing(conf_env, tif_writer, log):
    data = np.random.randint(0, 6, (10, 10)).astype("int16")
    path = tif_writer(data, dtype="int16", name="d.tif")
    out = rasters.add(_manifest(path), dry_run=True)
    log("OUTPUT", out["status"])
    assert out["verified"] is True and "dry-run" in out["status"]
    cat = yaml.safe_load((conf_env / "tiffs.yml").read_text())
    assert "hazard_toyheat" not in cat


# ----------------------------------------------------------------- overviews on landing

def _measure_manifest(path, **over):
    d = {"layer": "measure_test_amount", "file": path, "title": "Toy amount",
         "description": "toy continuous field", "source": "toy institute",
         "license": "CC-BY-4.0", "vintage": "2026-08",
         "declared": {"dtype": "float32", "valid_min": 0, "valid_max": 100, "units": "m"}}
    d.update(over)
    return d


def test_a_large_landed_raster_gets_overviews(conf_env, tif_writer, log, monkeypatch):
    import rasterio
    monkeypatch.setattr(rasters, "OVERVIEWS_FROM_BYTES", 0)
    data = np.random.randint(0, 6, (600, 600)).astype("int16")
    out = rasters.add(_manifest(tif_writer(data, dtype="int16", name="big.tif")))
    assert out["status"] == "landed", out
    with rasterio.open(out["file"]) as ds:
        assert ds.overviews(1) == [2]          # 600 // 2 = 300 cells >= 256; 600 // 4 = 150 is not
    assert "nearest" in out["passport"]["overviews"], out["passport"]


def test_a_measure_raster_is_averaged(conf_env, tif_writer, log, monkeypatch):
    import rasterio
    monkeypatch.setattr(rasters, "OVERVIEWS_FROM_BYTES", 0)
    data = (np.random.rand(600, 600) * 100).astype("float32")
    out = rasters.add(_measure_manifest(tif_writer(data, dtype="float32", name="amount.tif")))
    assert out["status"] == "landed", out
    with rasterio.open(out["file"]) as ds:
        assert ds.overviews(1) == [2]
    assert "average" in out["passport"]["overviews"], out["passport"]


def test_a_small_raster_is_left_alone(conf_env, tif_writer, log):
    import rasterio
    data = np.random.randint(0, 6, (20, 20)).astype("int16")
    out = rasters.add(_manifest(tif_writer(data, dtype="int16", name="small.tif")))
    assert out["status"] == "landed", out
    with rasterio.open(out["file"]) as ds:
        assert ds.overviews(1) == []
    assert "overviews" not in out["passport"]


def test_existing_overviews_are_kept(conf_env, tif_writer, log, monkeypatch):
    import rasterio
    from rasterio.enums import Resampling
    monkeypatch.setattr(rasters, "OVERVIEWS_FROM_BYTES", 0)
    data = np.random.randint(0, 6, (600, 600)).astype("int16")
    path = tif_writer(data, dtype="int16", name="has_ovr.tif")
    with rasterio.open(path, "r+") as ds:
        ds.build_overviews([2], Resampling.nearest)
    assert rasters.build_overviews(path, "hazard") is None     # already there: nothing to do
    out = rasters.add(_manifest(path))
    assert out["status"] == "landed", out
    with rasterio.open(out["file"]) as ds:
        assert ds.overviews(1) == [2]
    assert "overviews" not in out["passport"]
