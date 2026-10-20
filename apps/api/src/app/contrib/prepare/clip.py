"""clip, for vector layers: a layer too big or too wide for the cap, cut to the
features of an area — and a service's layer read whole, page by page.

The cases: HDX's Costa Rica boundaries come as one 145 MB zip of every admin level, so
each level was zipped on its own by hand; Global Mangrove Watch v4.1 is one 168 MB
gzipped GeoPackage of the world, filtered to Costa Rica by hand; a FeatureServer query
stops at its record limit (exceededTransferLimit), so the soils map was taken from a
portal download instead; a WFS layer was asked for one box by a hand-built request.
Now the link goes in as it is, with `clip` (the area, as a raster's clip: a bbox, a
place or a contributed area name) or `whole: true`. A file is downloaded whole, streamed
to disk under the preparation budget, decompressed or unpacked when it must be, and read
feature by feature; the features that intersect the box are kept whole (cut at its edge
only with `cut: true`, the contributor's choice), copied as they are, and the gate's own
reader converts what is kept. A service's query gets the box added and is paged until
every feature it counts is in. The record keeps the box, every input's sha256 and, for a
service, every page. A service url or a gzipped layer given with neither is read whole
by the platform itself (detect); the area is always the contributor's to give.
"""

from __future__ import annotations

import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

NAME = "clip"
STEP = "clip"
KINDS = ("vector",)
NEEDS_INPUT = False          # a service is paged here; a file is fetched here too
FIELDS = {
    "clip": "the area to keep, as a bbox [minLon, minLat, maxLon, maxLat] in degrees, or a "
            "place or contributed area name (its bounding box is used, as for a raster). ASK "
            "the contributor for the area — never guess one. Every feature that INTERSECTS "
            "it is kept whole (not cut at the edge), the rest left out. A FILE — GeoJSON, a "
            "zipped shapefile or a GeoPackage, or any of them gzipped (.gz) — may be far over "
            "the size cap: the platform downloads it whole (streamed to disk, under its "
            "preparation budget) and filters it as it reads; in an archive of several layers, "
            "layer_name names the shapefile or GeoPackage table. An ArcGIS "
            "FeatureServer/MapServer layer or query url, or a WFS GetFeature url, gets the box "
            "added to the request and is paged until every feature in it is in. The size cap "
            "applies to the layer as stored; the record keeps the box and every input's sha256",
    "whole": "true to take the WHOLE layer from a source the size cap or a server's record "
             "limit would stop: an ArcGIS FeatureServer/MapServer query (or layer) url or a WFS "
             "GetFeature url is paged (resultOffset/resultRecordCount; WFS startIndex/count) "
             "until every feature the server counts is in — an export cut at its record limit "
             "(exceededTransferLimit) is read whole this way; a file over the cap (an archive "
             "of several layers: name the one with layer_name) is downloaded whole, streamed "
             "to disk, and only that layer is kept. Nothing is filtered (with clip, only the "
             "area's features); the size cap applies to the layer as stored",
    "cut": "true, with `clip`, to CUT the features that cross the area's edge at it (shapely, "
           "on longitude/latitude) instead of keeping them whole — for a layer whose features "
           "reach far beyond the area (a country's admin polygons, a national river network) "
           "and would not fit the cap whole. Areas and lengths inside the area are exact; a "
           "cut feature's own attributes (an area or length column, a total) still describe "
           "the whole feature. The contributor's choice — ASK them, never set it unasked; "
           "the record and every citation say which features were cut",
}

ARCGIS_PAGE = 2000           # features per request at most (the server's own limit if lower)
WFS_PAGE = 2000
_ARCGIS = re.compile(r"/(?:FeatureServer|MapServer)/(\d+)(?:/query)?/?$", re.I)
# the request parameters this op sets itself (an ArcGIS layer's, a WFS's), case-blind
_ARCGIS_OURS = {"f", "resultoffset", "resultrecordcount", "returncountonly", "returnidsonly",
                "outsr", "returngeometry", "orderbyfields", "objectids"}
_ARCGIS_BOX = {"geometry", "geometrytype", "insr", "spatialrel"}
_WFS_OURS = {"service", "version", "request", "typename", "typenames", "startindex", "count",
             "maxfeatures", "resulttype"}


# --------------------------------------------------------------------------- the url

def service(url) -> dict | None:
    """{'type': 'arcgis', 'layer', 'params'} for an ArcGIS FeatureServer/MapServer layer or
    query url, {'type': 'wfs', 'base', 'params'} for a WFS GetFeature url, else None."""
    try:
        p = urlparse(str(url or "").strip().split("#", 1)[0])
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    params = parse_qsl(p.query, keep_blank_values=True)
    m = _ARCGIS.search(p.path)
    if m:
        return {"type": "arcgis", "params": params,
                "layer": urlunparse(p._replace(path=p.path[:m.end(1)], query="", fragment=""))}
    low = {k.lower(): v for k, v in params}
    if (low.get("service", "").lower() == "wfs"
            or (low.get("request", "").lower() == "getfeature"
                and (low.get("typenames") or low.get("typename")))):
        return {"type": "wfs", "params": params,
                "base": urlunparse(p._replace(query="", fragment=""))}
    return None


def _wfs_problems(svc: dict, clip) -> list[str]:
    low = {k.lower(): v for k, v in svc["params"]}
    problems = []
    req = low.get("request", "")
    if req and req.lower() != "getfeature":
        problems.append(f"the WFS url asks for {req}; give a GetFeature request for one layer: "
                        "request=GetFeature&typeNames=<workspace:layer>")
    if not (low.get("typenames") or low.get("typename")):
        problems.append("the WFS url names no layer — add typeNames=<workspace:layer> (the "
                        "layer's name in the service's GetCapabilities)")
    version = low.get("version", "")
    if low.get("bbox") and clip is None and version and not version.startswith("2."):
        problems.append(f"the WFS url's bbox is read in WFS {version} terms, and the platform "
                        "pages with WFS 2.0 — give the area as clip ([minLon, minLat, maxLon, "
                        "maxLat]) and drop bbox from the url")
    return problems


def validate(manifest) -> list[str]:
    from .. import remote_clip
    m = manifest
    problems = []
    if m.get("clip") is not None:
        problems += remote_clip.box_problems(m["clip"])
    if m.get("whole") is not None and m.get("whole") is not True:
        problems.append(f"whole must be true (or left out) — got {m['whole']!r}")
    if m.get("cut") is not None:
        if m.get("cut") is not True:
            problems.append(f"cut must be true (or left out) — got {m['cut']!r}")
        elif m.get("clip") is None:
            problems.append("cut cuts the features at the clip's edge — give clip: the area "
                            "(ASK the contributor for it; never guess one), or drop cut")
    svc = service(m.get("url"))
    if svc and svc["type"] == "wfs":
        problems += _wfs_problems(svc, m.get("clip"))
    return problems


# --------------------------------------------------------------------------- run

def run(ctx):
    """Page a service, or read a file (fetched here, or the step before's) feature by
    feature into what the gate's reader takes; return it with what was done."""
    from . import _features
    m = ctx.manifest
    box, where = None, ""
    if m.get("clip") is not None:
        from .. import remote_clip
        bbox, where = remote_clip.box_of(m["clip"])
        box = _features.Box(bbox)
        ctx.record.update({"clip": [round(v, 6) for v in bbox],
                           **({"clip_place": m["clip"]} if isinstance(m["clip"], str) else {})})
    own = ctx.path is None
    if own:
        svc = service(m["url"])
        if svc is not None:
            return _cut_at_edge(ctx, _paged(ctx, svc, box, where), box, where)
        ctx.begin("fetch")                        # the download is a step of its own
        ctx.path = _download(ctx, str(m["url"]).split("#", 1)[0])
        ctx.filename = ctx.steps[-1]["inputs"].get("filename")
        ctx.begin(NAME)
    return _cut_at_edge(ctx, _from_file(ctx, box, where, own), box, where)


def _edge(ctx) -> str:
    return ("then cut at its edge (cut: true)" if ctx.manifest.get("cut")
            else "whole features, not cut at its edge")


NEIGHBOURS = ("the clip is a box: it keeps every feature reaching into it, so a place's "
              "bounding box keeps its neighbours' features too — every pack clips to its own "
              "boundary when it measures, so those do not count there")


def _family(part, dim: int):
    """The pieces of an intersection of the original's dimension (a polygon cut at a box
    edge can leave a line along it), joined as one geometry; None when nothing is left."""
    import shapely
    from shapely.geometry import MultiLineString, MultiPoint, MultiPolygon
    pieces = [g for g in shapely.get_parts(part)
              if not g.is_empty and shapely.get_dimensions(g) == dim]
    for g in list(pieces):
        if g.geom_type.startswith("Multi") or g.geom_type == "GeometryCollection":
            pieces.remove(g)
            pieces += [x for x in shapely.get_parts(g) if shapely.get_dimensions(x) == dim]
    if not pieces:
        return None
    if len(pieces) == 1:
        return pieces[0]
    return {0: MultiPoint, 1: MultiLineString, 2: MultiPolygon}[dim](pieces)


def _cut_at_edge(ctx, made, box, where):
    """With cut: true, the kept features cut at the clip box's edge: the file read by the
    gate's own reader (graph/geo/vecio: any CRS to lon/lat, its repairs), each feature
    reaching outside the box replaced by its part inside, written as GeoJSON."""
    m = ctx.manifest
    if box is None or not m.get("cut"):
        return made
    import shapely
    from shapely.geometry import mapping, shape
    from ...graph.geo import vecio
    from .. import remote_clip
    from . import PrepRefused, PreparedFile, size_text
    quoted = _quoted(ctx)
    size = os.path.getsize(made.path)
    if size > remote_clip.RAW_FACTOR * ctx.cap:
        raise PrepRefused(f"the features of {quoted} that intersect the clip box "
                          f"{_box_text(box)}{where} are {size_text(size)} before cutting, far "
                          f"over the {size_text(ctx.cap)} cap — ask the contributor for a "
                          "smaller area (clip) and resubmit with it; never guess one")
    ctx.begin("cut")
    with open(made.path, "rb") as f:
        raw = f.read()
    name = ctx.filename if ctx.filename and os.path.splitext(ctx.filename)[1].lower() == \
        os.path.splitext(made.path)[1].lower() else os.path.basename(made.path)
    try:
        gj, read_notes = vecio.to_geojson(raw, name, m.get("layer_name"))
    except ValueError as exc:
        raise PrepRefused(f"{quoted}: {exc}") from None
    del raw
    os.unlink(made.path)
    fc = json.loads(gj)
    del gj
    kept, cut, dropped = [], 0, 0
    for k, ft in enumerate(fc.get("features") or []):
        if k % 512 == 0:
            ctx.check_time()
        g = ft.get("geometry") if isinstance(ft, dict) else None
        if not g or not g.get("coordinates") and not g.get("geometries"):
            kept.append(ft)                       # the gate counts and drops it, as ever
            continue
        try:
            geom = shape(g)
            if box.poly.contains(geom):
                kept.append(ft)
                continue
        except Exception:
            kept.append(ft)                       # the gate's reader says what is wrong
            continue
        try:
            part = geom.intersection(box.poly)
        except Exception:                         # an invalid polygon: repaired, then cut
            part = shapely.make_valid(geom).intersection(box.poly)
        part = _family(part, int(shapely.get_dimensions(geom)))
        if part is None:
            dropped += 1
            continue
        cut += 1
        kept.append({**ft, "geometry": mapping(part)})
    out = ctx.tmp(".geojson", "cut")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": kept}, f, ensure_ascii=False)
    note = (f"cut {cut:,} of the {len(kept) + dropped:,} features at the clip box's "
            f"edge {_box_text(box)}{where} (shapely, on longitude/latitude"
            + (f"; {dropped:,} only touched it and were left out" if dropped else "")
            + "): areas and lengths inside the box are exact; a cut feature's own attributes "
            "(an area or length column, a total) still describe the whole feature, not the "
            "part kept")
    inputs = dict((made.provenance or {}).get("inputs") or {})
    inputs["cut"] = {"features_cut": cut, "left_out": dropped, "features": len(kept)}
    ctx.record["cut"] = True
    return PreparedFile(path=out, notes=[*made.notes, *(read_notes or []), note],
                        provenance={"inputs": inputs})


def _download(ctx, url: str) -> str:
    """ctx.fetch, with a host too slow for the step's time said in words to act on."""
    from .. import fetch_policy
    from . import PrepRefused
    try:
        return ctx.fetch(url)
    except fetch_policy.FetchRefused as exc:
        if not str(exc).startswith("fetch exceeded"):
            raise
        raise PrepRefused(f"{url} did not finish downloading in the {ctx.op_seconds:g} s the "
                          "platform gives one download — the host is sending it slowly (Zenodo "
                          "can throttle); try again later, or put the file on Google Drive "
                          "(Anyone with the link) and give that link") from None


def _box_text(box) -> str:
    return "[" + ", ".join(f"{v:g}" for v in box.bbox) + "]"


def _quoted(ctx) -> str:
    return str(ctx.manifest.get("url") or "the file")


def _from_file(ctx, box, where, own=True):
    from . import PrepRefused, _features, size_text
    m = ctx.manifest
    quoted = _quoted(ctx)
    path = ctx.path
    name = os.path.basename(str(ctx.filename or path))
    packed = os.path.getsize(path)
    fetched = packed if own else None            # None: the step before made the file
    kind = _features.sniff(path)
    said = []
    if kind == "gzip":
        ctx.begin("gunzip")
        out = _features.gunzip(ctx, path, name)
        inner = name[:-3] if name.lower().endswith(".gz") else os.path.basename(out)
        said.append(f"decompressed {name} ({size_text(packed)} -> "
                    f"{size_text(os.path.getsize(out))}, streamed to disk)")
        ctx.adopt(out, (), {"op": "gunzip", "inputs": {"file": name}})
        os.unlink(path)
        path, name, ctx.filename = out, inner, inner
        ctx.begin(NAME)
        kind = _features.sniff(path)
    member = None
    if kind == "zip":
        files = ctx.unzip(path)
        os.unlink(path)
        if any(n.lower().endswith(".shp") for n in files):
            shp = _features.pick_shapefile(files, m.get("layer_name"), quoted)
            stem = os.path.splitext(os.path.basename(shp))[0]
            out = ctx.tmp(".zip", stem[-60:])
            info = _features.shapefile(ctx, files, shp, box, out)
            return _done(ctx, box, where, out, info, f"shapefile {stem}", "shapefile", fetched,
                         said)
        inner = sorted(n for n in files if n.lower().endswith((".gpkg", ".geojson", ".json")))
        if len(inner) != 1:
            held = ", ".join(sorted(files)[:8]) + (" ..." if len(files) > 8 else "")
            raise PrepRefused(
                f"the zip at {quoted} holds " + (f"several layer files ({', '.join(inner)}) — "
                                                 "give the link to one of them"
                                                 if inner else
                                                 f"no shapefile, GeoPackage or GeoJSON ({held})"))
        member = inner[0]
        path = files[member]
        kind = _features.sniff(path)
        said.append(f"read {member} from the zip")
    if kind == "gpkg":
        stem = os.path.splitext(os.path.basename(member or name))[0] or "layer"
        out = ctx.tmp(".gpkg", stem[-60:])
        info = _features.geopackage(ctx, path, m.get("layer_name"), box, out)
        return _done(ctx, box, where, out, info, f"GeoPackage table {info['member']}",
                     "GeoPackage", fetched, said, member)
    if kind == "geojson":
        if box is None:                           # whole: the file is the layer
            return _done(ctx, box, where, path, {"read": None, "kept": None}, "the GeoJSON",
                         "GeoJSON", fetched, said, member)
        out = ctx.tmp(".geojson", "kept")
        info = _features.filter_geojson(ctx, path, box, out)
        if info.pop("truncated"):
            raise PrepRefused(
                f"{quoted} is a server export cut at its record limit (exceededTransferLimit): "
                "clipping it cannot bring back what the server left out — give the service's "
                "own query url (…/FeatureServer/<n>/query or a WFS GetFeature) with this clip, "
                "and the platform pages it")
        return _done(ctx, box, where, out, info, "the GeoJSON", "GeoJSON", fetched, said, member)
    raise PrepRefused(_unknown(path, quoted, name))


def _unknown(path: str, quoted: str, name: str) -> str:
    """Why a file is not a layer the clip reads, in the gate's own words where it has them."""
    from .. import staging
    with open(path, "rb") as f:
        head = f.read(4096)
    what = "vector file (GeoJSON, zipped shapefile, GeoPackage)"
    if staging._is_web_page(head):
        return staging._WEB_PAGE.format(url=quoted, what=what)
    xml = staging._service_xml_problem(head, quoted, what)
    if xml:
        return xml
    part = staging._shapefile_part(head, name)
    if part:
        return (f"{quoted} is {part} — one part of a shapefile; the clip reads the .shp, .shx, "
                ".dbf and .prj together in one .zip (or a GeoPackage or GeoJSON)")
    return (f"{quoted} is not a GeoJSON, a zipped shapefile or a GeoPackage (nor one of them "
            "gzipped) — those are the vector files the clip reads")


def _done(ctx, box, where, out, info, fmt, family, fetched, said=(), member=None):
    from . import PrepRefused, PreparedFile, size_text
    quoted = _quoted(ctx)
    read, kept = info.get("read"), info.get("kept")
    if box is not None and not kept:
        raise PrepRefused(f"none of the {read:,} features of {fmt} in {quoted} intersects the "
                          f"clip box {_box_text(box)}{where} — check the area with the "
                          "contributor ([minLon, minLat, maxLon, maxLat], in degrees)")
    got = (f"{quoted} downloaded whole ({size_text(fetched)}, streamed to disk)"
           if fetched is not None else "read from the file the step before made")
    read_as = "downloaded whole (streamed to disk)" if fetched is not None else "prepared before"
    if box is not None:
        note = (f"kept the {kept:,} of {read:,} features of {fmt} that intersect the clip box "
                f"{_box_text(box)}{where} — {_edge(ctx)}; {got}")
        ctx.record["read"] = f"{read_as}, filtered to the clip box"
    else:
        count = f" ({kept:,} features)" if kept is not None else ""
        note = f"took {fmt} whole{count}; {got}"
        ctx.record["read"] = read_as
    inputs = {"url": quoted, "format": family,
              **({"member": member} if member else {}),
              **({"layer": info["member"]} if info.get("member") else {}),
              **({"layer_name": ctx.manifest["layer_name"]} if ctx.manifest.get("layer_name") else {}),
              **({"clip": [round(v, 6) for v in box.bbox]} if box is not None else {"whole": True}),
              **({"clip_place": ctx.manifest["clip"]}
                 if box is not None and isinstance(ctx.manifest.get("clip"), str) else {}),
              "features": {"read": read, "kept": kept}}
    if info.get("searched"):
        inputs["searched"] = info["searched"]
    return PreparedFile(path=out, notes=[note, *said, *([NEIGHBOURS] if box is not None else [])],
                        provenance={"inputs": inputs})


# --------------------------------------------------------------------------- services

def _get(ctx, url: str) -> tuple[str, dict]:
    """A request's answer on disk, under the preparation budget and this step's time;
    its sha256 and bytes go into the page list, not the step list."""
    path = ctx.fetch(url)
    st = ctx.steps.pop()
    return path, {"sha256": st["sha256"], "bytes": st["bytes"]}


def _json(ctx, url: str, what: str) -> dict:
    from . import PrepRefused
    path, _ = _get(ctx, url)
    try:
        if os.path.getsize(path) > 64 << 20:
            raise PrepRefused(f"{_quoted(ctx)}: {what} is over 64 MiB — not a service description")
        with open(path, "rb") as f:
            raw = f.read()
    finally:
        os.unlink(path)
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        raise PrepRefused(f"{_quoted(ctx)}: the service answered {what} that is not JSON "
                          f"({_excerpt(raw)})")
    err = doc.get("error")
    if err:
        raise PrepRefused(f"{_quoted(ctx)}: the service answered an error for {what}: "
                          f"{_error_text(err)}")
    return doc


def _error_text(err) -> str:
    if isinstance(err, dict):
        bits = [str(err.get("message") or "")] + [str(d) for d in err.get("details") or [] if d]
        return "; ".join(b for b in bits if b)[:300] or json.dumps(err)[:300]
    return str(err)[:300]


def _excerpt(raw: bytes) -> str:
    text = re.sub(r"<[^>]+>", " ", raw[:3000].decode("utf-8", "replace"))
    return " ".join(text.split())[:200] or "an empty answer"


def _page(ctx, url: str, out, seen: set, tally: dict) -> tuple[int, dict, dict]:
    """One page into `out`: (features, its sha256/bytes, its top-level members)."""
    from . import PrepRefused, _features
    path, rec = _get(ctx, url)
    members, n = {}, 0
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        if head.lstrip()[:1] == b"<":
            with open(path, "rb") as f:
                raw = f.read(1 << 16)
            raise PrepRefused(f"{_quoted(ctx)}: the service answered a page with XML, not "
                              f"GeoJSON ({_excerpt(raw)}) — it may not serve GeoJSON for this layer")

        def member(k, v):
            members[k] = v

        def feature(ft, text):
            nonlocal n
            fid = ft.get("id") if isinstance(ft, dict) else None
            if fid is not None:
                if fid in seen:
                    raise PrepRefused(
                        f"{_quoted(ctx)}: the service's pages overlap (feature {fid!r} came "
                        "twice), so paging it would not be complete — it does not page in a "
                        "stable order; give a file download of the layer instead")
                seen.add(fid)
            n += 1
            tally["vertices"] += _features.geometry_vertices(
                ft.get("geometry") if isinstance(ft, dict) else None)
            if n % 1024 == 0:
                ctx.check_vertices(tally["vertices"], "the features read")
            out.add(text)
        try:
            ok = _features.walk_geojson(path, member, feature, ctx.check_time)
        except ValueError as exc:
            raise PrepRefused(f"{_quoted(ctx)}: a page of the service is not GeoJSON ({exc})") from None
    finally:
        os.unlink(path)
    if members.get("error"):
        raise PrepRefused(f"{_quoted(ctx)}: the service answered an error: "
                          f"{_error_text(members['error'])}")
    if not ok:
        raise PrepRefused(f"{_quoted(ctx)}: the service answered without a features array — "
                          "it may not serve GeoJSON for this layer")
    ctx.check_vertices(tally["vertices"], "the features read")
    return n, rec, members


def _arcgis_params(params, box) -> list:
    keep = [(k, v) for k, v in params if k.lower() not in _ARCGIS_OURS
            and not (box is not None and k.lower() in _ARCGIS_BOX)]
    low = {k.lower() for k, _ in keep}
    if "where" not in low:
        keep.append(("where", "1=1"))
    if "outfields" not in low:
        keep.append(("outFields", "*"))
    keep += [("returnGeometry", "true"), ("outSR", "4326")]
    if box is not None:
        keep += [("geometry", ",".join(f"{v:.8f}" for v in box.bbox)),
                 ("geometryType", "esriGeometryEnvelope"), ("inSR", "4326"),
                 ("spatialRel", "esriSpatialRelIntersects")]
    return keep


def _arcgis(ctx, svc, box, out) -> dict:
    from . import PrepRefused
    layer = svc["layer"]
    meta = _json(ctx, layer + "?f=json", "its layer description")
    kind = meta.get("type")
    if kind and kind not in ("Feature Layer", "Table"):
        raise PrepRefused(f"{_quoted(ctx)} is an ArcGIS {kind}, not a feature layer — give "
                          "one of its feature layers (…/FeatureServer/<n> or …/MapServer/<n>)")
    oid = meta.get("objectIdField") or next(
        (f.get("name") for f in meta.get("fields") or [] if f.get("type") == "esriFieldTypeOID"),
        None)
    size = max(1, min(int(meta.get("maxRecordCount") or 1000), ARCGIS_PAGE))
    pages_ok = (meta.get("advancedQueryCapabilities") or {}).get("supportsPagination") is not False
    params = _arcgis_params(svc["params"], box)
    query = layer + "/query?"
    counted = _json(ctx, query + urlencode(params + [("returnCountOnly", "true"), ("f", "json")]),
                    "its feature count").get("count")
    if not isinstance(counted, int):
        raise PrepRefused(f"{_quoted(ctx)}: the service did not say how many features the "
                          "query holds (returnCountOnly), so a paged read could not be checked "
                          "complete — give a file download of the layer instead")
    seen, tally, pages, got = set(), {"vertices": 0}, [], 0
    if pages_ok and oid:
        how = "resultOffset/resultRecordCount"
        while got < counted:
            ctx.check_time()
            n, rec, _ = _page(ctx, query + urlencode(params + [
                ("orderByFields", f"{oid} ASC"), ("resultOffset", got),
                ("resultRecordCount", size), ("f", "geojson")]), out, seen, tally)
            pages.append({"offset": got, "features": n, **rec})
            if not n:
                break
            got += n
    else:
        how = "objectIds"
        ids = _json(ctx, query + urlencode(params + [("returnIdsOnly", "true"), ("f", "json")]),
                    "its feature ids").get("objectIds") or []
        ids = sorted(ids)
        for i in range(0, len(ids), size):
            ctx.check_time()
            chunk = ids[i:i + size]
            n, rec, _ = _page(ctx, query + urlencode(params + [
                ("objectIds", ",".join(str(x) for x in chunk)), ("f", "geojson")]), out, seen, tally)
            pages.append({"objectIds": [chunk[0], chunk[-1]], "features": n, **rec})
            got += n
    if got != counted:
        raise PrepRefused(f"{_quoted(ctx)}: the service counts {counted:,} features but its "
                          f"pages held {got:,} — a paged read would not be complete; give a "
                          "file download of the layer instead")
    return {"service": "ArcGIS", "where": layer, "query": query + urlencode(params),
            "page_size": size, "paged_by": how, "counted": counted, "features": got,
            "pages": pages}


def _wfs(ctx, svc, box, out) -> dict:
    from .. import fetch_policy
    from . import PrepRefused
    params = svc["params"]
    low = {k.lower(): v for k, v in params}
    layer = low.get("typenames") or low.get("typename")
    keep = [(k, v) for k, v in params if k.lower() not in _WFS_OURS
            and not (box is not None and k.lower() == "bbox")]
    kept = {k.lower(): v for k, v in keep}
    fmt = kept.get("outputformat", "")
    replaced = None
    if "json" not in fmt.lower():
        if fmt:
            replaced = fmt
        keep = [(k, v) for k, v in keep if k.lower() != "outputformat"] + [
            ("outputFormat", "application/json")]
    if "srsname" not in kept:
        keep.append(("srsName", "EPSG:4326"))
    if box is not None:
        w, s, e, n = box.bbox                    # WFS 2.0, EPSG:4326 as a urn: lat, lon order
        keep.append(("bbox", f"{s:.8f},{w:.8f},{n:.8f},{e:.8f},urn:ogc:def:crs:EPSG::4326"))
    base = [("service", "WFS"), ("version", "2.0.0"), ("request", "GetFeature"),
            ("typeNames", layer)] + keep
    query = svc["base"] + "?"
    counted = None
    try:                                         # numberMatched; a server that will not count
        hits = [(k, v) for k, v in base if k.lower() != "outputformat"] + [("resultType", "hits")]
        path, _ = _get(ctx, query + urlencode(hits))
        with open(path, "rb") as f:
            head = f.read(1 << 16)
        os.unlink(path)
        m = re.search(rb'numberMatched\s*=\s*"(\d+)"', head) or \
            re.search(rb'numberOfFeatures\s*=\s*"(\d+)"', head)
        counted = int(m.group(1)) if m else None
    except fetch_policy.FetchRefused:
        counted = None
    seen, tally, pages, got = set(), {"vertices": 0}, [], 0
    while True:                                  # until an empty page, or all counted
        ctx.check_time()
        n, rec, members = _page(ctx, query + urlencode(base + [("startIndex", got),
                                                               ("count", WFS_PAGE)]),
                                out, seen, tally)
        pages.append({"startIndex": got, "features": n, **rec})
        if counted is None and isinstance(members.get("numberMatched"), int):
            counted = members["numberMatched"]
        got += n
        if not n or (counted is not None and got >= counted):
            break
    if counted is not None and got != counted:
        raise PrepRefused(f"{_quoted(ctx)}: the service counts {counted:,} features but its "
                          f"pages held {got:,} — a paged read would not be complete; give a "
                          "file download of the layer instead")
    return {"service": "WFS", "where": f"{svc['base']} ({layer})", "query": query + urlencode(base),
            "page_size": WFS_PAGE, "paged_by": "startIndex/count", "counted": counted,
            "features": got, "pages": pages,
            **({"output_format_replaced": replaced} if replaced else {})}


def _paged(ctx, svc, box, where):
    from . import PrepRefused, PreparedFile, _features
    out_path = ctx.tmp(".geojson", "paged")
    out = _features.GeoJSONOut(out_path)
    try:
        info = (_arcgis if svc["type"] == "arcgis" else _wfs)(ctx, svc, box, out)
    finally:
        out.close()
    got, k = info["features"], len(info["pages"])
    if box is not None and not got:
        raise PrepRefused(f"the service at {_quoted(ctx)} holds no feature intersecting the clip "
                          f"box {_box_text(box)}{where} — check the area with the contributor "
                          "([minLon, minLat, maxLon, maxLat], in degrees)")
    counted = (f"every feature the server counts ({info['counted']:,})"
               if info["counted"] is not None else "until the server answered an empty page")
    note = (f"read {got:,} features from the {info['service']} layer {info['where']} in {k} "
            f"request(s) of up to {info['page_size']:,} ({info['paged_by']}) — {counted}")
    if box is not None:
        note += (f"; only those intersecting the clip box {_box_text(box)}{where} were asked "
                 f"for — {_edge(ctx)}")
    notes = [note]
    if info.get("output_format_replaced"):
        notes.append(f"asked for GeoJSON pages instead of the url's outputFormat "
                     f"{info['output_format_replaced']} (pages are joined as GeoJSON)")
    if box is not None:
        notes.append(NEIGHBOURS)
    ctx.record["read"] = f"paged: {k} request(s) of up to {info['page_size']:,} features"
    inputs = {"url": _quoted(ctx), "service": info["service"], "query": info["query"],
              "page_size": info["page_size"], "paged_by": info["paged_by"],
              "counted": info["counted"], "features": got, "pages": info["pages"],
              **({"clip": [round(v, 6) for v in box.bbox]} if box is not None else {"whole": True}),
              **({"clip_place": ctx.manifest["clip"]}
                 if box is not None and isinstance(ctx.manifest.get("clip"), str) else {})}
    return PreparedFile(path=out_path, notes=notes, provenance={"inputs": inputs})


# --------------------------------------------------------------------------- suggestions

def _cut(error) -> bool:
    """The gate reader's own words for an export cut at its record limit."""
    from ...graph.geo import vecio
    return vecio._TRUNCATED.split("{", 1)[0] in str(error or "")


_VECTOR_GZ = re.compile(r"\.(?:gpkg|geojson|json|zip)\.gz$", re.I)


def _gz_name(url) -> bool:
    """A url whose path names a gzipped vector file (.gpkg.gz, .geojson.gz, .zip.gz)."""
    try:
        return bool(_VECTOR_GZ.search(urlparse(str(url or "").strip()).path or ""))
    except ValueError:
        return False


_WHOLE_SERVICE = ("the url is {what}, so the platform pages it until every feature the server "
                  "counts is in (its record limit would otherwise cut it short)")
_WHOLE_GZ = ("the url is a gzipped layer, so the platform downloads it (streamed to disk), "
             "decompresses it and reads it whole")


def detect(kind: str, manifest) -> dict | None:
    """A vector's url that is a service's layer or query (ArcGIS FeatureServer/MapServer,
    a WFS GetFeature) or a gzipped layer file, given with no area: read whole — paging
    or decompressing is no person's choice. The area (clip) always is."""
    if kind not in KINDS:
        return None
    url = manifest.get("url")
    svc = service(url)
    if svc is not None:
        low = {k.lower(): v for k, v in svc["params"]}
        if svc["type"] == "wfs" and low.get("request", "").lower() != "getfeature":
            return None                           # a capabilities request: its own words say so
        what = ("an ArcGIS FeatureServer/MapServer layer" if svc["type"] == "arcgis"
                else "a WFS GetFeature request")
        return {"fields": {"whole": True}, "why": _WHOLE_SERVICE.format(what=what)}
    if _gz_name(url):
        return {"fields": {"whole": True}, "why": _WHOLE_GZ}
    return None


_WHOLE_BIG = ("the file is over the size cap as a download, so the platform downloads it "
              "whole under its preparation budget (streamed to disk) and keeps its layer; the "
              "cap applies to the layer as stored")


def auto(situation: dict) -> dict | None:
    """A vector file over the cap as a download (an archive of every admin level): read
    whole under the preparation budget — which layer, if it holds several, is still asked.
    A vector the gate could not read because it is gzipped (a link that does not say so
    in its name): decompressed and read whole."""
    from . import _features
    if situation.get("kind") not in KINDS:
        return None
    if situation.get("event") == "vector_file_too_large":
        return {"fields": {"whole": True}, "why": _WHOLE_BIG}
    if situation.get("event") != "vector_unreadable":
        return None
    head = situation.get("head") or b""
    if not isinstance(head, (bytes, bytearray)) or head[:2] != _features.GZIP_MAGIC:
        return None
    return {"fields": {"whole": True}, "why": _WHOLE_GZ}


def suggest(situation: dict) -> str | None:
    """A truncated service export: page it (whole: true) or ask for an area (clip). A
    layer over the cap: features cut at the area's edge (cut: true), the person's choice."""
    m = situation.get("manifest") or {}
    if situation.get("kind") not in KINDS:
        return None
    if situation.get("event") == "vector_too_large":
        if m.get("clip") is None:
            return ("the clip keeps each feature that reaches into the area WHOLE, so a feature "
                    "reaching far beyond it (an admin polygon, a long river) is kept entire; "
                    "with cut: true they are cut at the area's edge (areas and lengths inside "
                    "it stay exact, a cut feature's own totals do not) — ASK the contributor "
                    "whether to cut, never set it unasked")
        if not m.get("cut"):
            return ("or resubmit with cut: true — the features crossing the clip's edge are cut "
                    "at it (areas and lengths inside the area stay exact, a cut feature's own "
                    "totals do not) — ASK the contributor whether to cut or to give a smaller "
                    "area; never guess")
        return None
    if any(m.get(f) is not None for f in FIELDS):
        return None
    if situation.get("event") == "vector_file_too_large":
        return ("or resubmit the same link with whole: true — the platform downloads it whole "
                "under its preparation budget (streamed to disk) and keeps the one layer "
                "(layer_name names it in an archive of several); the cap applies to the layer "
                "as stored — or with clip: <the area> to keep only its features there (ASK the "
                "contributor for the area; never guess one)")
    if situation.get("event") != "vector_unreadable" or not _cut(situation.get("error")):
        return None
    if service(situation.get("url") or m.get("url")):
        return ("resubmit the same link with whole: true — the platform pages the query "
                "(resultOffset/resultRecordCount; a WFS by startIndex/count) until every "
                "feature is in, nothing to do by hand; or with clip: <the area> to keep only "
                "the features in an area (ASK the contributor for it — never guess one)")
    return ("give the service's own query url (…/FeatureServer/<n>/query, …/MapServer/<n>/query "
            "or a WFS GetFeature) with whole: true — the platform pages it until every feature "
            "is in")
