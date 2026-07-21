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
from . import registry


class FeedDecline(Exception):
    """An upstream refusal worth passing through in full (alternatives included)."""

    def __init__(self, note: str, **extra):
        super().__init__(note)
        self.note, self.extra = note, extra


def _adapt_cropmonitor(params: dict) -> dict:
    """GEOGLAM Crop Monitor -> the normalised feed shape."""
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


# adapter name -> implementation. A new upstream adds ONE entry here; a feed that
# reuses an existing shape needs none.
ADAPTERS = {"cropmonitor_conditions": _adapt_cropmonitor}


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
        res = adapter(params)
    except FeedDecline as exc:
        return {"status": "declined", "dataset": dataset, "note": exc.note, **exc.extra}

    passport = {"source": spec.get("source"), "validation": spec.get("validation"),
                "residency": spec.get("residency", "external call-out"),
                "as_of": res.get("as_of"), "url": res.get("url"),
                "query": res.get("query_receipt"), "stale_data": res.get("stale_data")}
    out = {"dataset": dataset, "as_of": res.get("as_of"), "count": res.get("count"),
           "summary": res.get("summary"), "records": res.get("records"),
           "passport": passport}
    if not res.get("records"):
        return {**out, "status": "empty",
                "note": res.get("note") or "the feed returned no rows for these params"}
    if res.get("stale_data"):                       # never present stale data as live
        out["note"] = ("SERVED FROM LAST-GOOD CACHE — the live feed was unavailable "
                       f"(last good fetch {res['stale_data'].get('last_good_fetch')}); "
                       "treat as possibly out of date")
    return {**out, "status": "ok"}
