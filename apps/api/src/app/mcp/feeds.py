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
        res = climate_indices.cached(f"declarative:{spec['declarative']}", build)
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
            "note": spec.get("note")}


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


def _adapt_generic_json(params: dict, spec: dict) -> dict:
    """Declarative adapter for JSON APIs: records_path finds the list, fields maps
    record keys ({out_name: in_path}). No params passthrough in v1 — a URL is a
    provenance statement and stays exactly what the spec declared."""
    from . import climate_indices
    fetch = spec["fetch"]
    limit = 12
    if params.get("limit") is not None:
        try:
            limit = max(1, int(params["limit"]))
        except (TypeError, ValueError) as exc:
            raise FeedDecline("'limit' must be a whole number") from exc

    def build() -> dict:
        import requests
        r = requests.get(fetch["url"], timeout=30,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        raw = _dig(r.json(), fetch["records_path"])
        if not isinstance(raw, list) or not raw:
            raise climate_indices.IndexUnavailable(
                f"records_path {fetch['records_path']!r} found no list at {fetch['url']}")
        rows = []
        for rec in raw:
            out = {}
            for name, path in (fetch.get("fields") or {}).items():
                out[name] = _dig(rec, path)
            rows.append(out)
        return {"rows": rows}

    try:
        res = climate_indices.cached(f"declarative:{spec['declarative']}", build)
    except climate_indices.IndexUnavailable as exc:
        raise FeedDecline(f"feed unavailable: {exc}") from exc
    except (OSError, ValueError) as exc:
        raise FeedDecline(f"feed unavailable: {type(exc).__name__}: {exc}") from exc
    rows = res["rows"]
    as_of = fetch.get("as_of_field") and rows[-1].get(fetch["as_of_field"])
    return {"as_of": as_of, "count": len(rows[-limit:]), "records": rows[-limit:],
            "summary": f"{len(rows)} records from {spec.get('title')}"
                       + (f", latest {as_of}" if as_of else ""),
            "query_receipt": f"{fetch['records_path']} via {fetch['url']}",
            "url": fetch["url"],
            "stale_data": {"cadence": spec.get("cadence"),
                           "retrieved_at": res.get("retrieved_at"),
                           "served_from_cache": res.get("cached"),
                           "served_stale": res.get("served_stale"),
                           "reason": res.get("stale_reason")},
            "note": spec.get("note")}


def _adapt_generic_csv(params: dict, spec: dict) -> dict:
    """Declarative adapter for LANDED tables (X2c). Serves the platform-archived
    copy and refuses if the bytes changed since landing — a table silently edited
    after citation is the provenance failure this platform exists to prevent."""
    import csv as _csv
    import hashlib as _hashlib
    import io as _io
    from pathlib import Path as _Path

    fetch = spec["fetch"]
    limit = 12
    if params.get("limit") is not None:
        try:
            limit = max(1, int(params["limit"]))
        except (TypeError, ValueError) as exc:
            raise FeedDecline("'limit' must be a whole number") from exc
    path = _Path(fetch["path"])
    if not path.is_file():
        raise FeedDecline(f"landed table missing at {path} — re-land it")
    raw = path.read_bytes()
    digest = _hashlib.sha256(raw).hexdigest()
    if fetch.get("sha256") and digest != fetch["sha256"]:
        raise FeedDecline(
            "the landed table's bytes no longer match the sha recorded at "
            "contribution time — the file was modified out-of-band; re-land it "
            "so provenance stays true",
            expected_sha256=fetch["sha256"], observed_sha256=digest)
    rows = []
    cols = fetch.get("columns") or {}
    for rec in _csv.DictReader(_io.StringIO(raw.decode("utf-8-sig"))):
        out = {name: rec.get(col) for name, col in cols.items()}
        for k, v in out.items():
            if isinstance(v, str):
                try:
                    out[k] = float(v) if "." in v else int(v)
                except ValueError:
                    pass
        rows.append(out)
    if not rows:
        raise FeedDecline("the landed table has a header but no rows")
    as_of = fetch.get("as_of_field") and rows[-1].get(fetch["as_of_field"])
    return {"as_of": as_of or spec.get("vintage"),
            "count": len(rows[-limit:]), "records": rows[-limit:],
            "summary": (f"{len(rows)} rows from {spec.get('title')} "
                        f"({fetch.get('units')}), landed copy sha {digest[:12]}"),
            "query_receipt": f"columns {cols} from platform-archived {path.name} "
                             f"(sha256 {digest[:12]})",
            "url": None,
            "stale_data": {"cadence": spec.get("cadence")},
            "note": spec.get("note")}


ADAPTERS = {"cropmonitor_conditions": _adapt_cropmonitor,
            "climate_index": _adapt_climate_index,
            "enso_forecast": _adapt_enso_forecast,
            "generic_table": _adapt_generic_table,
            "generic_json": _adapt_generic_json,
            "generic_csv": _adapt_generic_csv}


def describe() -> str:
    """The tool description, GENERATED from the registry — so it can never drift
    from what is actually registered, and no feed name is hardcoded in prose."""
    avail, pending = [], []
    for name, spec in sorted(registry.FEEDS.items()):
        params = ", ".join(f"{k}: {v}" for k, v in (spec.get("params") or {}).items())
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
                "query": res.get("query_receipt"), "stale_data": res.get("stale_data")}
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
    stale = res.get("stale_data") or {}
    if is_stale(stale):
        why = (stale.get("reason")
               or f"last good fetch {stale.get('last_good_fetch')}")
        out["note"] = ("SERVED FROM LAST-GOOD CACHE — the live feed was unavailable "
                       f"({why}); treat as possibly out of date")
    return {**out, "status": "ok"}
