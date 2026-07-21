"""The fetch bone's live-feed half: query a registered feed by name.

`dataset` is a PARAMETER, not a tool — so CHIRPS/CHIRTS/ERA5 land as registry
entries when the hub data lists arrive, adding zero tools. Pending feeds are
declared honestly (asking for one says why it isn't there yet, not "unknown").

Preserves the upstream's rich errors (nearest available month, available crops)
instead of flattening them to a shrug, and carries staleness through.
"""

from __future__ import annotations

from ..food_security import cropmonitor
from . import registry


def _passport(spec: dict, res: dict) -> dict:
    return {"source": spec.get("source"), "validation": spec.get("validation"),
            "as_of": res.get("as_of"), "residency": "external call-out",
            "url": (res.get("query") or {}).get("url"),
            # the literal upstream filter — provenance for the fetch itself
            "query": (res.get("query") or {}).get("where"),
            "stale_data": res.get("stale_data")}


def query(dataset: str = "geoglam_conditions", crop: str | None = None,
          place: str | None = None, month: str | None = None) -> dict:
    """Query a registered live feed. status "empty" = the feed answered but has no
    rows for this target; "declined" = bad/pending dataset or an upstream refusal,
    with the upstream's own alternatives preserved in `available`."""
    spec = registry.FEEDS.get(dataset)
    if spec is None:
        return {"status": "declined", "note": f"unknown dataset {dataset!r}",
                "available": sorted(registry.FEEDS)}
    if spec.get("status") != "available":
        return {"status": "declined", "dataset": dataset,
                "note": f"{dataset}: {spec.get('reason', 'not available yet')}",
                "available": sorted(k for k, v in registry.FEEDS.items()
                                    if v.get("status") == "available")}
    try:
        res = cropmonitor.conditions(month=month, crop=crop, place=place)
    except ValueError as exc:                       # malformed month, not a coverage gap
        return {"status": "declined", "dataset": dataset, "note": str(exc)}
    except cropmonitor.MonthNotAvailable as exc:
        return {"status": "declined", "dataset": dataset, "note": str(exc),
                "nearest_available": exc.nearest,
                "available": {"from": exc.available[0], "to": exc.available[-1],
                              "note": "not every month in this span is published"}
                if exc.available else None}
    except cropmonitor.CropNotFound as exc:
        return {"status": "declined", "dataset": dataset, "note": str(exc),
                "available": exc.available}
    except cropmonitor.CropMonitorError as exc:     # unreachable / upstream failure
        return {"status": "declined", "dataset": dataset,
                "note": f"feed unavailable: {exc}"}

    out = {"dataset": dataset, "as_of": res.get("as_of"), "count": res.get("count"),
           "summary": res.get("summary"), "records": res.get("records"),
           "passport": _passport(spec, res)}
    if not res.get("records"):
        return {**out, "status": "empty",
                "note": res.get("note", "the feed returned no assessment rows for "
                                        "this crop/place/month")}
    if res.get("stale_data"):                       # never present stale data as live
        out["note"] = ("SERVED FROM LAST-GOOD CACHE — the live feed was unavailable "
                       f"(last good fetch {res['stale_data'].get('last_good_fetch')}); "
                       "treat as possibly out of date")
    return {**out, "status": "ok"}
