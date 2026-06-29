"""BYOD-S3: POST /api/tiffs (TestClient multipart — real file, real verification, nothing
mocked). A good class raster registers (ok:true + a layer now in the thread's menu); a
multiband raster is rejected (ok:false, never registered); malformed requests get a 4xx.
"""
import numpy as np
from fastapi.testclient import TestClient

from app.graph.geo import byod_registry
from app.main import app

client = TestClient(app)
_CLASS = np.array([[0, 1, 2, 3, 4, 5]] * 6, dtype="int16")


def test_good_upload_registers(tif_writer, byod_env, log):
    data = open(tif_writer(_CLASS), "rb").read()
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


def test_multiband_upload_rejected(tif_writer, byod_env, log):
    data = open(tif_writer(_CLASS, count=2), "rb").read()
    r = client.post("/api/tiffs",
                    files={"file": ("bad.tif", data, "image/tiff")},
                    data={"thread_id": "t1", "hazard_label": "flood"})
    body = r.json()
    log("BODY", body)
    assert r.status_code == 200
    assert body["ok"] is False and body["layer"] is None
    assert any("band" in m for m in body["mismatches"])
    assert byod_registry.entries_for("t1") == {}                     # nothing registered


def test_bad_extension_rejected(byod_env, log):
    r = client.post("/api/tiffs",
                    files={"file": ("notes.txt", b"hello", "text/plain")},
                    data={"thread_id": "t1", "hazard_label": "flood"})
    log("STATUS", r.status_code)
    assert r.status_code == 400


def test_empty_file_rejected(byod_env):
    r = client.post("/api/tiffs",
                    files={"file": ("empty.tif", b"", "image/tiff")},
                    data={"thread_id": "t1", "hazard_label": "flood"})
    assert r.status_code == 400
