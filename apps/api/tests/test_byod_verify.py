"""BYOD-S1: the upload verification gate, exercised on REAL GeoTIFFs (rasterio-written,
no network, no mocks). A passing file is a clean single-band 0-5 EPSG:4326 class raster;
the unrecoverable cases (multi-band, no CRS, over-range, oversize) hard-fail; soft signals
(non-4326 CRS, continuous-looking) warn but still pass.
"""
import numpy as np
import rasterio
from rasterio.transform import from_bounds

from app.graph.geo.byod_verify import verify_upload


def _write_tif(path, data, *, crs="EPSG:4326", count=1, dtype="int16", nodata=None,
               bounds=(103.0, 13.0, 103.1, 13.1)):
    """Write a tiny real GeoTIFF; `data` is the 2D band array (written to every band)."""
    h, w = data.shape
    transform = from_bounds(*bounds, w, h)
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=count,
                       dtype=dtype, crs=crs, transform=transform, nodata=nodata) as dst:
        for band in range(1, count + 1):
            dst.write(data.astype(dtype), band)
    return str(path)


def test_clean_class_raster_passes(tmp_path, log):
    """A single-band 0-5 int16 EPSG:4326 class raster PASSES with no mismatches/warnings."""
    data = np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16")
    path = _write_tif(tmp_path / "good.tif", data)
    log("INPUT", "single-band 0-5 int16 EPSG:4326")
    r = verify_upload(path, hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is True
    assert r.mismatches == []
    assert r.warnings == []


def test_multiband_fails(tmp_path, log):
    """A 2-band raster hard-fails — windowed_stats would silently read only band 1."""
    data = np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16")
    path = _write_tif(tmp_path / "multi.tif", data, count=2)
    r = verify_upload(path, hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is False
    assert any("band" in m for m in r.mismatches)


def test_no_crs_fails(tmp_path, log):
    """A georef-less raster hard-fails — it can't be placed on the map."""
    data = np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16")
    path = _write_tif(tmp_path / "nocrs.tif", data, crs=None)
    r = verify_upload(path, hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is False
    assert any("CRS" in m or "georef" in m for m in r.mismatches)


def test_over_range_continuous_fails(tmp_path, log):
    """A continuous raster (values far above the declared 0-5 scale) hard-fails the range
    check — the same trap-catcher that flagged the mm-vs-metres layer."""
    data = (np.arange(36).reshape(6, 6) * 100).astype("int16")   # 0..3500, way over 5
    path = _write_tif(tmp_path / "continuous.tif", data)
    r = verify_upload(path, hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is False
    assert any("valid_max" in m for m in r.mismatches)


def test_oversize_fails_from_profile(tmp_path, log):
    """A pixel-count over the cap hard-fails from the profile alone (no full read needed)."""
    data = np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16")          # 36 px
    path = _write_tif(tmp_path / "small.tif", data)
    r = verify_upload(path, hazard_label="flood", severity_scale="0-5", max_pixels=10)
    log("REPORT", str(r))
    assert r.ok is False
    assert any("cap" in m for m in r.mismatches)


def test_non_4326_warns_but_passes(tmp_path, log):
    """A valid 0-5 class raster in EPSG:3857 PASSES but warns it needs reprojection."""
    data = np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16")
    path = _write_tif(tmp_path / "webmerc.tif", data, crs="EPSG:3857",
                      bounds=(11_000_000, 1_400_000, 11_001_000, 1_401_000))
    r = verify_upload(path, hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is True
    assert any("EPSG:3857" in w or "reprojection" in w for w in r.warnings)


def test_in_range_but_continuous_warns(tmp_path, log):
    """Float values within 0-5 but with many distinct levels PASS (in range) yet warn that
    the raster looks continuous rather than a 6-class severity raster."""
    grid = (np.arange(2500).reshape(50, 50) % 200) / 40.0            # 0..~4.98, ~200 distinct
    path = _write_tif(tmp_path / "floats.tif", grid, dtype="float32")
    r = verify_upload(path, hazard_label="flood", severity_scale="0-5")
    log("REPORT", str(r))
    assert r.ok is True
    assert any("continuous" in w or "distinct" in w for w in r.warnings)


def test_unknown_scale_fails_cleanly(tmp_path, log):
    """A bad severity_scale declaration fails with a clear message, never crashes."""
    data = np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16")
    path = _write_tif(tmp_path / "good.tif", data)
    r = verify_upload(path, hazard_label="flood", severity_scale="bogus")
    assert r.ok is False
    assert any("severity_scale" in m for m in r.mismatches)
