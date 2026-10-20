"""from_folder: a vector layer delivered as loose files, assembled on the server.

The case: hubs keep a shapefile as its parts in a shared Google Drive folder — .shp, .shx,
.dbf, .prj, .cpg beside .sbn/.sbx indexes, .xml metadata and an editor's lock file, often
in subfolders — and the platform takes one file per contribution, so every one was zipped
by hand. Now the folder link goes in with from_folder: true: the server lists the folder
and its subfolders, picks the layer (layer_name, a path in the folder, or the only one),
downloads its parts through fetch_policy and zips them; a GeoPackage, GeoJSON or zip on its
own in the folder is fetched as it is. Parts that are separate links elsewhere go in as
shapefile_parts. A shapefile with no .prj is declined unless the contributor states its
coordinate system (shapefile_crs). The listing, every part and the zip are on the record
with their sha256.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import zipfile
from pathlib import Path

from . import _drive

NAME = "from_folder"
STEP = "assemble"
KINDS = ("vector",)
NEEDS_INPUT = False
FIELDS = {
    "from_folder": "true when `url` is a Google Drive FOLDER link (shared 'Anyone with the "
                   "link') holding the layer as loose files — a shapefile's .shp, .shx, .dbf, "
                   ".prj, .cpg, or one GeoPackage/GeoJSON/zip among other files. The platform "
                   "lists the folder and its subfolders, picks the layer, downloads its parts "
                   "and zips them itself — nothing to zip by hand; .sbn/.sbx indexes, .xml "
                   "metadata and lock files are left out. When the folder holds several layers, "
                   "name one with layer_name (the shapefile's name without .shp), or give "
                   "from_folder the layer's path inside the folder instead of true (a subfolder "
                   "such as 'Costa Rica/cuencas50mil_costeras2', or a file). The record keeps "
                   "every file's link and sha256",
    "shapefile_parts": "when a shapefile's parts are separate links and NOT in one shared "
                       "Drive folder: the link of each part, [<.shp link>, <.shx link>, "
                       "<.dbf link>, <.prj link>] (and the .cpg's, if there is one); `url` is "
                       "one of them. The platform downloads the parts and zips them itself",
    "shapefile_crs": "ONLY when the platform says the shapefile came without a .prj: its "
                     "coordinate system as the contributor states it, e.g. 'EPSG:5367' (CR05 / "
                     "CRTM05) or 'EPSG:4326' (longitude/latitude) — ASK them, never guess. It is "
                     "written as the layer's .prj and the record says so",
}

PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".cst")    # what the reader reads
QGIS_PRJ = ".qpj"                     # QGIS's record of the CRS: read when no .prj came
MAX_LINKS = 12
_HINTS = 3                            # other .prj files read to suggest a CRS
_SHOWN = 12
_EPOCH = (1980, 1, 1, 0, 0, 0)        # a zip's dates, fixed: the same parts give the same sha256


# --------------------------------------------------------------------------- checks

def _subpath(v) -> str | None:
    s = str(v).strip().strip("/")
    if not s or ".." in s.split("/"):
        return None
    return s


def _scope(v) -> str | None:
    """None for the whole folder (true), else the path inside it."""
    if v is True or str(v).strip().lower() == "true":
        return None
    return _subpath(v)


def _crs(v):
    from rasterio.crs import CRS
    if v is None or isinstance(v, bool):
        return None
    try:
        return CRS.from_user_input(v if isinstance(v, int) else str(v).strip())
    except Exception:
        return None


def _epsg(crs):
    """The EPSG code of a CRS as a .prj carries it (ESRI WKT), else None."""
    from rasterio.crs import CRS
    try:
        return CRS.from_wkt(crs.to_wkt(version="WKT1_ESRI"), morph_from_esri_dialect=True).to_epsg()
    except Exception:
        return None


def _label(crs) -> str:
    m = re.match(r'\s*\w+\["([^"]+)"', crs.to_wkt())
    code = _epsg(crs)
    name = m.group(1) if m else "a coordinate system"
    return f"{name} (EPSG:{code})" if code else name


def _read_prj(text: str):
    from rasterio.crs import CRS
    for esri in (True, False):
        try:
            return CRS.from_wkt(text, morph_from_esri_dialect=esri)
        except Exception:
            continue
    return None


def validate(manifest) -> list[str]:
    from .. import fetch_policy
    m = manifest
    problems = []
    ff, links, crs = m.get("from_folder"), m.get("shapefile_parts"), m.get("shapefile_crs")
    if ff is not None and links is not None:
        problems.append("give from_folder (the Drive folder's link as url) or shapefile_parts "
                        "(each part's link), not both")
    if ff is not None:
        if not (ff is True or str(ff).strip().lower() == "true"
                or (isinstance(ff, str) and _subpath(ff))):
            problems.append("from_folder is true, or the layer's path inside the folder (a "
                            "subfolder such as 'Costa Rica/cuencas50mil_costeras2', or a file) "
                            f"— got {ff!r}")
        if not _drive.folder_id(m.get("url")):
            problems.append("from_folder lists a shared Google Drive FOLDER: url must be the "
                            "folder's link (https://drive.google.com/drive/folders/<id>, shared "
                            "'Anyone with the link'); parts that are separate links elsewhere "
                            "go in as shapefile_parts: [<each part's link>]")
    if links is not None:
        if (not isinstance(links, list) or not links
                or not all(isinstance(x, str) and x.strip() for x in links)):
            problems.append("shapefile_parts is a list of links, one per part of the shapefile: "
                            "[<.shp link>, <.shx link>, <.dbf link>, <.prj link>]")
        elif len(links) > MAX_LINKS:
            problems.append(f"shapefile_parts lists {len(links)} links — one shapefile has at "
                            f"most {MAX_LINKS} parts; list the parts of ONE layer")
        else:
            for x in links:
                problems += [f"shapefile_parts {x.strip()}: {p}" for p in fetch_policy.check_url(x)]
    if crs is not None:
        if ff is None and links is None:
            problems.append("shapefile_crs is the coordinate system of a shapefile assembled from "
                            "its parts (from_folder or shapefile_parts) that came without a .prj; "
                            "a zipped shapefile needs its .prj inside the zip — drop shapefile_crs")
        elif _crs(crs) is None:
            problems.append(f"shapefile_crs {crs!r} is no coordinate system the platform reads — "
                            "give the EPSG code the contributor states, e.g. 'EPSG:5367' (CR05 / "
                            "CRTM05), or the WKT text of a .prj")
    return problems


def answers(manifest, problem: str) -> bool:
    """The url refusal this op answers: a Drive FOLDER link, which it lists itself (with
    shapefile_parts the folder is only where the parts live)."""
    from urllib.parse import urlparse
    from .. import fetch_policy
    url = str(manifest.get("url") or "").strip()
    if not _drive.folder_id(url):
        return False
    return problem == fetch_policy._drive_problem(urlparse(url))


# --------------------------------------------------------------------------- run

def run(ctx):
    if ctx.manifest.get("shapefile_parts") is not None:
        return _from_links(ctx)
    return _from_drive(ctx)


def _get(ctx, link: str) -> str:
    """ctx.fetch, once more when the host did not answer: one stalled read among a layer's
    parts (measured on Drive, 2 in ~40 part downloads on 2026-10-19) should not decline it."""
    import requests
    from .. import fetch_policy
    try:
        return ctx.fetch(link)
    except (fetch_policy.FetchRefused, requests.RequestException) as exc:
        if (isinstance(exc, fetch_policy.FetchRefused)
                and "the site did not answer" not in str(exc)):   # fetch_policy's words for it
            raise
        ctx.check_time()
        return ctx.fetch(link)


def _ext(name: str, exts) -> str | None:
    low = name.lower()
    return next((e for e in sorted(exts, key=len, reverse=True) if low.endswith(e)), None)


def _names(entries, limit=_SHOWN) -> str:
    shown = [e.path for e in entries[:limit]]
    more = len(entries) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "") if shown else "nothing"


def _from_drive(ctx):
    from . import PrepRefused, PreparedFile
    m = ctx.manifest
    url = str(m["url"]).strip()
    fid = _drive.folder_id(url)
    files, listed = _drive.walk(fid, url, ctx)
    index = ctx.tmp(".json", "listing")
    Path(index).write_text(json.dumps([{"path": e.path, "id": e.id}
                                       for e in sorted(files, key=lambda e: e.path)],
                                      ensure_ascii=False, indent=0))
    ctx.adopt(index, (), {"op": "list_folder", "inputs": {
        "url": url, "listing": _drive.LISTING.format(id=fid), "folders": listed,
        "files": len(files)}})
    ctx.record["read"] = "listed the Google Drive folder; the layer's files downloaded whole"
    kind, picked = _pick(m, files, url)
    if kind == "file":
        link = _drive.FILE.format(id=picked.id)
        path = _get(ctx, link)
        ctx.filename = picked.name
        return PreparedFile(path, [f"fetched {picked.path} from the Google Drive folder {url} "
                                   f"({listed} folder(s), {len(files)} file(s) listed)"],
                            {"inputs": {"folder": url, "picked": picked.path, "link": link}})
    stem = picked.name[:-4]
    siblings = [e for e in files if e.parent == picked.parent
                and e.name.lower().startswith(stem.lower() + ".")]
    parts, spare = {}, None
    for e in siblings:
        ext = _ext(e.name, PARTS + (QGIS_PRJ,))
        if ext is None or e.name.lower() != (stem + ext).lower():
            continue
        if ext == QGIS_PRJ:
            spare = e
            continue
        if ext in parts:
            raise PrepRefused(f"the folder {url} holds two files named {e.path!r} — Drive keeps "
                              "both; remove the copy that is not the layer's and resubmit")
        parts[ext] = e
    where = f"{picked.parent}/" if picked.parent else ""
    if ".dbf" not in parts:
        raise PrepRefused(f"the folder {url} holds {picked.path} but no {where}{stem}.dbf — the "
                          "shapefile's attribute table; upload it beside the .shp and resubmit")
    crs = _crs(m.get("shapefile_crs"))
    if ".prj" not in parts and spare is None and crs is None:
        raise PrepRefused(_no_prj(f"{where}{stem}.shp", f"upload the layer's .prj to {url} and "
                                  "resubmit", _hints(files, picked, ctx)))
    if ".prj" not in parts and spare is not None and crs is None:
        parts[QGIS_PRJ] = spare
    links = {stem + ext: (_drive.FILE.format(id=e.id), e.path) for ext, e in parts.items()}
    used = set(parts.values())
    left = sorted(e.name for e in siblings if e not in used)
    note = (f"assembled on the server from the Google Drive folder {url}: the shapefile "
            f"{where}{stem}")
    return _assemble(ctx, stem, links, crs, note, left,
                     {"folder": url, "picked": picked.path, "folders": listed,
                      "files": len(files)})


def _pick(m, files, url):
    """('shapefile', the .shp entry) or ('file', a vector file entry), or declined with
    what the folder holds."""
    from . import PrepRefused
    scope = _scope(m.get("from_folder"))
    inside = files
    said = ""
    if scope:
        inside = [e for e in files if e.path == scope or e.path.startswith(scope + "/")]
        said = f" under {scope!r}"
        if not inside:
            tops = sorted({e.path.split("/", 1)[0] for e in files})
            raise PrepRefused(f"the folder {url} holds no subfolder or file {scope!r}; at its top "
                              f"it holds: {', '.join(tops[:_SHOWN]) or 'nothing'} — give "
                              "from_folder a path as the folder shows it, or true")
    shps = [e for e in inside if e.name.lower().endswith(".shp")]
    singles = [e for e in inside if _reads(e.name)]
    name = m.get("layer_name")
    if name:
        named = [e for e in shps if e.name[:-4] == str(name)]
        _once(named, url)
        if len(named) == 1:
            return "shapefile", named[0]
        if len(named) > 1:
            raise PrepRefused(f"the folder {url} holds {len(named)} shapefiles named {name!r} "
                              f"({_names(named)}) — give from_folder the one's subfolder, e.g. "
                              f"from_folder: {named[0].parent!r}")
        if not shps and len(singles) == 1 and singles[0].name.lower().endswith(".gpkg"):
            return "file", singles[0]                 # layer_name is the GeoPackage's layer
        held = [e.name[:-4] for e in shps]
        raise PrepRefused(f"no shapefile named {name!r} in the folder {url}{said}; it holds "
                          + (f"the shapefiles {', '.join(held[:_SHOWN])}" if held
                             else f"no shapefile ({_names(inside)})")
                          + " — layer_name is the shapefile's name without .shp, exactly as the "
                          "folder lists it")
    found = [("shapefile", e) for e in shps] + [("file", e) for e in singles]
    _once([e for _, e in found], url)
    if len(found) == 1:
        return found[0]
    if not found:
        raise PrepRefused(f"the folder {url}{said} holds no shapefile (.shp) and no GeoPackage, "
                          f"GeoJSON or zip — it holds: {_names(inside)}. A raster, a table or a "
                          "document goes in as its own kind, from its file's link")
    shown = [f"{e.path[:-4]} (shapefile)" if k == "shapefile" else e.path for k, e in found]
    eg = next((e.name[:-4] for k, e in found if k == "shapefile"), None)
    raise PrepRefused(f"missing required field 'layer_name': the folder {url}{said} holds "
                      f"{len(found)} layers: {', '.join(shown[:_SHOWN])}"
                      + (f" and {len(shown) - _SHOWN} more" if len(shown) > _SHOWN else "")
                      + " — ASK the contributor which one (never guess), and name it: "
                      + (f"layer_name: {eg!r} (a shapefile's name without .shp), or " if eg else "")
                      + "from_folder: '<its path in the folder>'; each layer is its own "
                      "contribution")


def _once(entries, url) -> None:
    """Drive keeps two files of one name side by side; which is the layer is not ours to say."""
    from . import PrepRefused
    paths = [e.path for e in entries]
    twice = sorted({p for p in paths if paths.count(p) > 1})
    if twice:
        raise PrepRefused(f"the folder {url} holds two files named {twice[0]!r} — Drive keeps "
                          "both; remove the copy that is not the layer's and resubmit")


def _reads(name: str) -> bool:
    """A file the vector gate reads whole (GeoJSON, GeoPackage, zip) — by the reader's
    own rule, never a list here; a shapefile's .shp is picked with its parts instead."""
    from ...graph.geo import vecio
    return vecio.kind_of(b"", name) != "unknown"


def _hints(files, picked, ctx) -> list[str]:
    """What the folder's other .prj files say, nearest first — a candidate for the
    contributor to confirm, never applied."""
    from .. import fetch_policy
    others = sorted((e for e in files if e.name.lower().endswith(".prj")),
                    key=lambda e: (e.parent != picked.parent, e.path))
    said = []
    for e in others[:_HINTS]:
        ctx.check_time()
        try:
            raw, _ = fetch_policy.fetch(_drive.FILE.format(id=e.id), max_bytes=64 << 10)
        except fetch_policy.FetchRefused:
            continue
        crs = _read_prj(raw.decode("utf-8", "replace"))
        if crs is not None:
            said.append(f"{e.path} names {_label(crs)}")
    return said


def _no_prj(shp: str, fix: str, hints: list[str]) -> str:
    text = (f"missing required field 'shapefile_crs': {shp} came without a .prj, so its "
            "coordinate system is unknown — ASK the "
            "contributor which it is (never guess) and resubmit with shapefile_crs: "
            "'EPSG:<code>'")
    if hints:
        text += (f" (the folder's other .prj files: {'; '.join(hints)} — if this layer is in "
                 "the same, that is its code)")
    text += f"; if its coordinates are longitude/latitude on WGS 84, that is 'EPSG:4326'. Or {fix}"
    return text


def _from_links(ctx):
    from .. import fetch_policy
    from . import PrepRefused
    m = ctx.manifest
    url = str(m["url"]).strip()
    links = [] if _drive.folder_id(url) else [url]
    seen = {fetch_policy.direct_url(x) for x in links}
    for x in m["shapefile_parts"]:
        if fetch_policy.direct_url(x.strip()) not in seen:
            seen.add(fetch_policy.direct_url(x.strip()))
            links.append(x.strip())
    ctx.record["read"] = "each part downloaded whole"
    got, left = {}, []
    for link in links:
        path = _get(ctx, link)
        name = Path(str(ctx.steps[-1]["inputs"].get("filename") or "")).name
        ext = _ext(name, PARTS + (QGIS_PRJ,))
        if ext is None:
            if re.search(r"\.(sbn|sbx|xml|lock|qmd|qix|aih|ain|atx|fbn|fbx|ixs|mxs)$", name, re.I):
                left.append(name)
                continue
            raise PrepRefused(f"{link} is {name!r}, not a part of a shapefile ({', '.join(PARTS)}) "
                              "— list only the parts of the layer in shapefile_parts")
        if ext in got:
            raise PrepRefused(f"shapefile_parts gives two {ext} files ({got[ext][2]!r} and "
                              f"{name!r}) — list the parts of ONE layer")
        got[ext] = (path, link, name)
    stems = sorted({n[:-len(e)] for e, (_, _, n) in got.items()}, key=str.lower)
    if ".shp" not in got:
        raise PrepRefused("shapefile_parts holds no .shp — " + (
            f"it gives {', '.join(n for _, _, n in got.values())}; " if got else "")
            + "add the .shp's link")
    if len({s.lower() for s in stems}) > 1:
        raise PrepRefused(f"the parts are of different layers ({', '.join(stems)}) — a "
                          "shapefile's parts share one name; list the parts of ONE layer")
    stem = got[".shp"][2][:-4]
    if ".dbf" not in got:
        raise PrepRefused(f"shapefile_parts holds {stem}.shp but no {stem}.dbf — the attribute "
                          "table; add its link")
    crs = _crs(m.get("shapefile_crs"))
    if ".prj" not in got and QGIS_PRJ not in got and crs is None:
        raise PrepRefused(_no_prj(f"{stem}.shp", "add the link of the layer's .prj to "
                                  "shapefile_parts", []))
    if QGIS_PRJ in got and (".prj" in got or crs is not None):
        left.append(got.pop(QGIS_PRJ)[2])
    files = {stem + ext: (link, name) for ext, (_, link, name) in got.items()}
    local = {stem + ext: p for ext, (p, _, _) in got.items()}
    return _assemble(ctx, stem, files, crs, f"assembled on the server from {len(links)} links: "
                     f"the shapefile {stem}", left, {"links": links}, local=local)


def _assemble(ctx, stem, links, crs, note, left, inputs, local=None):
    """Fetch the parts (unless `local` has them), check the .shp, write the .prj a
    stated CRS gives, zip them with fixed dates; the PreparedFile with the record."""
    from . import PrepRefused, PreparedFile
    local = dict(local or {})
    order = sorted(links, key=lambda a: (_ext(a, PARTS + (QGIS_PRJ,)) != ".shp", a.lower()))
    for arc in order:
        if arc not in local:
            local[arc] = _get(ctx, links[arc][0])
        if arc.lower().endswith(".shp"):
            records, vertices = _count(local[arc], links[arc][1], ctx)
            ctx.check_vertices(vertices, f"the shapefile {stem}")
    notes = []
    extra = {}
    qpj = next((a for a in local if a.lower().endswith(QGIS_PRJ)), None)
    prj = next((a for a in local if a.lower().endswith(".prj")), None)
    if qpj:
        text = Path(local.pop(qpj)).read_text("utf-8", "replace")
        found = _read_prj(text)
        if found is None:
            raise PrepRefused(f"{stem}{QGIS_PRJ} (QGIS's record of the CRS, the only one that came "
                              "with the layer) cannot be read as a coordinate system — resubmit "
                              "with shapefile_crs: 'EPSG:<code>' as the contributor states it")
        extra[stem + ".prj"] = found.to_wkt(version="WKT1_ESRI")
        notes.append(f"no .prj came with it: its {QGIS_PRJ} (QGIS's record of the CRS, "
                     f"{_label(found)}) was written as {stem}.prj")
    if crs is not None:
        if prj:
            own = _read_prj(Path(local[prj]).read_text("utf-8", "replace"))
            if own is not None and _epsg(own) != _epsg(crs):
                raise PrepRefused(f"{stem}.prj names {_label(own)} but shapefile_crs says "
                                  f"{_label(crs)} — the .prj is the producer's: drop "
                                  "shapefile_crs, or ASK the contributor which is right")
            notes.append(f"shapefile_crs {_label(crs)} agrees with the layer's own .prj")
        elif not qpj:
            extra[stem + ".prj"] = crs.to_wkt(version="WKT1_ESRI")
            notes.append(f"no .prj came with it: the contributor's shapefile_crs "
                         f"({_label(crs)}) was written as {stem}.prj")
    out = ctx.tmp(".zip", stem.replace(" ", "_")[:60] or "shapefile")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for arc in sorted(local, key=str.lower):
            zi = zipfile.ZipInfo(arc, date_time=_EPOCH)
            zi.compress_type, zi.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            with open(local[arc], "rb") as src, z.open(zi, "w") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            ctx.check_time()
        for arc, text in sorted(extra.items()):
            zi = zipfile.ZipInfo(arc, date_time=_EPOCH)
            zi.compress_type, zi.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            z.writestr(zi, text)
    members = sorted([*local, *extra], key=str.lower)
    head = f"{note} ({', '.join(a[len(stem):] for a in members)}; {records:,} records) zipped"
    if left:
        head += f"; left out: {', '.join(left[:_SHOWN])}"
    if not any(a.lower().endswith(".shx") for a in members):
        notes.append("no .shx came with it; none is needed — the platform reads the .shp's "
                     "records in order")
    ctx.filename = f"{stem}.zip"
    return PreparedFile(out, [head, *notes], {"inputs": {
        **inputs, "members": {a: links[a][0] for a in sorted(links, key=str.lower)},
        **({"written": sorted(extra)} if extra else {}),
        **({"left_out": left} if left else {}),
        **({"shapefile_crs": str(ctx.manifest.get("shapefile_crs"))} if crs is not None else {})}})


_POINTS = {1, 11, 21}                          # shape types: one point a record
_MULTI = {8, 18, 28}                           # numPoints at byte 36
_PARTED = {3, 5, 13, 15, 23, 25, 31}           # numPoints at byte 40


def _count(path, shown, ctx) -> tuple[int, int]:
    """(records, vertices) of a .shp, read record header by record header."""
    from . import PrepRefused
    records = vertices = 0
    with open(path, "rb") as f:
        head = f.read(100)
        if len(head) < 100 or head[:4] != b"\x00\x00\x27\x0a":
            raise PrepRefused(f"{shown} is not a shapefile's .shp (its header is not one) — "
                              "check the file in the folder")
        while True:
            h = f.read(8)
            if len(h) < 8:
                break
            content = struct.unpack(">i", h[4:8])[0] * 2
            body = f.read(min(max(content, 0), 44))
            if len(body) < 4:
                break
            shape = struct.unpack("<i", body[:4])[0]
            if shape in _POINTS:
                vertices += 1
            elif shape in _MULTI and len(body) >= 40:
                vertices += struct.unpack("<i", body[36:40])[0]
            elif shape in _PARTED and len(body) >= 44:
                vertices += struct.unpack("<i", body[40:44])[0]
            records += 1
            f.seek(content - len(body), 1)
            if records % 20000 == 0:
                ctx.check_time()
    return records, vertices


# --------------------------------------------------------------------------- by itself

def detect(kind: str, manifest) -> dict | None:
    """A vector's url that is a Google Drive FOLDER link: the folder is listed and the
    layer assembled from it — nothing in that is a person's choice (a folder of several
    layers is declined by the listing itself, asking which)."""
    if kind not in KINDS or not _drive.folder_id(manifest.get("url")):
        return None
    return {"fields": {"from_folder": True},
            "why": "the url is a Google Drive FOLDER, so the platform lists it and assembles "
                   "the layer from the folder's own files (a shapefile's parts zipped on the "
                   "server)"}


# --------------------------------------------------------------------------- suggestions

def suggest(situation: dict) -> str | None:
    """A Drive FOLDER link: from_folder. One loose part of a shapefile: the folder, or
    every part's link."""
    from .. import fetch_policy
    m = situation.get("manifest") or {}
    if situation.get("kind") not in KINDS or any(m.get(f) is not None for f in FIELDS):
        return None                       # declared already: its own checks say what is wrong
    event, url = situation.get("event"), situation.get("url")
    if event == "url_refused" and _drive.folder_id(url):
        return ("or resubmit the same folder link with from_folder: true — the platform lists "
                "the folder and its subfolders, downloads the layer's parts (.shp, .shx, .dbf, "
                ".prj, .cpg) and zips them itself, nothing to zip by hand; if the folder holds "
                "several layers, add layer_name: '<the shapefile's name without .shp>'")
    if event == "shapefile_part":
        drive = fetch_policy.drive_file_id(url)
        return (("or give the link of the shared Google Drive FOLDER that holds the parts with "
                 "from_folder: true, " if drive else "or ")
                + "resubmit with shapefile_parts: [<the .shp link>, <the .shx link>, <the .dbf "
                "link>, <the .prj link>] (and the .cpg's, if there is one) — the platform "
                "downloads the parts and zips them itself")
    return None
