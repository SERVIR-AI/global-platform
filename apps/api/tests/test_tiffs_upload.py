"""BYOD-S3: POST /api/tiffs (TestClient multipart — real file, real verification, nothing
mocked). A good class raster registers (ok:true + a layer now in the thread's menu); a
multiband raster is rejected (ok:false, never registered); malformed requests get a 4xx.
"""
import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_bounds

from app.config import get_settings
from app.graph.geo import byod_registry
from app.main import app

client = TestClient(app)


def _tif_bytes(tmp_path, data, *, count=1, crs="EPSG:4326", name="t.tif"):
    h, w = data.shape
    p = tmp_path / name
    with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=count, dtype="int16",
                       crs=crs, transform=from_bounds(103.0, 13.0, 103.1, 13.1, w, h)) as dst:
        for b in range(1, count + 1):
            dst.write(data.astype("int16"), b)
    return p.read_bytes()


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiffs_dir", tmp_path / "tiffs")
    byod_registry.clear()
    yield
    byod_registry.clear()


def test_good_upload_registers(tmp_path, log):
    data = _tif_bytes(tmp_path, np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16"))
    r = client.post("/api/tiffs",
                    files={"file": ("my_flood.tif", data, "image/tiff")},
                    data={"thread_id": "t1", "hazard_label": "flood", "severity_scale": "0-5"})
    body = r.json()
    log("STATUS", r.status_code)
    log("BODY", body)
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["layer"] and body["layer"].startswith("byod_flood_")
    assert body["mismatches"] == []
    assert body["layer"] in byod_registry.descriptions_for("t1")     # registered for this thread


def test_multiband_upload_rejected(tmp_path, log):
    data = _tif_bytes(tmp_path, np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16"), count=2)
    r = client.post("/api/tiffs",
                    files={"file": ("bad.tif", data, "image/tiff")},
                    data={"thread_id": "t1", "hazard_label": "flood"})
    body = r.json()
    log("BODY", body)
    assert r.status_code == 200
    assert body["ok"] is False and body["layer"] is None
    assert any("band" in m for m in body["mismatches"])
    assert byod_registry.entries_for("t1") == {}                     # nothing registered


def test_bad_extension_rejected(log):
    r = client.post("/api/tiffs",
                    files={"file": ("notes.txt", b"hello", "text/plain")},
                    data={"thread_id": "t1", "hazard_label": "flood"})
    log("STATUS", r.status_code)
    assert r.status_code == 400


def test_empty_file_rejected(log):
    r = client.post("/api/tiffs",
                    files={"file": ("empty.tif", b"", "image/tiff")},
                    data={"thread_id": "t1", "hazard_label": "flood"})
    assert r.status_code == 400
