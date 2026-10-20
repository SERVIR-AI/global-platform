"""A feature layer read for a preparation step, a chunk at a time, on EPSG:4326.

A WFS GetFeature link is PAGED (startIndex, as GeoJSON, as many features a request as make
about PAGE_BYTES — a layer of a few huge dissolved polygons comes one or two at a time) and, when
the step has a box, asked only for that box; any other source — a GeoJSON, a zipped
shapefile, a GeoPackage, or the file a step before made — is downloaded whole (streamed
to disk) and read by graph/geo/vecio, as the vector gate reads it. Invalid polygons are
repaired by vecio's rules, so a layer reads here as it would land as a vector. However
many pages a WFS takes, the record holds ONE fetch step: the link as asked, every page's
[startIndex, count] and sha256, and one sha256 over the pages in order — a replay asks the
same pages. A page is parsed from disk one feature at a time (`stream`), never whole: a
layer of dissolved national polygons weighs hundreds of MB a page.
"""

from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PAGE = 1000            # features a WFS request asks for, at most
PAGE_BYTES = 16 << 20  # what one page should weigh: fewer features a page when they are big
PAGE_FIRST = 16        # features the first page asks for; each next page at most 4x the last
# attributes that name each feature once: a layer with no primary key pages in a stable
# order only sorted by one (GeoServer: "Cannot do natural order without a primary key")
_ID_NAMES = ("gid", "fid", "objectid", "ogc_fid", "ogr_fid", "oid", "id")
WHOLE_FACTOR = 8       # a file read whole is parsed in memory up to 8x the stored cap
_FILTERS = ("bbox", "cql_filter", "filter", "featureid", "resourceid")


_WS = " \t\r\n"
_CHUNK = 1 << 20


def stream(path: str, members: dict):
    """Each feature of the GeoJSON FeatureCollection in the file at `path`, parsed one at a
    time; the collection's other members (crs, numberMatched, ...) land in `members` as they
    are met, "features" there once the list was read. ValueError when it is not one."""
    dec = json.JSONDecoder()
    with open(path, "r", encoding="utf-8-sig") as f:
        buf, pos, eof = "", 0, False

        def more(atleast=_CHUNK):
            nonlocal buf, pos, eof
            chunk = f.read(max(atleast, _CHUNK))
            if not chunk:
                eof = True
                return
            buf, pos = buf[pos:] + chunk, 0

        def peek():
            nonlocal pos
            while True:
                while pos < len(buf) and buf[pos] in _WS:
                    pos += 1
                if pos < len(buf) or eof:
                    return buf[pos] if pos < len(buf) else ""
                more()

        def take(ch):
            nonlocal pos
            if peek() != ch:
                raise ValueError(f"expected {ch!r}")
            pos += 1

        def value():
            nonlocal pos
            peek()
            while True:
                try:
                    v, end = dec.raw_decode(buf, pos)
                    if end < len(buf) or eof:          # a number cut at the buffer's end reads short
                        pos = end
                        return v
                except json.JSONDecodeError:
                    if eof:
                        raise ValueError("not JSON") from None
                more(len(buf) - pos)                   # double what is held: linear overall

        take("{")
        if peek() == "}":
            return
        while True:
            k = value()
            if not isinstance(k, str):
                raise ValueError("not a JSON object")
            take(":")
            if k == "features":
                take("[")
                if peek() == "]":
                    pos += 1
                else:
                    while True:
                        yield value()
                        c = peek()
                        pos += 1
                        if c == "]":
                            break
                        if c != ",":
                            raise ValueError("a list of features")
                members["features"] = True
            else:
                members[k] = value()
            c = peek()
            pos += 1
            if c == "}":
                return
            if c != ",":
                raise ValueError("a JSON object")


def wfs_query(url: str) -> dict | None:
    """{lower-case key: (key as given, value)} of a WFS GetFeature link, else None."""
    try:
        q = parse_qsl(urlsplit(str(url or "").strip()).query, keep_blank_values=True)
    except ValueError:
        return None
    keys = {k.lower(): (k, v) for k, v in q}
    if keys.get("request", ("", ""))[1].strip().lower() != "getfeature":
        return None
    if "service" in keys and keys["service"][1].strip().lower() != "wfs":
        return None
    return keys


def with_params(url: str, params: dict) -> str:
    """`url` with `params` set — a key already there (in any case) keeps its spelling and
    takes the new value; the rest keep their order."""
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    low = {k.lower(): i for i, (k, _) in enumerate(q)}
    for k, v in params.items():
        if k.lower() in low:
            q[low[k.lower()]] = (q[low[k.lower()]][0], str(v))
        else:
            low[k.lower()] = len(q)
            q.append((k, str(v)))
    return urlunsplit(parts._replace(query=urlencode(q, safe=":,/")))


class Reader:
    """Iterate to get lists of (properties, shapely geometry) on EPSG:4326 — features
    without a geometry are counted and left out. With `cut` ([w, s, e, n]) each geometry
    reaching beyond it is cut to it BEFORE it is checked and repaired: a service that
    answers a box with whole national polygons is worked on only where the step reads (an
    empty geometry then means the feature lies outside). After the last chunk: `notes`
    (what was done, in words) and `read` (how the source was read); a WFS's one fetch step
    is on ctx.steps."""

    def __init__(self, ctx, url: str, box=None, layer: str | None = None,
                 asker: str = "the step", layer_field: str = "layer_name", cut=None):
        self.ctx, self.url, self.box, self.layer, self.asker = ctx, url, box, layer, asker
        self.layer_field, self.cut = layer_field, cut
        self.notes: list[str] = []
        self.read = "downloaded whole"
        self.asked_box = None
        self.tally = {"empty": 0, "repaired": 0, "collapsed": 0}

    # ------------------------------------------------------------------ the sources
    def __iter__(self):
        ctx = self.ctx
        if ctx.path is not None:                       # a step before made the file
            yield self._whole(ctx.path, ctx.filename)
            return
        q = wfs_query(self.url)
        fmt = (q or {}).get("outputformat", ("", ""))[1].lower()
        if q is not None and (not fmt or "json" in fmt):
            yield from self._pages(q)
            self._said()
            return
        path = ctx.fetch(self.url)
        ctx.filename = ctx.steps[-1]["inputs"].get("filename")
        yield self._whole(path, ctx.filename)

    def _whole(self, path: str, filename: str | None) -> list:
        from ...graph.geo import vecio
        from . import PrepRefused, size_text
        size = os.path.getsize(path)
        limit = WHOLE_FACTOR * self.ctx.cap
        if size > limit:
            raise PrepRefused(
                f"the layer at {self.url} is {size_text(size)}, over the {size_text(limit)} "
                f"{self.asker} reads whole — give it as a WFS GetFeature link (the platform pages "
                "the service and asks only for the clip's box), or a smaller area or layer")
        with open(path, "rb") as f:
            raw = f.read()
        if path != self.ctx.path:
            os.unlink(path)                            # the record keeps its sha256
        self._not_features(raw, "the file")
        try:
            raw, notes = vecio.to_geojson(raw, filename or "", self.layer)
        except ValueError as exc:
            raise PrepRefused(f"{self.url}: " + str(exc).replace(
                "with layer_name", f"with {self.layer_field}")) from None
        try:
            fc = json.loads(raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw)
        except (UnicodeDecodeError, ValueError):
            raise PrepRefused(f"{self.url} is not GeoJSON, a zipped shapefile or a GeoPackage "
                              "— the vector formats the platform reads") from None
        del raw
        if not isinstance(fc, dict) or not isinstance(fc.get("features"), list):
            raise PrepRefused(f"{self.url} holds no FeatureCollection — give a layer of features")
        self.notes += notes                            # vecio said what it dropped or repaired
        return self._shapes(fc["features"], repair=False)

    def _look(self, base: str, size_key: str, q: dict, version: str) -> tuple:
        """(the attribute to page by, the bytes of one feature, the layer's CRS — None for
        lon/lat), from one feature asked for.
        The attribute keeps every page in the same order: the link's own sortBy (None: it
        stands), else one naming each feature once; None when the layer has none."""
        from . import PrepRefused
        path = self.ctx.fetch(with_params(base, {size_key: 1}))
        size = self.ctx.steps.pop()["bytes"]           # a look at the attributes, no input
        members: dict = {}
        try:
            feats = self._streamed(path, members, "the service's answer")
        finally:
            os.unlink(path)
        props = (feats[0].get("properties") or {}) if feats and isinstance(feats[0], dict) else {}
        low = {str(k).lower(): k for k in props}
        sort = (None if "sortby" in q or version.startswith("1.0")
                else next((low[n] for n in _ID_NAMES if n in low), None))
        return sort, size, self._crs(members)

    def _streamed(self, path: str, members: dict, what: str, batch: int = 0):
        """The features of a page on disk — all of them in a list, or with `batch` a
        generator of lists of that many — after checking it is no web page or XML."""
        from . import PrepRefused
        with open(path, "rb") as f:
            self._not_features(f.read(4096), what)

        def gen():
            out = []
            try:
                for ft in stream(path, members):
                    out.append(ft)
                    if batch and len(out) >= batch:
                        yield out
                        out = []
            except (UnicodeDecodeError, ValueError):
                raise PrepRefused(f"{what} at {self.url} is not GeoJSON — ask the service for "
                                  "outputFormat=application/json") from None
            if "features" not in members:
                raise PrepRefused(f"{what} at {self.url} holds no FeatureCollection")
            yield out
        if batch:
            return gen()
        return [ft for chunk in gen() for ft in chunk]

    def _pages(self, q: dict):
        """A WFS GetFeature read page by page; one fetch step for all of them."""
        from .. import fetch_policy
        from . import PrepRefused
        FetchRefused = fetch_policy.FetchRefused
        ctx = self.ctx
        paged = [q[k][0] for k in ("count", "maxfeatures", "startindex") if k in q]
        if paged:
            raise PrepRefused(f"the link sets {', '.join(paged)} — drop it: the platform pages "
                              "the service itself and reads every feature it sends")
        version = q.get("version", ("", "2.0.0"))[1].strip() or "2.0.0"
        size_key = "count" if version.startswith("2") else "maxFeatures"
        extra = {}
        if "outputformat" not in q:
            extra["outputFormat"] = "application/json"
        if "srsname" not in q:
            extra["srsName"] = "EPSG:4326"
        if self.box is not None and not any(k in q for k in _FILTERS):
            w, s, e, n = (float(v) for v in self.box)
            extra["bbox"] = (f"{w},{s},{e},{n},EPSG:4326" if version.startswith("1.0")
                             else f"{s},{w},{n},{e},urn:ogc:def:crs:EPSG::4326")
            self.asked_box = [w, s, e, n]
        base = with_params(self.url, extra)
        sort, one, crs = self._look(base, size_key, q, version)
        if sort:
            base = with_params(base, {"sortBy": sort})
        # one feature's weight says little of the next (a layer of dissolved national
        # polygons holds both a hamlet and a whole class): start small, grow by the pages' weight
        count = max(1, min(PAGE, PAGE_FIRST, PAGE_BYTES // max(1, one)))
        digest, shas, asked, nbytes = hashlib.sha256(), [], [], 0
        seen: set = set()
        start = pages = got = 0
        matched = None
        while True:
            ctx.check_time()
            try:
                path = ctx.fetch(with_params(base, {size_key: count, "startIndex": start}))
            except FetchRefused as exc:
                if pages:
                    raise
                raise PrepRefused(f"{exc} — the platform pages a WFS with startIndex"
                                  + (f", sorted by {sort}" if sort else "") + "; if the service "
                                  "cannot page this layer, add sortBy=<an attribute naming each "
                                  "feature once> to the link, or give the layer as a file") from None
            step = ctx.steps.pop()                     # one step for the pages, below
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(_CHUNK), b""):
                    digest.update(chunk)
            shas.append(step["sha256"])
            asked.append([start, count])
            nbytes += step["bytes"]
            pages += 1
            members: dict = {}
            n = 0
            try:
                for feats in self._streamed(path, members, f"page {pages} of the service's "
                                            "answer", batch=64):
                    for ft in feats:
                        fid = ft.get("id") if isinstance(ft, dict) else None
                        if fid is not None:
                            if fid in seen:
                                raise PrepRefused(
                                    f"the service sent feature {fid!r} twice while being paged — "
                                    "it does not page in a stable order, so a read could miss "
                                    "features; give the layer as a file (GeoJSON, zipped "
                                    "shapefile, GeoPackage) instead")
                            seen.add(fid)
                    n += len(feats)
                    yield self._chunk(crs, feats)
            finally:
                os.unlink(path)
            self._page_checks(members, crs, n)
            m = members.get("numberMatched")
            if isinstance(m, int) and not isinstance(m, bool):
                matched = m
            got += n
            asked_for, weight = count, step["bytes"] / max(1, n)
            if not n:
                break
            if matched is not None:
                if got >= matched:
                    break
            elif n < asked_for and not _next_link(members):
                break
            start += n
            count = max(1, min(PAGE, 4 * asked_for, int(PAGE_BYTES // max(1.0, weight))))
        if matched is not None and got != matched:
            raise PrepRefused(f"the service said {matched:,} features match but sent {got:,} "
                              f"over {pages} page(s) — it does not page completely; give the "
                              "layer as a file instead")
        ctx.steps.append({"op": "fetch", "inputs": {
            "url": base, "paged": {"by": "startIndex", "size_param": size_key, "pages": pages,
                                   "asked": asked, **({"sort_by": sort} if sort else {})},
            "features": got, **({"box": self.asked_box} if self.asked_box else {}),
            "page_sha256": shas}, "sha256": digest.hexdigest(), "bytes": nbytes})
        self.read = (f"WFS GetFeature, paged: {got:,} features in {pages} page(s)"
                     + (", only the clip's box asked for" if self.asked_box else ""))

    # ------------------------------------------------------------------ the features
    def _crs(self, members: dict):
        from ...graph.geo import vecio
        from . import PrepRefused
        try:
            return vecio._geojson_crs(members)
        except ValueError as exc:
            raise PrepRefused(f"{self.url}: {exc}") from None

    def _page_checks(self, members: dict, crs, n: int) -> None:
        """A page truncated by the server, or on another CRS than the features it already
        sent were read in (the crs member comes after them), is refused."""
        from ...graph.geo import vecio
        from . import PrepRefused
        if members.get("exceededTransferLimit") is True \
                or (members.get("properties") or {}).get("exceededTransferLimit") is True:
            raise PrepRefused(f"{self.url}: " + vecio._TRUNCATED.format(n=n))
        mine = self._crs(members)
        if (mine is None) != (crs is None) or (mine is not None and mine != crs):
            raise PrepRefused(f"{self.url}: its pages declare different coordinate systems — "
                              "add srsName=EPSG:4326 to the link")

    def _chunk(self, crs, feats: list) -> list:
        from ...graph.geo import vecio
        if crs is not None:
            geoms = vecio._reproject([ft.get("geometry") if vecio._located(ft.get("geometry"))
                                      else None for ft in feats], crs)
            feats = [{**ft, "geometry": g} for ft, g in zip(feats, geoms)]
            said = (f"reprojected from {vecio._crs_label(crs)} (the service's crs member) to "
                    "EPSG:4326 at ingest")
            if said not in self.notes:
                self.notes.append(said)
        return self._shapes(feats, repair=True)

    def _shapes(self, feats: list, repair: bool) -> list:
        import shapely
        from shapely.geometry import shape
        from ...graph.geo import vecio
        out = []
        for ft in feats:
            if not isinstance(ft, dict) or not vecio._located(ft.get("geometry")):
                self.tally["empty"] += 1
                continue
            try:
                geom = shape(ft["geometry"])
            except Exception:
                self.tally["collapsed"] += 1
                continue
            if self.cut is not None:
                x0, y0, x1, y1 = geom.bounds
                w, s, e, n = self.cut
                if x0 < w or y0 < s or x1 > e or y1 > n:
                    geom = shapely.clip_by_rect(geom, w, s, e, n)
                    if geom.is_empty:
                        out.append((ft.get("properties") or {}, geom))
                        continue
                    if geom.geom_type == "GeometryCollection":   # the cut left lines too
                        polys = [g for g in geom.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
                        geom = shapely.union_all(polys) if polys else shapely.Polygon()
                        if geom.is_empty:
                            out.append((ft.get("properties") or {}, geom))
                            continue
            if repair and geom.geom_type in ("Polygon", "MultiPolygon") and not geom.is_valid:
                fixed = vecio.repair(geom)
                if fixed is None:
                    self.tally["collapsed"] += 1
                    continue
                geom = fixed
                self.tally["repaired"] += 1
            out.append((ft.get("properties") or {}, geom))
        return out

    def _said(self) -> None:
        t = self.tally
        if t["empty"]:
            self.notes.append(f"{t['empty']:,} feature(s) without geometry were dropped")
        if t["repaired"]:
            self.notes.append(f"{t['repaired']:,} invalid polygon(s) (self-intersecting rings, "
                              "overlapping parts, holes written as parts) were repaired: shells "
                              "unioned, holes subtracted")
        if t["collapsed"]:
            self.notes.append(f"{t['collapsed']:,} invalid polygon(s) collapsed to no area when "
                              "repaired and were dropped")

    def _not_features(self, raw: bytes, what: str) -> None:
        """A web page or a service's XML (an exception report, GML) is refused in words."""
        from .. import staging
        from . import PrepRefused
        if staging._is_web_page(raw):
            raise PrepRefused(staging._WEB_PAGE.format(
                url=self.url, what="feature layer (GeoJSON, zipped shapefile, GeoPackage, WFS)"))
        said = staging._service_xml_problem(raw, self.url, "feature layer")
        if said:
            raise PrepRefused(said)
        if bytes(raw[:512]).lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
            raise PrepRefused(f"{what} at {self.url} is XML (GML?), not GeoJSON — ask the "
                              "service for outputFormat=application/json")


def _next_link(fc: dict) -> bool:
    return any(isinstance(lk, dict) and lk.get("rel") == "next" for lk in fc.get("links") or [])
