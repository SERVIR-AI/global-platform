"""Clip at the source: store only the part of a raster a contributor asked for.

National and global rasters are the sources a hub most wants to bring (a 1.6 GB
national DEM on Drive, a 4.7 GB MapBiomas map, a 37 GB global biomass COG, a 658 MB
WorldClim zip) and every one is over the size cap — which is for what the platform
STORES, not for what a host happens to publish. Every host that serves those files
answers range requests, so the gate reads only the window over the contributor's
`clip` box (reprojected to the file's CRS), writes it as a compressed GeoTIFF, and
hands it to the usual verify -> normalise -> stage -> land. A zip is read in place
through /vsizip/ when a member is named. This happens at CONTRIBUTION time, once;
a pack never reads a remote file.

The cap still holds: on the stored clip. A file over it with no `clip` is declined
the way a missing field is — the contributor's agent asks them for the area, and
never guesses one.

Security. GDAL's own HTTP client does the windowed reads, not the pinned fetcher
(fetch_policy.py), so before any GDAL read: the link is probed through the pinned
fetcher — every hop checked and pinned, redirects followed there and only there —
and GDAL is handed the FINAL url, checked again (public addresses only) just before
the read; CPL_VSIL_CURL_ALLOWED_FILENAME names that one path, so GDAL opens nothing
else over HTTP (no sidecar, no file a format points at) — and for a local file or a zip
downloaded whole it names a path that matches nothing; a text or XML file (a VRT, a
service description, even behind leading bytes) is refused before GDAL opens it; every
open names ONE driver, chosen from the file's first bytes (rasters.driver_for), so no
format GDAL would resolve other files through is ever tried; GDAL never sees a .netrc,
retries twice, and times out. Zip directories are listed through the pinned fetcher too.
RESIDUAL, accepted and recorded here: GDAL resolves the host itself, so a name that
answers a public address to the check and a private one to GDAL's connect (DNS
rebinding between check and read) reaches it; and GDAL follows redirects (it has no
switch to refuse them), so a host that answers the probe itself and then redirects
GDAL's range reads elsewhere is followed. Both need a hostile DNS or host, and what
comes back is parsed as raster cells and nothing else. Reading through the pinned
session instead (rasterio's `opener`) would close both, at the cost of GDAL's range
merging.
"""

from __future__ import annotations

import functools
import math
import os
import re
import time
from urllib.parse import unquote, urldefrag, urlencode, urljoin, urlparse

from . import fetch_policy

PROBE_BYTES = 65536        # one ranged request: enough to know what the file is
CLIP_DEADLINE = 300        # seconds for one windowed read (the 1.6 GB DEM's gulf: 48 s)
RAW_FACTOR = 8             # a window over 8x the cap before compression is refused unread
LIST_BUDGET = 16 << 20     # bytes a zip's directory may cost to list
SHOWN = 8                  # members named in a decline


class ClipRefused(ValueError):
    """A clip the gate cannot make; the message says why and what to do."""


def size_text(n: int | float | None) -> str:
    if n is None:
        return "of unknown size"
    for unit, k in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= k:
            v = n / k
            return f"{v:.0f} {unit}" if v >= 100 or float(v).is_integer() else f"{v:.1f} {unit}"
    return f"{int(n)} bytes"


def split_member(url: str) -> tuple[str, str | None]:
    """(the url to fetch, the zip member named after its '#'), as producers' docs write
    'wc2.1_2.5m_bio.zip#wc2.1_2.5m_bio_1.tif'."""
    base, frag = urldefrag(str(url or "").strip())
    return base, (unquote(frag).strip() or None)


def source_url(url: str) -> str:
    """The url that answers the BYTES and range requests. A Drive file's share link
    answers a page; its usercontent form with confirm=t answers the file, ranges and
    all, for any file shared with Anyone with the link (measured 2026-10-19)."""
    fid = fetch_policy.drive_file_id(url)
    if fid:
        return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    return fetch_policy.direct_url(url)


_DRIVE_PAGE = ("Google Drive answered with a web page, not the file — the file is not shared "
               "with 'Anyone with the link', or the link is a Google Doc/Sheet rather than an "
               "uploaded file. Share it (Share -> General access -> Anyone with the link) and "
               "give the file's link again")


def probe(url: str) -> dict:
    """What the link answers to ONE ranged request, made through the contribution
    fetcher's checks — each hop checked and pinned, at most PROBE_BYTES read:
    {final, size, ranges, head, html, agent}. A 401/403 or a bot challenge is retried
    once as a browser, as the fetcher does. Raises fetch_policy.FetchRefused."""
    try:
        return _probe(url, fetch_policy.USER_AGENT)
    except fetch_policy._Retryable as first:
        try:
            return _probe(url, fetch_policy.BROWSER_AGENT)
        except fetch_policy.FetchRefused:
            raise fetch_policy.FetchRefused(str(first)) from None


def _get(current: str, agent: str, first: int, last: int):
    """One pinned ranged GET of `current` (already checked); the caller closes both. The
    name is resolved and checked again here (fetch_policy._pin): the address connected to
    is the one checked, never one a later answer (DNS rebinding) put in its place."""
    parts = urlparse(current)
    addr = fetch_policy._pin(parts.hostname)
    session = fetch_policy._session(parts.hostname, parts.scheme)
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    r = session.get(fetch_policy._pinned_url(parts, addr), timeout=fetch_policy.TIMEOUT,
                    stream=True, allow_redirects=False,
                    headers={"User-Agent": agent, "Host": host, "Range": f"bytes={first}-{last}"})
    return session, r


def _probe(url: str, agent: str) -> dict:
    import requests
    current = source_url(url)
    drive = bool(fetch_policy.drive_file_id(url))
    hops: list[str] = []
    for _ in range(fetch_policy.MAX_REDIRECTS + 2):
        problems = fetch_policy.check_url(current)
        if problems:
            raise fetch_policy.FetchRefused("; ".join(problems))
        hops.append(current)
        quoted = fetch_policy._quote(current, url)
        try:
            session, r = _get(current, agent, 0, PROBE_BYTES - 1)
        except requests.RequestException as exc:
            raise fetch_policy.FetchRefused(
                f"could not fetch {quoted}: {type(exc).__name__} — the site did not answer; "
                "check the link in a browser") from None
        try:
            if 300 <= r.status_code < 400:
                nxt = r.headers.get("location")
                if not nxt:
                    raise fetch_policy.FetchRefused(f"{quoted} answered {r.status_code} without a location")
                current = urljoin(current, nxt)
                continue
            head = b""
            for chunk in r.iter_content(chunk_size=8192):
                head += chunk
                if len(head) >= PROBE_BYTES:
                    break
            head = head[:PROBE_BYTES]
            ctype = (r.headers.get("content-type") or "").lower()
            html = (ctype.startswith("text/html")
                    or head.lstrip().lower().startswith((b"<!doctype html", b"<html")))
            if drive and html:
                raise fetch_policy.FetchRefused(_DRIVE_PAGE)
            if not drive:
                fetch_policy._refuse_page(r.status_code, r.headers, head, hops, url, quoted,
                                          None, False, html=html)
            if r.status_code >= 400:
                hint = (" — check the link in a browser" if r.status_code == 404 else "")
                raise (fetch_policy._Retryable if r.status_code == 403 else fetch_policy.FetchRefused)(
                    f"{quoted} answered HTTP {r.status_code}{hint}")
            size, ranges = None, False
            if r.status_code == 206:
                m = re.match(r"bytes\s+\d+-\d+/(\d+)", r.headers.get("content-range") or "")
                size, ranges = (int(m.group(1)), True) if m else (None, False)
            elif str(r.headers.get("content-length") or "").isdigit():
                size = int(r.headers["content-length"])
            return {"final": current, "size": size, "ranges": ranges, "head": head,
                    "html": html, "agent": agent}
        finally:
            r.close()
            session.close()
    raise fetch_policy.FetchRefused(f"{url} redirected more than {fetch_policy.MAX_REDIRECTS} times")


class _Ranged:
    """A remote file as a seekable read-only file, one pinned ranged GET per read —
    enough for zipfile to list a zip's directory without fetching the archive."""

    def __init__(self, found: dict, quoted: str):
        self.url, self.size, self.agent, self.quoted = found["final"], found["size"], found["agent"], quoted
        self.pos, self.spent = 0, 0
        self._buf, self._at = b"", -1

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = {0: off, 1: self.pos + off, 2: self.size + off}[whence]
        return self.pos

    def read(self, n=-1):
        n = self.size - self.pos if n is None or n < 0 else min(n, self.size - self.pos)
        if n <= 0:
            return b""
        if not (self._at <= self.pos and self.pos + n <= self._at + len(self._buf)):
            want = max(n, PROBE_BYTES)
            last = min(self.size, self.pos + want) - 1
            if self.spent + (last - self.pos + 1) > LIST_BUDGET:
                raise ClipRefused(f"listing the zip at {self.quoted} would take more than "
                                  f"{size_text(LIST_BUDGET)} of reads — contribute one raster "
                                  "from it directly")
            problems = fetch_policy.check_url(self.url)
            if problems:
                raise fetch_policy.FetchRefused("; ".join(problems))
            import requests
            try:
                session, r = _get(self.url, self.agent, self.pos, last)
            except requests.RequestException as exc:
                raise ClipRefused(f"{self.quoted} stopped answering ({type(exc).__name__}) while "
                                  "its zip directory was read — try again, or check the link "
                                  "in a browser") from None
            try:
                if r.status_code != 206:
                    raise ClipRefused(f"{self.quoted} stopped answering range requests "
                                      f"(HTTP {r.status_code}) while its zip directory was read")
                body = r.content
            except requests.RequestException as exc:
                raise ClipRefused(f"{self.quoted} broke off ({type(exc).__name__}) while its zip "
                                  "directory was read — try again") from None
            finally:
                r.close()
                session.close()
            self.spent += len(body)
            self._buf, self._at = body, self.pos
        off = self.pos - self._at
        out = self._buf[off:off + n]
        self.pos += len(out)
        return out


# --------------------------------------------------------------------------- zips

def _raster_exts() -> set[str]:
    """Extensions GDAL reads as rasters, from GDAL's own driver table — less the
    sidecars and documents that table also lists (an .aux.xml, an ENVI .hdr, a PDF)."""
    from rasterio.drivers import raster_driver_extensions
    return {e for e in raster_driver_extensions() if e not in ("xml", "hdr", "pdf", "dat", "kml", "kmz")}


def zip_rasters(names: list[str]) -> list[str]:
    exts = _raster_exts()
    return [n for n in names if "." in n and n.rsplit(".", 1)[1].lower() in exts
            and not n.lower().endswith(".aux.xml")]


def _listed(names: list[str]) -> str:
    return ", ".join(names[:SHOWN]) + (f" and {len(names) - SHOWN:,} more" if len(names) > SHOWN else "")


def pick_member(names: list[str], member: str | None, quoted: str) -> tuple[str, str | None]:
    """(the member to read, a note) — the named one, or the only raster inside."""
    rasters = zip_rasters(names)
    if member:
        if member in names:
            return member, None
        same = [n for n in names if n.rsplit("/", 1)[-1].lower() == member.rsplit("/", 1)[-1].lower()]
        if len(same) == 1:
            return same[0], None
        raise ClipRefused(f"clip_member {member!r} is not in the zip at {quoted} — it holds "
                          f"{len(names):,} files" + (f"; its rasters: {_listed(rasters)}" if rasters
                                                     else f": {_listed(names)}")
                          + ". Give one exactly as written here")
    if len(rasters) == 1:
        return rasters[0], (f"read {rasters[0]!r}, the one raster in the zip "
                            f"({len(names):,} files)")
    if not rasters:
        vector = any(n.lower().endswith((".shp", ".gpkg", ".geojson", ".kml")) for n in names)
        raise ClipRefused(f"the file at {quoted} is a zip of {len(names):,} files and none is a "
                          f"raster ({_listed(names)})"
                          + (" — it holds vector data: contribute it as kind 'vector'" if vector
                             else " — give the raster itself"))
    raise ClipRefused(f"the file at {quoted} is a zip of {len(rasters):,} rasters "
                      f"({_listed(rasters)}) — name the one to contribute with clip_member, e.g. "
                      f"clip_member: {rasters[0]!r} (or end the url with '#{rasters[0]}'), and "
                      "submit each one you need as its own layer; for tiles, pick the one that "
                      "covers your area" + _proposed(rasters, quoted))


def _proposed(rasters: list[str], quoted: str) -> str:
    """What the server's preparation ops (contrib/prepare) propose for a zip of several
    rasters ('; ' and the proposal), or ''."""
    try:
        from . import prepare
        said = prepare.suggest({"event": "zip_of_rasters", "kind": "raster", "manifest": {},
                                "url": quoted, "rasters": list(rasters)})
    except Exception:                       # a suggestion never breaks a decline
        said = None
    return f"; {said}" if said else ""


def member_head(container, member: str, quoted: str) -> bytes:
    """The first 4 KB of `member` inside a zip — `container` a local path or a probe's
    `found` (read over ranges through the pinned fetcher) — so its format is named from
    its bytes before GDAL opens it."""
    import zipfile
    import zlib
    try:
        src = _Ranged(container, quoted) if isinstance(container, dict) else container
        with zipfile.ZipFile(src) as z, z.open(member) as f:
            return f.read(4096)
    except (zipfile.BadZipFile, zlib.error, OSError, KeyError, RuntimeError,
            NotImplementedError) as exc:
        raise ClipRefused(f"{member!r} inside the zip at {quoted} cannot be read "
                          f"({type(exc).__name__}); re-upload the zip and resubmit") from None


def remote_names(found: dict, quoted: str) -> list[str]:
    """Every file in a remote zip, read from its directory over range requests."""
    import zipfile
    try:
        with zipfile.ZipFile(_Ranged(found, quoted)) as z:
            return [i.filename for i in z.infolist() if not i.is_dir()]
    except zipfile.BadZipFile as exc:
        raise ClipRefused(f"the file at {quoted} begins like a zip but its directory cannot "
                          f"be read ({exc})") from None


# --------------------------------------------------------------------------- the box

def box_of(clip) -> tuple[list[float], str]:
    """([minLon, minLat, maxLon, maxLat], how it was found) for a `clip` — a bbox as
    given, or a place / contributed area name resolved the way every pack resolves a
    place (contributed boundaries first). A name that resolves only to a point is
    refused: the box around it would be a guess."""
    if not isinstance(clip, str):
        return [float(v) for v in clip], ""
    from ..graph.geo import ingest
    try:
        r = ingest._resolve(clip, cap=float("inf"))
    except ValueError as exc:
        raise ClipRefused(f"clip {clip!r} does not resolve to an area: {exc} — give a bbox "
                          "[minLon, minLat, maxLon, maxLat] instead") from None
    if r.get("approximate"):
        raise ClipRefused(f"clip {clip!r} resolved only to a point ({r['how']}); the platform "
                          "will not guess an area around it — ask the contributor for a bbox "
                          "[minLon, minLat, maxLon, maxLat] or a place with a boundary")
    minx, miny, maxx, maxy = r["geometry"].bounds
    return [minx, miny, maxx, maxy], f", the bounding box of {r['name']} ({r['how']})"


def box_problems(clip) -> list[str]:
    """What is wrong with a `clip` as given — checked before anything is fetched."""
    if isinstance(clip, str):
        return [] if clip.strip() else ["clip is empty — give a bbox [minLon, minLat, maxLon, "
                                         "maxLat] in degrees, or a place or contributed area name"]
    if not isinstance(clip, (list, tuple)) or len(clip) != 4:
        return [f"clip must be a bbox [minLon, minLat, maxLon, maxLat] in degrees, or a place "
                f"or contributed area name — got {clip!r}"]
    try:
        w, s, e, n = (float(v) for v in clip)
    except (TypeError, ValueError):
        return [f"clip must hold four numbers [minLon, minLat, maxLon, maxLat] — got {clip!r}"]
    fails = []
    if not (-180 <= w < e <= 180):
        fails.append(f"clip longitudes must run west to east within -180..180 (minLon {w:g} < "
                     f"maxLon {e:g}) — the order is [minLon, minLat, maxLon, maxLat]")
    if not (-90 <= s < n <= 90):
        fails.append(f"clip latitudes must run south to north within -90..90 (minLat {s:g} < "
                     f"maxLat {n:g}) — the order is [minLon, minLat, maxLon, maxLat]")
    return fails


# --------------------------------------------------------------------------- GDAL reads

def vsicurl(url: str) -> str:
    """The GDAL path for an http(s) url, query string and all (the plain /vsicurl/ form
    cannot carry a Drive url's ?id=)."""
    return "/vsicurl?" + urlencode({"use_head": "no", "max_retry": "2", "retry_delay": "1",
                                    "url": url})


def in_zip(container: str, member: str) -> str:
    return "/vsizip/{" + container + "}/" + member


def _gdal_env(agent: str, allowed: str) -> dict:
    from . import rasters
    return {**rasters.gdal_env(allowed),
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "GDAL_HTTP_USERAGENT": agent,
            "GDAL_HTTP_TIMEOUT": "60", "GDAL_HTTP_CONNECTTIMEOUT": "10",
            "GDAL_HTTP_LOW_SPEED_TIME": "30", "GDAL_HTTP_LOW_SPEED_LIMIT": "1",
            "GDAL_HTTP_MAX_RETRY": "2", "GDAL_HTTP_RETRY_DELAY": "1",
            "CPL_VSIL_CURL_USE_HEAD": "NO"}


@functools.lru_cache(maxsize=1)
def _gdal():
    """The GDAL library rasterio loaded, for the one call rasterio has no binding for."""
    import ctypes
    import ctypes.util
    import glob
    import rasterio
    base = os.path.dirname(rasterio.__file__)
    found = (glob.glob(os.path.join(base, ".dylibs", "libgdal*"))
             + glob.glob(os.path.join(base + ".libs", "libgdal*")))
    try:
        lib = ctypes.CDLL(found[0] if found else ctypes.util.find_library("gdal"))
        lib.VSICurlPartialClearCache.argtypes = [ctypes.c_char_p]
        return lib
    except (OSError, AttributeError, TypeError):
        return None


def _forget(path: str) -> None:
    """Drop what GDAL's in-process cache holds for this one remote file. A contributor
    who fixes a file and resubmits the same link must be read afresh — measured: without
    this GDAL served the old size and bytes. Only this file's entries, so a clip running
    for another contributor keeps its own. Best effort."""
    lib = _gdal()
    if lib is not None:
        lib.VSICurlPartialClearCache(path.encode())


def _said(exc: Exception) -> str:
    """GDAL's words without its paths: a /vsicurl? path carries the whole url, and a
    pre-signed url's query is a credential."""
    text = re.sub(r"/vsi(?:zip|curl)[^\s'\"]*", "the remote file", str(exc))
    return " ".join(text.split())[:300]


def cut(path: str, box: list[float] | None, dest: str, cap: int, *, quoted: str,
        remote: dict | None = None, layer_name=None, started: float | None = None,
        driver: str | None = None) -> dict:
    """Read the cells of `path` (a GDAL path) under `box` — every cell when None — and
    write them, unchanged and unresampled, as a compressed GeoTIFF at `dest`. `remote`
    {agent, allowed} when GDAL reads over HTTP. `driver`: the one GDAL driver the
    file's first bytes name (rasters.driver_for), required for a path GDAL reads inside
    a zip or over HTTP. Returns {cells, of, window, notes}. The window is whole cells
    covering the box's extent in the file's own CRS; a window too big to fit the cap
    even compressed is refused before a cell is read."""
    import numpy as np
    import rasterio
    from rasterio.windows import Window
    from rasterio.warp import transform_bounds
    from . import rasters
    started = started or time.monotonic()
    notes: list[str] = []
    # GDAL may open over HTTP the one remote path, or nothing at all for a local file
    env = (_gdal_env(remote["agent"], remote["allowed"]) if remote else rasters.gdal_env())
    if remote:
        _forget(remote["allowed"])
    try:
        with rasterio.Env(**env):
            try:
                target, picked, driver = rasters._open_target(path, layer_name, driver)
            except ValueError as exc:
                raise ClipRefused(str(exc)) from None
            if picked:
                notes.append(picked)
            with rasterio.open(target, driver=driver) as src:
                try:
                    crs, said = rasters.crs_of(src, ", so a box in degrees cannot be placed on it")
                except ValueError as exc:
                    raise ClipRefused(str(exc)) from None
                if said:
                    notes.append(said)
                refusal, band_note = rasters._bands(src)
                if refusal:
                    raise ClipRefused(refusal)
                if band_note:
                    notes.append(band_note)
                if box is None:
                    c0, r0, c1, r1 = 0, 0, src.width, src.height
                else:
                    b = (box if crs.to_epsg() == 4326 else
                         transform_bounds("EPSG:4326", crs, *box, densify_pts=21))
                    inv = ~src.transform
                    cols, rows = zip(*(inv * (x, y) for x in (b[0], b[2]) for y in (b[1], b[3])))
                    c0, c1 = max(0, math.floor(min(cols))), min(src.width, math.ceil(max(cols)))
                    r0, r1 = max(0, math.floor(min(rows))), min(src.height, math.ceil(max(rows)))
                    if c1 > c0 and r1 > r0 and (min(cols) < 0 or min(rows) < 0
                                                or max(cols) > src.width or max(rows) > src.height):
                        notes.append("the clip box reaches beyond the file's edge; the clip holds "
                                     "the part of the box the file covers")
                if c1 <= c0 or r1 <= r0:
                    have = transform_bounds(crs, "EPSG:4326", *src.bounds, densify_pts=21)
                    raise ClipRefused(
                        f"the clip box [{', '.join(f'{v:g}' for v in box)}] does not overlap the "
                        f"file at {quoted}, which covers lon {have[0]:.3f} to {have[2]:.3f}, lat "
                        f"{have[1]:.3f} to {have[3]:.3f} — check the order [minLon, minLat, "
                        "maxLon, maxLat] and that this is the file for the area")
                w, h = c1 - c0, r1 - r0
                dtype = src.dtypes[0]
                raw = w * h * np.dtype(dtype).itemsize
                if raw > RAW_FACTOR * cap:
                    if box is None:
                        raise ClipRefused(ask_text(
                            f"the raster in {quoted} is {w:,} x {h:,} cells ({size_text(raw)} "
                            f"unpacked), far over the {size_text(cap)} cap"))
                    raise ClipRefused(
                        f"the clip box covers {w:,} x {h:,} cells of the file ({size_text(raw)} "
                        f"before compression) — the stored clip must fit the {size_text(cap)} cap; "
                        "give a smaller box")
                win = Window(c0, r0, w, h)
                with rasterio.open(dest, "w", driver="GTiff", width=w, height=h, count=1,
                                   dtype=dtype, crs=crs, transform=src.window_transform(win),
                                   nodata=src.nodata, BIGTIFF="IF_SAFER",
                                   **rasters.packing(dtype, w, h)) as dst:
                    step = 1024
                    for r in range(0, h, step):
                        if time.monotonic() - started > CLIP_DEADLINE:
                            raise ClipRefused(f"reading the clip from {quoted} took over "
                                              f"{CLIP_DEADLINE} s — give a smaller box, or try "
                                              "again if the host was slow")
                        part = src.read(1, window=Window(c0, r0 + r, w, min(step, h - r)))
                        dst.write(part, 1, window=Window(0, r, w, part.shape[0]))
                    try:
                        dst.write_colormap(1, src.colormap(1))
                    except ValueError:
                        pass                      # no palette: nothing to carry
                of = [src.width, src.height]
    except ClipRefused:
        raise
    except Exception as exc:
        raise ClipRefused(f"could not read the clip from {quoted}: {_said(exc)}") from None
    return {"cells": [w, h], "of": of, "window": [c0, r0, w, h], "notes": notes}


def note(m_url: str, box: list[float] | None, where: str | None, made: dict,
         member: str | None, remote: bool) -> str:
    """The clip in words, for the row's `prepared` list (every pack reads it as 'At
    ingest: ...')."""
    w, h = made["cells"]
    W, H = made["of"]
    if box is None:
        return (f"read {member!r} from inside the zip at {fetch_policy.shown(m_url)}, all "
                f"{w:,} x {h:,} cells; cell values and grid unchanged")
    return (f"clipped at the source to lon {box[0]:g} to {box[2]:g}, lat {box[1]:g} to "
            f"{box[3]:g}{where}: read {w:,} x {h:,} of the file's {W:,} x {H:,} cells"
            + (f" from {member!r} inside the zip" if member else "")
            + f" at {fetch_policy.shown(m_url)}"
            + (" over HTTP range requests (nothing else was downloaded)" if remote else "")
            + "; cell values and grid unchanged, no resampling")


def ask_for_clip(url: str, cap: int, member: str | None = None, size: int | None = None) -> str:
    """The decline for a raster over the cap with no `clip`: worded as a missing field,
    so the contributor's agent asks them for the area — never picks one itself. `size`:
    what the refused fetch was told, when a probe learns nothing."""
    quoted = str(url).strip()
    base, frag = split_member(url)
    try:
        found = probe(base)
    except (fetch_policy.FetchRefused, ClipRefused):
        found = None
    size = (found or {}).get("size") or size
    big = (f"{quoted} is {size_text(size)}, over the {size_text(cap)} cap" if size
           else f"{quoted} is over the {size_text(cap)} cap")
    if found and not found["ranges"] and size:
        return whole_only(url, {**found, "size": size}, cap)
    zipped = ""
    if found and found["ranges"]:
        from .rasters import sniff
        if sniff(found["head"]) == "zip":
            try:
                names = remote_names(found, quoted)
                inside = zip_rasters(names)
                if not (member or frag) and len(inside) != 1:
                    zipped = (f" It is a zip of {len(inside):,} rasters ({_listed(inside)}): ask "
                              "which one too, and give it as clip_member"
                              + _proposed(inside, quoted) + ".")
            except (ClipRefused, fetch_policy.FetchRefused):
                pass
    return ask_text(big) + zipped


def ask_text(what: str) -> str:
    """`what` ("<url> is 1.5 GB, over the 50 MB cap") as a missing `clip`."""
    return (f"missing required field 'clip': {what} — ask the contributor which area to keep "
            "(clip: a bbox [minLon, minLat, maxLon, maxLat] in degrees, or a place or "
            "contributed area name) and resubmit with it; never guess one. The platform then "
            "reads only that part of the file and stores it, and the cap applies to the "
            "stored clip.")


def whole_only(url: str, found: dict, cap: int) -> str:
    """A file over the cap from a host that will not send a part of it. `found["size"]`
    None: the host did not say how big it is either (a streamed reply), so the clip
    already given cannot be read remotely — asking for it again would loop."""
    host = urlparse(found["final"]).hostname
    size = found.get("size")
    big = (f"is {size_text(size)}, over the {size_text(cap)} cap" if size
           else f"is over the {size_text(cap)} cap")
    why = ("it does not answer range requests" if size
           else "it does not answer range requests or say the file's size")
    return (f"{str(url).strip()} {big}, and {host} sends it only whole ({why}), so the "
            "platform cannot read just a part of it — download it, clip it to the area "
            "yourself (gdal_translate -projwin, or QGIS: Raster -> Extraction -> Clip) and give "
            "the clip's link, e.g. shared from Google Drive (Anyone with the link)")


# A file that is a RECIPE naming other files or services: GDAL finds these markers
# anywhere in a file's first bytes (a VRT after a line of junk is still a VRT), so the
# gate looks for them in the first 4 KB, whatever the first byte is.
_RECIPES = ((b"<vrtdataset", "a GDAL virtual raster (VRT)"),
            (b"<ogrvrtdatasource", "an OGR virtual dataset (VRT)"),
            (b"<gdaltileindexdataset", "a GDAL tile index"),
            (b"<gdal_wms", "a GDAL web-map service description"),
            (b"<gdal_wmts", "a GDAL web-map tile service description"),
            (b"<wms_capabilities", "a WMS service description"),
            (b"<wmt_ms_capabilities", "a WMS service description"),
            (b"<wcs_capabilities", "a WCS service description"),
            (b"<capabilities", "a web map service description"))


def recipe(head: bytes) -> str | None:
    """What recipe `head` holds (a VRT, a service description) in its first 4 KB, or None."""
    low = bytes(head[:4096]).lower()
    return next((what for marker, what in _RECIPES if marker in low), None)


def text_problem(head: bytes, url: str) -> str | None:
    """A text or XML file offered as a raster — a VRT, a map service's description: GDAL
    would open whatever it names, on this server or anywhere. Refused before GDAL sees it,
    when its first byte is '<' or when a recipe's marker is anywhere in its first 4 KB."""
    kind = recipe(head)
    if kind is None:
        lead = bytes(head[:512]).lstrip(b"\xef\xbb\xbf \t\r\n")
        if not lead.startswith(b"<"):
            return None
        kind = "an XML/text file"
    return (f"{url} answered with {kind}, not a raster: a description that points at other "
            "files or services, which the platform does not follow — give the raster file "
            "itself (GeoTIFF, .img, NetCDF, GeoPackage)")