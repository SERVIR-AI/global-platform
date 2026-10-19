"""Sprint dry-run fixes (2026-10-20): vector ingest and vector measures.

What the dry run hit, each rebuilt here on synthetic files written from the formats'
own specs (never the real ones): counter-clockwise outer rings dropped from a WDPA
shapefile; a GeoServer .cst charset ignored; one null geometry refusing a whole
layer; a GeoJSON in EPSG:3857 declined; an ArcGIS query cut at its record limit taken
as complete; a missing .prj told to "reproject"; 6-decimal rounding lengthening dense
lines; invalid polygons repaired or lost without a word; 'largest named' for points;
within= recomputed on every question; areas on the sphere, 0.4% high at 10° N."""
import io
import json
import math
import struct
import zipfile

import numpy as np
import pytest
from rasterio.warp import transform as warp_transform
from shapely.geometry import box, mapping, shape

from app.config import get_settings
from app.contrib import fetch_policy, identity, staging
from app.graph.geo import vecio, vectors
from app.mcp import store, vectors as vectors_step

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
A, E2 = 6378137.0, 0.00669437999014


# ----------------------------------------------------------------- writers
def _ring_cw(x0, y0, x1, y1):
    return [(x0, y0), (x0, y1), (x1, y1), (x1, y0), (x0, y0)]


def _ring_ccw(x0, y0, x1, y1):
    return list(reversed(_ring_cw(x0, y0, x1, y1)))


def _shp_zip(records, rows=(), fields=(("NAME", "C", 40, 0),), encoding="utf-8", stype=5,
             prj=None, cpg=None, cst=None, deleted=(), stem="capa"):
    """A zipped shapefile written by hand from the format: `records` are lists of parts
    (each a list of (x, y)) for PolyLine/Polygon, an (x, y) for Point, or None."""
    body = b""
    allpts = []
    for k, rec in enumerate(records):
        if rec is None:
            content = struct.pack("<i", 0)
        elif stype == 1:
            content = struct.pack("<idd", 1, *rec)
            allpts.append(rec)
        else:
            pts = [p for part in rec for p in part]
            allpts += pts
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            content = struct.pack("<i4d2i", stype, min(xs), min(ys), max(xs), max(ys), len(rec), len(pts))
            off = 0
            for part in rec:
                content += struct.pack("<i", off)
                off += len(part)
            for x, y in pts:
                content += struct.pack("<dd", x, y)
        body += struct.pack(">ii", k + 1, len(content) // 2) + content
    xs, ys = [p[0] for p in allpts] or [0], [p[1] for p in allpts] or [0]
    header = (struct.pack(">i", 9994) + b"\x00" * 20 + struct.pack(">i", (100 + len(body)) // 2)
              + struct.pack("<ii", 1000, stype) + struct.pack("<4d", min(xs), min(ys), max(xs), max(ys))
              + struct.pack("<4d", 0, 0, 0, 0))
    hlen = 32 + 32 * len(fields) + 1
    rlen = 1 + sum(f[2] for f in fields)
    dbf = bytes([3, 126, 10, 20]) + struct.pack("<IHH", len(records), hlen, rlen) + b"\x00" * 20
    for name, t, ln, dec in fields:
        dbf += name.encode("ascii").ljust(11, b"\x00") + t.encode() + b"\x00" * 4 + bytes([ln, dec]) + b"\x00" * 14
    dbf += b"\x0d"
    for k in range(len(records)):
        row = rows[k] if k < len(rows) else {}
        dbf += b"*" if k in deleted else b" "
        for name, t, ln, _dec in fields:
            v = row.get(name, "")
            cell = (v if isinstance(v, bytes) else str(v).encode(encoding))[:ln]
            dbf += cell.rjust(ln) if t in "NF" else cell.ljust(ln)
    dbf += b"\x1a"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{stem}.shp", header + body)
        z.writestr(f"{stem}.dbf", dbf)
        for ext, val in ((".prj", prj), (".cpg", cpg), (".cst", cst)):
            if val is not None:
                z.writestr(stem + ext, val)
    return buf.getvalue()


def _fc(features, **top):
    return json.dumps({"type": "FeatureCollection", **top, "features": features}).encode()


def _ft(geom, **props):
    return {"type": "Feature", "properties": props, "geometry": geom}


def _cell_km2(lon0, lat0, lon1, lat1):
    """A lon/lat cell's area on the WGS 84 ellipsoid by integrating its area element —
    independent of the authalic-latitude formula under test."""
    phi = np.linspace(math.radians(lat0), math.radians(lat1), 20001)
    f = A * A * (1 - E2) * np.cos(phi) / (1 - E2 * np.sin(phi) ** 2) ** 2
    return float(np.trapezoid(f, phi)) * math.radians(lon1 - lon0) / 1e6


UTM16N = ('PROJCS["WGS_1984_UTM_Zone_16N",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",'
          '6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
          'PROJECTION["Transverse_Mercator"],PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",0.0],'
          'PARAMETER["Central_Meridian",-87.0],PARAMETER["Scale_Factor",0.9996],PARAMETER["Latitude_Of_Origin",0.0],'
          'UNIT["Meter",1.0]]')


# ----------------------------------------------------------------- 1. ring orientation
def test_counter_clockwise_outer_rings_are_kept_and_holes_found_by_where_they_lie(log):
    """WDPA-style: one feature whose second outer ring is wound counter-clockwise (the
    old reader took it for a hole, found no shell around it and dropped it), with a
    clockwise-wound hole inside it and an island in that hole."""
    a = _ring_cw(-85.0, 10.0, -84.9, 10.1)                 # outer, by the book
    b = _ring_ccw(-84.8, 10.0, -84.5, 10.3)                # outer, wound the other way
    hole = _ring_cw(-84.75, 10.05, -84.55, 10.25)          # hole of b, wound like an outer
    island = _ring_ccw(-84.7, 10.1, -84.6, 10.2)           # island in that hole
    plain = [_ring_cw(-85.0, 9.0, -84.9, 9.1), _ring_ccw(-84.98, 9.02, -84.92, 9.08)]
    raw = _shp_zip([[a, b, hole, island], plain], rows=[{"NAME": "Agua y Paz"}, {"NAME": "Plain"}])
    out, notes = vecio.to_geojson(raw, "wdpa.zip")
    log("OUTPUT", notes)
    feats = json.loads(out)["features"]
    got = vectors.area_km2(shape(feats[0]["geometry"]))
    want = (_cell_km2(-85.0, 10.0, -84.9, 10.1) + _cell_km2(-84.8, 10.0, -84.5, 10.3)
            - _cell_km2(-84.75, 10.05, -84.55, 10.25) + _cell_km2(-84.7, 10.1, -84.6, 10.2))
    assert got == pytest.approx(want, rel=1e-6)
    assert vectors.area_km2(shape(feats[1]["geometry"])) == pytest.approx(
        _cell_km2(-85.0, 9.0, -84.9, 9.1) - _cell_km2(-84.98, 9.02, -84.92, 9.08), rel=1e-6)
    assert any(n.startswith("3 ring(s) in 1 feature(s) were re-oriented") for n in notes)


def test_a_lone_counter_clockwise_ring_is_an_outer_ring():
    out, notes = vecio.to_geojson(_shp_zip([[_ring_ccw(-85.0, 10.0, -84.9, 10.1)]]), "one.zip")
    g = shape(json.loads(out)["features"][0]["geometry"])
    assert vectors.area_km2(g) == pytest.approx(_cell_km2(-85.0, 10.0, -84.9, 10.1), rel=1e-6)
    assert any("1 ring(s) in 1 feature(s) were re-oriented" in n for n in notes)


# ----------------------------------------------------------------- 2. .dbf encoding
def test_dbf_text_follows_cst_or_cpg_else_utf8_else_cp1252(log):
    poly = [[_ring_cw(-80.0, -3.0, -79.9, -2.9)]]
    name = "RESERVA ECOLÓGICA"
    # GeoServer writes the charset as .cst — honoured
    out, notes = vecio.to_geojson(_shp_zip(poly, [{"NAME": name}], encoding="utf-8", cst="UTF-8"), "snap.zip")
    assert json.loads(out)["features"][0]["properties"]["NAME"] == name
    assert "decoded as UTF-8 as its .cst declares" in notes[0]
    # ESRI's .cpg, in its 'ANSI 1252' form
    out, notes = vecio.to_geojson(_shp_zip(poly, [{"NAME": name}], encoding="cp1252", cpg="ANSI 1252"), "a.zip")
    assert json.loads(out)["features"][0]["properties"]["NAME"] == name and "cp1252" in notes[0]
    # neither: valid UTF-8 is read as UTF-8, and the note says why
    out, notes = vecio.to_geojson(_shp_zip(poly, [{"NAME": name}], encoding="utf-8"), "b.zip")
    log("OUTPUT", notes)
    assert json.loads(out)["features"][0]["properties"]["NAME"] == name
    assert any("no .cpg or .cst names its encoding; read as UTF-8" in n for n in notes)
    # neither, and not UTF-8: Windows Latin-1, said so
    out, notes = vecio.to_geojson(_shp_zip(poly, [{"NAME": "Río Térraba"}], encoding="cp1252"), "c.zip")
    assert json.loads(out)["features"][0]["properties"]["NAME"] == "Río Térraba"
    assert any("read as cp1252 (Windows Latin-1), as the text is not valid UTF-8" in n for n in notes)
    # a .cpg naming nothing Python knows falls back to the same test, in words
    out, notes = vecio.to_geojson(_shp_zip(poly, [{"NAME": name}], cpg="KLINGON-8"), "d.zip")
    assert json.loads(out)["features"][0]["properties"]["NAME"] == name
    assert any("its .cpg names 'KLINGON-8', not an encoding the platform knows" in n for n in notes)


def test_a_utf8_character_cut_by_the_field_width_is_dropped_not_garbled():
    raw = _shp_zip([[_ring_cw(0, 0, 0.1, 0.1)]], [{"NAME": "ÁREA".encode("utf-8")[:1]}],
                   fields=(("NAME", "C", 1, 0),))
    out, notes = vecio.to_geojson(raw, "cut.zip")
    assert json.loads(out)["features"][0]["properties"]["NAME"] is None     # nothing left, not '�'
    assert any("read as UTF-8" in n for n in notes)


# ----------------------------------------------------------------- 3. null geometries
def test_features_without_geometry_are_dropped_and_counted_not_the_layer(log):
    pts = [_ft({"type": "Point", "coordinates": [-85.0 + i / 100, 10.0]}, farm=f"F{i}") for i in range(5)]
    raw = _fc(pts[:2] + [_ft(None, farm="ghost"), _ft({"type": "Point", "coordinates": []}, farm="empty")] + pts[2:])
    out, notes = vecio.to_geojson(raw, "wfs.json")
    log("OUTPUT", notes)
    assert notes[0] == "read 5 feature(s) from GeoJSON (7 in the file)"
    assert "2 feature(s) without geometry were dropped" in notes
    assert vectors.inspect(out)["features"] == 5
    # the inspector itself sets them aside too, and counts the WHOLE file in a refusal
    assert vectors.inspect(raw)["without_geometry"] == 2
    bad = _fc(pts[:3] + [_ft({"type": "GeometryCollection", "geometries": [pts[0]["geometry"]]})] + pts[3:] + [_ft(None)])
    with pytest.raises(ValueError) as exc:
        vectors.inspect(bad)
    assert "{'Point': 5, 'GeometryCollection': 1}" in str(exc.value) and "1 without geometry" in str(exc.value)


def test_a_shapefile_null_shape_and_a_deleted_record_are_said():
    raw = _shp_zip([(-85.0, 10.0), None, (-84.9, 10.1), (-84.8, 10.2)], stype=1,
                   rows=[{"NAME": "a"}, {"NAME": "b"}, {"NAME": "c"}, {"NAME": "d"}], deleted=(2,))
    out, notes = vecio.to_geojson(raw, "pts.zip")
    names = [f["properties"]["NAME"] for f in json.loads(out)["features"]]
    assert names == ["a", "d"]
    assert "1 record(s) marked deleted in the .dbf were skipped" in notes
    assert "1 feature(s) without geometry were dropped" in notes


# ----------------------------------------------------------------- 4. GeoJSON in another CRS
@pytest.mark.parametrize("name", ["EPSG:3857", "urn:ogc:def:crs:EPSG::3857", "EPSG:102100"])
def test_a_geojson_declaring_another_crs_is_reprojected_and_said(name):
    lon, lat = [-84.9, -84.8, -84.8, -84.9, -84.9], [10.0, 10.0, 10.1, 10.1, 10.0]
    xs, ys = warp_transform("EPSG:4326", "EPSG:3857", lon, lat)
    raw = _fc([_ft({"type": "Polygon", "coordinates": [list(zip(xs, ys))]}, NAME="x")],
              crs={"type": "name", "properties": {"name": name}})
    out, notes = vecio.to_geojson(raw, "hub.geojson")
    fc = json.loads(out)
    assert "crs" not in fc and any("(the file's crs member) to EPSG:4326" in n for n in notes)
    got = fc["features"][0]["geometry"]["coordinates"][0]
    assert got[0] == pytest.approx([-84.9, 10.0], abs=1e-6) and got[2] == pytest.approx([-84.8, 10.1], abs=1e-6)
    assert vectors.inspect(out)["geometry"] == "polygons"


def test_wgs84_geojson_passes_through_untouched_and_an_unreadable_crs_is_declined():
    raw = _fc([_ft({"type": "Point", "coordinates": [-84.123456789, 10.0]})],
              crs={"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}})
    assert vecio.to_geojson(raw, "a.geojson") == (raw, [])
    bad = _fc([_ft({"type": "Point", "coordinates": [1, 2]})], crs={"type": "name", "properties": {"name": "MY:LOCAL:GRID"}})
    with pytest.raises(ValueError, match="declares crs 'MY:LOCAL:GRID', which the platform cannot read"):
        vecio.to_geojson(bad, "b.geojson")


# ----------------------------------------------------------------- 5. truncated exports
@pytest.mark.parametrize("where", ["top", "properties"])
def test_a_server_export_cut_at_its_record_limit_is_declined(where):
    feats = [_ft({"type": "Point", "coordinates": [-84.0, 10.0]}) for _ in range(3)]
    top = {"exceededTransferLimit": True} if where == "top" else {"properties": {"exceededTransferLimit": True}}
    with pytest.raises(ValueError) as exc:
        vecio.to_geojson(_fc(feats, **top), "query.geojson")
    assert str(exc.value) == ("the server truncated this export at 3 features (exceededTransferLimit) — use the "
                              "portal's full download (e.g. the Hub 'Download' GeoJSON/shapefile) or page the query")
    assert vecio.to_geojson(_fc(feats, exceededTransferLimit=False), "q.geojson")[1] == []


# ----------------------------------------------------------------- 6. missing .prj
def test_a_zip_without_prj_in_projected_units_names_the_missing_prj():
    raw = _shp_zip([[_ring_cw(686625, 1098915, 745725, 1138725)]], stem="c8514_dis")
    with pytest.raises(ValueError) as exc:
        vecio.to_geojson(raw, "c8514_dis_as_delivered_no_prj.zip")
    msg = str(exc.value)
    assert msg.startswith("the zip has no .prj, so its coordinate system is unknown — add the .prj "
                          "(or reproject to EPSG:4326)")
    assert "c8514_dis.shp's coordinates run x 686,625 to 745,725" in msg and "must be reprojected" not in msg
    # in lon/lat it still reads, and says what it assumed
    _out, notes = vecio.to_geojson(_shp_zip([[_ring_cw(-85.0, 10.0, -84.9, 10.1)]]), "ll.zip")
    assert any("declares no CRS; its coordinates were read as lon/lat" in n for n in notes)


def test_a_geopackage_layer_with_no_crs_in_projected_units_is_declined_in_words(tmp_path):
    import sqlite3
    from shapely.geometry import Polygon
    p = tmp_path / "t.gpkg"
    con = sqlite3.connect(p)
    con.executescript("""
      create table gpkg_spatial_ref_sys(srs_name text, srs_id integer primary key, organization text, organization_coordsys_id integer, definition text);
      insert into gpkg_spatial_ref_sys values ('Undefined Cartesian', -1, 'NONE', -1, 'undefined');
      create table gpkg_contents(table_name text primary key, data_type text, identifier text, srs_id integer);
      create table gpkg_geometry_columns(table_name text, column_name text, geometry_type_name text, srs_id integer, z integer, m integer);
      create table manglar(fid integer primary key, geom blob);
      insert into gpkg_contents values ('manglar','features','manglar',-1);
      insert into gpkg_geometry_columns values ('manglar','geom','POLYGON',-1,0,0);
    """)
    blob = b"GP" + bytes([0, 0x01]) + struct.pack("<i", -1) + Polygon(_ring_cw(400000, 1105000, 401000, 1106000)).wkb
    con.execute("insert into manglar values (1, ?)", (blob,))
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="declares no coordinate system .* not lon/lat"):
        vecio.to_geojson(p.read_bytes(), "t.gpkg")


# ----------------------------------------------------------------- 7. precision
def test_dense_site_lines_keep_their_length(monkeypatch):
    """A contour traced every 0.3 m: 6 decimals (11 cm) lengthened it by over 1%."""
    t = np.linspace(0, 2 * math.pi, 1500)
    xs, ys = 700000 + 70 * np.cos(t) + 3 * np.sin(9 * t), 1110000 + 70 * np.sin(t)
    line = [list(zip(xs.tolist(), ys.tolist()))]
    raw = _shp_zip([line], stype=3, prj=UTM16N)
    lon, lat = warp_transform(UTM16N, "EPSG:4326", xs.tolist(), ys.tolist())
    exact = vectors.length_km(shape({"type": "LineString", "coordinates": list(zip(lon, lat))}))
    assert vecio.ROUND == 7
    got = vectors.length_km(shape(json.loads(vecio.to_geojson(raw, "c.zip")[0])["features"][0]["geometry"]))
    assert got == pytest.approx(exact, rel=1e-3)
    monkeypatch.setattr(vecio, "ROUND", 6)
    coarse = vectors.length_km(shape(json.loads(vecio.to_geojson(raw, "c.zip")[0])["features"][0]["geometry"]))
    assert coarse / exact - 1 > 0.003                 # the defect, measured: the test can see it


# ----------------------------------------------------------------- 8. invalid features
BOWTIE = {"type": "Polygon", "coordinates": [[[-85.0, 10.0], [-84.9, 10.1], [-84.9, 10.0], [-85.0, 10.1], [-85.0, 10.0]]]}
HOLE_AS_PART = {"type": "MultiPolygon", "coordinates": [            # an ArcGIS Hub export's hole
    [[[-84.8, 10.0], [-84.6, 10.0], [-84.6, 10.2], [-84.8, 10.2], [-84.8, 10.0]]],
    [[[-84.75, 10.05], [-84.65, 10.05], [-84.65, 10.15], [-84.75, 10.15], [-84.75, 10.05]]]]}
SLIVER = {"type": "Polygon", "coordinates": [[[-84.5, 10.0], [-84.4, 10.0], [-84.3, 10.0], [-84.5, 10.0]]]}


def test_invalid_polygons_are_repaired_or_dropped_and_said(log):
    raw = _fc([_ft(BOWTIE, NAME="bow"), _ft(HOLE_AS_PART, NAME="ring"), _ft(SLIVER, NAME="sliver")])
    out, notes = vecio.to_geojson(raw, "m.geojson")
    log("OUTPUT", notes)
    feats = {f["properties"]["NAME"]: shape(f["geometry"]) for f in json.loads(out)["features"]}
    assert set(feats) == {"bow", "ring"} and all(g.is_valid for g in feats.values())
    assert vectors.area_km2(feats["bow"]) == pytest.approx(_cell_km2(-85.0, 10.0, -84.9, 10.1) / 2, rel=1e-3)
    assert vectors.area_km2(feats["ring"]) == pytest.approx(
        _cell_km2(-84.8, 10.0, -84.6, 10.2) - _cell_km2(-84.75, 10.05, -84.65, 10.15), rel=1e-6)
    assert notes[0] == "read 2 feature(s) from GeoJSON (3 in the file)"
    assert any(n.startswith("2 invalid polygon(s)") and n.endswith("holes subtracted") for n in notes)
    assert "1 invalid polygon(s) collapsed to no area when repaired and were dropped" in notes


def test_overlapping_parts_count_their_shared_ground_once():
    over = {"type": "MultiPolygon", "coordinates": [
        [[[-85.0, 10.0], [-84.8, 10.0], [-84.8, 10.2], [-85.0, 10.2], [-85.0, 10.0]]],
        [[[-84.9, 10.0], [-84.7, 10.0], [-84.7, 10.2], [-84.9, 10.2], [-84.9, 10.0]]]]}
    fixed = vecio.repair(shape(over))
    assert fixed.is_valid and vectors.area_km2(fixed) == pytest.approx(_cell_km2(-85.0, 10.0, -84.7, 10.2), rel=1e-6)


# ----------------------------------------------------------------- the gate, end to end
@pytest.fixture
def gate(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr(s, "tiffs_dir", tmp_path / "cache" / "tiffs")
    monkeypatch.setattr(s, "grp_mattermost_webhook", "")
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(fetch_policy, "check_url", lambda url: [])
    bodies: dict = {}
    monkeypatch.setattr(fetch_policy, "fetch", lambda url, max_bytes=None: (bodies[url], url.rsplit("/", 1)[-1]))
    staging.STAGED_VECTORS.clear()
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    monkeypatch.setattr(s, "grp_auto_approve", True)
    yield bodies
    staging.STAGED_VECTORS.clear()


def _as(caller, fn):
    tok = identity.bind(caller)
    try:
        return fn()
    finally:
        identity.unbind(tok)


def _submit(bodies, layer, url, body, **extra):
    bodies[url] = body
    return staging.submit("vector", {"layer": layer, "url": url, "title": f"{layer} (synthetic)",
                                     "description": "synthetic", "source": "test fixture", "license": "CC0-1.0",
                                     "vintage": "unstated", **extra}, OWNER)


def test_the_gate_keeps_a_layer_with_a_null_geometry_and_says_so_in_preview_and_record(gate):
    pts = [_ft({"type": "Point", "coordinates": [-85.0 + i / 100, 10.0]}, farm_name=f"F{i}") for i in range(4)]
    r = _submit(gate, "test_farms", "https://x.test/wfs.json", _fc(pts + [_ft(None, farm_name="ghost")]))
    assert r["status"] == "approved", r
    assert "1 feature(s) without geometry were dropped" in r["preview"]["observed"]["prepared"]
    assert r["preview"]["observed"]["features"] == 4
    entry = vectors.landed()["test_farms"]
    assert "1 feature(s) without geometry were dropped" in entry["prepared"] and entry["features"] == 4


def test_the_gate_declines_a_missing_prj_and_a_truncated_export_with_the_right_fix(gate):
    r = _submit(gate, "test_noprj", "https://x.test/c8514.zip", _shp_zip([[_ring_cw(686625, 1098915, 745725, 1138725)]]))
    assert r["status"] == "declined" and "the zip has no .prj" in json.dumps(r)
    r = _submit(gate, "test_cut", "https://x.test/q.geojson",
                _fc([_ft({"type": "Point", "coordinates": [-84, 10]})], properties={"exceededTransferLimit": True}))
    assert r["status"] == "declined" and "truncated this export at 1 features" in json.dumps(r)


def test_a_fresh_shapefile_is_measured_right_by_a_pack_that_did_not_ask_for_it(gate, log):
    """CLAUDE.md: beyond the asking pack. A contributed zip with a counter-clockwise outer
    ring, a .cst charset and an invalid polygon is measured by the seam for a pack id
    that names nothing — and its citation states the repair and the ellipsoid."""
    outer_ccw = _ring_ccw(-84.8, 10.0, -84.5, 10.3)
    bow = [(-85.0, 10.0), (-84.9, 10.1), (-84.9, 10.0), (-85.0, 10.1), (-85.0, 10.0)]
    raw = _shp_zip([[_ring_cw(-85.0, 10.2, -84.9, 10.3), outer_ccw], [bow]],
                   rows=[{"NAME": "Área Protegida"}, {"NAME": "Reserva Ecológica"}], cst="UTF-8")
    r = _submit(gate, "test_pa_zip", "https://x.test/pa.zip", raw, name_field="NAME", group_field="NAME")
    assert r["status"] == "approved", r
    stats = {"_area": {"name": "Test bay", "how": "supplied polygon", "geometry": mapping(box(-85.1, 9.9, -84.4, 10.4))}}
    citations, gaps = [], []
    block, _where = _as(OWNER, lambda: vectors_step.attach({"id": "any-pack"}, {"place": "Test bay"}, stats,
                                                           citations, gaps, ["test_pa_zip"], {}))
    rec = block["test_pa_zip"]
    want = _cell_km2(-85.0, 10.2, -84.9, 10.3) + _cell_km2(-84.8, 10.0, -84.5, 10.3) + _cell_km2(-85.0, 10.0, -84.9, 10.1) / 2
    assert rec["total"] == pytest.approx(want, rel=1e-3)
    assert set(rec["by_group"]) == {"Área Protegida", "Reserva Ecológica"}
    text = citations[-1]["text"]
    log("OUTPUT", text)
    assert "At ingest, 1 invalid polygon(s)" in text and "ellipsoid" in text and "authalic sphere" not in text


# ----------------------------------------------------------------- 9. point layers
def test_point_names_are_counted_by_frequency_not_ranked_by_size(gate, log):
    names = ["Avicennia bicolor"] * 3 + ["Rhizophora mangle"] * 2 + ["Laguncularia racemosa", None]
    pts = [_ft({"type": "Point", "coordinates": [-85.0 + i / 1000, 10.0]}, species=n) for i, n in enumerate(names)]
    assert _submit(gate, "test_occ", "https://x.test/occ.geojson", _fc(pts), name_field="species")["status"] == "approved"
    entry = vectors.landed()["test_occ"]
    rec = vectors.summarise(entry, box(-85.1, 9.9, -84.9, 10.1))
    assert rec["most_frequent_named"] == [{"name": "Avicennia bicolor", "count": 3},
                                          {"name": "Rhizophora mangle", "count": 2},
                                          {"name": "Laguncularia racemosa", "count": 1}]
    assert rec["distinct_names"] == 3 and rec["unnamed"] == 1 and "largest_named" not in rec
    text = vectors_step.citation_text("test_occ", entry, rec, "Test bay")
    log("OUTPUT", text)
    assert ("Most frequent species: Avicennia bicolor (3), Rhizophora mangle (2), Laguncularia racemosa (1); "
            "1 unnamed.") in text and "Largest named" not in text


def test_a_layer_outside_the_area_is_described_in_geometry_neutral_words():
    na = vectors.not_applicable({"bbox": [-80.0, -3.0, -79.0, -2.0], "geometry": "polygons"}, box(-85, 9, -84, 10))
    assert na["reason"].startswith("its features lie wholly outside this area")


# ----------------------------------------------------------------- 10. within: prefilter + cache
def test_within_unions_only_nearby_features_and_reuses_its_result(tmp_path, monkeypatch):
    feats = [_ft(mapping(box(-85.0 + i * 0.01, 10.0, -84.995 + i * 0.01, 10.005)), NAME=f"p{i}") for i in range(400)]
    p = tmp_path / "pa.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    monkeypatch.setattr(vectors, "master_path", lambda e: p)
    entry = {"geometry": "polygons"}
    calls = []
    real_inside = vectors._Clip.inside
    monkeypatch.setattr(vectors._Clip, "inside", lambda self, g: calls.append(1) or real_inside(self, g))
    area = box(-85.0, 9.9, -84.955, 10.1)                     # holds the first five squares
    u1 = vectors.union_inside(entry, area)
    assert len(calls) <= 6                                     # never the 400
    assert vectors.area_km2(u1) == pytest.approx(5 * _cell_km2(-85.0, 10.0, -84.995, 10.005), rel=1e-6)
    assert vectors.union_inside(entry, area) is u1 and len(calls) <= 6     # the second ask is free
    p.write_text(json.dumps({"type": "FeatureCollection", "features": feats[1:]}))
    assert vectors.area_km2(vectors.union_inside(entry, area)) == pytest.approx(
        4 * _cell_km2(-85.0, 10.0, -84.995, 10.005), rel=1e-6)                # a changed layer is re-read


# ----------------------------------------------------------------- 11. the ellipsoid
def test_areas_and_lengths_are_the_wgs84_ellipsoids():
    for lat in (0.0, 10.0, -45.0, 60.0):
        cell = shape(mapping(box(-85.0, lat, -84.0, lat + 1)))
        assert vectors.area_km2(cell) == pytest.approx(_cell_km2(-85.0, lat, -84.0, lat + 1), rel=1e-7)
    sphere = 6371.0072 ** 2 * math.radians(1) * (math.sin(math.radians(11)) - math.sin(math.radians(10)))
    assert sphere / vectors.area_km2(box(0, 10, 1, 11)) - 1 == pytest.approx(0.0041, abs=0.0002)
    assert vectors.R_Q_KM == pytest.approx(6371.0072, abs=1e-3)
    assert vectors.length_km(shape({"type": "LineString", "coordinates": [[0, 0], [1, 0]]})) == pytest.approx(
        A * math.radians(1) / 1000, rel=1e-9)                                # 111.319 km on the equator
    assert vectors.length_km(shape({"type": "LineString", "coordinates": [[0, 0], [0, 1]]})) == pytest.approx(
        110.574, abs=0.001)                                                  # the meridian arc 0-1°


def test_a_record_measured_on_the_sphere_replays_as_a_method_change_not_a_mismatch(gate):
    sq = _fc([_ft(mapping(box(-85.0, 10.0, -84.9, 10.1)), NAME="a")])
    assert _submit(gate, "test_sq", "https://x.test/sq.geojson", sq)["status"] == "approved"
    stats = {"_area": {"name": "Bay", "how": "supplied", "geometry": mapping(box(-85.1, 9.9, -84.8, 10.2))}}
    block, where = _as(OWNER, lambda: vectors_step.attach({"id": "any-pack"}, {"place": "Bay"}, stats, [], [],
                                                          ["test_sq"], {}))
    pack = {"vectors": block, "vectors_area": where}
    assert _as(OWNER, lambda: vectors_step.verify(pack)) == {"test_sq": True}
    old = {"test_sq": {k: v for k, v in block["test_sq"].items() if k != "measure"}}
    said = _as(OWNER, lambda: vectors_step.verify({"vectors": old, "vectors_area": where}))["test_sq"]
    assert said.startswith("measured by an earlier method (the authalic sphere)")
