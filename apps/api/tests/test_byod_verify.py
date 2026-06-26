"""BYOD-S1: the upload verification gate, exercised on REAL GeoTIFFs (rasterio-written,
no network, no mocks). A passing file is a clean single-band 0-5 EPSG:4326 class raster;
the unrecoverable cases (multi-band, no CRS, over-range, oversize) hard-fail; soft signals
(non-4326 CRS, continuous-looking) warn but still pass.
"""
import numpy as np

from app.graph.geo.byod_verify import verify_upload

_CLASS = np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16")


def test_clean_class_raster_passes(tif_writer, log):
    """A single-band 0-5 int16 EPSG:4326 class raster PASSES with no mismatches/warnings."""
    r = verify_upload(tif_writer(_CLASS), hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is True
    assert r.mismatches == []
    assert r.warnings == []


def test_multiband_fails(tif_writer, log):
    """A 2-band raster hard-fails — windowed_stats would silently read only band 1."""
    r = verify_upload(tif_writer(_CLASS, count=2), hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is False
    assert any("band" in m for m in r.mismatches)


def test_no_crs_fails(tif_writer, log):
    """A georef-less raster hard-fails — it can't be placed on the map."""
    r = verify_upload(tif_writer(_CLASS, crs=None), hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is False
    assert any("CRS" in m or "georef" in m for m in r.mismatches)


def test_over_range_continuous_fails(tif_writer, log):
    """A continuous raster whose values far exceed the declared 0-5 scale hard-fails the range check."""
    r = verify_upload(tif_writer((np.arange(36).reshape(6, 6) * 100).astype("int16")),
                      hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is False
    assert any("valid_max" in m for m in r.mismatches)


def test_oversize_fails_from_profile(tif_writer, log):
    """A pixel-count over the cap hard-fails from the profile alone (no full read needed)."""
    r = verify_upload(tif_writer(_CLASS), hazard_label="flood", severity_scale="0-5", max_pixels=10)
    log("REPORT", str(r))
    assert r.ok is False
    assert any("cap" in m for m in r.mismatches)


def test_non_4326_warns_but_passes(tif_writer, log):
    """A valid 0-5 class raster in EPSG:3857 PASSES but warns it needs reprojection."""
    r = verify_upload(tif_writer(_CLASS, crs="EPSG:3857",
                                 bounds=(11_000_000, 1_400_000, 11_001_000, 1_401_000)),
                      hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is True
    assert any("EPSG:3857" in w or "reprojection" in w for w in r.warnings)


def test_in_range_but_continuous_warns(tif_writer, log):
    """Float values within 0-5 but with many distinct levels PASS, yet warn that the raster
    looks continuous rather than a 6-class severity raster."""
    grid = (np.arange(2500).reshape(50, 50) % 200) / 40.0            # 0..~4.98, ~200 distinct
    r = verify_upload(tif_writer(grid, dtype="float32"), hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is True
    assert any("continuous" in w or "distinct" in w for w in r.warnings)


def test_unknown_scale_fails_cleanly(tif_writer):
    """A bad severity_scale declaration fails with a clear message, never crashes."""
    r = verify_upload(tif_writer(_CLASS), hazard_label="flood", severity_scale="bogus")
    assert r.ok is False
    assert any("severity_scale" in m for m in r.mismatches)
