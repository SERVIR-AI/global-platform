"""The vector clip's readers: a layer read from disk feature by feature, keeping those
that intersect a lon/lat box — never the whole file in memory.

A GeoJSON is read one feature at a time (its text kept as it is); a shapefile one record
at a time, the kept records copied byte for byte into a smaller shapefile; a GeoPackage
through its spatial index, the kept rows copied as they are into a smaller GeoPackage.
Nothing kept is changed: the gate's own reader (graph/geo/vecio) then converts the result
exactly as it would the file a contributor cut by hand — CRS, encoding, repairs and all.
"""

from __future__ import annotations

import codecs
import json
import os
import sqlite3
import struct
import zipfile

from shapely import get_num_coordinates
from shapely.geometry import box as _box, mapping, shape

from ...graph.geo import vecio
from . import PrepRefused

GZIP_MAGIC = b"\x1f\x8b"
# a shapefile's parts the gate reads; a spatial index (.sbn, .qix) would be stale once cut
SHP_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".cst")
_DEC = json.JSONDecoder()
_WS = " \t\r\n"


# --------------------------------------------------------------------------- the box

class Box:
    """A [minLon, minLat, maxLon, maxLat] box and the tests against it, in any CRS."""

    def __init__(self, bbox):
        self.bbox = [float(v) for v in bbox]
        self.poly = _box(*self.bbox)
        import shapely
        shapely.prepare(self.poly)
        self._native: dict = {}

    def overlaps(self, env) -> bool:
        w, s, e, n = self.bbox
        return not (env[2] < w or env[0] > e or env[3] < s or env[1] > n)

    def native(self, crs):
        """The box's envelope in `crs`, padded; None when `crs` is lon/lat (the box
        itself) or the box does not transform into it (then nothing is pre-filtered)."""
        if crs is None or vecio._is_wgs84(crs):
            return None
        key = crs.to_string()
        if key not in self._native:
            import math
            from rasterio.warp import transform_bounds
            try:
                b = transform_bounds("EPSG:4326", crs, *self.bbox, densify_pts=101)
                pad = 0.01 * max(b[2] - b[0], b[3] - b[1])
                self._native[key] = (None if not all(math.isfinite(v) for v in b)
                                     else (b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad))
            except Exception:
                self._native[key] = None
        return self._native[key]

    def candidate(self, env, crs) -> bool:
        """Can a feature with this envelope (in `crs`) touch the box?"""
        if crs is None or vecio._is_wgs84(crs):
            return self.overlaps(env)
        nb = self.native(crs)
        return nb is None or not (env[2] < nb[0] or env[0] > nb[2] or env[3] < nb[1] or env[1] > nb[3])

    def hits(self, geom, crs) -> bool:
        """Does the shapely geometry (in `crs`) intersect the box? An invalid geometry
        GEOS cannot test is kept when its envelope touches the box (the gate repairs it)."""
        if geom is None or geom.is_empty:
            return False
        if crs is not None and not vecio._is_wgs84(crs):
            from rasterio.warp import transform_bounds, transform_geom
            try:
                if not self.overlaps(transform_bounds(crs, "EPSG:4326", *geom.bounds,
                                                      densify_pts=21)):
                    return False
                geom = shape(transform_geom(crs, "EPSG:4326", mapping(geom)))
            except Exception:
                return True                  # cannot place it here: the gate's reader will
        elif not self.overlaps(geom.bounds):
            return False
        try:
            return bool(self.poly.intersects(geom))
        except Exception:
            return True


def vertices(coords) -> int:
    """How many positions a GeoJSON geometry's coordinates hold."""
    if not isinstance(coords, (list, tuple)) or not coords:
        return 0
    if isinstance(coords[0], (int, float)):
        return 1
    return sum(vertices(c) for c in coords)


def geometry_vertices(g) -> int:
    if not isinstance(g, dict):
        return 0
    if g.get("type") == "GeometryCollection":
        return sum(geometry_vertices(x) for x in g.get("geometries") or [])
    return vertices(g.get("coordinates"))


# --------------------------------------------------------------------------- sniffing

def sniff(path: str) -> str:
    """'gzip' | 'zip' | 'gpkg' | 'geojson' | 'unknown', from the file's first bytes."""
    with open(path, "rb") as f:
        head = f.read(4096)
    if head[:2] == GZIP_MAGIC:
        return "gzip"
    if head[:4] == vecio.ZIP_MAGIC:
        return "zip"
    if head[:16] == vecio.SQLITE_MAGIC:
        return "gpkg"
    if head.lstrip().lstrip(b"\xef\xbb\xbf").lstrip()[:1] == b"{":
        return "geojson"
    return "unknown"


def gunzip(ctx, path: str, name: str) -> str:
    """The gzip at `path` decompressed to a file in the workdir (streamed, a MiB at a
    time), under what is left of the input budget and the disk, refused as a bomb when
    it grows over ZIP_RATIO times its packed size."""
    import gzip
    from . import ZIP_RATIO, ZIP_RATIO_FROM, size_text
    inner = name[:-3] if name.lower().endswith(".gz") else name + ".out"
    stem, ext = os.path.splitext(os.path.basename(inner))
    dest = ctx.tmp(ext or ".bin", (stem or "unzipped")[-60:])
    packed = max(1, os.path.getsize(path))
    room = ctx._room()
    total = 0
    try:
        with gzip.open(path, "rb") as src, open(dest, "wb") as out:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > room:
                    raise PrepRefused(f"{name} unpacks to more than {size_text(room)}, more than "
                                      "the platform unpacks to prepare one contribution — give "
                                      "a smaller file (one layer, one area)")
                if total > ZIP_RATIO_FROM and total > ZIP_RATIO * packed:
                    raise PrepRefused(f"{name} unpacks over {ZIP_RATIO} times its packed size — "
                                      "refused as a gzip bomb")
                out.write(chunk)
                ctx.check_time()
    except (OSError, EOFError, zipfile.BadZipFile) as exc:
        raise PrepRefused(f"{name} cannot be decompressed (it is truncated or not gzip: "
                          f"{type(exc).__name__}) — re-upload it and resubmit") from None
    return dest


# --------------------------------------------------------------------------- GeoJSON

class _Reader:
    """A JSON text read from a file a chunk at a time; value() decodes the value at the
    cursor, reading more only as far as that value goes."""

    def __init__(self, path: str, chunk: int = 1 << 20):
        self.f = open(path, "rb")
        self.dec = codecs.getincrementaldecoder("utf-8-sig")()
        self.buf, self.pos, self.eof, self.chunk = "", 0, False, chunk

    def close(self):
        self.f.close()

    def _more(self, n: int) -> bool:
        if self.eof:
            return False
        data = self.f.read(n)
        if self.pos:
            self.buf, self.pos = self.buf[self.pos:], 0
        try:
            self.buf += self.dec.decode(data, final=not data)
        except UnicodeDecodeError:
            raise ValueError("the file is not UTF-8 text") from None
        if not data:
            self.eof = True
        return bool(data)

    def peek(self) -> str:
        while True:
            while self.pos < len(self.buf) and self.buf[self.pos] in _WS:
                self.pos += 1
            if self.pos < len(self.buf):
                return self.buf[self.pos]
            if not self._more(self.chunk):
                return ""

    def expect(self, ch: str) -> None:
        got = self.peek()
        if got != ch:
            raise ValueError(f"expected {ch!r}, found {got or 'the end of the file'!r}")
        self.pos += 1

    def value(self):
        """(value, its text) — the text exactly as the file has it."""
        self.peek()
        n = self.chunk
        while True:
            try:
                obj, end = _DEC.raw_decode(self.buf, self.pos)
                if end < len(self.buf) or self.eof:
                    text = self.buf[self.pos:end]
                    self.pos = end
                    return obj, text
            except json.JSONDecodeError as exc:
                if self.eof:
                    raise ValueError(f"not valid JSON ({exc.msg})") from None
            self._more(n)
            n *= 2


def walk_geojson(path: str, on_member, on_feature, check=None, chunk: int = 1 << 20) -> bool:
    """Read a FeatureCollection: on_member(key, value) for each top-level member but
    `features`, on_feature(feature, text) for each feature, in file order. False when
    it holds no `features` array. Raises ValueError in words when it is not JSON."""
    r = _Reader(path, chunk)
    seen = False
    try:
        if r.peek() != "{":
            raise ValueError("the file is not a JSON object")
        r.pos += 1
        first = True
        while True:
            c = r.peek()
            if c == "}" or c == "":
                break
            if not first:
                r.expect(",")
            first = False
            key, _ = r.value()
            r.expect(":")
            if key == "features" and r.peek() == "[":
                seen = True
                r.pos += 1
                k = 0
                while True:
                    if r.peek() == "]":
                        r.pos += 1
                        break
                    if k:
                        r.expect(",")
                    feat, text = r.value()
                    on_feature(feat, text)
                    k += 1
                    if check is not None and k % 256 == 0:
                        check()
            else:
                val, _ = r.value()
                on_member(key, val)
    finally:
        r.close()
    return seen


class GeoJSONOut:
    """A FeatureCollection written a feature at a time, each feature's text as read.
    `members` (crs, name) may change until the first feature is written."""

    def __init__(self, path: str, members: dict | None = None):
        self.path, self.n, self.members = path, 0, dict(members or {})
        self.f = open(path, "w", encoding="utf-8")
        self._open = False

    def _head(self) -> None:
        if not self._open:
            self._open = True
            self.f.write('{"type": "FeatureCollection"')
            for k, v in self.members.items():
                self.f.write(f", {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}")
            self.f.write(', "features": [')

    def add(self, text: str) -> None:
        self._head()
        self.f.write(("," if self.n else "") + text)
        self.n += 1

    def close(self) -> None:
        if not self.f.closed:
            self._head()
            self.f.write("]}")
            self.f.close()


def truncated(key, value) -> bool:
    """A member that says the server cut the export at its record limit."""
    return (key == "exceededTransferLimit" and value is True) or (
        key == "properties" and isinstance(value, dict) and value.get("exceededTransferLimit") is True)


def filter_geojson(ctx, path: str, box: Box, out_path: str) -> dict:
    """Keep the features of the GeoJSON at `path` that intersect `box`. A `crs` member
    naming another CRS is honoured: the box is tested where the features lie (read
    again when the member comes after them)."""
    seen = {"crs": None, "cut": False, "members": {}}

    def once(crs) -> dict:
        tally = {"read": 0, "kept": 0, "vertices": 0, "crs": crs}
        out = GeoJSONOut(out_path, seen["members"])

        def member(k, v):
            if k == "crs":
                seen["crs"] = v
                seen["members"]["crs"] = v
                if not tally["read"]:             # before the features: test in it
                    tally["crs"] = vecio._geojson_crs({"crs": v})
                    out.members["crs"] = v
            elif k == "name" and isinstance(v, str):
                seen["members"]["name"] = v
                if not tally["read"]:
                    out.members["name"] = v
            if truncated(k, v):
                seen["cut"] = True

        def feature(ft, text):
            tally["read"] += 1
            g = ft.get("geometry") if isinstance(ft, dict) else None
            if not vecio._located(g):
                return
            try:
                geom = shape(g)
            except Exception:
                return
            if box.hits(geom, tally["crs"]):
                tally["kept"] += 1
                tally["vertices"] += int(get_num_coordinates(geom))
                if tally["kept"] % 1024 == 0:
                    ctx.check_vertices(tally["vertices"], "the features kept")
                out.add(text)
        try:
            if not walk_geojson(path, member, feature, ctx.check_time):
                raise ValueError("it holds no `features` array — the clip reads a GeoJSON "
                                 "FeatureCollection")
        finally:
            out.close()
        ctx.check_vertices(tally["vertices"], "the features kept")
        return tally

    try:
        tally = once(None)
        late = vecio._geojson_crs({"crs": seen["crs"]}) if seen["crs"] is not None else None
        if late is not None and tally["crs"] is None:
            tally = once(late)
    except ValueError as exc:
        raise PrepRefused(f"{ctx.manifest.get('url')} could not be read as GeoJSON: {exc}") from None
    tally.pop("crs", None)
    return {**tally, "truncated": seen["cut"]}


# --------------------------------------------------------------------------- shapefile

def pick_shapefile(files: dict, layer_name: str | None, quoted: str) -> str:
    """The .shp member to read, by layer_name (the file's stem) or as the only one —
    the gate reader's own rule (graph/geo/vecio)."""
    shps = sorted(n for n in files if n.lower().endswith(".shp"))
    stems = [os.path.splitext(os.path.basename(n))[0] for n in shps]
    if layer_name:
        pick = [n for n, s in zip(shps, stems) if s == layer_name]
        if not pick:
            raise PrepRefused(f"no shapefile named {layer_name!r} in the zip at {quoted}; it "
                              f"holds: {', '.join(stems)} — set layer_name to one of them")
        return pick[0]
    if len(shps) > 1:
        raise PrepRefused(f"the zip at {quoted} holds several shapefiles ({', '.join(stems)}) "
                          "— name the one to contribute with layer_name")
    return shps[0]


def _sidecar(files: dict, shp: str, ext: str) -> str | None:
    want = (shp[:-4] + ext).lower()
    for n, p in files.items():
        if n.lower() == want:
            return p
    return None


def _shape_record(content: bytes):
    """(envelope, vertices) of one .shp record's content; None for a null shape."""
    if len(content) < 4:
        return None
    stype = struct.unpack("<i", content[:4])[0]
    fam = vecio._SHAPE_TYPES.get(stype)
    if fam is None:
        return None
    if fam == "Point":
        if len(content) < 20:
            return None
        x, y = struct.unpack("<dd", content[4:20])
        return (x, y, x, y), 1
    if len(content) < 40:
        return None
    env = struct.unpack("<4d", content[4:36])
    if fam == "MultiPoint":
        return env, struct.unpack("<i", content[36:40])[0]
    if len(content) < 44:
        return None
    return env, struct.unpack("<ii", content[36:44])[1]


def shapefile(ctx, files: dict, shp: str, box: Box | None, out_zip: str) -> dict:
    """A zip holding the shapefile `shp` (with its .shx, .dbf, .prj, .cpg, .cst) — every
    record when `box` is None, else the records intersecting it, copied byte for byte."""
    stem = os.path.splitext(os.path.basename(shp))[0]
    parts = {ext: _sidecar(files, shp, ext) for ext in SHP_PARTS}
    parts[".shp"] = files[shp]
    crs = None
    if parts[".prj"]:
        try:
            with open(parts[".prj"], "rb") as f:
                crs = vecio._crs_from(f.read().decode("utf-8", "replace"))
        except Exception:
            crs = None                  # the gate's reader says what is wrong with it
    total = (max(0, os.path.getsize(parts[".shx"]) - 100) // 8) if parts[".shx"] else None
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True,
                         compresslevel=1) as z:
        if box is None:
            for ext, p in parts.items():
                if p:
                    z.write(p, stem + ext)
                    ctx.check_time()
            return {"read": total, "kept": total, "member": shp}
        tally = _filter_records(ctx, parts, box, crs, z, stem)
        for ext in (".prj", ".cpg", ".cst"):
            if parts[ext]:
                z.write(parts[ext], stem + ext)
    return {**tally, "member": shp}


def _filter_records(ctx, parts: dict, box: Box, crs, z, stem: str) -> dict:
    work = ctx.tmp("", f"{stem}-kept")
    os.makedirs(work)
    out_shp, out_shx, out_dbf = (os.path.join(work, stem + e) for e in (".shp", ".shx", ".dbf"))
    read = kept = verts = 0
    words = 50                                   # the 100-byte header, in 16-bit words
    env_all = None
    dbf = open(parts[".dbf"], "rb") if parts[".dbf"] else None
    try:
        if dbf is not None:
            dhead = dbf.read(32)
            hlen, rlen = struct.unpack("<HH", dhead[8:12]) if len(dhead) >= 12 else (0, 0)
            dbf.seek(0)
            dhead = dbf.read(hlen)
        with open(parts[".shp"], "rb") as src, open(out_shp, "wb") as shp_o, \
                open(out_shx, "wb") as shx_o, open(out_dbf, "wb") as dbf_o:
            head = src.read(100)
            if len(head) < 100 or struct.unpack(">i", head[:4])[0] != 9994:
                raise PrepRefused(f"{stem}.shp is not a shapefile (its header is not one)")
            shp_o.write(head)
            shx_o.write(head)
            if dbf is not None:
                dbf_o.write(dhead)
            rows = 0
            while True:
                rh = src.read(8)
                if len(rh) < 8:
                    break
                _num, clen = struct.unpack(">ii", rh)
                content = src.read(clen * 2)
                if len(content) < clen * 2:
                    break
                idx = read
                read += 1
                if read % 4096 == 0:
                    ctx.check_time()
                rec = _shape_record(content)
                if rec is None or not box.candidate(rec[0], crs):
                    continue
                env, n = rec
                if not (crs is None or vecio._is_wgs84(crs)) or n > 1:
                    data = b"\0" * 100 + struct.pack(">ii", 1, clen) + content
                    geom = next(vecio._read_shp_records(data), None)
                    if not box.hits(geom, crs):
                        continue
                kept += 1
                verts += n
                if kept % 1024 == 0:
                    ctx.check_vertices(verts, "the features kept")
                shp_o.write(struct.pack(">ii", kept, clen) + content)
                shx_o.write(struct.pack(">ii", words, clen))
                words += 4 + clen
                env_all = env if env_all is None else (min(env_all[0], env[0]), min(env_all[1], env[1]),
                                                       max(env_all[2], env[2]), max(env_all[3], env[3]))
                if dbf is not None and rlen:
                    dbf.seek(hlen + idx * rlen)
                    row = dbf.read(rlen)
                    if len(row) == rlen and rows == kept - 1:
                        dbf_o.write(row)
                        rows += 1
            ctx.check_vertices(verts, "the features kept")
            if dbf is not None:
                dbf_o.write(b"\x1a")
        _patch_shp(out_shp, words, env_all)
        _patch_shp(out_shx, 50 + 4 * kept, env_all)
        if dbf is not None:
            with open(out_dbf, "r+b") as f:
                f.seek(4)
                f.write(struct.pack("<I", rows))
    finally:
        if dbf is not None:
            dbf.close()
    for p, ext in ((out_shp, ".shp"), (out_shx, ".shx"), (out_dbf, ".dbf")):
        if ext != ".dbf" or dbf is not None:
            z.write(p, stem + ext)
    return {"read": read, "kept": kept, "vertices": verts}


def _patch_shp(path: str, words: int, env) -> None:
    with open(path, "r+b") as f:
        f.seek(24)
        f.write(struct.pack(">i", words))
        if env is not None:
            f.seek(36)
            f.write(struct.pack("<4d", *env))


# --------------------------------------------------------------------------- GeoPackage

def _gpkg_tables(con) -> list:
    return con.execute(
        "select c.table_name, g.column_name, g.srs_id from gpkg_contents c "
        "join gpkg_geometry_columns g on g.table_name = c.table_name "
        "where c.data_type = 'features' order by c.table_name").fetchall()


def _gpkg_crs(con, srs):
    row = con.execute("select definition, organization, organization_coordsys_id "
                      "from gpkg_spatial_ref_sys where srs_id = ?", (srs,)).fetchone()
    if not row:
        return None
    definition, org, code = row
    try:
        if str(org or "").upper() == "EPSG" and code:
            return vecio._crs_from(int(code))
        if definition and str(definition).strip().lower() not in ("undefined", ""):
            return vecio._crs_from(definition)
    except Exception:
        return None
    return None


def _blob_envelope(blob) -> tuple | None:
    """(minx, miny, maxx, maxy) from a GeoPackage geometry blob's header, when it has one."""
    if not blob or blob[:2] != b"GP" or len(blob) < 40:
        return None
    flags = blob[3]
    if not (flags >> 1) & 0x07:
        return None
    minx, maxx, miny, maxy = struct.unpack(("<" if flags & 1 else ">") + "4d", blob[8:40])
    return minx, miny, maxx, maxy


def geopackage(ctx, path: str, layer_name: str | None, box: Box | None, out_path: str) -> dict:
    """A GeoPackage holding one feature table of the one at `path` — every row when `box`
    is None, else the rows whose geometry intersects it (through the table's R-tree when
    it has one), copied as they are."""
    quoted = ctx.manifest.get("url")
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    out = None
    try:
        try:
            tables = _gpkg_tables(con)
        except sqlite3.DatabaseError as exc:
            raise PrepRefused(f"{quoted} is not a readable GeoPackage ({exc})") from None
        if not tables:
            raise PrepRefused(f"the GeoPackage at {quoted} holds no feature table")
        if layer_name:
            pick = [t for t in tables if t[0] == layer_name]
            if not pick:
                raise PrepRefused(f"no feature table named {layer_name!r} in the GeoPackage at "
                                  f"{quoted}; it holds: {', '.join(t[0] for t in tables)} — set "
                                  "layer_name to one of them")
            table, gcol, srs = pick[0]
        elif len(tables) > 1:
            raise PrepRefused(f"the GeoPackage at {quoted} holds several feature tables ("
                              + ", ".join(t[0] for t in tables) + ") — name the one to "
                              "contribute with layer_name")
        else:
            table, gcol, srs = tables[0]
        crs = _gpkg_crs(con, srs)
        cols = con.execute(f'pragma table_info("{table}")').fetchall()
        names = [c[1] for c in cols]
        gi = names.index(gcol)
        pk = next((c[1] for c in cols if c[5] == 1), None)
        total = con.execute(f'select count(*) from "{table}"').fetchone()[0]
        out = sqlite3.connect(out_path)
        for meta in ("gpkg_spatial_ref_sys", "gpkg_contents", "gpkg_geometry_columns"):
            sql = con.execute("select sql from sqlite_master where type = 'table' and name = ?",
                              (meta,)).fetchone()
            out.execute(sql[0])
        out.executemany("insert into gpkg_spatial_ref_sys values (" + ",".join(
            "?" * len(con.execute("select * from gpkg_spatial_ref_sys limit 1").description))
            + ")", con.execute("select * from gpkg_spatial_ref_sys"))
        for meta in ("gpkg_contents", "gpkg_geometry_columns"):
            rows = con.execute(f"select * from {meta} where table_name = ?", (table,)).fetchall()
            if rows:
                out.executemany(f"insert into {meta} values ({','.join('?' * len(rows[0]))})", rows)
        out.execute(con.execute("select sql from sqlite_master where type = 'table' and name = ?",
                                (table,)).fetchone()[0])
        rtree = f"rtree_{table}_{gcol}"
        has_rtree = bool(con.execute("select 1 from sqlite_master where name = ?",
                                     (rtree,)).fetchone())
        nb = None
        if box is not None:
            nb = box.bbox if (crs is None or vecio._is_wgs84(crs)) else box.native(crs)
        if box is not None and nb is not None and has_rtree and pk:
            rows = con.execute(f'select * from "{table}" where "{pk}" in (select id from "{rtree}" '
                               "where maxx >= ? and minx <= ? and maxy >= ? and miny <= ?)",
                               (nb[0], nb[2], nb[1], nb[3]))
            how = "its spatial index"
        else:
            rows = con.execute(f'select * from "{table}"')
            how = "every row" if box is not None else None
        ins = f'insert into "{table}" values ({",".join("?" * len(names))})'
        kept = verts = seen = 0
        batch = []
        for row in rows:
            seen += 1
            if seen % 2048 == 0:
                ctx.check_time()
            if box is not None:
                blob = row[gi]
                env = _blob_envelope(blob)
                if env is not None and not box.candidate(env, crs):
                    continue
                try:
                    geom = vecio._gpkg_geometry(blob)
                except Exception:
                    geom = None
                if not box.hits(geom, crs):
                    continue
                verts += int(get_num_coordinates(geom))
            kept += 1
            batch.append(row)
            if len(batch) >= 2048:
                ctx.check_vertices(verts, "the features kept")
                out.executemany(ins, batch)
                batch = []
        if batch:
            out.executemany(ins, batch)
        ctx.check_vertices(verts, "the features kept")
        out.execute("pragma application_id = 1196444487")      # 'GPKG'
        out.execute("pragma user_version = 10200")
        out.commit()
        return {"read": total, "kept": kept, "vertices": verts, "member": table,
                **({"searched": how} if how else {})}
    finally:
        con.close()
        if out is not None:
            out.close()
