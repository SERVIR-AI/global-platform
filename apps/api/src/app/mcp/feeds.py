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
ADAPTERS = {"cropmonitor_conditions": _adapt_cropmonitor,
            "climate_index": _adapt_climate_index,
            "enso_forecast": _adapt_enso_forecast}


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
        "Query a registered LIVE FEED. `dataset` names the feed and `params` carries "
        "its arguments — both come from the registry, so new feeds appear here "
        "without a tool change.\n\nAvailable now:\n" + ("\n".join(avail) or "  (none)") +
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
