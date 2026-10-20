"""api_points: a paged JSON API's records turned into a point layer, every page read on
the server.

The case: GBIF has no GeoJSON output and its occurrence search answers 300 records a
page, so the Costa Rica mangrove-species query (1,984 records) went in as one page of
300 and the gulf's records were paged into a GeoJSON by hand; OBIS answers occurrences
as JSON records (its /points endpoint is a bare MultiPoint without one attribute), and
each record's licence had to be joined by hand from its dataset table. Now the QUERY is
the layer's url and api_points says how to read it: a preset ('gbif', 'obis', from
PRESETS) or the generic form for any JSON API that answers records with a longitude and
a latitude. The server pages the query to its end under the preparation budget; each
record becomes a point keeping its own licence and attribution fields when the API gives
them; the record keeps the query, every page's sha256 and the count against the API's
own total.
"""

from __future__ import annotations

import json
import math
import os
import re
from urllib.parse import quote, unquote_plus, urljoin, urlparse

NAME = "api_points"
STEP = "assemble"            # it makes its own input: every page of the query
KINDS = ("vector",)
NEEDS_INPUT = False

MAX_RECORDS = 100_000        # records paged into one layer unless max_records says otherwise
AHEAD = 4                    # pages fetched side by side once every later page is known
_SHOWN = 12

# How each API pages and what each point keeps — data, one entry per API; the form text,
# the checks and the suggestions all derive from it. `keep` is {property: field in a
# record}; `join` adds fields from a second answer (one request), matched on a key.
PRESETS = {
    "gbif": {
        "api": "GBIF's occurrence search",
        "host": "api.gbif.org", "path": "/v1/occurrence/search",
        "records_path": "results", "lon": "decimalLongitude", "lat": "decimalLatitude",
        "total_path": "count", "end_path": "endOfRecords", "id": "gbifID",
        "paging": {"offset": "offset", "limit": "limit", "page_size": 300},
        "reach": 100_000,
        "beyond": ("GBIF's search pages through the first 100,000 records only — narrow the "
                   "query, or make a GBIF download of it (a free account; it gives a DOI to "
                   "cite) and contribute that file"),
        "keep": {"gbif_id": "gbifID", "species": "species", "scientific_name": "scientificName",
                 "event_date": "eventDate", "year": "year", "basis_of_record": "basisOfRecord",
                 "license": "license", "rights_holder": "rightsHolder",
                 "dataset_name": "datasetName", "dataset_key": "datasetKey",
                 "institution": "institutionCode",
                 "uncertainty_m": "coordinateUncertaintyInMeters"},
    },
    "obis": {
        "api": "OBIS's occurrence API",
        "host": "api.obis.org", "path": "/v3/occurrence",
        "also": r"/v3/occurrence/(?:points|grid|centroid)(?:/.*)?",
        "records_path": "results", "lon": "decimalLongitude", "lat": "decimalLatitude",
        "total_path": "total", "id": "id",
        "paging": {"after": "after", "after_field": "id", "limit": "size", "page_size": 10_000},
        "keep": {"obis_id": "id", "scientific_name": "scientificName", "species": "species",
                 "phylum": "phylum", "class": "class", "family": "family",
                 "event_date": "eventDate", "year": "date_year",
                 "basis_of_record": "basisOfRecord", "dataset_id": "dataset_id",
                 "institution": "institutionCode", "citation": "bibliographicCitation"},
        # OBIS keeps the licence on the dataset, not the record: the same query asked of
        # its dataset endpoint names each dataset's rights (a path: on the query's host).
        "join": {"url": "/v3/dataset", "same_query": True,
                 "limit": "size", "page_size": 10_000, "records_path": "results",
                 "key": "id", "on": "dataset_id",
                 "keep": {"dataset_title": "title", "dataset_rights": "intellectualrights"}},
    },
}

_FORM_KEYS = ("preset", "records_path", "lon", "lat", "total_path", "end_path", "id",
              "paging", "keep", "join", "max_records")
_STYLES = ("offset", "page", "after", "next_path")
_JOIN_KEYS = ("url", "same_query", "limit", "page_size", "records_path", "key", "on", "keep")
# a record's own licence and attribution, kept whatever `keep` says when the API gives them
_ATTRIBUTION = re.compile(r"licen[cs]e|rights|attribution|citation", re.I)


def _presets_text() -> str:
    return ", ".join(f"'{n}' ({p['host']}{p['path']})" for n, p in PRESETS.items())


FIELDS = {
    "api_points": (
        "for a POINT layer that is a JSON API's records rather than a file (an occurrence "
        "search, a station or sample registry): the url is the API QUERY (its filters: taxa, "
        "country, area, years) and api_points says how to read it — "
        f"{_presets_text()}, or for another API {{records_path: <where the list of records "
        "is in the answer, e.g. results or data.items>, lon: <a record's longitude field>, "
        "lat: <its latitude field>, total_path?: <the answer's total count>, paging?: "
        "{offset: <param>, limit: <param>, page_size: N} | {page: <param>, limit: <param>, "
        "page_size: N} | {after: <param>, after_field: <the record field the next page "
        "starts after>, limit: <param>, page_size: N} | {next_path: <where the answer gives "
        "the next page's url>}, end_path?: <a true/false 'last page' flag>, id?: <a record's "
        "unique id, so a record seen twice while paging is kept once>, keep?: {property: "
        "field, ...} or [field, ...] (default: every plain field of a record), join?: {url, "
        "key, on, keep, same_query?} (one more answer — a dataset table holding each "
        "dataset's licence — whose `keep` fields a record gets when its `on` field equals a "
        "row's `key`), max_records?: N}; with a preset it may be {preset: "
        f"{next(iter(PRESETS))}, max_records: N}}. The platform pages the query to its END "
        "on the server (every page, not only the first), makes each record a point that keeps its "
        "own licence and attribution fields when the API gives them, and records the query, "
        f"every page's sha256 and the count against the API's total. A query matching more "
        f"than max_records (default {MAX_RECORDS:,}) records is declined, or with max_records "
        "given its first max_records land and the record says it is a part. Never guess an "
        "API's parameter names: take them from its documentation or ask the contributor"),
}


# --------------------------------------------------------------------------- the reading

def _spec(manifest) -> dict:
    """The reading the manifest declares: a preset's, with the form's overrides.
    Raises ValueError in words the contributor's agent can act on."""
    v = manifest.get("api_points")
    names = " or ".join(f"'{n}'" for n in PRESETS)
    if isinstance(v, str):
        name = v.strip().lower()
        if name not in PRESETS:
            raise ValueError(f"api_points {v!r} is no API the platform knows — give {names}, or "
                             "the generic form {records_path, lon, lat, paging?, ...}")
        return {"preset": name, **PRESETS[name]}
    if not isinstance(v, dict) or not v:
        raise ValueError(f"api_points must be {names}, or {{records_path, lon, lat, paging?, "
                         f"...}} for another API — got {v!r}")
    unknown = sorted(set(v) - set(_FORM_KEYS))
    if unknown:
        raise ValueError(f"api_points does not take {', '.join(unknown)} — its fields are "
                         f"{', '.join(_FORM_KEYS)}")
    base = {}
    if v.get("preset") is not None:
        name = str(v["preset"]).strip().lower()
        if name not in PRESETS:
            raise ValueError(f"api_points preset {v['preset']!r} is no API the platform knows "
                             f"— give {names}, or drop preset and describe the API")
        base = {"preset": name, **PRESETS[name]}
    return {**base, **{k: x for k, x in v.items() if k != "preset" and x is not None}}


def _host_path(url: str) -> tuple[str, str]:
    p = urlparse(str(url or "").strip())
    return (p.hostname or "").lower(), (p.path or "/").rstrip("/") or "/"


def _preset_for(url: str) -> tuple[str | None, str | None]:
    """(preset, None) when the url is a preset's query; (preset, url) when it asks the
    same API for another shape of the records (OBIS's /points), with the query's url."""
    host, path = _host_path(url)
    for name, p in PRESETS.items():
        if host != p["host"]:
            continue
        if path == p["path"]:
            return name, None
        if p.get("also") and re.fullmatch(p["also"], path):
            parts = urlparse(str(url).strip())
            return name, parts._replace(path=p["path"], fragment="").geturl()
    return None, None


def _paging_problems(pg) -> list[str]:
    if pg is None:
        return []
    if not isinstance(pg, dict):
        return [f"api_points paging must be a mapping, e.g. {{offset: offset, limit: limit, "
                f"page_size: 100}} — got {pg!r}"]
    styles = [s for s in _STYLES if pg.get(s)]
    if len(styles) != 1:
        return ["api_points paging names ONE way the API pages: offset (records skipped), "
                "page (a page number), after (the last record's id) or next_path (the next "
                f"page's url in the answer) — got {', '.join(styles) or 'none of them'}"]
    problems = []
    known = {"offset": ("offset", "limit", "page_size"),
             "page": ("page", "limit", "page_size", "first"),
             "after": ("after", "after_field", "limit", "page_size"),
             "next_path": ("next_path",)}[styles[0]]
    extra = sorted(set(pg) - set(known))
    if extra:
        problems.append(f"api_points paging by {styles[0]} does not take {', '.join(extra)} — "
                        f"it takes {', '.join(known)}")
    if styles[0] != "next_path":
        size = pg.get("page_size")
        if pg.get("limit") and not _whole(size, 1):
            problems.append("api_points paging names the page-size parameter (limit); give the "
                            "records per page too: page_size (what the API documents as its "
                            "most)")
        if not pg.get("limit") and size is not None:
            problems.append("api_points paging gives page_size but not the parameter that sets "
                            "it (limit)")
    if styles[0] == "after" and not pg.get("after_field"):
        problems.append("api_points paging by after needs after_field: the record field whose "
                        "value the next page starts after (often id)")
    if styles[0] == "page" and pg.get("first") is not None and not _whole(pg["first"], 0):
        problems.append("api_points paging first must be the number of the first page (0 or 1)")
    return problems


def _whole(v, lo: int) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo


def validate(manifest) -> list[str]:
    from .. import fetch_policy
    try:
        spec = _spec(manifest)
    except ValueError as exc:
        return [str(exc)]
    problems = []
    url = str(manifest.get("url") or "")
    name = spec.get("preset")
    if name and url:
        found, fixed = _preset_for(url)
        p = PRESETS[name]
        if fixed and found == name:
            problems.append(f"{fetch_policy.shown(url)} answers the records' locations without "
                            f"their attributes — give the same query at {p['host']}{p['path']}: "
                            f"url: {fixed} with api_points: {name}")
        elif found != name:
            problems.append(f"api_points: {name} reads {p['api']} ({p['host']}{p['path']}); the "
                            f"url is {fetch_policy.shown(url)} — give that API's query url, or "
                            "the generic form for another API")
    for k in ("records_path", "lon", "lat"):
        if not isinstance(spec.get(k), str) or (k != "records_path" and not spec[k].strip()):
            problems.append(f"api_points needs {k}: " + {
                "records_path": "where the list of records is in the API's answer (e.g. "
                                "results, data.items; '' when the answer is the list itself)",
                "lon": "the field of a record holding its longitude (e.g. decimalLongitude, "
                       "location.lon)",
                "lat": "the field of a record holding its latitude (e.g. decimalLatitude, "
                       "location.lat)"}[k])
    for k in ("total_path", "end_path", "id"):
        if spec.get(k) is not None and not isinstance(spec[k], str):
            problems.append(f"api_points {k} must be a field path such as count, got {spec[k]!r}")
    problems += _paging_problems(spec.get("paging"))
    keep = spec.get("keep")
    if keep is not None and not (
            (isinstance(keep, dict) and keep and all(isinstance(a, str) and isinstance(b, str)
                                                     for a, b in keep.items()))
            or (isinstance(keep, list) and keep and all(isinstance(a, str) for a in keep))):
        problems.append("api_points keep must be {property: field, ...} or [field, ...] — the "
                        "fields of a record each point keeps")
    if spec.get("max_records") is not None and not _whole(spec["max_records"], 1):
        problems.append(f"api_points max_records must be a whole number of records, got "
                        f"{spec['max_records']!r}")
    join = spec.get("join")
    if join is not None:
        if not isinstance(join, dict) or not all(join.get(k) for k in ("url", "key", "on", "keep")):
            problems.append("api_points join must be {url, key, on, keep, records_path?, "
                            "same_query?}: a second answer (one request) whose records' key "
                            "matches each record's `on` field, and the fields it adds")
        else:
            extra = sorted(set(join) - set(_JOIN_KEYS))
            if extra:
                problems.append(f"api_points join does not take {', '.join(extra)}")
            if not isinstance(join["keep"], dict):
                problems.append("api_points join keep must be {property: field, ...}")
            if urlparse(str(join["url"])).scheme:      # else a path on the query's own host
                problems += [f"api_points join url: {p}"
                             for p in fetch_policy.check_url(join["url"])]
    return problems


# --------------------------------------------------------------------------- reading answers

def _at(obj, path: str | None):
    """The value at a dotted path ('data.items', 'location.lon', 'coords.0'); '' is the
    object itself; None when any step is missing."""
    if path is None:
        return None
    if path in ("", "."):
        return obj
    for part in str(path).split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif (isinstance(obj, list) and part.lstrip("-").isdigit()
              and -len(obj) <= int(part) < len(obj)):
            obj = obj[int(part)]
        else:
            return None
        if obj is None:
            return None
    return obj


def _coord(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(str(v).strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _plain(v):
    """A property value a GeoJSON and a YAML record hold: scalars as they are, a list of
    scalars joined, anything else as compact JSON."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, list) and all(isinstance(x, (str, int, float, bool)) for x in v):
        return "; ".join(str(x) for x in v)
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))[:2000]


def _number(v) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if math.isfinite(f) and f >= 0 else None


def _with(url: str, params: dict, drop) -> str:
    """`url` with the query parameters in `drop` removed and `params` appended; every
    other parameter kept exactly as the contributor wrote it (repeated keys too)."""
    base = str(url).strip().split("#", 1)[0]
    head, _, query = base.partition("?")
    kept = [p for p in query.split("&") if p and unquote_plus(p.split("=", 1)[0]) not in drop]
    kept += [f"{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in params.items()]
    return head + ("?" + "&".join(kept) if kept else "")


def _own_paging(url: str, drop) -> list[str]:
    query = str(url).split("#", 1)[0].partition("?")[2]
    return sorted({unquote_plus(p.split("=", 1)[0]) for p in query.split("&")
                   if p and unquote_plus(p.split("=", 1)[0]) in drop})


def _load(path: str, where: str, n: int) -> object:
    from . import PrepRefused
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        head = " ".join(raw[:120].decode("utf-8", "replace").split())
        raise PrepRefused(f"page {n} of {where} is not JSON (it starts {head!r}) — api_points "
                          "reads an API that answers JSON records") from None


def _keys(obj) -> str:
    return ", ".join(list(obj)[:_SHOWN]) if isinstance(obj, dict) else type(obj).__name__


# --------------------------------------------------------------------------- run

def run(ctx):
    """Page the query to its end (or to max_records), one point per record with a usable
    longitude and latitude; join the second answer when the reading has one. Without a
    total or an end flag, a short page does not end the query (an API may answer fewer
    records than asked); an empty page does. Once the first page shows the API honours the
    page size and gives a total, the next AHEAD pages are fetched side by side."""
    from .. import fetch_policy
    from . import PrepRefused, PreparedFile, size_text
    m = ctx.manifest
    spec = _spec(m)
    url = str(m["url"]).strip().split("#", 1)[0]
    shown = fetch_policy.shown(url)
    api = spec.get("api") or f"the API at {_host_path(url)[0]}"
    pg = spec.get("paging") or {}
    style = next((s for s in _STYLES if pg.get(s)), None)
    drop = {pg[k] for k in ("offset", "page", "after", "limit") if pg.get(k)}
    size = int(pg.get("page_size") or 0)
    rp, lon_p, lat_p = spec["records_path"], spec["lon"], spec["lat"]
    id_p = spec.get("id")
    keep = spec.get("keep")
    if isinstance(keep, list):
        keep = {re.sub(r"\W+", "_", k).strip("_") or k: k for k in keep}
    used = {str(p).split(".", 1)[0] for p in (keep or {}).values()} | {
        lon_p.split(".", 1)[0], lat_p.split(".", 1)[0]}
    explicit = spec.get("max_records") is not None
    budget, why = _budget(spec, ctx, api)
    join = spec.get("join")

    def params_for(k: int, got: int, cur) -> dict:
        """The paging parameters of page k (1-based) after `got` records."""
        p = {}
        want = max(1, min(size, budget - got)) if size else None
        if style == "offset":
            p[pg["offset"]] = got
        elif style == "page":
            p[pg["page"]] = int(pg.get("first", 1)) + k - 1
        elif style == "after" and cur is not None:
            p[pg["after"]] = cur
        if want and pg.get("limit"):
            p[pg["limit"]] = want
        return p

    def at(p: dict) -> str:
        return _with(url, p, drop) if (p or drop) else url

    feats, join_on, pages = [], [], []
    seen: set = set()
    visited = {url}
    dup = nocoord = outside = received = 0
    nested: set = set()
    total = None
    stored = 64
    cursor = None
    page_url = url
    prev, prev_short = None, False
    truncated = False
    ahead = _Ahead(ctx, shown)
    try:
        while True:
            ctx.check_time()
            n = len(pages) + 1
            params = params_for(n, received, cursor) if style != "next_path" else {}
            want = params.get(pg.get("limit")) if pg.get("limit") else None
            if style != "next_path":
                page_url = at(params)
            got = ahead.take(page_url)
            fetched, doc = got if got else _page(ctx, page_url, shown, n)
            if prev is not None and fetched["sha256"] == prev:
                if prev_short:
                    break               # asked past the end, the API answered its last page again
                raise PrepRefused(
                    f"{api} answered page {n} exactly as page {n - 1}: it does not page by "
                    + (", ".join(sorted(params)) if params else "the paging given")
                    + " — take its paging parameters from its documentation (or ask the "
                      "contributor) and resubmit")
            prev = fetched["sha256"]
            recs = _at(doc, rp)
            if not isinstance(recs, list):
                raise PrepRefused(
                    f"{api}'s answer has no list of records at records_path {rp!r} — its answer "
                    f"holds {_keys(doc)}; give the path to the list (e.g. results, data.items)")
            if n == 1:
                total = _number(_at(doc, spec.get("total_path")))
                if total is not None and total > budget and not explicit:
                    raise PrepRefused(_over(total, budget, why, spec))
                if recs and not any(_coord(_at(r, lon_p)) is not None
                                    for r in recs[:50] if isinstance(r, dict)):
                    raise PrepRefused(
                        f"no record of {api}'s first page has a longitude at {lon_p!r} — a "
                        f"record holds {_keys(recs[0])}; give lon and lat as the fields that "
                        "hold them")
                if (style in ("offset", "page") and total is not None and want
                        and len(recs) == want == size):
                    # the API honours the page size: every later page is known now
                    plan, got_, k = [], len(recs), 2
                    while got_ < min(total, budget):
                        p = params_for(k, got_, None)
                        plan.append(at(p))
                        got_, k = got_ + p[pg["limit"]], k + 1
                    ahead.plan(plan)
            entry = {"records": len(recs), "sha256": fetched["sha256"], "bytes": fetched["bytes"]}
            if style == "next_path" and n > 1:
                entry["url"] = page_url
            elif params:
                entry["params"] = params
            pages.append(entry)
            for r in recs:
                if received >= budget:
                    truncated = True
                    break
                received += 1
                if not isinstance(r, dict):
                    nocoord += 1
                    continue
                x, y = _coord(_at(r, lon_p)), _coord(_at(r, lat_p))
                if x is None or y is None:
                    nocoord += 1
                    continue
                if not (-180 <= x <= 180 and -90 <= y <= 90):
                    outside += 1
                    continue
                rid = _at(r, id_p) if id_p else None
                if rid is not None:
                    if str(rid) in seen:
                        dup += 1
                        continue
                    seen.add(str(rid))
                props = {}
                for out, p in (keep or {}).items():
                    v = _plain(_at(r, p))
                    if v is not None:
                        props[out] = v
                for k, v in r.items():
                    if k in used or (keep is not None and not _ATTRIBUTION.search(k)):
                        continue
                    if isinstance(v, dict) or (isinstance(v, list) and not all(
                            isinstance(x_, (str, int, float, bool)) for x_ in v)):
                        nested.add(k)
                        continue
                    v = _plain(v)
                    if v is not None and k not in props:
                        props[k] = v
                ft = {"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [round(x, 7), round(y, 7)]}}
                stored += len(json.dumps(ft, ensure_ascii=False)) + 2
                if stored > ctx.cap:
                    raise PrepRefused(
                        f"the first {len(feats):,} points of {api}'s"
                        + (f" {total:,}" if total is not None else "")
                        + f" records already make {size_text(stored)} of GeoJSON, over the "
                          f"{size_text(ctx.cap)} the platform stores for one layer — keep fewer "
                          "fields (api_points keep), narrow the query, or set max_records")
                feats.append(ft)
                if join:
                    join_on.append(_at(r, join["on"]))
            ctx.check_vertices(len(feats), "the query's points")
            # where the next page starts, or the end
            if truncated or not recs or style is None:
                break
            if spec.get("end_path") and _at(doc, spec["end_path"]) is True:
                break
            if total is not None and received >= total:
                break
            if received >= budget:
                truncated = True        # more may follow: the API gives no total
                break
            prev_short = want is not None and len(recs) < want
            if style == "after":
                cursor = _at(recs[-1], pg["after_field"])
                if cursor is None:
                    raise PrepRefused(f"the last record of page {n} has no "
                                      f"{pg['after_field']!r} to page after — give "
                                      "paging.after_field as the field the API pages by")
            elif style == "next_path":  # the answer names the next page
                nxt = _at(doc, pg["next_path"])
                if not isinstance(nxt, str) or not nxt.strip():
                    break
                page_url = urljoin(page_url, nxt.strip())
                if page_url in visited:
                    raise PrepRefused(f"{api}'s next link on page {n} points back to a page "
                                      "already read — check next_path")
                visited.add(page_url)
        joined = _join(ctx, join, url, drop, feats, join_on) if join and feats else None
    except (PrepRefused, fetch_policy.FetchRefused) as exc:
        if ctx.remaining() <= 0.5 and not isinstance(exc, fetch_policy.TooLarge):
            raise PrepRefused(
                f"{api} answered too slowly: {len(pages):,} page(s), {received:,}"
                + (f" of {total:,}" if total is not None else "")
                + f" records were read when the step's {ctx.op_seconds:g} s ran out — try "
                  "again later (an API often answers faster the second time), or narrow the "
                  "query") from None
        raise
    finally:
        ahead.close()
    if truncated and not explicit:
        raise PrepRefused(_over(total, budget, why, spec, received=received))
    if not feats:
        raise PrepRefused(
            f"{api} answered {received:,} record(s) for the query and none has a usable "
            f"longitude and latitude at {lon_p!r}/{lat_p!r}"
            + (f" ({outside:,} outside lon/lat range)" if outside else "")
            + " — check the query (it may match nothing) and the lon/lat fields")

    out = ctx.tmp(".geojson", "points")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": feats}, f, ensure_ascii=False,
                  separators=(",", ":"))
    ctx.check_time()

    if truncated:
        of = (f"the first {received:,} of the {total:,} records the query matched ({why})"
              if total is not None else f"the first {received:,} records ({why}) — the API "
              "may hold more (it gives no total)")
    elif total is None:
        of = f"all {received:,} records the API answered (it gives no total)"
    elif received >= total:
        of = f"every record of the {total:,} the query matched"
    else:
        of = (f"{received:,} records of the {total:,} the query matched — the API stopped "
              "answering records before its own total")
    per = f" of up to {size:,}" if size else ""
    notes = [f"built at ingest from {api}: {len(feats):,} points from {of}, read in "
             f"{len(pages):,} page(s){per} from {shown}"]
    if truncated:
        notes.append(f"a PART of the query: {received:,} of "
                     + (f"{total:,}" if total is not None else "more")
                     + " records — say so wherever the layer is counted")
    attrib = sorted({k for ft in feats[:2000] for k in ft["properties"]
                     if _ATTRIBUTION.search(k) or _ATTRIBUTION.search((keep or {}).get(k, ""))}
                    | (set(join["keep"]) if joined else set()))
    if attrib:
        notes.append("each point keeps its own licence and attribution as the API gives "
                     f"them: {', '.join(attrib)}")
    if nocoord:
        notes.append(f"{nocoord:,} record(s) without a longitude and latitude left out")
    if outside:
        notes.append(f"{outside:,} record(s) with coordinates outside lon/lat range left out")
    if dup:
        notes.append(f"{dup:,} record(s) seen twice while paging (same {id_p}) kept once")
    if nested:
        notes.append("nested fields not kept: " + ", ".join(sorted(nested)[:_SHOWN])
                     + " — name what is wanted in api_points keep")
    own = _own_paging(url, drop)
    if own:
        notes.append(f"the url's own {', '.join(own)} {'is' if len(own) == 1 else 'are'} "
                     "set by the paging")
    if joined:
        notes.append(joined["note"])
    ctx.record["read"] = (f"paged from {api} on the server: {len(pages):,} page(s), "
                          f"{received:,} record(s)"
                          + (f" of {total:,}" if total is not None else ""))
    inputs = {"url": url, "api": spec.get("preset") or "custom",
              "reading": {k: spec[k] for k in ("records_path", "lon", "lat", "total_path",
                                               "end_path", "id") if spec.get(k) is not None},
              "paging": dict(pg) or None, "keep": keep, "max_records": budget,
              "total": total, "records": received, "points": len(feats),
              "left_out": {"no coordinates": nocoord, "outside lon/lat": outside,
                           "duplicates": dup},
              "part": truncated, "pages": pages}
    if joined:
        inputs["join"] = joined["record"]
    return PreparedFile(path=out, notes=notes, provenance={"inputs": inputs}, kind_hint="vector")


class _Ahead:
    """The planned pages, fetched AHEAD at a time on worker threads straight through
    fetch_policy (each hop checked and pinned), each streamed to its own file in the
    workdir, read and removed; handed back in the loop's order by url, never more than
    AHEAD read and waiting. A page that failed ahead is fetched again in order (with its
    retry); a loop that leaves the plan (a short page) stops it."""

    def __init__(self, ctx, shown: str):
        self.ctx, self.shown, self.pool = ctx, shown, None
        self.queue: list[str] = []
        self.futures: dict = {}

    def plan(self, urls: list[str]) -> None:
        import concurrent.futures as cf
        if urls:
            self.queue = list(urls)
            self.pool = cf.ThreadPoolExecutor(max_workers=AHEAD,
                                              thread_name_prefix="api-points")
            self._fill()

    def _fill(self) -> None:
        while self.queue and len(self.futures) < AHEAD:
            u = self.queue.pop(0)
            self.futures[u] = self.pool.submit(
                _fetch_ahead, self.ctx.workdir, u, max(1, self.ctx._room() // AHEAD),
                max(1.0, self.ctx.remaining()), self.shown)

    def take(self, url: str):
        from . import PrepRefused, size_text
        fut = self.futures.pop(url, None)
        if fut is None:
            self.stop()
            return None
        try:
            fetched, doc = fut.result(timeout=max(1.0, self.ctx.remaining()))
        except Exception:
            return None                 # fetched again in order, with its retry and its words
        finally:
            if self.pool is not None:
                self._fill()
        self.ctx.fetched_bytes += int(fetched["bytes"])
        if self.ctx.fetched_bytes > self.ctx.max_input_bytes:
            raise PrepRefused(f"the query's pages reached the "
                              f"{size_text(self.ctx.max_input_bytes)} the platform downloads to "
                              "prepare one contribution — narrow the query")
        return fetched, doc

    def stop(self) -> None:
        self.queue.clear()
        for f in self.futures.values():
            f.cancel()
        self.futures.clear()

    def close(self) -> None:
        self.stop()
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)


def _fetch_ahead(workdir, url: str, limit: int, deadline: float, shown: str):
    import uuid
    from .. import fetch_policy
    from . import _digest
    path = os.path.join(str(workdir), f"ahead-{uuid.uuid4().hex}.part")
    fetch_policy.fetch_to_file(url, path, max_bytes=limit, deadline=deadline)
    try:
        return _digest(path), _load(path, shown, "ahead")
    finally:
        os.unlink(path)


def _budget(spec: dict, ctx, api: str) -> tuple[int, str]:
    """(records this query may page, what sets that number, in words)."""
    asked = spec.get("max_records")
    room = [(int(asked) if asked else MAX_RECORDS,
             "max_records" if asked else "the most the platform pages into one layer"),
            (int(ctx.max_vertices), "the preparation's vertex budget")]
    if spec.get("reach"):
        room.append((int(spec["reach"]), f"the most {api} pages"))
    return min(room, key=lambda r: r[0])


def _over(total, budget: int, why: str, spec: dict, received: int | None = None) -> str:
    said = (f"the query matches {total:,} records" if total is not None else
            f"the query holds more than the {received:,} records read (the API gives no total)")
    if spec.get("reach") and budget == spec["reach"] and spec.get("beyond"):
        return f"{said}; {spec['beyond']}; or set max_records: N to keep the first N"
    return (f"{said}, over {budget:,} ({why}) — narrow the query (an area, a year range, "
            "fewer taxa), or set max_records: N to keep the first N (the record then says it "
            "holds N of " + (f"{total:,})" if total is not None else "more)"))


def _page(ctx, url: str, shown: str, n: int) -> tuple[dict, object]:
    """One page fetched (streamed to disk, under the budget and policy), read as JSON and
    removed: (its sha256 and bytes, the answer). A page that fails is asked once more."""
    import time
    from .. import fetch_policy
    try:
        path = ctx.fetch(url)
    except fetch_policy.TooLarge:
        raise
    except fetch_policy.FetchRefused:
        if ctx.remaining() < 5:
            raise
        time.sleep(2)
        path = ctx.fetch(url)
    fetched = ctx.steps.pop()           # recorded once, as a page of the api_points step
    try:
        return fetched, _load(path, shown, n)
    finally:
        os.unlink(path)


def _join(ctx, join: dict, url: str, drop, feats: list, join_on: list) -> dict:
    """Add the join's fields to each point whose `on` value one of its records' key holds
    (one request: the join url — a path is on the query's host — with the query's own
    filters when same_query)."""
    from .. import fetch_policy
    from . import PrepRefused
    params = ({join["limit"]: join["page_size"]}
              if join.get("limit") and join.get("page_size") else {})
    jurl = urljoin(url, str(join["url"]).strip())
    if join.get("same_query"):
        query = "&".join(p for p in url.partition("?")[2].split("&")
                         if p and unquote_plus(p.split("=", 1)[0]) not in drop)
        jurl = jurl.split("?", 1)[0] + (f"?{query}" if query else "")
    jurl = _with(jurl, params, set(params)) if params else jurl
    fetched, doc = _page(ctx, jurl, fetch_policy.shown(jurl), 1)
    rows = _at(doc, join.get("records_path") or "")
    if not isinstance(rows, list):
        raise PrepRefused(f"the join at {fetch_policy.shown(jurl)} has no list of records at "
                          f"{join.get('records_path')!r} — its answer holds {_keys(doc)}")
    table = {}
    for row in rows:
        k = _at(row, join["key"]) if isinstance(row, dict) else None
        if k is not None:
            table[str(k)] = {out: _plain(_at(row, p)) for out, p in join["keep"].items()}
    wanted = {str(k) for k in join_on if k is not None}
    found = wanted & set(table)
    hit = 0
    for ft, k in zip(feats, join_on):
        add = table.get(str(k)) if k is not None else None
        if add:
            hit += 1
            for out, v in add.items():
                if v is not None and out not in ft["properties"]:
                    ft["properties"][out] = v
    note = (f"{', '.join(join['keep'])} joined from {fetch_policy.shown(jurl)} on "
            f"{join['on']}: {len(found):,} of {len(wanted):,} {join['on']} value(s) found, "
            f"{hit:,} of {len(feats):,} points")
    return {"note": note, "record": {"url": jurl, "on": join["on"], "key": join["key"],
                                     "keep": dict(join["keep"]), "rows": len(rows),
                                     "matched": len(found), "of": len(wanted),
                                     "sha256": fetched["sha256"], "bytes": fetched["bytes"]}}


# --------------------------------------------------------------------------- suggestions

_LON = re.compile(r"^(?:decimal_?)?(?:lon|lng|long|longitude)$", re.I)
_LAT = re.compile(r"^(?:decimal_?)?(?:lat|latitude)$", re.I)
_TOTAL = re.compile(r"^(?:count|total|total_?count|total_?results|num_?found|hits)$", re.I)


def _coords_in(rec: dict) -> tuple[str, str] | None:
    """(lon path, lat path) of a record — at its top level or one level down."""
    def pick(d, pre=""):
        lon = next((k for k in d if _LON.match(str(k)) and _coord(d[k]) is not None), None)
        lat = next((k for k in d if _LAT.match(str(k)) and _coord(d[k]) is not None), None)
        return (pre + lon, pre + lat) if lon and lat else None
    found = pick(rec)
    if found:
        return found
    for k, v in rec.items():
        if isinstance(v, dict):
            found = pick(v, f"{k}.")
            if found:
                return found
    return None


def _guess(doc) -> dict | None:
    """Where an answer keeps its records and their coordinates, when it looks like a
    record list: {records_path, lon, lat, n, total_path?, total?}."""
    cands = [("", doc)] if isinstance(doc, list) else []
    if isinstance(doc, dict):
        for k, v in doc.items():
            if isinstance(v, list):
                cands.append((k, v))
            elif isinstance(v, dict):
                cands += [(f"{k}.{k2}", v2) for k2, v2 in v.items() if isinstance(v2, list)]
    for path, recs in cands:
        first = [r for r in recs[:20] if isinstance(r, dict)]
        found = next((c for c in map(_coords_in, first) if c), None)
        if found:
            out = {"records_path": path, "lon": found[0], "lat": found[1], "n": len(recs)}
            if isinstance(doc, dict):
                levels = [("", doc)] + [(f"{k}.", v) for k, v in doc.items() if isinstance(v, dict)]
                tot = next(((pre + k, _number(v)) for pre, d in levels for k, v in d.items()
                            if _TOTAL.match(str(k)) and _number(v) is not None), None)
                if tot:
                    out.update(total_path=tot[0], total=tot[1])
            return out
    return None


def suggest(situation: dict) -> str | None:
    """For an answer that is a JSON API's records rather than a layer: the preset the url
    belongs to, or the generic form with the records' path and coordinate fields read
    from the answer itself — the paging is the API's to name, never guessed."""
    m = situation.get("manifest") or {}
    if m.get("api_points") is not None or situation.get("event") != "not_features":
        return None
    url = str(situation.get("url") or m.get("url") or "")
    name, fixed = _preset_for(url)
    if name:
        p = PRESETS[name]
        what = (f"the platform pages {p['api']} to its END on the server (every page of up to "
                f"{p['paging']['page_size']:,}, not only the first), one point per record "
                "keeping its own licence and attribution, and the record keeps the query and "
                "every page's sha256 — nothing to do by hand")
        if fixed:
            return (f"that url answers the records' locations without their attributes; "
                    f"resubmit with url: {fixed} (the same query) and api_points: {name} — "
                    f"{what}")
        return f"resubmit the same link with api_points: {name} — {what}"
    raw = situation.get("raw")
    if not raw or len(raw) > (64 << 20):
        return None
    try:
        doc = json.loads(raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else raw)
    except (UnicodeDecodeError, ValueError, TypeError):
        return None
    g = _guess(doc)
    if not g:
        return None
    form = f"{{records_path: '{g['records_path']}', lon: '{g['lon']}', lat: '{g['lat']}'"
    form += f", total_path: '{g['total_path']}'}}" if g.get("total_path") else "}"
    holds = f"this answer holds {g['n']:,} record(s)" + (
        f" and says the query matches {g['total']:,}" if g.get("total") is not None else "")
    return (f"it is a JSON API's records, not a layer: resubmit the same link with api_points: "
            f"{form} — the platform makes each record a point keeping its own licence and "
            f"attribution fields; {holds}: if the API pages its answer, add api_points paging "
            "with the API's OWN parameter names ({offset: <param>, limit: <param>, page_size: "
            "N}, {page: ...}, {after: ...} or {next_path: ...}) — take them from its "
            "documentation or ask the contributor, never guess")
