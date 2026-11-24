"""Phase-1 climate-driver indices — ONI (ENSO) and DMI (Indian Ocean Dipole).

Both upstreams publish a fixed-width ASCII table over plain HTTP, so one parser
shape serves both and `feeds` needs a single adapter. Values are cached briefly:
these are MONTHLY series, so re-fetching per question buys nothing and makes the
platform's latency someone else's uptime.

Deliberately NOT interpreted here. This module reports the index, its date, and
its classification against the published thresholds — it says nothing about crops.
The use case is explicit that Phase 1 describes the signal and must not infer
local agricultural impact until later pillars land.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import requests

_TIMEOUT = (10, 30)
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 6 * 3600          # monthly data; six hours is already over-fresh
_MISSING = -9990.0       # PSL writes -9999.000 for "not yet published"

# The 3-month overlapping seasons ONI is reported in, in order.
_SEASONS = ["DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ"]


class IndexUnavailable(Exception):
    """Upstream unreachable or unparseable — the caller declines with this text."""


def _get(url: str) -> str:
    try:
        r = requests.get(url, timeout=_TIMEOUT, headers={"User-Agent": "SERVIR-GRP/0.1"})
        r.raise_for_status()
    except requests.RequestException as exc:
        raise IndexUnavailable(f"{url} unreachable: {exc}") from exc
    return r.text


def _classify_oni(v: float) -> str:
    """NOAA CPC's published bands. Naming the band is reporting, not forecasting."""
    if v >= 2.0:
        return "very strong El Nino"
    if v >= 1.5:
        return "strong El Nino"
    if v >= 1.0:
        return "moderate El Nino"
    if v >= 0.5:
        return "weak El Nino"
    if v <= -2.0:
        return "very strong La Nina"
    if v <= -1.5:
        return "strong La Nina"
    if v <= -1.0:
        return "moderate La Nina"
    if v <= -0.5:
        return "weak La Nina"
    return "neutral"


def _classify_dmi(v: float) -> str:
    """+/-0.4 C is the conventional IOD event threshold."""
    if v >= 0.4:
        return "positive IOD"
    if v <= -0.4:
        return "negative IOD"
    return "neutral"


def oni(limit: int = 12) -> dict:
    """NOAA CPC Oceanic Nino Index — 3-month running Nino-3.4 SST anomaly."""
    url = "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt"
    rows = []
    for line in _get(url).splitlines()[1:]:            # first line is the header
        parts = line.split()
        if len(parts) != 4:
            continue
        season, year, _total, anom = parts
        try:
            v = float(anom)
        except ValueError:
            continue
        rows.append({"season": season, "year": int(year), "value": round(v, 2),
                     "classification": _classify_oni(v)})
    if not rows:
        raise IndexUnavailable(f"{url} returned no parseable rows")
    return {"index": "ONI", "long_name": "Oceanic Nino Index (Nino 3.4 SST anomaly)",
            "units": "degrees C anomaly", "source": "NOAA CPC", "url": url,
            "latest": rows[-1], "series": rows[-limit:], "full_series_from": rows[0]["year"]}


def dmi(limit: int = 12) -> dict:
    """NOAA PSL Dipole Mode Index — the Indian Ocean Dipole co-driver."""
    url = "https://psl.noaa.gov/gcos_wgsp/Timeseries/Data/dmi.had.long.data"
    rows = []
    for line in _get(url).splitlines():
        parts = line.split()
        if len(parts) != 13:                           # year + 12 months, else prose
            continue
        try:
            year, vals = int(parts[0]), [float(p) for p in parts[1:]]
        except ValueError:
            continue
        for m, v in enumerate(vals, start=1):
            if v <= _MISSING:                          # not yet published — skip, never report
                continue
            rows.append({"year": year, "month": m, "value": round(v, 3),
                         "classification": _classify_dmi(v)})
    if not rows:
        raise IndexUnavailable(f"{url} returned no parseable rows")
    return {"index": "DMI", "long_name": "Dipole Mode Index (Indian Ocean Dipole)",
            "units": "degrees C anomaly", "source": "NOAA PSL (HadISST)", "url": url,
            "latest": rows[-1], "series": rows[-limit:], "full_series_from": rows[0]["year"]}


def enso_events(min_seasons: int = 5) -> dict:
    """Historical ENSO event catalogue, DERIVED from the ONI table.

    CPC's own definition: an event is >= 5 consecutive overlapping 3-month seasons
    at or beyond +/-0.5. Derived rather than fetched because no separate machine-
    readable catalogue exists — so the provenance is the ONI series itself, and it
    is labelled as derived rather than presented as a published product.
    """
    series = oni(limit=10**6)["series"]
    events, run = [], []

    def flush() -> None:
        if len(run) >= min_seasons:
            peak = max(run, key=lambda r: abs(r["value"]))
            events.append({
                "phase": "El Nino" if peak["value"] > 0 else "La Nina",
                "start": f"{run[0]['season']} {run[0]['year']}",
                "end": f"{run[-1]['season']} {run[-1]['year']}",
                "seasons": len(run), "peak_oni": peak["value"],
                "peak_season": f"{peak['season']} {peak['year']}",
                "strength": _classify_oni(peak["value"]),
            })

    sign = 0
    for row in series:
        s = 1 if row["value"] >= 0.5 else -1 if row["value"] <= -0.5 else 0
        if s == 0 or s != sign:
            flush()
            run, sign = ([row], s) if s else ([], 0)
        else:
            run.append(row)
    flush()
    # Dated by the ONI series it was derived THROUGH, not by the last event's end.
    # A run in progress that has not yet reached `min_seasons` is correctly absent,
    # so dating the catalogue by its newest event would make a current derivation
    # look years stale and invite a reader to distrust it.
    through = f"{series[-1]['season']} {series[-1]['year']}" if series else None
    return {"index": "ENSO event catalogue", "derived_from": "NOAA CPC ONI",
            "definition": f">= {min_seasons} consecutive overlapping seasons at or beyond +/-0.5 C",
            "url": "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt",
            "derived_through": through,
            "in_progress_note": (
                f"the ONI series runs to {through}; any run since the last listed event "
                f"has not yet reached {min_seasons} seasons and so is not yet an event"),
            "count": len(events), "events": events}


_BUILDERS = {"oni": oni, "dmi": dmi, "enso_events": enso_events}


def cached(key: str, builder, ttl: int = _TTL) -> dict:
    """Short cache in front of one monthly upstream. Shared by every monthly product
    (indices here, IRI forecasts in `enso_forecast`) so the staleness contract is
    written once.

    On upstream failure, serve the last good value FLAGGED rather than declining:
    these series change monthly, so a value from this morning is still the current
    month's number. It is never presented as fresh — `served_stale` and the age say
    otherwise — which is the difference between honest staleness and a silent lie.
    """
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return {**hit[1], "cached": True, "served_stale": False}
    try:
        out = builder()
    except IndexUnavailable:
        if hit is None:
            raise                                    # nothing good to fall back to
        age = int(time.time() - hit[0])
        return {**hit[1], "cached": True, "served_stale": True,
                "stale_reason": f"upstream unreachable; last good value is {age // 60} min old"}
    out["retrieved_at"] = datetime.now(timezone.utc).isoformat()
    _CACHE[key] = (time.time(), out)
    return {**out, "cached": False, "served_stale": False}


def fetch(kind: str, **kwargs) -> dict:
    """Cached read of one index. Cache is per-kind and short — the data is monthly."""
    return cached(f"{kind}:{sorted(kwargs.items())}", lambda: _BUILDERS[kind](**kwargs))
