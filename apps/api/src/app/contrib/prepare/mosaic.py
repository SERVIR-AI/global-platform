"""mosaic: a raster delivered as tiles, joined at ingest into one layer.

The case: a producer publishes a global map as tiles — Global Mangrove Watch as a 66 MB
zip of 1,521 one-degree tiles, GWL_FCS30D as a 2.5 GB zip of 958 five-degree tiles, ETH
canopy height as 400 MB three-degree tiles behind separate links — and a gulf or a
country needs several of them; they were mosaicked by hand. Now the layer goes in from
the producer's own links: `mosaic: 'auto'` with a clip takes every tile of the zip the
clip touches (each candidate's HEADER says where it lies; its name only says which
headers are worth reading), or `mosaic` lists the tiles. Only what is needed is read:
from a zip, each tile's own bytes by ONE pinned range request (fetch_policy's checks; the
tile unpacked here against the zip directory's size and CRC — never the whole zip, and
GDAL never reads a zip over HTTP); from a tile's own link, its window under the clip
(remote_clip.cut, as every clip). The tiles join onto the grid of the tile that covers
most of the result; a tile on another grid is resampled the way the layer's namespace
regrids (conf/raster_schema.yml `regrid`, never a list here). One file is stored.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

NAME = "mosaic"
STEP = "mosaic"
KINDS = ("raster",)
NEEDS_INPUT = False          # it reads its own tiles; the manifest's url is one of them or their zip
FIELDS = {
    "mosaic": "for a raster delivered as TILES (a producer's 1x1- or 5x5-degree tiles in one "
              "zip, or separate links) that must land as ONE layer: 'auto' to take every tile "
              "of the zip at `url` that the `clip` touches — the platform reads each candidate "
              "tile's header to see where it lies, never guessing from its name; `clip` is then "
              "required (ASK the contributor for the area, never guess one) — or the list of "
              "tiles: their links (a link may end '#<member>' for a raster inside a zip), or "
              "members of the zip at `url` as named inside it. Nothing to do by hand: the "
              "platform reads only what the mosaic needs where the host answers range requests "
              "(Zenodo and Google Drive do) — from a zip, each tile's own bytes, never the whole "
              "zip; from a tile's own link, only its cells under `clip` — else the file is "
              "downloaded whole, within the preparation budget; it joins them onto the grid "
              "of the tile that covers most of the result (a tile on another grid is resampled "
              "the way the layer's namespace regrids: nearest for classes, bilinear for amounts; "
              "a count grid is refused) and stores ONE file; the cap applies to it. Cells no "
              "tile covers take the tiles' nodata (or declared.nodata); when the legend names "
              "that value as a class (0: Not mangrove), the file carries no nodata tag and those "
              "cells are that class. `url`: the zip the tiles are in, or for a list of links the "
              "first tile's. The record keeps every tile's link, member and window",
}

MAX_TILES = 64               # tile headers one mosaic reads, and tiles it joins
STRIP_ROWS = 1024            # rows joined at a time at most (a multiple of the 256-cell blocks)
STRIP_CELLS = 1 << 24        # cells joined at a time, about (memory: a few bytes each)
_EPS = 1e-6                  # of a cell: closer than this to a cell edge IS the edge
_SHOWN = 8


# --------------------------------------------------------------------------- tile names

_POS = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?P<a>[NS])(?P<alat>\d{1,2}(?:\.\d+)?)[_-]?(?P<b>[EW])(?P<blon>\d{1,3}(?:\.\d+)?)"
    r"|(?P<c>[EW])(?P<clon>\d{1,3}(?:\.\d+)?)[_-]?(?P<d>[NS])(?P<dlat>\d{1,2}(?:\.\d+)?)"
    r"|(?P<elat>\d{1,2}(?:\.\d+)?)(?P<e>[NS])[_-]?(?P<flon>\d{1,3}(?:\.\d+)?)(?P<f>[EW])"
    r"|(?P<glon>\d{1,3}(?:\.\d+)?)(?P<g>[EW])[_-]?(?P<hlat>\d{1,2}(?:\.\d+)?)(?P<h>[NS])"
    r")(?![0-9])", re.I)


def position(name: str) -> tuple[float, float] | None:
    """(lon, lat) a tile's file name gives ('GMW_N09W083', 'GWL_..._W85N10', 'S03W081',
    '10N_080W') — whichever corner the producer names it by — or None. Only a guide to
    which headers are worth reading; the header says where the tile lies."""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    m = _POS.search(base)
    if not m:
        return None
    g = m.groupdict()
    for lat, lon, ns, ew in (("alat", "blon", "a", "b"), ("dlat", "clon", "d", "c"),
                             ("elat", "flon", "e", "f"), ("hlat", "glon", "h", "g")):
        if g[lat] is not None:
            y = float(g[lat]) * (-1 if g[ns].upper() == "S" else 1)
            x = float(g[lon]) * (-1 if g[ew].upper() == "W" else 1)
            return (x, y) if abs(y) <= 90 and abs(x) <= 180 else None
    return None


def _spacing(values) -> float | None:
    v = sorted(set(values))
    gaps = [b - a for a, b in zip(v, v[1:]) if b - a > _EPS]
    return min(gaps) if gaps else None


def near(names: list[str], box) -> tuple[list[str], list[str]]:
    """(the rasters whose names place them within one tile spacing of `box`, the rasters
    whose names place them nowhere) — the headers an 'auto' mosaic reads. The spacing is
    read from the names themselves (1 degree for GMW, 5 for GWL, 3 for ETH)."""
    pos = {n: position(n) for n in names}
    placed = {n: p for n, p in pos.items() if p}
    dx = _spacing(p[0] for p in placed.values())
    dy = _spacing(p[1] for p in placed.values())
    dx, dy = dx or dy, dy or dx
    if dx is None:                           # fewer than two positions: names guide nothing
        return [], list(names)
    w, s, e, n = box
    hits = [k for k, (x, y) in placed.items()
            if w - dx - _EPS <= x <= e + dx + _EPS and s - dy - _EPS <= y <= n + dy + _EPS]
    return sorted(hits), sorted(k for k in names if not pos[k])


def _is_auto(spec) -> bool:
    return isinstance(spec, str) and spec.strip().lower() == "auto"


def _is_link(item: str) -> bool:
    return "://" in str(item)


def _listed(names) -> str:
    names = list(names)
    return ", ".join(names[:_SHOWN]) + (f" and {len(names) - _SHOWN:,} more"
                                        if len(names) > _SHOWN else "")


# --------------------------------------------------------------------------- validate

def validate(manifest) -> list[str]:
    from .. import fetch_policy, remote_clip as rc
    m = manifest
    spec = m.get("mosaic")
    problems = []
    frag = rc.split_member(m.get("url") or "")[1]
    if _is_auto(spec):
        if m.get("clip") is None:
            problems.append("mosaic: 'auto' takes the tiles of the zip at url that the clip "
                            "touches — give clip: the area to keep, a bbox [minLon, minLat, "
                            "maxLon, maxLat] or a place (ASK the contributor for it; never guess "
                            "one), or list the tiles in mosaic")
        if frag:
            problems.append(f"mosaic: 'auto' reads the zip at url, but url names one member "
                            f"('#{frag}') — give the zip's link without '#{frag}'")
    elif isinstance(spec, (list, tuple)):
        items = [str(i).strip() for i in spec if isinstance(i, str) and str(i).strip()]
        if len(items) != len(spec):
            problems.append(f"mosaic lists the tiles as text — links, or members of the zip at "
                            f"url — got {list(spec)!r}")
        elif len(items) < 2:
            problems.append("a mosaic joins two or more tiles — for one tile give its link as "
                            "url (and clip_member for a raster inside a zip), without mosaic")
        elif len(items) > MAX_TILES:
            problems.append(f"mosaic lists {len(items):,} tiles; the platform joins at most "
                            f"{MAX_TILES} in one layer — give a smaller clip and the tiles it "
                            "needs, or mosaic: 'auto' with the clip")
        dup = sorted({i for i in items if items.count(i) > 1})
        if dup:
            problems.append(f"mosaic lists {_listed(dup)} more than once — list each tile once")
        for item in dict.fromkeys(items):
            if _is_link(item):
                problems += [f"mosaic tile {item}: {p}"
                             for p in fetch_policy.check_url(rc.split_member(item)[0])]
        if frag and any(not _is_link(i) for i in items):
            problems.append(f"mosaic names members of the zip at url, but url names one member "
                            f"('#{frag}') — give the zip's link without '#{frag}'")
    else:
        problems.append(f"mosaic must be 'auto' (with a clip: every tile of the zip at url the "
                        f"clip touches) or the list of tiles — their links, or members of the "
                        f"zip at url — got {spec!r}")
    if m.get("clip_member") is not None:
        problems.append("clip_member names one raster of a zip; mosaic names the tiles — drop "
                        "clip_member (list the members in mosaic)")
    return problems


# --------------------------------------------------------------------------- sources

@dataclass(eq=False)
class _Source:
    """One link the tiles come from: answering range requests (`found`, the probe) or
    downloaded whole (`local`); a zip's members in `infos`."""
    url: str
    found: dict | None = None
    local: str | None = None
    zip: bool = False
    infos: dict = field(default_factory=dict)
    checked: bool = False


@dataclass(eq=False)
class _Tile:
    src: _Source
    member: str | None
    label: str
    file: str | None = None               # the tile as a local file, when it was fetched whole
    sha256: str | None = None
    bytes: int = 0
    driver: str = ""
    crs: object = None
    transform: object = None
    width: int = 0
    height: int = 0
    dtype: str = ""
    nodata: object = None
    colormap: dict | None = None
    lonlat: tuple = ()                    # its bounds in degrees
    notes: list = field(default_factory=list)
    # planned
    aligned: bool = True
    foot: tuple = ()                      # (col0, row0, col1, row1) on the mosaic's grid
    path: str = ""                        # the local GDAL path the join reads
    read: str = ""
    window: list = field(default_factory=list)
    of: list = field(default_factory=list)
    cells: int = 0


def _refuse(text):
    from . import PrepRefused
    return PrepRefused(text)


def _open(ctx, url: str) -> _Source:
    """The link probed through the pinned fetcher. A host that answers range requests is
    read in place (a zip: its directory, then only the members needed); one that does not
    sends the file whole (streamed to disk, under the input budget)."""
    import zipfile
    from .. import rasters, remote_clip as rc
    from . import ZIP_MEMBERS
    found = rc.probe(url)
    said = (f"{url} answered with a web page, not a raster — give the tile's own link"
            if found.get("html") else rc.text_problem(found["head"], url))
    if said:
        raise _refuse(said)
    src = _Source(url=url)
    try:
        if found["ranges"]:
            src.found = found
            if rasters.sniff(found["head"]) == "zip":
                src.zip = True
                with zipfile.ZipFile(rc._Ranged(found, url)) as z:      # the directory only
                    src.infos = {i.filename: i for i in z.infolist() if not i.is_dir()}
        else:
            src.local = ctx.fetch(url)
            if rasters.sniff(src.local) == "zip":
                src.zip = True
                with zipfile.ZipFile(src.local) as z:
                    src.infos = {i.filename: i for i in z.infolist() if not i.is_dir()}
    except (zipfile.BadZipFile, OSError) as exc:
        raise _refuse(f"the zip at {url} cannot be read ({type(exc).__name__}: it is truncated "
                      "or damaged); re-upload it and resubmit") from None
    if src.zip and len(src.infos) > ZIP_MEMBERS:
        raise _refuse(f"the zip at {url} holds {len(src.infos):,} files — over the "
                      f"{ZIP_MEMBERS:,} the platform reads; give a zip of the layer's tiles")
    return src


def _member(src: _Source, member: str | None, item: str) -> str:
    """The member to read: the one named (exactly, or by its file name when that is
    unique), or the zip's only raster. Refused: a name reaching outside the zip, a link,
    an encrypted member, a zip bomb."""
    import stat
    from pathlib import PurePosixPath
    from .. import remote_clip as rc
    from . import ZIP_RATIO, ZIP_RATIO_FROM
    names = list(src.infos)
    tiles = rc.zip_rasters(names)
    if member is None:
        if len(tiles) != 1:
            raise _refuse(f"{item} is a zip of {len(tiles):,} rasters ({_listed(tiles)}) — name "
                          f"the tile to join: '{item}#<member>', e.g. '{item}#{(tiles or ['x'])[0]}'")
        member = tiles[0]
    elif member not in src.infos:
        same = [n for n in names if n.rsplit("/", 1)[-1].lower() == member.rsplit("/", 1)[-1].lower()]
        if len(same) != 1:
            raise _refuse(f"mosaic tile {member!r} is not in the zip at {src.url} — it holds "
                          f"{len(names):,} files; its rasters: {_listed(tiles)}. Give each as "
                          "written there")
        member = same[0]
    info = src.infos[member]
    parts = PurePosixPath(member.replace("\\", "/")).parts
    if member.startswith(("/", "\\")) or ".." in parts or re.match(r"^[A-Za-z]:", member):
        raise _refuse(f"the zip at {src.url} names a file outside itself ({member!r}) — the "
                      "platform does not read it")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise _refuse(f"{member!r} in the zip at {src.url} is a link — the platform does not "
                      "follow links; zip the tiles themselves")
    if info.flag_bits & 0x1:
        raise _refuse(f"{member!r} in the zip at {src.url} is encrypted — give the tiles "
                      "unencrypted")
    if info.file_size > ZIP_RATIO_FROM and info.file_size > ZIP_RATIO * max(1, info.compress_size):
        raise _refuse(f"{member!r} in the zip at {src.url} unpacks "
                      f"{info.file_size // max(1, info.compress_size):,} times its packed size "
                      "— refused as a zip bomb")
    return member


def _checked(src: _Source) -> None:
    """The final url the probe reached, checked again (public addresses only) right
    before anything else reads it."""
    from .. import fetch_policy
    if not src.checked:
        problems = fetch_policy.check_url(src.found["final"])
        if problems:
            raise _refuse("; ".join(problems))
        src.checked = True


RETRIES = 2                  # a range a host dropped (a refused or reset connection) is asked again


def _get_range(ctx, src: _Source, first: int, last: int, dest: str) -> int:
    """Bytes first..last of the source streamed to `dest` by ONE pinned range request
    (fetch_policy's checks, remote_clip's pinned session), asked again up to RETRIES
    times when the connection fails; returns how many came."""
    import time
    import requests
    for attempt in range(RETRIES + 1):
        try:
            return _get_range_once(ctx, src, first, last, dest)
        except requests.RequestException as exc:
            if attempt == RETRIES:
                raise _refuse(f"{src.url} did not answer ({type(exc).__name__}) while a tile was "
                              "read — try again, or check the link in a browser") from None
            ctx.check_time()
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def _get_range_once(ctx, src: _Source, first: int, last: int, dest: str) -> int:
    from .. import remote_clip as rc
    _checked(src)
    want = last - first + 1
    session, r = rc._get(src.found["final"], src.found["agent"], first, last)
    n = 0
    try:
        if r.status_code == 429:
            raise _refuse(f"{src.url} is limiting how fast the platform may read it (HTTP 429) "
                          "— try again in a minute")
        if r.status_code != 206 or not str(r.headers.get("content-range") or "").startswith(
                f"bytes {first}-"):
            raise _refuse(f"{src.url} stopped answering range requests (HTTP {r.status_code}) "
                          "while a tile was read — try again")
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                n += len(chunk)
                if n > want:
                    raise _refuse(f"{src.url} sent more than was asked while a tile was read")
                f.write(chunk)
                ctx.check_time()
    finally:
        r.close()
        session.close()
    if n != want:
        raise _refuse(f"{src.url} sent {n:,} of the {want:,} bytes asked while a tile was read "
                      "— try again")
    return n


CHUNK = 256 << 10            # a member is fetched in pieces this big, FETCHERS at a time:
FETCHERS = 8                 # Zenodo sends ~20 KB/s per connection from deep in a 2.5 GB zip
                             # (one 2 MB range: 122 s; eight 256 KB ranges at once: 19 s)


def _span(src: _Source, member: str) -> tuple[int, int]:
    """The bytes of the zip holding a member: its local header, name, extra field and
    packed data (the extra field's length is the local header's; 512 bytes are allowed)."""
    info = src.infos[member]
    name = len(info.orig_filename.encode("utf-8" if info.flag_bits & 0x800 else "cp437",
                                         errors="replace"))
    first = info.header_offset
    last = first + 30 + name + len(info.extra) + 512 + info.compress_size - 1
    if src.found.get("size"):
        last = min(last, src.found["size"] - 1)
    return first, last


def _inflate(ctx, src: _Source, member: str, raw: str, dest: str) -> int:
    """Unpack a member's bytes as fetched (`raw`, from its local header) into `dest`; its
    size and CRC must be the directory's. Returns any bytes fetched to complete it."""
    import os
    import struct
    import zipfile
    import zlib
    info = src.infos[member]
    damaged = (f"{member!r} in the zip at {src.url} does not unpack to what the zip's "
               "directory says (it is truncated or damaged) — re-upload the zip and resubmit")
    try:
        dec = zipfile._get_decompressor(info.compress_type)
    except NotImplementedError:
        raise _refuse(f"{member!r} in the zip at {src.url} is packed with a method the platform "
                      f"does not unpack (zip method {info.compress_type}) — zip it with "
                      "deflate") from None
    extra = 0
    try:
        got = os.path.getsize(raw)
        with open(raw, "rb") as f:
            head = f.read(30)
        if len(head) < 30 or head[:4] != b"PK\x03\x04":
            raise _refuse(damaged)
        at = 30 + sum(struct.unpack("<HH", head[26:30]))       # past the name and extra field
        if at + info.compress_size > got:                       # a longer extra field
            more = dest + ".more"
            extra = _get_range(ctx, src, info.header_offset + got,
                               info.header_offset + at + info.compress_size - 1, more)
            with open(raw, "ab") as out, open(more, "rb") as f:
                out.write(f.read())
            os.unlink(more)
        n, crc = 0, 0
        with open(raw, "rb") as f, open(dest, "wb") as out:
            f.seek(at)
            left = info.compress_size
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    raise _refuse(damaged)
                left -= len(chunk)
                part = dec.decompress(chunk) if dec else chunk
                n += len(part)
                if n > info.file_size:
                    raise _refuse(damaged)
                crc = zlib.crc32(part, crc)
                out.write(part)
                ctx.check_time()
            tail = dec.flush() if dec is not None and hasattr(dec, "flush") else b""
            n += len(tail)
            crc = zlib.crc32(tail, crc)
            out.write(tail)
    except (zlib.error, EOFError, OSError) as exc:
        raise _refuse(f"{damaged} ({type(exc).__name__})") from None
    finally:
        if os.path.exists(raw):
            os.unlink(raw)
    if n != info.file_size or crc != info.CRC:
        raise _refuse(damaged)
    return extra


def _unzip_one(ctx, src: _Source, member: str, dest: str) -> None:
    """A member of a zip downloaded whole, unpacked (zipfile checks its CRC)."""
    import zipfile
    import zlib
    info = src.infos[member]
    damaged = (f"{member!r} in the zip at {src.url} does not unpack to what the zip's "
               "directory says (it is truncated or damaged) — re-upload the zip and resubmit")
    n = 0
    try:
        with zipfile.ZipFile(src.local) as z, z.open(member) as f, open(dest, "wb") as out:
            while chunk := f.read(1 << 20):
                n += len(chunk)
                if n > info.file_size:
                    raise _refuse(damaged)
                out.write(chunk)
                ctx.check_time()
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
        raise _refuse(f"{damaged} ({type(exc).__name__})") from None


def _fetch_all(ctx, tiles: list) -> None:
    """Every tile that comes whole, as a local file: a zip's members — from a host that
    answers range requests, each member's own bytes in CHUNK pieces, FETCHERS at a time
    (never the rest of the zip); from a zip downloaded whole, unpacked from it — or a
    link whose host sends only whole files (downloaded already). A tile behind its own
    link that answers range requests stays remote: read in place."""
    import os
    from concurrent.futures import ThreadPoolExecutor
    from . import _digest, size_text
    todo = [t for t in tiles if t.member is not None and t.file is None]
    spans = {id(t): _span(t.src, t.member) for t in todo if t.src.found}
    packed = sum(b - a + 1 for a, b in spans.values())
    unpacked = sum(t.src.infos[t.member].file_size for t in todo)
    if packed > ctx.max_input_bytes - ctx.fetched_bytes or packed + unpacked > ctx._room():
        raise _refuse(f"the {len(todo)} tiles to read are {size_text(packed)} packed and "
                      f"{size_text(unpacked)} unpacked — more than the platform downloads and "
                      "unpacks to prepare one contribution (or the server has room for now); "
                      "give a smaller clip, or fewer tiles")
    pieces = []                               # (tile, first, last, file) of every range asked
    for t in todo:
        suffix = re.sub(r"[^a-z0-9.]", "", "." + t.member.rsplit(".", 1)[-1].lower())[:8]
        t.file = ctx.tmp(suffix if len(suffix) > 1 else ".tif", "tile")
        if id(t) in spans:
            a, b = spans[id(t)]
            pieces += [(t, p, min(p + CHUNK, b + 1) - 1, f"{t.file}.{i:05d}")
                       for i, p in enumerate(range(a, b + 1, CHUNK))]
    with ThreadPoolExecutor(max_workers=FETCHERS) as pool:
        got = list(pool.map(lambda c: _get_range(ctx, c[0].src, c[1], c[2], c[3]), pieces))
    ctx.fetched_bytes += sum(got)
    for t in todo:
        if id(t) in spans:
            raw = t.file + ".part"
            with open(raw, "wb") as out:
                for c in (c for c in pieces if c[0] is t):
                    with open(c[3], "rb") as f:
                        out.write(f.read())
                    os.unlink(c[3])
            ctx.fetched_bytes += _inflate(ctx, t.src, t.member, raw, t.file)
        else:
            _unzip_one(ctx, t.src, t.member, t.file)
    for t in tiles:
        if t.member is not None:
            t.read = ("range requests, the member only" if t.src.found else
                      "unpacked from the zip as downloaded")
        elif t.src.local and t.file is None:
            t.file, t.read = t.src.local, "downloaded whole"
        if t.file and t.sha256 is None:
            d = _digest(t.file)
            t.sha256, t.bytes = d["sha256"], d["bytes"]


def _gdal(src: _Source) -> tuple[str, dict, dict]:
    """(what GDAL opens, its config, the remote read {agent, allowed}) for a tile read in
    place: GDAL may open the one final url the probe reached, nothing else."""
    from .. import remote_clip as rc
    _checked(src)
    base = rc.vsicurl(src.found["final"])
    return base, rc._gdal_env(src.found["agent"], base), {"agent": src.found["agent"],
                                                          "allowed": base}


def _header(ctx, t: _Tile) -> _Tile:
    """Where a tile lies and what it holds, from its header — read from the local file,
    or in place over range requests; one driver, named from the tile's first bytes."""
    import rasterio
    from rasterio.warp import transform_bounds
    from .. import rasters, remote_clip as rc
    ctx.check_time()
    try:
        if t.file:
            t.driver = rasters.driver_for(t.file)
            path, env = t.file, rasters.gdal_env()
        else:
            t.driver = rasters.driver_for(t.src.found["head"])
            path, env, _ = _gdal(t.src)
    except ValueError as exc:
        raise _refuse(f"mosaic tile {t.label}: {exc}") from None
    try:
        with rasterio.Env(**env):
            target, picked, driver = rasters._open_target(path, ctx.manifest.get("layer_name"),
                                                          t.driver)
            with rasterio.open(target, driver=driver) as src:
                refusal, band_note = rasters._bands(src)
                if refusal:
                    raise ValueError(refusal)
                crs, said = rasters.crs_of(src, ", so its place in the mosaic is unknown")
                tr = src.transform
                if tr.b or tr.d:
                    raise ValueError("its grid is rotated — the platform joins north-up tiles")
                t.crs, t.transform, t.width, t.height = crs, tr, src.width, src.height
                t.dtype, t.nodata = src.dtypes[0], src.nodata
                try:
                    t.colormap = src.colormap(1)
                except ValueError:
                    t.colormap = None
                b = src.bounds
                t.lonlat = (tuple(b) if crs.to_epsg() == 4326 else
                            transform_bounds(crs, "EPSG:4326", *b, densify_pts=21))
                t.notes = [n for n in (picked, said, band_note) if n]
    except ValueError as exc:
        raise _refuse(f"mosaic tile {t.label}: {exc}") from None
    except Exception as exc:
        raise _refuse(f"could not read the header of mosaic tile {t.label}: "
                      f"{rc._said(exc)}") from None
    return t


# --------------------------------------------------------------------------- which tiles

def _tiles(ctx, box) -> tuple[list[_Tile], dict, list[str]]:
    """(the tiles with their headers read, how they were picked, notes)."""
    from .. import remote_clip as rc
    m = ctx.manifest
    spec = m["mosaic"]
    sources: dict[str, _Source] = {}

    def source(url):
        if url not in sources:
            sources[url] = _open(ctx, url)
        return sources[url]

    if not _is_auto(spec):
        tiles = []
        for item in spec:
            item = str(item).strip()
            if _is_link(item):
                url, member = rc.split_member(item)
            else:
                url, member = rc.split_member(m["url"])[0], item
            src = source(url)
            if src.zip:
                member = _member(src, member, url)
            elif member:
                raise _refuse(f"mosaic tile {item} names a member ('#{member}'), but the file at "
                              f"{url} is not a zip — drop '#{member}'")
            tiles.append(_Tile(src, member, item if _is_link(item) else member))
        _fetch_all(ctx, tiles)
        return [_header(ctx, t) for t in tiles], {"by": "listed"}, []

    url = rc.split_member(m["url"])[0]
    src = source(url)
    if not src.zip:
        raise _refuse(f"mosaic: 'auto' picks tiles inside a zip, and the file at {url} is one "
                      "raster — give it as url with the clip and no mosaic, or list the tiles' "
                      "links in mosaic")
    rasters_in = rc.zip_rasters(list(src.infos))
    if not rasters_in:
        raise _refuse(f"the zip at {url} holds no raster ({_listed(list(src.infos))})")
    hits, unplaced = near(rasters_in, box)
    wanted = [*hits, *unplaced]
    if len(wanted) > MAX_TILES:
        why = (f"{len(hits):,} of its tiles are named within one tile of the clip"
               if len(hits) > MAX_TILES or not unplaced else
               f"{len(unplaced):,} of its rasters carry no position in their names")
        raise _refuse(f"the zip at {url} holds {len(rasters_in):,} rasters and {why}; the "
                      f"platform reads at most {MAX_TILES} tile headers for one layer — give a "
                      "smaller clip, or list the tiles in mosaic")
    if not wanted:
        raise _refuse(f"none of the {len(rasters_in):,} tiles in the zip at {url} is named near "
                      f"the clip [{', '.join(f'{v:g}' for v in box)}] (they are named like "
                      f"{_listed(rasters_in[:3])}) — check the order [minLon, minLat, maxLon, "
                      "maxLat] and that this zip covers the area")
    candidates = {name: _Tile(src, _member(src, name, url), name) for name in wanted}
    read = _pick(ctx, candidates, hits, unplaced, box)
    tiles = [t for t in read if _overlap(t.lonlat, box)]
    read = [t.label for t in read]
    if not tiles:
        raise _refuse(f"none of the tiles in the zip at {url} lies under the clip "
                      f"[{', '.join(f'{v:g}' for v in box)}]: the {len(read)} named nearest it "
                      f"({_listed(read)}) were read and lie outside it — check the order "
                      "[minLon, minLat, maxLon, maxLat] and that this zip covers the area")
    note = (f"the tiles were picked by their headers: of the {len(rasters_in):,} rasters in the "
            f"zip, {len(read)} " + ("named nearest the clip" if hits else "")
            + (" and " if hits and unplaced else "")
            + (f"{len(unplaced)} with no position in their names" if unplaced else "")
            + f" were read, and {len(tiles)} lie under it: {_listed(t.label for t in tiles)}")
    return tiles, {"by": "header bounds", "of": len(rasters_in), "headers_read": read}, [note]


def _overlap(b, box) -> bool:
    return min(b[2], box[2]) - max(b[0], box[0]) > 0 and min(b[3], box[3]) - max(b[1], box[1]) > 0


def _corner(t: _Tile) -> tuple | None:
    """Which corner of its header's bounds a tile's name gives ('left'/'right',
    'top'/'bottom'), or None."""
    pos = position(t.label)
    if not pos:
        return None
    w, s, e, n = t.lonlat
    tx, ty = (e - w) / 100, (n - s) / 100
    cx = "left" if abs(pos[0] - w) <= tx else "right" if abs(pos[0] - e) <= tx else None
    cy = "top" if abs(pos[1] - n) <= ty else "bottom" if abs(pos[1] - s) <= ty else None
    return (cx, cy) if cx and cy else None


def _pick(ctx, candidates: dict, hits: list, unplaced: list, box) -> list:
    """The candidates whose headers were read. The tile named nearest the clip's centre
    is read first: the corner its name gives and its size say where every other name
    puts its tile, so only those that would touch the clip are read too. Any tile read
    that breaks that rule (another corner, another size) has every candidate read."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    order = sorted(hits, key=lambda k: (position(k)[0] - cx) ** 2 + (position(k)[1] - cy) ** 2)
    done: list = []

    def read(names):
        todo = [candidates[k] for k in names if candidates[k] not in done]
        _fetch_all(ctx, todo)
        for t in todo:
            done.append(_header(ctx, t))
    read(order[:1])
    rule = _corner(done[0]) if done else None
    if rule:
        w, s, e, n = done[0].lonlat
        size = (e - w, n - s)

        def where(k):
            x, y = position(k)
            left = x if rule[0] == "left" else x - size[0]
            top = y if rule[1] == "top" else y + size[1]
            return (left, top - size[1], left + size[0], top)
        read([k for k in order[1:] if _overlap(where(k), box)])
        if any(_corner(t) != rule or abs(t.lonlat[2] - t.lonlat[0] - size[0]) > size[0] / 100
               or abs(t.lonlat[3] - t.lonlat[1] - size[1]) > size[1] / 100 for t in done):
            read(order)                       # the names do not follow one rule: read them all
    else:
        read(order)
    read(unplaced)
    return done


# --------------------------------------------------------------------------- the grid

def _edge(v: float, fn) -> int:
    r = round(v)
    return int(r) if abs(v - r) < _EPS else int(fn(v))


def _same_grid(t: _Tile, ref: _Tile) -> bool:
    a, b = t.transform, ref.transform
    if t.crs != ref.crs:
        return False
    if abs(a.a / b.a - 1) > 1e-9 or abs(a.e / b.e - 1) > 1e-9:
        return False
    dc, dr = (a.c - b.c) / b.a, (a.f - b.f) / b.e
    return abs(dc - round(dc)) < _EPS and abs(dr - round(dr)) < _EPS


def _plan(tiles: list[_Tile], box):
    """The mosaic's grid — the reference tile's CRS and cells, over the tiles' extent
    (cut to the clip), snapped outward to whole cells — and each tile's place on it."""
    from affine import Affine
    from rasterio.warp import transform_bounds

    def lonlat_overlap(t):
        w, s, e, n = t.lonlat
        if box is not None:
            w, s, e, n = max(w, box[0]), max(s, box[1]), min(e, box[2]), min(n, box[3])
        return max(0.0, e - w) * max(0.0, n - s)
    ref = max(tiles, key=lonlat_overlap)          # max keeps the first of equals: the first given
    crs = ref.crs

    def in_ref(t):
        if t.crs == crs:
            return _bounds(t)
        return transform_bounds(t.crs, crs, *_bounds(t), densify_pts=21)
    boxes = [in_ref(t) for t in tiles]
    w = min(b[0] for b in boxes)
    s = min(b[1] for b in boxes)
    e = max(b[2] for b in boxes)
    n = max(b[3] for b in boxes)
    if box is not None:
        cb = box if crs.to_epsg() == 4326 else transform_bounds("EPSG:4326", crs, *box, densify_pts=21)
        w, s, e, n = max(w, cb[0]), max(s, cb[1]), min(e, cb[2]), min(n, cb[3])
    rx, ry = ref.transform.a, -ref.transform.e
    ox, oy = ref.transform.c, ref.transform.f
    c0, c1 = _edge((w - ox) / rx, math.floor), _edge((e - ox) / rx, math.ceil)
    r0, r1 = _edge((oy - n) / ry, math.floor), _edge((oy - s) / ry, math.ceil)
    if c1 <= c0 or r1 <= r0:
        raise _refuse("the tiles do not overlap the clip by a single cell — check the clip "
                      "and the tiles")
    # the origin counted from the tile on the grid nearest it: a tile's edge that is the
    # mosaic's edge stays exactly that number, not one carried across thousands of cells
    near_tile = min((t for t in tiles if _same_grid(t, ref)),
                    key=lambda t: abs(ox + c0 * rx - t.transform.c) + abs(oy - r0 * ry - t.transform.f))
    kc = round((ox + c0 * rx - near_tile.transform.c) / rx)
    kr = round((near_tile.transform.f - (oy - r0 * ry)) / ry)
    grid = Affine(rx, 0.0, near_tile.transform.c + kc * rx, 0.0, -ry,
                  near_tile.transform.f - kr * ry)
    W, H = c1 - c0, r1 - r0
    for t, (bw, bs, be, bn) in zip(tiles, boxes):
        t.aligned = _same_grid(t, ref)
        if t.aligned:
            fc = _edge((t.transform.c - grid.c) / rx, round)
            fr = _edge((grid.f - t.transform.f) / ry, round)
            t.foot = (max(0, fc), max(0, fr), min(W, fc + t.width), min(H, fr + t.height))
        else:
            t.foot = (max(0, _edge((bw - grid.c) / rx, math.floor)),
                      max(0, _edge((grid.f - bn) / ry, math.floor)),
                      min(W, _edge((be - grid.c) / rx, math.ceil)),
                      min(H, _edge((grid.f - bs) / ry, math.ceil)))
    return ref, grid, W, H


def _bounds(t: _Tile) -> tuple:
    a = t.transform
    return (a.c, a.f + a.e * t.height, a.c + a.a * t.width, a.f)


def _foot_lonlat(t: _Tile, grid, crs, margin: float) -> list[float]:
    """The part of the mosaic a tile fills, in degrees, with `margin` cells of the tile
    around it (a resampled tile needs the cells beside its edge)."""
    from rasterio.warp import transform_bounds
    c0, r0, c1, r1 = t.foot
    b = (grid.c + c0 * grid.a, grid.f + r1 * grid.e, grid.c + c1 * grid.a, grid.f + r0 * grid.e)
    if crs.to_epsg() != 4326:
        b = transform_bounds(crs, "EPSG:4326", *b, densify_pts=21)
    w, s, e, n = t.lonlat
    dx, dy = (e - w) / max(1, t.width) * margin, (n - s) / max(1, t.height) * margin
    return [max(-180.0, b[0] - dx), max(-90.0, b[1] - dy), min(180.0, b[2] + dx),
            min(90.0, b[3] + dy)]


# --------------------------------------------------------------------------- run

def run(ctx):
    """Read each tile's cells for the mosaic, join them strip by strip (never the whole
    grid in memory) into one GeoTIFF on the reference tile's grid."""
    import numpy as np
    from .. import rasters, remote_clip as rc
    from ...graph.geo import schema
    from . import PreparedFile
    m = ctx.manifest
    box, where = (rc.box_of(m["clip"]) if m.get("clip") is not None else (None, ""))
    tiles, picked, notes = _tiles(ctx, box)
    ref, grid, W, H = _plan(tiles, box)
    skipped = [t for t in tiles if t.foot[2] <= t.foot[0] or t.foot[3] <= t.foot[1]]
    tiles = [t for t in tiles if t not in skipped]
    if skipped:
        notes.append(f"{_listed(t.label for t in skipped)} lie outside the clip; not read")
    role = rasters._role(str(m.get("layer") or ""))
    regrid = str((schema.namespaces().get(role) or {}).get("regrid") or "refuse")
    moved = [t for t in tiles if not t.aligned]
    if moved and regrid == "refuse":
        raise _refuse(f"{_listed(t.label for t in moved)} lie on another grid than "
                      f"{ref.label}; a {role}_* layer is a count grid, which the platform never "
                      "re-grids (resampling moves counts between cells) — give tiles on one grid")
    method = "bilinear" if regrid == "bilinear" else "nearest"
    dtype = np.result_type(*(np.dtype(t.dtype) for t in tiles)).name
    ctx.check_cells(W * H, "the mosaic")
    raw = W * H * np.dtype(dtype).itemsize
    if raw > rc.RAW_FACTOR * ctx.cap:
        raise _refuse(f"the mosaic covers {W:,} x {H:,} cells ({rc.size_text(raw)} before "
                      f"compression) — the stored file must fit the {rc.size_text(ctx.cap)} cap; "
                      + ("give a smaller clip" if box is not None else
                         "give clip: the area to keep (ASK the contributor; never guess one)"))
    out_nodata, fill, said, as_class = _nodata(m, tiles, dtype)
    notes += said
    for t in tiles:
        _fetch_cells(ctx, t, grid, ref.crs, box)
    order = [ref, *(t for t in tiles if t is not ref)]   # the grid's own tile first
    dest = ctx.tmp(".tif", "mosaic")
    gaps, empty = _join(ctx, order, grid, W, H, dtype, out_nodata, fill, method, dest)
    if empty and fill is None:
        raise _refuse(f"{empty:,} cells of the mosaic lie under no tile, and the tiles carry no "
                      "nodata — declare declared.nodata: the value to write there (one that is "
                      "no class and no reading), and resubmit")
    if gaps:
        what = (f"{_num(fill)} (the legend's class {as_class})" if as_class is not None
                else f"nodata ({_num(fill)})")
        notes.append(f"{gaps:,} cells of the mosaic lie under no tile and were written as {what}")
    unused = [t for t in order if not t.cells]
    if unused:
        notes.append(f"{_listed(t.label for t in unused)} add no reading to the mosaic (their "
                     "cells there are no data, or a tile before them gave them)")

    # what was done, in words and in the record
    kinds = {t.read for t in tiles}
    unit = "deg" if ref.crs.is_geographic else (ref.crs.linear_units or "unit")
    srcs = list(dict.fromkeys(t.src.url for t in tiles))
    whence = (f"from {len(srcs)} links" if len(srcs) > 1 else
              f"of the zip at {srcs[0]}" if tiles[0].src.zip else f"from {srcs[0]}")
    said_read = {
        "range requests, the member only": "each tile's own bytes fetched from the zip by range "
                                           "requests (nothing else of the zip was downloaded)",
        "range requests, only the window": "only each tile's cells under the clip read over HTTP "
                                           "range requests (nothing else was downloaded)",
        "unpacked from the zip as downloaded": "the tiles unpacked from the zip, downloaded whole "
                                               "(its host does not answer range requests)",
        "downloaded whole": "the tiles downloaded whole"}
    read = "; ".join(said_read[k] for k in said_read if k in kinds)
    if box is not None:
        read += (f"; cut to the clip lon {box[0]:g} to {box[2]:g}, lat {box[1]:g} to "
                 f"{box[3]:g}{where}")
    how = ("cell values unchanged, no resampling" if not moved else
           f"{_listed(t.label for t in moved)} lie on another grid and were resampled onto it "
           "by " + ("nearest neighbour — class values unchanged, cell edges moved by under a cell"
                    if method == "nearest" else "bilinear interpolation — a cell's value may "
                    "differ slightly from any one source cell"))
    head = (f"mosaicked {len(tiles)} tile{'s' if len(tiles) != 1 else ''} {whence} "
            f"({_listed(t.label for t in tiles)}) into one {W:,} x {H:,}-cell grid "
            f"({ref.crs.to_string()}, cells {abs(grid.a):.9g} x {abs(grid.e):.9g} {unit}, the "
            f"grid of {ref.label}): {read}; {how}; where tiles overlap, the grid's own tile "
            "comes first, then the order given")
    for t in tiles:
        notes += [n for n in t.notes if n not in notes]
    if box is not None:
        ctx.clipped = True                 # the clip is read: the framework cuts nothing again
        ctx.record.update({"clip": [round(v, 6) for v in box],
                           **({"clip_place": m["clip"]} if isinstance(m["clip"], str) else {})})
    ranged = {k for k in kinds if k.startswith("range")}
    ctx.record["read"] = ("range requests, only the windows" if kinds == {"range requests, only the window"}
                          else "range requests, only the tiles" if ranged == kinds
                          else "downloaded whole" if not ranged
                          else "range requests and whole downloads")
    b = (grid.c, grid.f + grid.e * H, grid.c + grid.a * W, grid.f)
    inputs = {"mosaic": "auto" if _is_auto(m["mosaic"]) else [str(i).strip() for i in m["mosaic"]],
              "picked": picked,
              "tiles": [{"label": t.label, "url": t.src.url,
                         **({"member": t.member} if t.member else {}),
                         **({"sha256": t.sha256, "bytes": int(t.bytes)} if t.sha256 else {}),
                         "read": t.read, "window": [int(v) for v in t.window],
                         "of": [int(v) for v in t.of], "grid": "own" if t.aligned else method,
                         "cells": int(t.cells)} for t in order],
              **({"clip": [round(v, 6) for v in box]} if box is not None else {}),
              "grid": {"crs": ref.crs.to_string(), "of": ref.label,
                       "cell": [float(abs(grid.a)), float(abs(grid.e))], "cells": [W, H],
                       "bounds": [round(float(v), 9) for v in b]},
              "nodata": _num(out_nodata), "fill": _num(fill), "uncovered_cells": int(gaps),
              **({"resampling": method} if moved else {})}
    return PreparedFile(path=dest, notes=[head, *notes], provenance={"inputs": inputs})


def _fetch_cells(ctx, t: _Tile, grid, crs, box) -> None:
    """A tile's cells for the mosaic, local: a tile behind its own link that answers range
    requests has the window under its part of the mosaic cut in place (remote_clip.cut, as
    every clip) — or, with no clip, is downloaded whole; any other tile is a local file
    already (a zip's member, a whole download)."""
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds
    from .. import remote_clip as rc
    from . import _digest
    ctx.check_time()
    if t.file is None and box is None:
        t.file, t.read = ctx.fetch(t.src.url), "downloaded whole"
        d = _digest(t.file)
        t.sha256, t.bytes = d["sha256"], d["bytes"]
    if t.file is None:
        path, env, remote = _gdal(t.src)
        dest = ctx.tmp(".tif", "tile")
        # a resampled tile needs the cells beside its part's edge; one on the grid none
        made = rc.cut(path, _foot_lonlat(t, grid, crs, 0.5 if t.aligned else 2.0), dest,
                      ctx.cap, quoted=t.label, remote=remote,
                      layer_name=ctx.manifest.get("layer_name"), driver=t.driver)
        t.path, t.driver, t.window, t.of = dest, "GTiff", made["window"], made["of"]
        t.read = "range requests, only the window"
        t.notes += [n for n in made["notes"] if "beyond the file's edge" not in n and n not in t.notes]
        return
    t.path = t.file
    fb = _foot_lonlat(t, grid, crs, 0.0)
    b = fb if t.crs.to_epsg() == 4326 else transform_bounds("EPSG:4326", t.crs, *fb, densify_pts=21)
    win = from_bounds(*b, transform=t.transform)
    c0, r0 = max(0, math.floor(win.col_off + _EPS)), max(0, math.floor(win.row_off + _EPS))
    c1 = min(t.width, math.ceil(win.col_off + win.width - _EPS))
    r1 = min(t.height, math.ceil(win.row_off + win.height - _EPS))
    t.window, t.of = [c0, r0, max(0, c1 - c0), max(0, r1 - r0)], [t.width, t.height]


def _join(ctx, order, grid, W, H, dtype, out_nodata, fill, method, dest) -> tuple[int, int]:
    """Write the mosaic strip by strip: each cell from the first tile in `order` that has
    a reading there. Returns (cells under no tile, cells with no reading from any tile)."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT
    from rasterio.windows import Window
    from .. import rasters
    m = ctx.manifest
    ref = order[0]
    gaps = empty = 0
    profile = {"driver": "GTiff", "width": W, "height": H, "count": 1, "dtype": dtype,
               "crs": ref.crs, "transform": grid, "nodata": out_nodata, "BIGTIFF": "IF_SAFER",
               **rasters.packing(dtype, W, H)}
    with rasterio.Env(**rasters.gdal_env()):        # local files only, one driver each
        opened, readers = [], []
        try:
            for t in order:
                target, _, drv = rasters._open_target(t.path, m.get("layer_name"), t.driver)
                ds = rasterio.open(target, driver=drv)
                opened.append(ds)
                if t.aligned:
                    off = (_edge((ds.transform.c - grid.c) / grid.a, round),
                           _edge((ds.transform.f - grid.f) / grid.e, round))
                    readers.append((t, ds, off))
                else:
                    vrt = WarpedVRT(ds, crs=ref.crs, transform=grid, width=W, height=H,
                                    resampling=getattr(Resampling, method),
                                    src_nodata=ds.nodata, nodata=ds.nodata,
                                    add_alpha=ds.nodata is None)
                    opened.append(vrt)
                    readers.append((t, vrt, None))
            # strips of whole 256-row blocks, about STRIP_CELLS cells each, however wide
            rows = max(256, min(STRIP_ROWS, STRIP_CELLS // max(1, W)) // 256 * 256)
            with rasterio.open(dest, "w", **profile) as dst:
                for r in range(0, H, rows):
                    ctx.check_time()
                    h = min(rows, H - r)
                    out = np.full((h, W), 0 if fill is None else fill, dtype=dtype)
                    done = np.zeros((h, W), dtype=bool)
                    under = np.zeros((h, W), dtype=bool)
                    for t, ds, off in readers:
                        c0, r0, c1, r1 = t.foot
                        a, b = max(r, r0), min(r + h, r1)
                        if b <= a:
                            continue
                        under[a - r:b - r, c0:c1] = True
                        if off is not None:
                            vals, valid = _read_on_grid(ds, off, c0, a, c1, b)
                        else:
                            win = Window(c0, a, c1 - c0, b - a)
                            vals = ds.read(1, window=win)
                            valid = ds.read_masks(1, window=win) > 0
                            if ds.nodata is not None:
                                valid &= _valid(vals, ds.nodata)
                        sub = done[a - r:b - r, c0:c1]
                        take = valid & ~sub
                        n = int(take.sum())
                        if n:
                            out[a - r:b - r, c0:c1][take] = vals[take].astype(dtype)
                            sub |= take
                            t.cells += n
                    gaps += int((~under).sum())
                    empty += int((~done).sum())
                    dst.write(out, 1, window=Window(0, r, W, h))
                if ref.colormap and np.dtype(dtype).kind == "u" and np.dtype(dtype).itemsize <= 2:
                    try:
                        dst.write_colormap(1, ref.colormap)
                    except ValueError:
                        pass                      # no palette: nothing to carry
        finally:
            for ds in reversed(opened):
                ds.close()
    return gaps, empty


def _read_on_grid(ds, off, c0, r0, c1, r1):
    """(values, which are readings) of the mosaic's cells [c0, c1) x [r0, r1) from a file
    on the mosaic's grid whose first cell is at `off` — only the part on the file is read."""
    import numpy as np
    from rasterio.windows import Window
    h, w = r1 - r0, c1 - c0
    vals = np.zeros((h, w), dtype=ds.dtypes[0])
    valid = np.zeros((h, w), dtype=bool)
    fc0, fr0 = max(c0 - off[0], 0), max(r0 - off[1], 0)
    fc1, fr1 = min(c1 - off[0], ds.width), min(r1 - off[1], ds.height)
    if fc1 <= fc0 or fr1 <= fr0:
        return vals, valid
    part = ds.read(1, window=Window(fc0, fr0, fc1 - fc0, fr1 - fr0))
    oc, orow = fc0 + off[0] - c0, fr0 + off[1] - r0
    vals[orow:orow + part.shape[0], oc:oc + part.shape[1]] = part
    valid[orow:orow + part.shape[0], oc:oc + part.shape[1]] = _valid(part, ds.nodata)
    return vals, valid


def _valid(vals, nodata):
    import numpy as np
    if nodata is None:
        return np.ones(vals.shape, dtype=bool)
    if isinstance(nodata, float) and math.isnan(nodata):
        return ~np.isnan(vals)
    return vals != nodata


def _num(v):
    """A JSON- and YAML-safe number: an int when whole, 'nan' for NaN, None for None."""
    if v is None:
        return None
    f = float(v)
    if math.isnan(f):
        return "nan"
    return int(f) if f.is_integer() else f


def _nodata(m: dict, tiles: list[_Tile], dtype: str) -> tuple:
    """(the mosaic's nodata tag, the value of cells no tile covers, notes, the legend
    class that value is or None). declared.nodata when given; else the tiles' own — and
    no tag when the legend names it a class; else none (a cell no tile covers is then
    declined, with what to declare)."""
    import numpy as np
    from .. import rasters
    decl = m.get("declared") if isinstance(m.get("declared"), dict) else {}
    dn = decl.get("nodata")
    dt = np.dtype(dtype)

    def fits(v):
        if dt.kind in "iu":
            if math.isnan(v) or not float(v).is_integer():
                return False
            info = np.iinfo(dt)
            return info.min <= v <= info.max
        return True
    if rasters._isnum(dn) or rasters._isnan(dn):
        v = float("nan") if rasters._isnan(dn) else float(dn)
        if not fits(v):
            raise _refuse(f"declared.nodata {dn} cannot be stored in the tiles' {dtype} cells — "
                          "declare a nodata that type holds")
        return v, v, [], None
    seen = []
    for t in tiles:
        if t.nodata is not None and not any(_same(t.nodata, s) for s in seen):
            seen.append(float(t.nodata))
    if not seen:
        return None, None, [], None
    v = seen[0]
    notes = []
    if len(seen) > 1:
        notes.append(f"the tiles mark no data with {', '.join(str(_num(s)) for s in seen)}; the "
                     f"mosaic marks it with {_num(v)}")
    try:
        legend = rasters._norm_legend(m.get("legend")) if isinstance(m.get("legend"), dict) else {}
    except (TypeError, ValueError):
        legend = {}
    if not math.isnan(v) and v.is_integer() and int(v) in legend:
        label = legend[int(v)]
        notes.insert(0, f"the tiles mark {_num(v)} as no data, and the legend names {_num(v)} a "
                        f"class ({label}): the mosaic carries no nodata tag, and its {_num(v)} "
                        "cells are that class")
        return None, v, notes, label
    if not fits(v):
        raise _refuse(f"the tiles mark no data with {_num(v)}, which their joined {dtype} cells "
                      "cannot hold — declare declared.nodata")
    return v, v, notes, None


def _same(a, b) -> bool:
    a, b = float(a), float(b)
    return (math.isnan(a) and math.isnan(b)) or a == b


# --------------------------------------------------------------------------- suggestions

def auto(situation: dict) -> dict | None:
    """A zip of rasters given with a clip, every raster in it a TILE named by where it lies
    (GMW_N09W083, GWL_..._W85N10): join the tiles the clip touches — which they are, each
    header says. A zip of other rasters (19 climate variables, a map and its mask) is the
    contributor's to pick from: a question, never a mosaic."""
    m = situation.get("manifest") or {}
    if (situation.get("kind") not in KINDS or situation.get("event") != "zip_of_rasters"
            or m.get("clip") is None or m.get("mosaic") is not None
            or m.get("clip_member") is not None):
        return None
    tiles = list(situation.get("rasters") or [])
    places = [position(t) for t in tiles]
    if len(tiles) < 2 or any(p is None for p in places) or len(set(places)) < 2:
        return None
    return {"fields": {"mosaic": "auto"},
            "why": f"the url is a zip of {len(tiles):,} raster tiles named by where they lie "
                   "and a clip is given, so the platform reads each candidate tile's header, "
                   "takes every tile the clip touches and joins them into one layer"}


def suggest(situation: dict) -> str | None:
    """For a zip of several rasters: join the tiles the area needs — 'auto' when their
    names place them (each header is still read), else the members to list."""
    m = situation.get("manifest") or {}
    if (situation.get("kind") not in KINDS or m.get("mosaic") is not None
            or situation.get("event") != "zip_of_rasters"):
        return None
    tiles = list(situation.get("rasters") or [])
    if len(tiles) < 2:
        return None
    placed = [t for t in tiles if position(t)]
    if len(placed) >= 2:
        return ("if the area needs more than one of its tiles, resubmit the same link with "
                "mosaic: 'auto' and clip: the area (ASK the contributor for it; never guess "
                "one) — the platform reads each tile's header, takes every tile the clip "
                "touches, reads only their cells under it and stores them joined as one layer; "
                "nothing to do by hand")
    return ("if they are tiles of one layer, resubmit the same link with mosaic: [<member>, "
            "<member>, ...] — the members to join, as named in the zip (ASK the contributor "
            "which; never guess) — and the platform joins them into one layer; nothing to do "
            "by hand")
