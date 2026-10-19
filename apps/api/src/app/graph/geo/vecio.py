"""The vector formats a hub actually hands over, read into GeoJSON on EPSG:4326.

GeoJSON is what the platform stores and measures. A hub's files are a zipped
shapefile or a GeoPackage, usually in a projected CRS (UTM, CRTM05). Rather than
asking every contributor to export first, the gate reads these here — a small
shapefile reader (the .shp record format, the .dbf table, the .prj CRS), a
GeoPackage reader on sqlite3 (the GeoPackage binary header, then WKB) — and
reprojects through PROJ (rasterio.warp) when the source is not geographic WGS 84.
A GeoJSON that declares another CRS (an ArcGIS Hub export in EPSG:3857) takes the
same path. No GDAL command line, no fiona: nothing a deployed host does not already
have.

Every conversion says what it did (`notes`): which layer, which CRS, how many
features, what was dropped or repaired — the contributor's record carries it.
"""

from __future__ import annotations

import codecs
import io
import json
import os
import re
import sqlite3
import struct
import tempfile
import zipfile

from shapely import wkb as _wkb
from shapely.geometry import LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon, mapping, shape

ZIP_MAGIC = b"PK\x03\x04"
SQLITE_MAGIC = b"SQLite format 3\x00"
ROUND = 7                 # decimals kept on output coordinates (1.1 cm at the equator): 6 (11 cm)
                          # lengthened dense site-scale lines (0.3 m vertex spacing) by over 1%


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
    done. A GeoJSON that is already what the platform stores passes through untouched."""
    kind = kind_of(raw, filename)
    if kind == "geojson":
        return _read_geojson(raw)
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


def _crs_label(crs) -> str:
    try:
        return f"EPSG:{crs.to_epsg()}" if crs.to_epsg() else crs.to_string()[:80]
    except Exception:
        return "the file's CRS"


def _lonlat_bounds(geoms) -> tuple | None:
    """(minx, miny, maxx, maxy) over shapely geometries when any coordinate falls outside
    lon/lat range, else None."""
    bx = [g.bounds for g in geoms if g is not None and not g.is_empty]
    if not bx:
        return None
    minx, miny = min(b[0] for b in bx), min(b[1] for b in bx)
    maxx, maxy = max(b[2] for b in bx), max(b[3] for b in bx)
    if -180 <= minx and maxx <= 180 and -90 <= miny and maxy <= 90:
        return None
    return minx, miny, maxx, maxy


def _located(g) -> bool:
    """A geometry that places the feature somewhere — not null, not empty. (A type the
    platform does not take still counts: inspect() refuses it in words.)"""
    if not isinstance(g, dict) or not g.get("type"):
        return False
    return bool(g.get("geometries") if g["type"] == "GeometryCollection" else g.get("coordinates"))


def _nesting(polys: list) -> list:
    """For polygons sorted largest first, [(depth, holder)]: how deep each lies (0
    inside none of the others, 1 inside one, 2 inside one that is inside another...)
    and the index of the smallest one holding it. Held means every vertex of its
    outer ring covered by the holder's ground — a polygon in another's hole is not
    inside it, nor is one that only overlaps it. As GDAL's organizePolygons reads."""
    import shapely
    tree = shapely.STRtree(polys) if len(polys) > 32 else None
    depth, parent = [0] * len(polys), [None] * len(polys)
    for i in range(1, len(polys)):
        minx, miny, maxx, maxy = polys[i].bounds
        cands = (sorted((int(j) for j in tree.query(polys[i]) if j < i), reverse=True)
                 if tree is not None else range(i - 1, -1, -1))
        pts = None
        for j in cands:              # the next larger first: the first that holds it is the nearest
            bx = polys[j].bounds
            if bx[0] > minx or bx[1] > miny or bx[2] < maxx or bx[3] < maxy:
                continue
            if pts is None:
                pts = shapely.points(list(polys[i].exterior.coords))
            try:
                inside = bool(shapely.covers(polys[j], pts).all())
            except Exception:
                inside = False
            if inside:
                parent[i], depth[i] = j, depth[j] + 1
                break
    return list(zip(depth, parent))


def _nest_parts(geom):
    """A MultiPolygon whose parts lie wholly inside other parts, re-read as holes in
    them — how an export that wrote each hole as a separate part (an ArcGIS Hub
    GeoJSON does) meant it; a part inside such a hole is an island again. Parts that
    merely overlap are left as they are (unioned by the repair)."""
    parts = sorted((p for p in getattr(geom, "geoms", []) if p.geom_type == "Polygon" and not p.is_empty),
                   key=lambda p: -Polygon(p.exterior).area)
    if len(parts) < 2:
        return geom
    nest = _nesting(parts)
    if all(d == 0 for d, _ in nest):
        return geom
    polys = []
    for i, p in enumerate(parts):
        if nest[i][0] % 2 == 0:
            holes = list(p.interiors) + [parts[k].exterior for k, (d, up) in enumerate(nest)
                                         if up == i and d % 2]
            polys.append(Polygon(p.exterior, holes))
        else:
            polys.extend(Polygon(r) for r in p.interiors)
    return MultiPolygon(polys)


def _ring_area(ring) -> list:
    """The polygons one ring encloses. A ring that crosses or touches itself is read
    even-odd, on its own: the ground it walks around (a raster-traced shell's
    'inverted holes') stays out, both lobes of a bow-tie stay in."""
    from shapely import make_valid
    p = Polygon(ring)
    return _polygons(p if p.is_valid else make_valid(p))


def _polygons(g) -> list:
    """The Polygons with area in any geometry, collections and Multi forms flattened."""
    if g.geom_type == "Polygon":
        return [g] if g.area > 0 else []
    return [q for sub in getattr(g, "geoms", ()) for q in _polygons(sub)]


def repair(geom):
    """A valid (Multi)Polygon for an invalid one, or None when nothing with area is
    left. A hub's GIS (or a reprojection, or rounding) leaves self-intersecting rings,
    parts that overlap, holes written as parts; GEOS refuses to clip any of them.
    Parts drawn wholly inside other parts become their holes (`_nest_parts`); each
    ring is made valid on its own (`_ring_area`); each part is its shell less its
    holes; the parts are unioned, so overlapping parts count their shared ground once.
    (make_valid's own methods either drop that shared ground or fill a traced shell's
    inverted holes: the Kenya mangrove map read 2% over its producer's areas.)"""
    from shapely.ops import unary_union
    try:
        if geom.geom_type == "MultiPolygon":
            geom = _nest_parts(geom)
        if geom.is_valid:
            fixed = geom
        else:
            pieces = []
            for part in getattr(geom, "geoms", None) or [geom]:
                if part.geom_type != "Polygon" or part.is_empty:
                    continue
                shell = unary_union(_ring_area(part.exterior))
                holes = [q for r in part.interiors for q in _ring_area(r)]
                pieces.append(shell.difference(unary_union(holes)) if holes else shell)
            fixed = unary_union(pieces)
        if fixed.geom_type == "GeometryCollection":
            fixed = unary_union(_polygons(fixed))
    except Exception:
        return None
    if fixed.is_empty or fixed.area <= 0 or fixed.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    return fixed


def _repair(g: dict):
    """(geometry dict, 'ok' | 'repaired' | 'collapsed') for one polygon: repaired once at
    ingest, so the stored layer is valid. A polygon that collapses to no area is
    reported, never kept as a zero."""
    try:
        geom = shape(g)
        if geom.is_valid:
            return g, "ok"
    except Exception:
        return None, "collapsed"
    fixed = repair(geom)
    return (mapping(fixed), "repaired") if fixed is not None else (None, "collapsed")


def _clean(features: list, notes: list) -> tuple[list, bool]:
    """Features with no geometry dropped; invalid polygons repaired (or dropped when
    nothing is left of them) — each said in `notes`, counted over the whole file.
    Returns (features, whether anything changed)."""
    out, empty, repaired, collapsed = [], 0, 0, 0
    for ft in features:
        g = ft.get("geometry")
        if not _located(g):
            empty += 1
            continue
        if g.get("type") in ("Polygon", "MultiPolygon"):
            fixed, how = _repair(g)
            if how == "collapsed":
                collapsed += 1
                continue
            if how == "repaired":
                repaired += 1
                ft = {**ft, "geometry": fixed}
        out.append(ft)
    if empty:
        notes.append(f"{empty:,} feature(s) without geometry were dropped")
    if repaired:
        notes.append(f"{repaired:,} invalid polygon(s) (self-intersecting rings, overlapping parts, "
                     "holes written as parts) were repaired: shells unioned, holes subtracted")
    if collapsed:
        notes.append(f"{collapsed:,} invalid polygon(s) collapsed to no area when repaired and "
                     "were dropped")
    return out, bool(empty or repaired or collapsed)


def losses(notes) -> list[str]:
    """The ingest notes that say features were dropped or repaired — what a measure
    of the layer must repeat, since the stored file no longer holds them as given."""
    return [n for n in notes or [] if n.endswith(("were dropped", "holes subtracted"))]


def _finish(geoms, props, crs, notes, source_label):
    """Geometries (GeoJSON dicts or None) + properties -> a FeatureCollection on
    EPSG:4326, reprojected when needed, coordinates rounded, empties dropped, invalid
    polygons repaired."""
    if crs is not None and not _is_wgs84(crs):
        geoms = _reproject(geoms, crs)
        notes.append(f"reprojected from {_crs_label(crs)} to EPSG:4326 at ingest")
    elif crs is None:
        notes.append("the file declares no CRS; its coordinates were read as lon/lat (EPSG:4326)")
    feats = [{"type": "Feature", "properties": p, "geometry": _round(g) if g else None}
             for g, p in zip(geoms, props)]
    total = len(feats)
    feats, _changed = _clean(feats, notes)
    notes.insert(0, f"read {len(feats):,} feature(s) from {source_label}"
                 + (f" ({total:,} in the file)" if total != len(feats) else ""))
    return {"type": "FeatureCollection", "features": feats}, notes


# ------------------------------------------------------------------ GeoJSON
_TRUNCATED = ("the server truncated this export at {n:,} features (exceededTransferLimit) — use "
              "the portal's full download (e.g. the Hub 'Download' GeoJSON/shapefile) or page the query")


def _geojson_crs(fc: dict):
    """The CRS a GeoJSON's (pre-RFC 7946) `crs` member names, or None for WGS 84 lon/lat
    or no member. Raises ValueError in words when the name cannot be read."""
    crs = fc.get("crs")
    if not isinstance(crs, dict):
        return None
    name = str((crs.get("properties") or {}).get("name") or "").strip()
    if not name or any(tag in name.upper() for tag in ("CRS84", "WGS84", "WGS 84")) \
            or re.search(r"(?<!\d)4326$", name):
        return None
    from rasterio.crs import CRS
    code = re.search(r"EPSG:+(\d+)$", name, re.I)
    # An ArcGIS export may say EPSG:102100 for what is ESRI:102100 (Web Mercator).
    tries = ((lambda: CRS.from_epsg(int(code.group(1))),
              lambda: CRS.from_user_input(f"ESRI:{code.group(1)}")) if code
             else (lambda: CRS.from_user_input(name),))
    for attempt in tries:
        try:
            return attempt()
        except Exception:
            continue
    raise ValueError(f"the file declares crs {name!r}, which the platform cannot read — export "
                     "it in WGS 84 (EPSG:4326, lon/lat) and resubmit")


def _read_geojson(raw: bytes) -> tuple[bytes, list[str]]:
    """A GeoJSON FeatureCollection as handed over -> (bytes, notes). Untouched when it is
    already what the platform stores; reprojected when its `crs` member names another
    CRS; features without geometry dropped and invalid polygons repaired, each said.
    A truncated server export is declined. Anything not a FeatureCollection passes
    through for inspect() to refuse in words."""
    try:
        fc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return raw, []
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection" \
            or not isinstance(fc.get("features"), list):
        return raw, []
    feats = [ft if isinstance(ft, dict) else {} for ft in fc["features"]]
    if fc.get("exceededTransferLimit") is True \
            or (fc.get("properties") or {}).get("exceededTransferLimit") is True:
        raise ValueError(_TRUNCATED.format(n=len(feats)))
    crs = _geojson_crs(fc)
    notes: list[str] = []
    if crs is not None:
        geoms = _reproject([ft.get("geometry") if _located(ft.get("geometry")) else None
                            for ft in feats], crs)
        feats = [{**ft, "geometry": g} for ft, g in zip(feats, geoms)]
        notes.append(f"reprojected from {_crs_label(crs)} (the file's crs member) to EPSG:4326 at ingest")
    total = len(feats)
    feats, changed = _clean(feats, notes)
    if crs is None and not changed:
        return raw, []
    notes.insert(0, f"read {len(feats):,} feature(s) from GeoJSON"
                 + (f" ({total:,} in the file)" if total != len(feats) else ""))
    out = {k: v for k, v in fc.items() if k not in ("crs", "features")}
    out["features"] = feats
    return json.dumps(out, ensure_ascii=False).encode("utf-8"), notes


# ------------------------------------------------------------------ shapefile
_SHAPE_TYPES = {0: None, 1: "Point", 3: "PolyLine", 5: "Polygon", 8: "MultiPoint",
                11: "Point", 13: "PolyLine", 15: "Polygon", 18: "MultiPoint",
                21: "Point", 23: "PolyLine", 25: "Polygon", 28: "MultiPoint"}


def _ring_is_clockwise(ring) -> bool:
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += (x2 - x1) * (y2 + y1)
    return s > 0


def _polygon_from_rings(rings, tally: dict | None = None):
    """Shapefile rings -> one Polygon or a MultiPolygon, organised by where the rings
    lie (`_nesting`), not by their winding. The format says outer rings run
    clockwise and holes counter-clockwise, but WDPA exports (among others) carry
    counter-clockwise OUTER rings, and trusting the winding dropped them (a 218 km²
    protected zone was stored as 9 km²). So a ring inside no other ring is an outer
    ring whatever its winding, a ring inside an outer ring is a hole of the smallest
    one that holds it, and a ring inside a hole is an island — an outer ring again.
    `tally` counts the rings read against their winding ("rings") and the features
    holding any ("features")."""
    rs = []
    for r in rings:
        if len(r) < 4:
            continue
        poly = Polygon(r)
        # A self-crossing ring is sized by what it encloses (a bow-tie's lobes cancel
        # in the signed area); the repair at ingest makes it valid.
        size = poly.area if poly.is_valid else sum(q.area for q in _ring_area(r))
        if size > 0:
            rs.append((size, r, poly))
    if not rs:
        return None
    rs.sort(key=lambda t: -t[0])
    nest = _nesting([t[2] for t in rs]) if len(rs) > 1 else [(0, None)]
    holes: dict = {}
    for i, (d, up) in enumerate(nest):
        if d % 2:
            holes.setdefault(up, []).append(rs[i][1])
    flipped = sum(1 for i, (d, _) in enumerate(nest) if _ring_is_clockwise(rs[i][1]) == bool(d % 2))
    polys = [Polygon(rs[i][1], holes.get(i, [])) for i, (d, _) in enumerate(nest) if d % 2 == 0]
    if flipped and tally is not None:
        tally["rings"] = tally.get("rings", 0) + flipped
        tally["features"] = tally.get("features", 0) + 1
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def _read_shp_records(data: bytes, tally: dict | None = None):
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
                yield _polygon_from_rings(pieces, tally)


_CODEC_ALIASES = {"88591": "latin-1", "8859_1": "latin-1", "UTF8": "utf-8", "65001": "utf-8",
                  "SYSTEM": None, "OEM": None}


def _codec(name: str) -> str | None:
    """A Python codec for a .cpg / .cst value ('UTF-8', 'ANSI 1252', '1252', '88591'), or
    None when it names none."""
    t = name.strip().strip("\x00").strip()
    u = t.upper().replace("ANSI ", "").replace("ANSI_", "")
    if not t or (u in _CODEC_ALIASES and _CODEC_ALIASES[u] is None):
        return None
    cand = _CODEC_ALIASES.get(u) or (f"cp{u}" if u.isdigit() else t)
    try:
        return codecs.lookup(cand).name
    except LookupError:
        return None


def _decode(cell: bytes, encoding: str, fallback: str | None = None) -> str:
    """One .dbf text cell. A multi-byte character cut by the field width is dropped,
    not shown as a replacement mark; `fallback` reads what `encoding` cannot."""
    try:
        return cell.decode(encoding)
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data" and exc.end == len(cell):
            try:
                return cell[:exc.start].decode(encoding)
            except UnicodeDecodeError:
                pass
        return cell.decode(fallback or encoding, "replace")


def _reads_as_utf8(data: bytes, hlen: int, rlen: int, nrec: int, fields) -> bool:
    """Every text cell of the .dbf is valid UTF-8 (a character cut at a field's end aside)."""
    block = data[hlen:hlen + nrec * rlen]
    if block.isascii():
        return True
    for k in range(nrec):
        rec = block[k * rlen:(k + 1) * rlen]
        off = 1
        for _name, ftype, flen, _dec in fields:
            cell = rec[off:off + flen].rstrip(b"\x00 ")
            off += flen
            if ftype in ("N", "F", "L", "D") or cell.isascii():
                continue
            try:
                cell.decode("utf-8")
            except UnicodeDecodeError as exc:
                if not (exc.reason == "unexpected end of data" and exc.end == len(cell)):
                    return False
    return True


def _read_dbf(data: bytes, encoding: str | None):
    """Field names, one dict per record (None for a record marked deleted) and the
    encoding the text was read in, from a .dbf (dBase III/IV). With no declared
    `encoding`: UTF-8 when every text cell reads as UTF-8, else cp1252."""
    if len(data) < 32:
        return [], [], encoding or "utf-8"
    nrec = struct.unpack("<I", data[4:8])[0]
    hlen, rlen = struct.unpack("<HH", data[8:12])
    raw_fields = []
    pos = 32
    while pos + 32 <= hlen and data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\x00")[0]
        ftype = chr(data[pos + 11])
        flen = data[pos + 16]
        fdec = data[pos + 17]
        raw_fields.append((name, ftype, flen, fdec))
        pos += 32
    fallback = None
    if encoding is None:
        encoding = "utf-8" if _reads_as_utf8(data, hlen, rlen, nrec, raw_fields) else "cp1252"
        fallback = "latin-1" if encoding == "cp1252" else None
    fields = [(_decode(n, encoding, "latin-1"), t, ln, d) for n, t, ln, d in raw_fields]
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
                row[name] = _decode(cell.rstrip(b"\x00 "), encoding, fallback).strip() or None
        rows.append(row)
    return [f[0] for f in fields], rows, encoding


def _read_shapefile_zip(raw: bytes, layer_name: str | None):
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("the zip cannot be read (it is truncated or damaged); re-upload it "
                         "and resubmit") from None
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
    stem = os.path.basename(base)
    def side(ext):
        for n in names:
            if n.lower() == (base + ext).lower():
                return zf.read(n)
        return None
    dbf, prj = side(".dbf"), side(".prj")
    # The .dbf's text encoding: a .cpg (ESRI) or a .cst (GeoServer) names it; with
    # neither, UTF-8 when every value reads as UTF-8, else cp1252 — said either way.
    declared, said = None, None
    for ext in (".cpg", ".cst"):
        val = side(ext)
        if val is None:
            continue
        text = val.decode("ascii", "replace").strip()
        declared = _codec(text)
        said = (f"as its {ext} declares" if declared
                else f"its {ext} names {text!r}, not an encoding the platform knows")
        if declared:
            break
    tally: dict = {}
    geoms = list(_read_shp_records(zf.read(shp), tally))
    fields, rows, encoding = _read_dbf(dbf, declared) if dbf else ([], [], declared or "utf-8")
    if dbf and not declared:
        auto = ("UTF-8, as every text value reads as UTF-8" if encoding == "utf-8"
                else "cp1252 (Windows Latin-1), as the text is not valid UTF-8")
        said = (f"{said}; read as {auto}" if said
                else f"no .cpg or .cst names its encoding; read as {auto}")
    if prj:
        try:
            crs = _crs_from(prj.decode("utf-8", "replace"))
        except Exception as exc:
            raise ValueError(f"the .prj of {stem}.shp could not be read as a coordinate system "
                             f"({type(exc).__name__}) — replace it with the .prj your GIS writes for "
                             "the layer's CRS (or reproject to EPSG:4326)") from None
    else:
        crs = None
        far = _lonlat_bounds(geoms)
        if far:
            raise ValueError(
                f"the zip has no .prj, so its coordinate system is unknown — add the .prj (or "
                f"reproject to EPSG:4326). {stem}.shp's coordinates run x {far[0]:,.0f} to "
                f"{far[2]:,.0f}, y {far[1]:,.0f} to {far[3]:,.0f}: projected units, not lon/lat")
    notes = []
    deleted = sum(1 for i in range(len(geoms)) if i < len(rows) and rows[i] is None)
    if deleted:
        notes.append(f"{deleted:,} record(s) marked deleted in the .dbf were skipped")
    keep = [i for i in range(len(geoms)) if not (i < len(rows) and rows[i] is None)]
    if tally.get("rings"):
        notes.append(f"{tally['rings']:,} ring(s) in {tally['features']:,} feature(s) were re-oriented: "
                     "wound against the shapefile convention (outer rings clockwise, holes "
                     "counter-clockwise), they were read by where they lie, as GDAL does")
    if said and not declared:
        notes.append(f"the .dbf text: {said}")
    label = (f"shapefile {stem} (.dbf text decoded as "
             + ("UTF-8" if encoding == "utf-8" else encoding)
             + (f" {said}" if said and declared else "")
             + ("; .prj CRS" if prj else "; no .prj") + ")")
    return _finish([mapping(geoms[i]) if geoms[i] is not None else None for i in keep],
                   [(rows[i] if i < len(rows) else {}) for i in keep], crs, notes, label)


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
            shapes, props = [], []
            for rec in con.execute(f'select {sel} from "{table}"'):
                g = _gpkg_geometry(rec[0])
                shapes.append(g if g is not None and not g.is_empty else None)
                props.append({c: (v.decode("utf-8", "replace") if isinstance(v, bytes) else v)
                              for c, v in zip(others, rec[1:])})
        finally:
            con.close()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    far = _lonlat_bounds(shapes) if crs is None else None
    if far:
        raise ValueError(
            f"the GeoPackage layer {table} declares no coordinate system (srs_id {srs} is undefined), "
            f"so its coordinates cannot be placed — they run x {far[0]:,.0f} to {far[2]:,.0f}, "
            f"y {far[1]:,.0f} to {far[3]:,.0f}, not lon/lat. Assign the layer its CRS in your GIS "
            "(or reproject to EPSG:4326) and resubmit")
    return _finish([mapping(g) if g is not None else None for g in shapes], props, crs, [],
                   f"GeoPackage layer {table}")
