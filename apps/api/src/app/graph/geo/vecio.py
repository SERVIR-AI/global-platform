"""The vector formats a hub actually hands over, read into GeoJSON on EPSG:4326.

GeoJSON is what the platform stores and measures. A hub's files are a zipped
shapefile or a GeoPackage, usually in a projected CRS (UTM, CRTM05). Rather than
asking every contributor to export first, the gate reads these here — a small
shapefile reader (the .shp record format, the .dbf table, the .prj CRS), a
GeoPackage reader on sqlite3 (the GeoPackage binary header, then WKB) — and
reprojects through PROJ (rasterio.warp) when the source is not geographic WGS 84.
No GDAL command line, no fiona: nothing a deployed host does not already have.

Every conversion says what it did (`notes`): which layer, which CRS, how many
features, what was dropped — the contributor's record carries it.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import struct
import tempfile
import zipfile

from shapely import wkb as _wkb
from shapely.geometry import LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon, mapping
from shapely.geometry.polygon import orient

ZIP_MAGIC = b"PK\x03\x04"
SQLITE_MAGIC = b"SQLite format 3\x00"
ROUND = 6                 # decimals kept on output coordinates (0.11 m at the equator)


def kind_of(raw: bytes, filename: str = "") -> str:
    """'geojson' | 'shapefile-zip' | 'gpkg' | 'unknown', from the bytes first."""
    if raw[:4] == ZIP_MAGIC:
        return "shapefile-zip"
    if raw[:16] == SQLITE_MAGIC:
        return "gpkg"
    head = raw[:4096].lstrip()
    if head[:1] in (b"{", b"[") or head.startswith(b"\xef\xbb\xbf{"):
        return "geojson"
    ext = os.path.splitext(filename or "")[1].lower()
    return {".geojson": "geojson", ".json": "geojson", ".zip": "shapefile-zip", ".gpkg": "gpkg"}.get(ext, "unknown")


def to_geojson(raw: bytes, filename: str = "", layer_name: str | None = None) -> tuple[bytes, list[str]]:
    """GeoJSON bytes on EPSG:4326 for any supported input, plus the notes of what was
    done. GeoJSON passes through untouched (its CRS is WGS 84 by specification)."""
    kind = kind_of(raw, filename)
    if kind == "geojson":
        return raw, []
    if kind == "shapefile-zip":
        fc, notes = _read_shapefile_zip(raw, layer_name)
    elif kind == "gpkg":
        fc, notes = _read_gpkg(raw, layer_name)
    else:
        raise ValueError("the file is not GeoJSON, a zipped shapefile or a GeoPackage — those are "
                         "the vector formats the platform reads")
    return json.dumps(fc, ensure_ascii=False).encode("utf-8"), notes


# ------------------------------------------------------------------ CRS + reprojection
def _crs_from(text_or_epsg):
    from rasterio.crs import CRS
    if text_or_epsg is None:
        return None
    if isinstance(text_or_epsg, int):
        return CRS.from_epsg(text_or_epsg)
    t = str(text_or_epsg).strip()
    if not t:
        return None
    try:
        return CRS.from_wkt(t, morph_from_esri_dialect=True)
    except Exception:
        return CRS.from_user_input(t)


def _is_wgs84(crs) -> bool:
    if crs is None:
        return False
    try:
        if crs.to_epsg() == 4326:
            return True
    except Exception:
        pass
    try:
        return bool(crs.is_geographic) and "WGS" in crs.to_wkt().upper()[:200]
    except Exception:
        return False


def _reproject(geoms, crs):
    from rasterio.warp import transform_geom
    out = []
    for g in geoms:
        if g is None:
            out.append(None)
            continue
        out.append(transform_geom(crs, "EPSG:4326", g, precision=ROUND))
    return out


def _round(obj):
    if isinstance(obj, dict) and "coordinates" in obj:
        return {**obj, "coordinates": _round(obj["coordinates"])}
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(v), ROUND) for v in obj[:2]]
        return [_round(x) for x in obj]
    return obj


def _finish(geoms, props, crs, notes, source_label):
    """Geometries (GeoJSON dicts or None) + properties -> a FeatureCollection on
    EPSG:4326, reprojected when needed, coordinates rounded, empties dropped."""
    if crs is not None and not _is_wgs84(crs):
        geoms = _reproject(geoms, crs)
        try:
            label = f"EPSG:{crs.to_epsg()}" if crs.to_epsg() else crs.to_string()[:80]
        except Exception:
            label = "the file's CRS"
        notes.append(f"reprojected from {label} to EPSG:4326 at ingest")
    elif crs is None:
        notes.append("the file declares no CRS; its coordinates were read as lon/lat (EPSG:4326)")
    feats, dropped = [], 0
    for g, p in zip(geoms, props):
        if not g or not g.get("coordinates"):
            dropped += 1
            continue
        feats.append({"type": "Feature", "properties": p, "geometry": _round(g)})
    if dropped:
        notes.append(f"{dropped} feature(s) with no geometry were dropped")
    notes.insert(0, f"read {len(feats)} feature(s) from {source_label}")
    return {"type": "FeatureCollection", "features": feats}, notes


# ------------------------------------------------------------------ shapefile
_SHAPE_TYPES = {0: None, 1: "Point", 3: "PolyLine", 5: "Polygon", 8: "MultiPoint",
                11: "Point", 13: "PolyLine", 15: "Polygon", 18: "MultiPoint",
                21: "Point", 23: "PolyLine", 25: "Polygon", 28: "MultiPoint"}


def _ring_is_clockwise(ring) -> bool:
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += (x2 - x1) * (y2 + y1)
    return s > 0


def _polygon_from_rings(rings):
    """Shapefile rings: clockwise = outer, counter-clockwise = hole; holes belong to
    the outer ring that contains them. One Polygon or a MultiPolygon."""
    outers, holes = [], []
    for r in rings:
        if len(r) < 4:
            continue
        (outers if _ring_is_clockwise(r) else holes).append(r)
    if not outers and holes:          # a file with the convention reversed
        outers, holes = holes, []
    polys = []
    for o in outers:
        shell = Polygon(o)
        mine = [h for h in holes if shell.contains(Point(h[0]))]
        polys.append(Polygon(o, mine))
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def _read_shp_records(data: bytes):
    """Yield shapely geometries (or None) for every record of a .shp."""
    pos = 100
    n = len(data)
    while pos + 8 <= n:
        _num, length = struct.unpack(">ii", data[pos:pos + 8])
        pos += 8
        rec = data[pos:pos + length * 2]
        pos += length * 2
        if len(rec) < 4:
            yield None
            continue
        stype = struct.unpack("<i", rec[:4])[0]
        fam = _SHAPE_TYPES.get(stype)
        if fam is None:
            yield None
        elif fam == "Point":
            x, y = struct.unpack("<dd", rec[4:20])
            yield Point(x, y)
        elif fam == "MultiPoint":
            npts = struct.unpack("<i", rec[36:40])[0]
            pts = struct.unpack("<" + "dd" * npts, rec[40:40 + 16 * npts])
            yield MultiPoint([(pts[2 * i], pts[2 * i + 1]) for i in range(npts)])
        else:
            nparts, npts = struct.unpack("<ii", rec[36:44])
            parts = struct.unpack("<" + "i" * nparts, rec[44:44 + 4 * nparts])
            off = 44 + 4 * nparts
            pts = struct.unpack("<" + "dd" * npts, rec[off:off + 16 * npts])
            coords = [(pts[2 * i], pts[2 * i + 1]) for i in range(npts)]
            bounds = list(parts) + [npts]
            pieces = [coords[bounds[i]:bounds[i + 1]] for i in range(nparts)]
            if fam == "PolyLine":
                lines = [p for p in pieces if len(p) >= 2]
                yield (None if not lines else LineString(lines[0]) if len(lines) == 1
                       else MultiLineString(lines))
            else:
                yield _polygon_from_rings(pieces)


def _read_dbf(data: bytes, encoding: str):
    """Field names and one dict per record from a .dbf (dBase III/IV)."""
    if len(data) < 32:
        return [], []
    nrec = struct.unpack("<I", data[4:8])[0]
    hlen, rlen = struct.unpack("<HH", data[8:12])
    fields = []
    pos = 32
    while pos + 32 <= hlen and data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\x00")[0].decode("latin-1")
        ftype = chr(data[pos + 11])
        flen = data[pos + 16]
        fdec = data[pos + 17]
        fields.append((name, ftype, flen, fdec))
        pos += 32
    rows = []
    pos = hlen
    for _ in range(nrec):
        if pos + rlen > len(data):
            break
        rec = data[pos:pos + rlen]
        pos += rlen
        if rec[:1] == b"*":              # deleted
            rows.append(None)
            continue
        off = 1
        row = {}
        for name, ftype, flen, fdec in fields:
            cell = rec[off:off + flen]
            off += flen
            if ftype in ("N", "F"):
                t = cell.strip()
                try:
                    row[name] = float(t) if (b"." in t or fdec) else int(t)
                except ValueError:
                    row[name] = None
            elif ftype == "L":
                row[name] = cell[:1] in (b"T", b"t", b"Y", b"y")
            elif ftype == "D":
                t = cell.decode("ascii", "replace").strip()
                row[name] = f"{t[:4]}-{t[4:6]}-{t[6:8]}" if len(t) == 8 else (t or None)
            else:
                row[name] = cell.decode(encoding, "replace").rstrip("\x00 ").strip() or None
        rows.append(row)
    return [f[0] for f in fields], rows


def _read_shapefile_zip(raw: bytes, layer_name: str | None):
    zf = zipfile.ZipFile(io.BytesIO(raw))
    names = [n for n in zf.namelist() if not n.endswith("/")]
    shps = [n for n in names if n.lower().endswith(".shp")]
    if not shps:
        raise ValueError("the zip holds no .shp — a zipped shapefile needs .shp, .shx, .dbf "
                         "(and .prj for its CRS)")
    if layer_name:
        pick = [n for n in shps if os.path.splitext(os.path.basename(n))[0] == layer_name]
        if not pick:
            raise ValueError(f"no shapefile named {layer_name!r} in the zip; it holds: "
                             + ", ".join(os.path.splitext(os.path.basename(n))[0] for n in shps))
        shp = pick[0]
    elif len(shps) > 1:
        raise ValueError("the zip holds several shapefiles (" + ", ".join(
            os.path.splitext(os.path.basename(n))[0] for n in shps)
            + ") — name one with layer_name")
    else:
        shp = shps[0]
    base = shp[:-4]
    def side(ext):
        for n in names:
            if n.lower() == (base + ext).lower():
                return zf.read(n)
        return None
    dbf, prj, cpg = side(".dbf"), side(".prj"), side(".cpg")
    encoding = (cpg.decode("ascii", "replace").strip() if cpg else "latin-1") or "latin-1"
    try:
        "x".encode(encoding)
    except LookupError:
        encoding = "latin-1"
    geoms = list(_read_shp_records(zf.read(shp)))
    fields, rows = _read_dbf(dbf, encoding) if dbf else ([], [])
    props = [(rows[i] if i < len(rows) and rows[i] is not None else {}) for i in range(len(geoms))]
    crs = _crs_from(prj.decode("utf-8", "replace")) if prj else None
    label = f"shapefile {os.path.basename(base)} (.dbf text decoded as {encoding}" + (
        "; .prj CRS" if prj else "; no .prj") + ")"
    notes = []
    return _finish([mapping(g) if g is not None else None for g in geoms], props, crs, notes, label)


# ------------------------------------------------------------------ GeoPackage
def _gpkg_geometry(blob: bytes):
    """The WKB inside a GeoPackage geometry blob (header: magic, version, flags,
    srs_id, optional envelope)."""
    if not blob or blob[:2] != b"GP":
        return None
    flags = blob[3]
    env_code = (flags >> 1) & 0x07
    env_len = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(env_code, 0)
    start = 8 + env_len
    if flags & 0x20:            # empty geometry flag
        return None
    return _wkb.loads(blob[start:])


def _read_gpkg(raw: bytes, layer_name: str | None):
    fd, path = tempfile.mkstemp(suffix=".gpkg")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            tables = con.execute(
                "select c.table_name, g.column_name, g.srs_id from gpkg_contents c "
                "join gpkg_geometry_columns g on g.table_name = c.table_name "
                "where c.data_type = 'features' order by c.table_name").fetchall()
            if not tables:
                raise ValueError("the GeoPackage holds no feature layer (gpkg_contents has no "
                                 "data_type = 'features' row)")
            if layer_name:
                pick = [t for t in tables if t[0] == layer_name]
                if not pick:
                    raise ValueError(f"no feature layer named {layer_name!r} in the GeoPackage; it "
                                     "holds: " + ", ".join(t[0] for t in tables))
                table, gcol, srs = pick[0]
            elif len(tables) > 1:
                raise ValueError("the GeoPackage holds several feature layers (" + ", ".join(
                    t[0] for t in tables) + ") — name one with layer_name")
            else:
                table, gcol, srs = tables[0]
            row = con.execute("select definition, organization, organization_coordsys_id "
                              "from gpkg_spatial_ref_sys where srs_id = ?", (srs,)).fetchone()
            crs = None
            if row:
                definition, org, code = row
                if str(org or "").upper() == "EPSG" and code:
                    crs = _crs_from(int(code))
                elif definition and str(definition).strip().lower() not in ("undefined", ""):
                    crs = _crs_from(definition)
            cols = [r[1] for r in con.execute(f'pragma table_info("{table}")').fetchall()]
            others = [c for c in cols if c != gcol]
            sel = ", ".join(f'"{c}"' for c in [gcol] + others)
            geoms, props = [], []
            for rec in con.execute(f'select {sel} from "{table}"'):
                g = _gpkg_geometry(rec[0])
                geoms.append(mapping(g) if g is not None and not g.is_empty else None)
                props.append({c: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                              for c, v in zip(others, rec[1:])})
        finally:
            con.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return _finish(geoms, props, crs, [], f"GeoPackage layer {table}")
