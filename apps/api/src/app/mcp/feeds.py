"""The fetch bone's live-feed half — ENTRY + ADAPTER, generic from the start.

Nothing here knows about any particular upstream. `registry.FEEDS` declares each
feed (id, params schema, passport, which adapter); this module holds small
per-upstream ADAPTERS that normalise a response into one shape. Adding a feed that
reuses an adapter shape is pure config; a genuinely new upstream adds one adapter.
Dispatch, declines and the tool description are all derived — no feed name is
hardcoded in logic or prose.
"""

from __future__ import annotations

from ..food_security import cropmonitor
from . import climate_indices, enso_forecast, registry


def scope_miss(spec: dict, countries, bounds=None):
    """None when a table or feed may be cited for an area, else (where it is scoped, why
    the area is not in it) — the rule every pack applies. A source that declares
    `countries` or a `bbox` [w, s, e, n] speaks only to an area in one of those countries
    and touching that box; one that declares neither is global. `countries`: the area's
    (unknown rules nothing out, as for documents and vectors); `bounds` (w, s, e, n): its
    extent, or None for a pack that answers for a whole country and holds no area (food
    security) — a box is then placed only through the countries the source also
    declares, and a box alone is said, never guessed."""
    from ..rag.filters import speaks_to
    declared = [str(c) for c in (spec.get("countries") or
                                 ([spec["country"]] if spec.get("country") else [])) if str(c).strip()]
    bbox = spec.get("bbox")
    try:
        bbox = [float(v) for v in bbox] if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None
    except (TypeError, ValueError):
        bbox = None
    if not declared and not bbox:
        return None
    countries = [str(c) for c in (countries or []) if str(c).strip()]
    where = ", ".join(declared) + (" " if declared and bbox else "") + (
        f"(lon {bbox[0]:.2f} to {bbox[2]:.2f}, lat {bbox[1]:.2f} to {bbox[3]:.2f})" if bbox else "")
    if bbox and bounds is not None:
        w, s, e, n = bounds
        if bbox[2] < w or bbox[0] > e or bbox[3] < s or bbox[1] > n:
            return where, "this area lies outside that box"
    if declared and countries and not speaks_to(declared, countries):
        return where, f"this area is in {', '.join(countries)}"
    if bbox and bounds is None and not (declared and countries):
        return where, ("this pack answers for a whole country and holds no area to place that "
                       "box against")
    return None


def scope_gap(missed: dict, noun: str = "source") -> str | None:
    """One gap naming every bound source a pack did not cite because it is scoped
    elsewhere: {dataset: (where, why)}."""
    if not missed:
        return None
    return (f"{len(missed)} {noun}(s) bound to this pack were NOT cited because they are "
            "scoped elsewhere: " + "; ".join(f"{ds} speaks to {w} — {why}"
                                            for ds, (w, why) in sorted(missed.items()))
            + ". They are available through feeds_query.")


def is_stale(stale_data: dict | None) -> bool:
    """THE single definition of "this evidence was served stale".

    Two shapes exist. The climate adapters ALWAYS attach a `stale_data` dict
    (cadence, retrieved_at, served_stale, ...) so its presence means nothing —
    only the flag does. The conditions feed attaches the key only when it fell
    back, so there presence IS the signal. Testing presence alone marked every
    healthy feed as cache-served; that bug was fixed in `query` and then shipped
    again in `record`, because the rule lived in one call site instead of here.
    """
    if not stale_data:
        return False
    if "served_stale" in stale_data:
        return bool(stale_data["served_stale"])
    return True


class FeedDecline(Exception):
    """An upstream refusal worth passing through in full (alternatives included)."""

    def __init__(self, note: str, **extra):
        super().__init__(note)
        self.note, self.extra = note, extra


def _adapt_cropmonitor(params: dict, spec: dict) -> dict:
    """GEOGLAM Crop Monitor -> the normalised feed shape. (`spec` unused: this feed
    needs no registry-driven behaviour.)"""
    try:
        res = cropmonitor.conditions(month=params.get("month"), crop=params.get("crop"),
                                     place=params.get("place"))
    except ValueError as exc:                       # malformed month, not a coverage gap
        raise FeedDecline(str(exc)) from exc
    except cropmonitor.MonthNotAvailable as exc:
        raise FeedDecline(str(exc), nearest_available=exc.nearest,
                          available=({"from": exc.available[0], "to": exc.available[-1],
                                      "note": "not every month in this span is published"}
                                     if exc.available else None)) from exc
    except cropmonitor.CropNotFound as exc:
        raise FeedDecline(str(exc), available=exc.available) from exc
    except cropmonitor.CropMonitorError as exc:
        raise FeedDecline(f"feed unavailable: {exc}") from exc
    return {"as_of": res.get("as_of"), "count": res.get("count"),
            "summary": res.get("summary"), "records": res.get("records"),
            "query_receipt": (res.get("query") or {}).get("where"),
            "url": (res.get("query") or {}).get("url"),
            "stale_data": res.get("stale_data"), "note": res.get("note")}


def _adapt_climate_index(params: dict, spec: dict) -> dict:
    """Monthly climate-index series (ONI, DMI, derived ENSO events) -> feed shape.

    ONE adapter for three feeds: the registry row names which index via `index`, so
    a fourth index of the same shape is a config row and no code at all."""
    kind = spec.get("index")
    allowed = {"limit": int, "min_seasons": int}
    kwargs = {}
    for k, cast in allowed.items():
        if params.get(k) is not None:
            try:
                kwargs[k] = cast(params[k])
            except (TypeError, ValueError) as exc:
                raise FeedDecline(f"{k!r} must be a whole number") from exc
    try:
        res = climate_indices.fetch(kind, **kwargs)
    except TypeError as exc:                       # a param this index does not take
        raise FeedDecline(f"{kind} does not accept those params: {exc}") from exc
    except climate_indices.IndexUnavailable as exc:
        raise FeedDecline(f"feed unavailable: {exc}") from exc

    latest = res.get("latest") or {}
    if "events" in res:                            # the derived catalogue
        records, count = res["events"], res["count"]
        # Dated by the ONI series it was derived through, not left blank: an undated
        # source in a pack is one a reader cannot age, which is the point of the
        # freshness work. See climate_indices.enso_events for why not the last event.
        as_of = res.get("derived_through")
        summary = (f"{count} ENSO events on the {res['definition']}, derived through "
                   f"{as_of or 'unknown'}; most recent listed event ended "
                   f"{records[-1]['end'] if records else 'n/a'}. {res.get('in_progress_note', '')}")
    else:
        as_of = (f"{latest.get('season')} {latest.get('year')}" if latest.get("season")
                 else f"{latest.get('year')}-{latest.get('month'):02d}")
        summary = (f"{res['index']} {as_of}: {latest.get('value')} "
                   f"{res['units']} — {latest.get('classification')}")
        records, count = res.get("series", []), len(res.get("series", []))
    return {"as_of": as_of, "count": count, "summary": summary, "records": records,
            "query_receipt": f"{res['index']} via {res['url']}", "url": res["url"],
            # These are MONTHLY series; a value can legitimately be weeks old. Say so
            # rather than let a consumer read it as today's number.
            "stale_data": {"cadence": spec.get("cadence"),
                           "retrieved_at": res.get("retrieved_at"),
                           "served_from_cache": res.get("cached"),
                           "served_stale": res.get("served_stale"),
                           "reason": res.get("stale_reason")},
            "note": ("Driver signal only. This says nothing about local rainfall, crops "
                     "or food security — those need the later pillars." )}


def _adapt_enso_forecast(params: dict, spec: dict) -> dict:
    """IRI ENSO plume / narrative outlook -> feed shape. `product` on the registry
    row picks which, so both share one adapter."""
    product = spec.get("product")
    kwargs = {}
    for k in ("year", "month"):
        if params.get(k) is not None:
            try:
                kwargs[k] = int(params[k])
            except (TypeError, ValueError) as exc:
                raise FeedDecline(f"{k!r} must be a whole number") from exc
    try:
        res = enso_forecast.fetch(product, **kwargs)
    except TypeError as exc:
        # A param this product does not take. `enso_discussion` accepts none, so
        # {"year": 2026} reached discussion() and raised — escaping as a transport
        # error rather than a governed decline, which breaks rule 2. The sibling
        # climate-index adapter has always caught this; this one did not.
        raise FeedDecline(
            f"{product} does not accept those params ({exc}). Accepted: "
            + (", ".join(spec.get("params") or {}) or "none")) from exc
    except climate_indices.IndexUnavailable as exc:
        raise FeedDecline(f"feed unavailable: {exc}") from exc

    if "sections" in res:                          # the verbatim narratives
        records = res.get("sections", [])
        status = res.get("alert_status")
        summary = (f"{res['source']} {res['issued_for']} {res['product']}"
                   + (f" — ENSO Alert System Status: {status}" if status else "")
                   + f" (published {res.get('published')}), {len(records)} sections verbatim")
        note = res["handling"]
    else:
        agree = res.get("model_agreement") or []
        nxt = agree[0] if agree else {}
        summary = (f"IRI {res['issued_for']} plume — {res['model_count']} models across "
                   f"{len(res.get('lead_seasons', []))} lead seasons"
                   + (f"; at {nxt.get('season')}, {nxt.get('el_nino')} of "
                      f"{nxt.get('models_reporting')} models are at or above +0.5 C"
                      if nxt else ""))
        records = res.get("models", [])
        note = res["model_agreement_caveat"]

    # An upstream that answers a month it was not asked for, with HTTP 200, is
    # staleness by another name — carry it in the same field a cache-serve uses.
    stale = {"cadence": spec.get("cadence"), "retrieved_at": res.get("retrieved_at"),
             "served_from_cache": res.get("cached"),
             "served_stale": bool(res.get("served_stale") or res.get("substituted")),
             "reason": res.get("stale_reason") or res.get("substitution_note")}
    return {"as_of": res.get("issued_for"), "count": len(records), "summary": summary,
            "records": records, "query_receipt": f"{product} {res['issued_for']}",
            "url": res["url"], "stale_data": stale, "note": note}


# adapter name -> implementation, called as adapter(params, spec). A new upstream
# adds ONE entry here; a feed reusing an existing shape needs none.
def _classify_bands(v: float, bands: list) -> str | None:
    """Label a value against spec-declared bands [{min?, max?, label}]."""
    for b in bands or []:
        lo, hi = b.get("min"), b.get("max")
        if (lo is None or v >= lo) and (hi is None or v < hi):
            return b.get("label")
    return None



def _landed_table(recorded: str) -> _Path:
    """Where a contributed table actually lives ON THIS HOST.

    The spec records the path as it was at contribution time. An absolute one is
    the staging machine's, so it cannot resolve anywhere else — a developer's
    /Users/... path shipped to a Linux host left the feed permanently dark. A
    relative path is resolved against this host's cache dir; an absolute one is
    honoured if it exists, and otherwise recovered by basename under cache/tables
    so specs already written that way keep working.
    """
    from pathlib import Path
    from ..config import get_settings
    root = Path(get_settings().cache_dir)
    p = Path(recorded)
    if not p.is_absolute():
        return root / p
    if p.is_file():
        return p
    return root / "tables" / p.name


def _cache_key(spec: dict) -> str:
    """A declarative row's fetch cache: by its name AND what it fetches. By name alone,
    a feed withdrawn and resubmitted under the same name with a corrected URL or field
    map was answered from the old one's rows for the cache's six hours."""
    import hashlib
    import json
    what = json.dumps(spec.get("fetch") or {}, sort_keys=True, default=str)
    return f"declarative:{spec['declarative']}:{hashlib.sha256(what.encode()).hexdigest()[:12]}"


def _adapt_generic_table(params: dict, spec: dict) -> dict:
    """Declarative adapter for the NOAA text-series family: one row per year,
    12 monthly values, sentinel for not-yet-published. ONI/DMI/SOI all share it —
    a new index of this shape is a YAML file and no code."""
    from . import climate_indices
    fetch = spec["fetch"]
    limit = 12
    if params.get("limit") is not None:
        try:
            limit = max(1, int(params["limit"]))
        except (TypeError, ValueError) as exc:
            raise FeedDecline("'limit' must be a whole number") from exc
    missing = float(fetch.get("missing_below", -90))
    nd = int(fetch.get("round", 3))

    def build() -> dict:
        import requests
        r = requests.get(fetch["url"], timeout=30)
        r.raise_for_status()
        rows = []
        for line in r.text.splitlines():
            parts = line.split()
            if len(parts) != 13:
                continue
            try:
                year, vals = int(parts[0]), [float(x) for x in parts[1:]]
            except ValueError:
                continue
            for m, v in enumerate(vals, start=1):
                if v <= missing:
                    continue
                rec = {"year": year, "month": m, "value": round(v, nd)}
                label = _classify_bands(v, fetch.get("bands"))
                if label:
                    rec["classification"] = label
                rows.append(rec)
        if not rows:
            raise climate_indices.IndexUnavailable(
                f"{fetch['url']} returned no parseable rows")
        return {"rows": rows}

    try:
        res = climate_indices.cached(_cache_key(spec), build)
    except climate_indices.IndexUnavailable as exc:
        raise FeedDecline(f"feed unavailable: {exc}") from exc
    except OSError as exc:
        raise FeedDecline(f"feed unavailable: {type(exc).__name__}: {exc}") from exc
    rows = res["rows"]
    latest = rows[-1]
    as_of = f"{latest['year']}-{latest['month']:02d}"
    name, units = fetch["index_name"], fetch["units"]
    summary = (f"{name} {as_of}: {latest['value']} {units}"
               + (f" — {latest['classification']}" if latest.get("classification") else ""))
    return {"as_of": as_of, "count": len(rows[-limit:]), "summary": summary,
            "records": rows[-limit:],
            "query_receipt": f"{name} via {fetch['url']}", "url": fetch["url"],
            "stale_data": {"cadence": spec.get("cadence"),
                           "retrieved_at": res.get("retrieved_at"),
                           "served_from_cache": res.get("cached"),
                           "served_stale": res.get("served_stale"),
                           "reason": res.get("stale_reason")},
            "note": spec.get("usage_notes") or spec.get("note")}


def _dig(obj, path: str):
    """Dot-path into nested dicts/lists ('data.rows' / 'a.0.b')."""
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


_TOTAL_KEYS = ("count", "total", "totalCount", "total_count", "totalResults",
               "numFound", "numberMatched", "totalRecords")


def _upstream_total(doc) -> int | None:
    """How many records the upstream says it matched, when the response says."""
    if not isinstance(doc, dict):
        return None
    for k in _TOTAL_KEYS:
        v = doc.get(k)
        if isinstance(v, dict):                   # {"total": {"value": n}} (Elastic)
            v = v.get("value")
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return v
    return None


def _adapt_generic_json(params: dict, spec: dict) -> dict:
    """Declarative adapter for JSON APIs: records_path finds the list, fields maps
    record keys ({out_name: in_path}). The URL is never rewritten — it is a
    provenance statement and stays exactly what the spec declared — so `params`
    filter the records it answered: `limit`, an equality filter per output field,
    and `bbox` when the feed maps latitude/longitude fields. Anything else is
    declined with the accepted list: a species filter silently ignored answered
    all 300 records of a GBIF page, status ok."""
    from . import climate_indices
    fetch = spec["fetch"]
    params = dict(params or {})
    limit = 12
    if params.get("limit") is not None:
        try:
            limit = max(1, int(params["limit"]))
        except (TypeError, ValueError) as exc:
            raise FeedDecline("'limit' must be a whole number") from exc
    fields = list((fetch.get("fields") or {}))
    lat_f, lon_f = _coord_columns(fields)
    filters, box = _filters(params, {_fold(f): f for f in fields}, lat_f, lon_f, fields,
                            "this feed", "output field")

    def build() -> dict:
        import requests
        r = requests.get(fetch["url"], timeout=30,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        doc = r.json()
        raw = _dig(doc, fetch["records_path"])
        if not isinstance(raw, list) or not raw:
            raise climate_indices.IndexUnavailable(
                f"records_path {fetch['records_path']!r} found no list at {fetch['url']}")
        rows = []
        for rec in raw:
            out = {}
            for name, path in (fetch.get("fields") or {}).items():
                out[name] = _dig(rec, path)
            rows.append(out)
        return {"rows": rows, "total": _upstream_total(doc)}

    try:
        res = climate_indices.cached(_cache_key(spec), build)
    except climate_indices.IndexUnavailable as exc:
        raise FeedDecline(f"feed unavailable: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise FeedDecline(f"feed unavailable: {type(exc).__name__}: {exc}") from exc
    rows = list(res["rows"])
    total = res.get("total") if isinstance(res.get("total"), int) and res["total"] > len(rows) else None
    read = len(rows)
    asked = _asked(filters, box)
    if asked:
        rows = [r for r in rows if _keeps(r, filters, box, lat_f, lon_f)]
    # WHICH END IS RECENT? The tail is only the newest if the upstream happens to
    # publish oldest-first. USGS publishes newest-first, so "the latest M4.5+
    # earthquakes" were the OLDEST twelve of the past month — month-old events
    # served as current values, to a risk platform. Sort by the timestamp when the
    # feed names one, and the question stops depending on a publisher's house
    # style. Without one, say which end was taken instead of implying recency.
    # Sorted by the PARSED time, not the raw value: OBIS mixes offsets, 'Z', bare
    # dates and missing dates, and one undated record made the old all-or-nothing sort
    # give up — "latest 2021-08-22" over records reaching 2026-07-08. An unreadable
    # date keeps its record, at the old end, and never sets as_of.
    af = fetch.get("as_of_field")
    order = ""
    if af:
        when = {id(r): _when(r.get(af)) for r in rows}
        undated = [r for r in rows if when[id(r)] is None]
        rows = undated + sorted((r for r in rows if when[id(r)] is not None),
                                key=lambda r: when[id(r)])
        if len(undated) < len(rows):
            order = (f"; ordered by {af}, oldest first"
                     + ((f" — these {limit:,} are the most recent" if limit <= len(rows) - len(undated)
                         else f" — the last {limit:,} in that order, {limit - (len(rows) - len(undated)):,} of them undated")
                        if limit < len(rows) else "")
                     + (f"; {len(undated):,} record(s) with no readable {af} are kept "
                        "before the dated ones and never count as latest" if undated else ""))
    tail = rows[-limit:]
    as_of = (tail[-1].get(af) if af and tail and when[id(tail[-1])] is not None else None)
    return {"as_of": as_of, "count": len(tail), "records": tail,
            "record_order": (f"sorted newest-last by {af}; undated records first"
                             if as_of is not None else
                             "as published by the upstream, unsorted — "
                             + (f"no record carries a readable {af}" if af else
                                "this feed declares no timestamp field")
                             + ", so these are the last records in publication order, "
                               "which is NOT necessarily the most recent"),
            # A paged API answers one page of what it matched: "300 records from GBIF"
            # read as the whole, while GBIF had matched 1,984. Say first N of M — and
            # a filter searches that page, never what the upstream did not send.
            "summary": ((f"{len(rows):,} of the {read:,} records read match {asked}, from "
                         f"{spec.get('title')}"
                         + (f" (the upstream matched {total:,}; the feed reads one page and "
                            "filters only what it read)" if total else "")) if asked else
                        f"first {read:,} of {total:,} records the upstream matched, from "
                        f"{spec.get('title')} (the feed reads one page)" if total else
                        f"{read} records from {spec.get('title')}")
                       + (f", latest {af} {as_of}{order}" if as_of is not None else
                          "" if not tail else
                          f" (no record carries a readable {af}, so recency is unverified)"
                          if af else
                          " (no timestamp field declared, so recency is unverified)"),
            **({"upstream_total": total} if total else {}),
            "query_receipt": f"{fetch['records_path']} via {fetch['url']}"
                             + (f" where {asked}" if asked else ""),
            "url": fetch["url"],
            "stale_data": {"cadence": spec.get("cadence"),
                           "retrieved_at": res.get("retrieved_at"),
                           "served_from_cache": res.get("cached"),
                           "served_stale": res.get("served_stale"),
                           "reason": res.get("stale_reason")},
            "note": ((spec.get("usage_notes") or spec.get("note")) if rows or not asked else
                     f"no record matches {asked} (the feed read {read:,} records)")}


def _when(v):
    """A record's timestamp as an aware datetime (naive read as UTC), or None when
    unreadable: ISO date/datetime, YYYY-MM, YYYY, a 'start/end' range by its end,
    epoch seconds or milliseconds."""
    import re
    from datetime import datetime, timezone
    if v is None or isinstance(v, bool):
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if abs(v) > 1e11 else v, timezone.utc)
        s = str(v).strip()
        if s.count("/") == 1:
            s = s.split("/")[1]
        if re.fullmatch(r"\d{4}(-\d{2})?", s):
            s += "-01" * (2 - s.count("-"))
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _fold(v) -> str:
    """Case- and accent-insensitive form of a cell or a filter value."""
    import unicodedata
    t = unicodedata.normalize("NFKD", str(v if v is not None else ""))
    return " ".join("".join(ch for ch in t if not unicodedata.combining(ch)).casefold().split())


def _same(cell, wanted) -> bool:
    """A filter value matches a cell: as numbers when both are, else as folded text;
    a list matches any of its values."""
    if isinstance(wanted, (list, tuple, set)):
        return any(_same(cell, w) for w in wanted)
    try:
        return float(cell) == float(wanted)
    except (TypeError, ValueError):
        return _fold(cell) == _fold(wanted)


def _coord_columns(names) -> tuple[str | None, str | None]:
    """(lat, lon) column among `names`, by name: lat/latitude, lon/lng/long/longitude,
    with or without a suffix (Latitude_decimal_degrees) or prefix (decimalLatitude)."""
    import re
    lat = lon = None
    for n in names:
        f = str(n).casefold()
        if lat is None and re.fullmatch(r"(?:\w*latitude|lat)(?:[_ ].*)?", f):
            lat = n
        elif lon is None and re.fullmatch(r"(?:\w*longitude|lon|lng|long)(?:[_ ].*)?", f):
            lon = n
    return lat, lon


def _filters(params: dict, by_key: dict, lat, lon, names, noun: str, unit: str):
    """([(asked name, column, wanted)], bbox) for a query's `params` — one rule for
    every source that is filtered by its own columns, a landed table or a JSON feed.
    `by_key` maps a folded name to the column it filters. A name it does not hold,
    or a bbox with no latitude/longitude to test, is declined with what is accepted."""
    wanted = {k: v for k, v in params.items() if k not in ("limit", "bbox")}
    unknown = sorted(k for k in wanted if _fold(k) not in by_key)
    if unknown or (params.get("bbox") is not None and not (lat and lon)):
        accepted = ["limit"] + (["bbox"] if lat and lon else [])
        article = "an" if unit[:1] in "aeiou" else "a"
        raise FeedDecline(
            (f"{noun} does not take {unknown}" if unknown else
             f"{noun} has no latitude/longitude {unit}s, so it cannot be filtered by bbox")
            + f" — accepted: {', '.join(accepted)}, or an equality filter on {article} {unit}: "
            + ", ".join(dict.fromkeys(str(n) for n in names)))
    box = None
    if params.get("bbox") is not None:
        from ..contrib import tables as _tables
        box = _tables.bbox_of(params["bbox"])
        if box is None:
            raise FeedDecline("'bbox' must be [minLon, minLat, maxLon, maxLat], e.g. "
                              f"[-85.4, 9.6, -84.6, 10.4] — got {params['bbox']!r}")
    return [(k, by_key[_fold(k)], v) for k, v in wanted.items()], box


def _keeps(rec: dict, filters, box, lat, lon) -> bool:
    for _, col, v in filters:
        if not _same(rec.get(col), v):
            return False
    if box:
        try:
            y, x = float(rec.get(lat)), float(rec.get(lon))
        except (TypeError, ValueError):
            return False
        return box[0] <= x <= box[2] and box[1] <= y <= box[3]
    return True


def _asked(filters, box) -> str:
    """The filters as the caller wrote them, for a summary and a receipt."""
    return ", ".join([f"{k} = {v!r}" for k, _, v in filters]
                     + ([f"inside bbox {box}"] if box else []))


def params_doc(spec: dict) -> dict:
    """What feeds_query takes on a declarative row, derived from the row itself — a
    feed's filters are its own output fields, so the text can never drift from them."""
    adapter = spec.get("adapter")
    if adapter == "generic_csv":
        from ..contrib import tables as _tables
        return _tables.params_doc()
    if adapter == "generic_json":
        fields = list(((spec.get("fetch") or {}).get("fields") or {}))
        lat, lon = _coord_columns(fields)
        return {"limit": "records to return, from the end (default 12)",
                "<field>": ("equality filter on an output field (" + ", ".join(fields)
                            + "), matched case- and accent-insensitively; a list matches any "
                              "of its values. It filters the records the feed read; the "
                              "upstream query itself never changes"),
                **({"bbox": f"[minLon, minLat, maxLon, maxLat] — records whose {lat}/{lon} "
                            "fall inside"} if lat and lon else {})}
    if adapter == "generic_table":
        return {"limit": "months of series to return (default 12)"}
    return dict(spec.get("params") or {})


def _adapt_generic_csv(params: dict, spec: dict) -> dict:
    """Declarative adapter for LANDED tables (X2c). Serves the platform-archived
    copy and refuses if the bytes changed since landing — a table silently edited
    after citation is the provenance failure this platform exists to prevent.

    `params`: `limit`, an equality filter per column (output field or CSV header,
    matched case- and accent-insensitively), and `bbox` when the table has
    latitude/longitude columns. Anything else is declined with the accepted list:
    a filter silently ignored returned five United States rows to a question
    about Costa Rica, status ok."""
    import csv as _csv
    import hashlib as _hashlib
    import io as _io

    from ..contrib import tables as _tables

    fetch = spec["fetch"]
    params = dict(params or {})
    limit = 12
    if params.get("limit") is not None:
        try:
            limit = max(1, int(params["limit"]))
        except (TypeError, ValueError) as exc:
            raise FeedDecline("'limit' must be a whole number") from exc
    path = _landed_table(fetch["path"])
    if not path.is_file():
        raise FeedDecline(
            f"landed table missing at {path} — the feed spec recorded "
            f"{fetch['path']!r}, which is a path on the machine that staged the "
            "contribution, not on this host. Re-land the table here.")
    raw = path.read_bytes()
    digest = _hashlib.sha256(raw).hexdigest()
    if fetch.get("sha256") and digest != fetch["sha256"]:
        raise FeedDecline(
            "the landed table's bytes no longer match the sha recorded at "
            "contribution time — the file was modified out-of-band; re-land it "
            "so provenance stays true",
            expected_sha256=fetch["sha256"], observed_sha256=digest)
    enc = fetch.get("encoding")
    text = raw.decode(enc) if enc else _tables.decode(raw)[0]
    reader = _csv.DictReader(_io.StringIO(text.lstrip("﻿")))
    header = list(reader.fieldnames or [])
    cols = fetch.get("columns") or {}

    # Filter keys: the table's output fields first, then its raw CSV headers.
    by_key = {}
    for name, col in cols.items():
        by_key.setdefault(_fold(name), col)
    for col in header:
        by_key.setdefault(_fold(col), col)
    lat_col, lon_col = _coord_columns([*cols.values(), *header])
    filters, box = _filters(params, by_key, lat_col, lon_col, [*cols, *header],
                            "this table", "column")

    rows, total = [], 0
    for rec in reader:
        total += 1
        if not _keeps(rec, filters, box, lat_col, lon_col):
            continue
        out = {name: rec.get(col) for name, col in cols.items()}
        for k, v in out.items():
            if isinstance(v, str):
                try:
                    out[k] = float(v) if "." in v else int(v)
                except ValueError:
                    pass
        rows.append(out)
    if not total:
        raise FeedDecline("the landed table has a header but no rows")
    asked = _asked(filters, box)
    as_of = fetch.get("as_of_field") and rows and rows[-1].get(fetch["as_of_field"])
    tail = rows[-limit:]
    return {"as_of": as_of or spec.get("vintage"),
            "count": len(tail), "records": tail,
            "summary": ((f"{len(rows):,} of {total:,} rows match {asked}" if asked else
                         f"{total:,} rows")
                        + f" in {spec.get('title')} ({fetch.get('units')})"
                        + (f"; the last {len(tail)} are returned" if len(tail) < len(rows) else "")
                        + f", landed copy sha {digest[:12]}"),
            "query_receipt": f"columns {cols} from platform-archived {path.name} "
                             f"(sha256 {digest[:12]})" + (f" where {asked}" if asked else ""),
            "url": None,
            # The landed copy IS the source — there is no live upstream to be
            # stale against. Without the explicit flag, is_stale reads bare
            # presence as the fallback signal and cries wolf (caught by a cold
            # model in UAT repeating the false cache warning).
            "stale_data": {"cadence": spec.get("cadence"), "served_stale": False},
            "note": ((spec.get("usage_notes") or spec.get("note")) if rows else
                     f"no row matches {asked} (the table holds {total:,} rows)")}


ADAPTERS = {"cropmonitor_conditions": _adapt_cropmonitor,
            "climate_index": _adapt_climate_index,
            "enso_forecast": _adapt_enso_forecast,
            "generic_table": _adapt_generic_table,
            "generic_json": _adapt_generic_json,
            "generic_csv": _adapt_generic_csv}
# Adapters that filter by the source's own columns decline an unknown name
# themselves, naming those columns; every other one takes what its row declares.
_FILTERS_ITS_OWN = ("generic_csv", "generic_json")


def _undeclared_params(params: dict, spec: dict) -> str | None:
    """Why `params` holds a name this feed does not take, or None. A parameter an
    adapter never reads used to be dropped without a word, so the answer looked
    filtered and was not. `limit` passes everywhere: every pack hands it to every
    source it reads, and an index series takes it without declaring it."""
    if spec.get("adapter") in _FILTERS_ITS_OWN:
        return None
    declared = list(params_doc(spec) if spec.get("adapter") == "generic_table"
                    else spec.get("params") or {})
    unknown = sorted(str(k) for k in (params or {}) if k != "limit" and k not in declared)
    if not unknown:
        return None
    return (f"this feed does not accept those params: {', '.join(unknown)} — accepted: "
            + (", ".join(declared) or "none (call it with no params)"))


def describe() -> str:
    """The tool description, GENERATED from the registry — so it can never drift
    from what is actually registered, and no feed name is hardcoded in prose."""
    avail, pending = [], []
    for name, spec in sorted(registry.FEEDS.items()):
        params = ", ".join(f"{k}: {v}" for k, v in (spec.get("params") or params_doc(spec)).items())
        if spec.get("status") == "available":
            avail.append(f"  - `{name}` ({spec.get('source')}) — {spec.get('description','')}"
                         f" params: {{{params}}}")
        else:
            pending.append(f"  - `{name}` — DECLARED GAP: {spec.get('reason')}")
    return (
        "CURRENT CLIMATE AND CROP DATA, live from the source — use this INSTEAD OF A "
        "WEB SEARCH for these numbers. Every value arrives with its source, its date "
        "and how it was validated. Covers: "
        + "; ".join(s.get("title") or n for n, s in sorted(registry.FEEDS.items())
                    if s.get("status") == "available") + ".\n\n"
        "`dataset` names the feed and `params` carries its arguments — both come from "
        "the registry, so new feeds appear here without a tool change.\n\n"
        "Available now:\n" + ("\n".join(avail) or "  (none)") +
        "\n\nRegistered but not yet available (asking returns why):\n" +
        ("\n".join(pending) or "  (none)") +
        "\n\nReturns: {status, dataset, as_of, count, summary, records, passport}. The "
        "passport carries source, validation, residency, the literal upstream query, "
        "and `stale_data` with a note when served from last-good cache (never present "
        "stale data as live). status \"empty\" -> the feed answered but has no rows; "
        "\"declined\" -> `note` plus the upstream's own alternatives.")


def query(dataset: str, params: dict | None = None) -> dict:
    """Dispatch to the feed's adapter. Unknown / pending / unimplemented feeds all
    decline with a reason rather than a shrug."""
    params = params or {}
    spec = registry.FEEDS.get(dataset)
    if spec is None:
        # A staged contribution serves ONLY to its owner and reviewers; to anyone
        # else it is simply unknown (contrib/staging.py, the visibility rule).
        from ..contrib import staging
        spec = staging.visible_staged_feed(dataset)
    if spec is None:
        return {"status": "declined", "note": f"unknown dataset {dataset!r}",
                "available": sorted(k for k, v in registry.FEEDS.items()
                                    if v.get("status") == "available")}
    if spec.get("status") != "available":
        return {"status": "declined", "dataset": dataset,
                "note": f"{dataset}: {spec.get('reason', 'not available yet')}",
                "available": sorted(k for k, v in registry.FEEDS.items()
                                    if v.get("status") == "available")}
    adapter = ADAPTERS.get(spec.get("adapter"))
    if adapter is None:
        return {"status": "declined", "dataset": dataset,
                "note": f"{dataset} is registered but has no adapter (not implemented)"}
    undeclared = _undeclared_params(params, spec)
    if undeclared:
        return {"status": "declined", "dataset": dataset, "note": undeclared}
    if spec.get("contributed") or spec.get("staged_by"):
        # A contributed upstream is re-checked on every read: the address the
        # contributor named must still be a public one (contrib/fetch_policy.py).
        from ..contrib import fetch_policy
        url = (spec.get("fetch") or {}).get("url")
        problems = fetch_policy.check_url(url) if url else []
        if problems:
            return {"status": "declined", "dataset": dataset,
                    "note": f"{dataset}: upstream refused by the fetch policy — " + "; ".join(problems)}
    try:
        res = adapter(params, spec)   # adapters get their own registry row
    except FeedDecline as exc:
        return {"status": "declined", "dataset": dataset, "note": exc.note, **exc.extra}

    passport = {"source": spec.get("source"), "validation": spec.get("validation"),
                "residency": spec.get("residency", "external call-out"),
                "as_of": res.get("as_of"), "url": res.get("url"),
                # Two feeds can report the SAME season with different numbers because
                # they rest on different SST datasets (ours ERSSTv5, IRI's OISST — the
                # gap widens as an event intensifies). Unstated, that reads as an error.
                "sst_basis": spec.get("sst_basis"),
                # The licence the contributor declared rides every answer built on it:
                # a CC-BY-NC record set is not cited like a public-domain one.
                "license": spec.get("license"),
                "query": res.get("query_receipt"), "stale_data": res.get("stale_data")}
    if spec.get("staged_by"):
        passport["staged"] = {"contribution_id": spec.get("contribution_id"),
                              "note": ("STAGED — a contribution awaiting review; served only "
                                       "to its contributor and to reviewers until approved")}
    out = {"dataset": dataset, "as_of": res.get("as_of"), "count": res.get("count"),
           "summary": res.get("summary"), "records": res.get("records"),
           "passport": passport}
    if not res.get("records"):
        return {**out, "status": "empty",
                "note": res.get("note") or "the feed returned no rows for these params"}
    # Never present stale data as live — but never cry wolf either. The climate
    # adapters always attach a `stale_data` dict (cadence, retrieved_at, ...), so
    # testing the dict's mere PRESENCE flagged every healthy feed as cache-served
    # and put a false staleness warning into the evidence pack. Test the flag.
    # The adapter's note (contributor usage_notes ride here) belongs on the OK
    # path too — guidance that only surfaces on failure paths guides nothing.
    if res.get("note"):
        out["note"] = res["note"]
    stale = res.get("stale_data") or {}
    if is_stale(stale):
        why = (stale.get("reason")
               or f"last good fetch {stale.get('last_good_fetch')}")
        warn = ("SERVED FROM LAST-GOOD CACHE — the live feed was unavailable "
                f"({why}); treat as possibly out of date")
        out["note"] = f"{warn}. {out['note']}" if out.get("note") else warn
    return {**out, "status": "ok"}
