"""Land-cover pack, slices 2-7 on synthetic data: protected-area boundaries, the pixel
budget read through overviews, the map payload, the per-class series, the fire
points, a hub's own stack through the gate, and the composition row."""
import json
import struct

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from app.config import get_settings
from app.graph.geo import ingest, stacks
from app.land_cover import fire
from app.mcp import assemble

from test_landcover_pack import H, LEFT, LEGEND, PX, TOP, W, lc_env  # noqa: F401  (fixture reuse)


def test_resolver_accepts_protected_areas_and_estimates_by_latitude():
    park = {"class": "boundary", "type": "national_park", "display_name": "Chitwan National Park, Nepal",
            "geojson": {"type": "Polygon", "coordinates": [[[84.0, 27.4], [84.4, 27.4], [84.4, 27.7], [84.0, 27.7], [84.0, 27.4]]]}}
    hit = ingest._under_cap_admin([park], 25_000)
    assert hit[3] == "protected-area boundary" and hit[1] == "Chitwan National Park"
    # 0.4 x 0.3 degrees at 27.5 N: cos-latitude, not a constant tuned to 13 N
    expect = 0.12 * 111.32 ** 2 * np.cos(np.radians(27.55))
    assert abs(hit[0] - expect) / expect < 0.01
    poi = {"class": "amenity", "type": "office", "display_name": "x", "geojson": {"type": "Point"}}
    assert ingest._under_cap_admin([poi], 25_000) is None


def test_clip_honours_a_pixel_budget_and_records_the_level(lc_env):
    aoi = ingest.ensure_aoi(place="Test District", layers=())
    full = ingest.hazard_clip(aoi, "landcover_test_2022")
    assert stacks.clip_level(full) == 1
    import os
    os.remove(full)
    coarse = ingest.hazard_clip(aoi, "landcover_test_2022", max_pixels=16)
    with rasterio.open(coarse) as ds:
        level = stacks.clip_level(coarse)
        assert level > 1 and ds.width <= W // level + 1
        assert abs(ds.transform.a - PX * level) < 1e-9
    g = stacks.category_grid(coarse, [0, 15], {k: {"label": v} for k, v in LEGEND.items()})
    assert g["encoding"] == "legend" and [e["code"] for e in g["legend"]] == list(LEGEND)
    assert set(g["cells"]) <= set("0123456789") and "9" not in g["cells"]   # 15 is never a class


def test_pack_carries_a_map_payload_and_change_raster(lc_env):
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022)
    assert p["status"] == "ok", p.get("note")
    assert "viz_recorded" in p
    from app.mcp import store
    body = store.load_pack(p["pack_id"])
    viz = body["viz"]
    assert viz["layer_kind"] == "categorical" and viz["layer_label"].startswith("Forest change 2000 -> 2022")
    assert set(viz["legend"]) == {"1", "2", "3"} and viz["legend"]["2"]["label"] == "forest loss"
    assert viz["hazard_layer"]["raster_url"].endswith("landcover_change_3_2000_2022.tif")
    assert viz["metric"]["label"] == "net forest change"
    assert viz["hazard_grid"] and set(viz["hazard_grid"]["cells"]) <= set("0123")
    single = assemble.assemble(pack="land-cover", place="Test District", year=2022)
    v1 = store.load_pack(single["pack_id"])["viz"]
    assert len(v1["legend"]) == 9 and v1["legend"]["3"]["label"] == "Forest"
    assert v1["metric"]["label"] == "Forest" and v1["hazard_layer"]["raster_url"].endswith("landcover_test_2022.tif")


def test_named_classes_get_a_yearly_series(lc_env):
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022,
                          classes=["Forest", "Cropland"])
    kinds = [c["kind"] for c in p["citations"]]
    assert kinds.count("class_series") == 2 and kinds.count("class_transitions") == 2
    s = next(c for c in p["citations"] if c["kind"] == "class_series")
    assert s["series"]["period"] == "year" and [pt["t"] for pt in s["series"]["points"]] == ["2000", "2021", "2022"]
    assert "No map was published for 2012" in s["text"]
    assert "2022 vs 2000:" in s["text"]


def _tiny_shapefile(tmp_path, rows):
    """A 3-record point shapefile (.shp + .dbf) written by hand, the way the HKH one is laid out."""
    fields = [("LATITUDE", "F", 19), ("LONGITUDE", "F", 19), ("BRIGHTNESS", "F", 19), ("SCAN", "F", 19),
              ("TRACK", "F", 19), ("ACQ_DATE", "D", 8), ("ACQ_TIME", "C", 4), ("SATELLITE", "C", 5),
              ("INSTRUMENT", "C", 5), ("CONFIDENCE", "N", 10), ("VERSION", "C", 10), ("BRIGHT_T31", "F", 19),
              ("FRP", "F", 19), ("DAYNIGHT", "C", 1), ("LC_CODE", "N", 5), ("LC_CLASS", "C", 50), ("ACQ_YEAR", "N", 5)]
    rl = 1 + sum(f[2] for f in fields)
    hl = 32 + 32 * len(fields) + 1
    head = bytearray(32)
    head[0] = 3
    struct.pack_into("<I", head, 4, len(rows))
    struct.pack_into("<HH", head, 8, hl, rl)
    desc = b"".join(name.encode().ljust(11, b"\x00") + kind.encode() + b"\x00" * 4 + bytes([ln, 0]) + b"\x00" * 14
                    for name, kind, ln in fields) + b"\x0D"
    body = b""
    for r in rows:
        rec = b" "
        for name, kind, ln in fields:
            rec += str(r[name]).encode().rjust(ln) if kind in "FND" else str(r[name]).encode().ljust(ln)
        body += rec
    (tmp_path / "pts.dbf").write_bytes(bytes(head) + desc + body)
    shp = bytearray(100)
    struct.pack_into("<i", shp, 32, 1)
    for i, r in enumerate(rows):
        shp += struct.pack(">ii", i + 1, 10) + struct.pack("<idd", 1, float(r["LONGITUDE"]), float(r["LATITUDE"]))
    struct.pack_into(">i", shp, 24, len(shp) // 2)
    (tmp_path / "pts.shp").write_bytes(bytes(shp))


def test_fire_points_build_and_query(tmp_path):
    base = {"BRIGHTNESS": 320.0, "SCAN": 1.0, "TRACK": 1.0, "ACQ_TIME": "0442", "SATELLITE": "Terra",
            "INSTRUMENT": "MODIS", "CONFIDENCE": 60, "VERSION": "6.03", "BRIGHT_T31": 300.0, "FRP": 12.5,
            "DAYNIGHT": "D", "LC_CODE": 3, "LC_CLASS": "Forest"}
    rows = [{**base, "LATITUDE": 27.5, "LONGITUDE": 85.1, "ACQ_DATE": "20210304", "ACQ_YEAR": 2021},
            {**base, "LATITUDE": 27.6, "LONGITUDE": 85.2, "ACQ_DATE": "20220115", "ACQ_YEAR": 2022,
             "LC_CODE": 9, "LC_CLASS": "Grassland", "VERSION": "6.1NRT", "DAYNIGHT": "N"},
            {**base, "LATITUDE": 30.0, "LONGITUDE": 90.0, "ACQ_DATE": "20220601", "ACQ_YEAR": 2022}]
    _tiny_shapefile(tmp_path, rows)
    out = tmp_path / "vectors" / "pts.npz"
    pp = fire.build(str(tmp_path), "pts", str(out), str(out) + ".passport.json")
    assert pp["records"] == 3 and pp["date_range"] == ["2021-03-04", "2022-06-01"]
    assert pp["land_cover_codes"] == {3: 2, 9: 1} and pp["nrt_share"] == round(1 / 3, 4)
    q = fire.query(str(out), box(85.0, 27.0, 85.5, 28.0), {3: "Forest", 9: "Grassland"})
    assert q["n"] == 2 and q["by_year"] == {2021: 1, 2022: 1} and q["by_class"] == {"Forest": 1, "Grassland": 1}
    assert q["night"] == 1 and q["nrt"] == 1 and q["first"] == "2021-03-04"
    assert fire.query(str(out), box(0, 0, 1, 1), {})["n"] == 0
