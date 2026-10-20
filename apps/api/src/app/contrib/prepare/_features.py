"""A feature layer read for a preparation step, a chunk at a time, on EPSG:4326.

A WFS GetFeature link is PAGED (startIndex, as GeoJSON, as many features a request as make
about PAGE_BYTES — a layer of a few huge dissolved polygons comes one or two at a time) and, when
the step has a box, asked only for that box; any other source — a GeoJSON, a zipped
shapefile, a GeoPackage, or the file a step before made — is downloaded whole (streamed
to disk) and read by graph/geo/vecio, as the vector gate reads it. Invalid polygons are
repaired by vecio's rules, so a layer reads here as it would land as a vector. However
many pages a WFS takes, the record holds ONE fetch step: the link as asked, every page's
[startIndex, count] and sha256, and one sha256 over the pages in order — a replay asks the
same pages.
"""

from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PAGE = 1000            # features a WFS request asks for, at most
PAGE_BYTES = 16 << 20  # what one page should weigh: fewer features a page when they are big
# attributes that name each feature once: a layer with no primary key pages in a stable
# order only sorted by one (GeoServer: "Cannot do natural order without a primary key")
_ID_NAMES = ("gid", "fid", "objectid", "ogc_fid", "ogr_fid", "oid", "id")
WHOLE_FACTOR = 8       # a file read whole is parsed in memory up to 8x the stored cap
_FILTERS = ("bbox", "cql_filter", "filter", "featureid", "resourceid")


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
        """(the attribute to page by, the bytes of one feature), from one feature asked for.
        The attribute keeps every page in the same order: the link's own sortBy (None: it
        stands), else one naming each feature once; None when the layer has none."""
        from . import PrepRefused
        path = self.ctx.fetch(with_params(base, {size_key: 1}))
        self.ctx.steps.pop()                           # a look at the attributes, no input
        with open(path, "rb") as f:
            raw = f.read()
        os.unlink(path)
        self._not_features(raw, "the service's answer")
        try:
            feats = json.loads(raw.decode("utf-8-sig")).get("features") or []
        except (UnicodeDecodeError, ValueError, AttributeError):
            raise PrepRefused(f"{self.url} did not answer GeoJSON — ask the service for "
                              "outputFormat=application/json") from None
        props = (feats[0].get("properties") or {}) if feats and isinstance(feats[0], dict) else {}
        low = {str(k).lower(): k for k in props}
        sort = (None if "sortby" in q or version.startswith("1.0")
                else next((low[n] for n in _ID_NAMES if n in low), None))
        return sort, len(raw)

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
        sort, one = self._look(base, size_key, q, version)
        if sort:
            base = with_params(base, {"sortBy": sort})
        count = max(1, min(PAGE, PAGE_BYTES // max(1, one)))
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
                raw = f.read()
            os.unlink(path)
            digest.update(raw)
            shas.append(step["sha256"])
            asked.append([start, count])
            nbytes += step["bytes"]
            pages += 1
            self._not_features(raw, f"page {pages} of the service's answer")
            try:
                fc = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError):
                raise PrepRefused(f"page {pages} of {self.url} is not GeoJSON — ask the service "
                                  "for outputFormat=application/json") from None
            del raw
            feats = fc.get("features") if isinstance(fc, dict) else None
            if not isinstance(feats, list):
                raise PrepRefused(f"page {pages} of {self.url} holds no FeatureCollection")
            m = fc.get("numberMatched")
            if isinstance(m, int) and not isinstance(m, bool):
                matched = m
            for ft in feats:
                fid = ft.get("id") if isinstance(ft, dict) else None
                if fid is not None:
                    if fid in seen:
                        raise PrepRefused(
                            f"the service sent feature {fid!r} twice while being paged — it does "
                            "not page in a stable order, so a read could miss features; give the "
                            "layer as a file (GeoJSON, zipped shapefile, GeoPackage) instead")
                    seen.add(fid)
            got += len(feats)
            asked_for, weight = count, step["bytes"] / max(1, len(feats))
            yield self._chunk(fc, feats)
            if not feats:
                break
            if matched is not None:
                if got >= matched:
                    break
            elif len(feats) < asked_for and not _next_link(fc):
                break
            start += len(feats)
            count = max(1, min(PAGE, int(PAGE_BYTES // max(1.0, weight))))
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
    def _chunk(self, fc: dict, feats: list) -> list:
        from ...graph.geo import vecio
        from . import PrepRefused
        if fc.get("exceededTransferLimit") is True \
                or (fc.get("properties") or {}).get("exceededTransferLimit") is True:
            raise PrepRefused(f"{self.url}: " + vecio._TRUNCATED.format(n=len(feats)))
        try:
            crs = vecio._geojson_crs(fc)
        except ValueError as exc:
            raise PrepRefused(f"{self.url}: {exc}") from None
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
