"""GEOGLAM Crop Monitor conditions — a thin client for the CMET Global_SHP FeatureServer.

Crop conditions are EXPERT ASSESSMENTS (analysts read satellite indicators plus
ground context and classify each region) — this module reports them verbatim and
never computes, re-derives, or extrapolates a rating. Everything here is
deterministic: resolve the month to a layer, resolve a crop word to the
season-level crop names, query, count.

Service facts this client is built around (all hand-verified against the live
service; see the query provenance echoed on every response):
- One layer per month, named ``Global_Synthesis_YYYYMM``; December is skipped, so
  the month->layer mapping is READ LIVE from the service, never derived.
- Fields: Country, Region, Crop, Conditions, Drivers (+ MultiPolygon geometry).
- Crop names are season-level ("Winter Wheat", "Maize 1"), so a user's "wheat"
  must resolve against the live distinct-crop list, not a name-prefix guess.
- A blank Conditions value — "" or a padded " " in the live data — is the
  service's out-of-season marker.
- ArcGIS reports query errors as HTTP 200 with an {"error": ...} body; those (and
  4xx) are deterministic rejections and are never retried or served-around.
- The host sends a LEAF-ONLY TLS chain (verified live: no intermediate), so plain
  certifi cannot build a trust path (curl works only because the OS does AIA
  chasing). The published InCommon intermediate ships in conf/cropmonitor_ca.pem
  and is appended to the certifi bundle at runtime — a CA rotation still verifies
  through the normal certifi roots. CROPMONITOR_VERIFY_TLS=false is the last-ditch
  fallback, never the default.

Caching (cache_dir/cropmonitor, TTL in settings): reads tolerate corrupt files
(treated as a miss and deleted), writes are atomic (temp file + os.replace), and
paginated queries are cached as ONE assembled result so an outage can never mix
pages from different fetches. When the service is unreachable the last good
response is served — but always FLAGGED: conditions() carries a `stale_data`
block and a coverage decline based on a stale layer list says so, because
presenting stale data as live would break the provenance contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from ..config import get_settings

FIELDS = ("Country", "Region", "Crop", "Conditions", "Drivers")
_LAYER_RE = re.compile(r"^Global_Synthesis_(\d{4})(\d{2})$")
# "2026-4"/"2026-04"/"202604" are unambiguous; a bare 5-digit "20261" is not — reject it.
_MONTH_RE = re.compile(r"^(\d{4})(?:-(\d{1,2})|(\d{2}))$")
_RETRIES = 3
_BACKOFF = 1.0            # seconds; doubles per attempt
_TIMEOUT = (5, 30)        # (connect, read) seconds


class CropMonitorError(Exception):
    """The service rejected the request (bad query, 4xx, ArcGIS error body)."""


class ServiceUnreachable(CropMonitorError):
    """The service could not be reached (network, 5xx, garbage response) —
    the only failure the last-good cache fallback applies to."""


class MonthNotAvailable(CropMonitorError):
    """The asked month has no published layer — carries the honest alternative."""

    def __init__(self, month: str, available: list[str], stale_at: float | None = None):
        self.month, self.available, self.stale_at = month, available, stale_at
        self.nearest = (min(available, key=lambda m: abs(_months(m) - _months(month)))
                        if available else None)
        msg = (f"The GEOGLAM Crop Monitor has not published a synthesis layer for {month}; "
               f"the nearest available month is {self.nearest}.")
        if stale_at:  # a decline from a stale list is a guess unless it says so
            msg += (" Note: the service is currently unreachable, so this is based on a "
                    f"cached layer list from {_iso(stale_at)} and newer months may exist.")
        super().__init__(msg)


class CropNotFound(CropMonitorError):
    """The asked crop matches nothing the service assesses — carries what it does."""

    def __init__(self, crop: str, available: list[str], stale_at: float | None = None):
        self.crop, self.available, self.stale_at = crop, available, stale_at
        msg = (f"The GEOGLAM Crop Monitor does not assess a crop matching {crop!r}; "
               f"it assesses: {', '.join(available)}.")
        if stale_at:  # a decline from a stale list is a guess unless it says so
            msg += (" Note: the service is currently unreachable, so this is based on "
                    f"a cached crop list from {_iso(stale_at)}.")
        super().__init__(msg)


def _months(key: str) -> int:
    y, m = key.split("-")
    return int(y) * 12 + int(m)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


def _verify() -> str | bool:
    """requests' `verify` for the Crop Monitor host. The server omits its
    intermediate certificate, so certifi alone can't build a path; append the
    shipped intermediate (conf/cropmonitor_ca.pem) to the certifi bundle. Rebuilt
    whenever either source file is newer than the merged copy."""
    settings = get_settings()
    if not settings.cropmonitor_verify_tls:
        return False
    extra = Path(settings.cropmonitor_ca_extra)
    if not extra.exists():
        return True                                # plain certifi behavior
    roots = Path(certifi.where())
    merged = Path(settings.cache_dir) / "cropmonitor" / "ca_bundle.pem"
    sources_mtime = max(roots.stat().st_mtime, extra.stat().st_mtime)
    if not merged.exists() or merged.stat().st_mtime < sources_mtime:
        merged.parent.mkdir(parents=True, exist_ok=True)
        merged.write_text(roots.read_text() + "\n" + extra.read_text())
    return str(merged)


def _make_session() -> requests.Session:
    """Transport resilience lives in urllib3's Retry: 5xx and connection errors
    retried with exponential backoff; 4xx passes through untouched. Everything the
    transport can't judge (ArcGIS error bodies, non-JSON 200s) stays in _fetch_json."""
    retry = Retry(total=_RETRIES - 1, backoff_factor=_BACKOFF,
                  status_forcelist=(500, 502, 503, 504))
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s


_SESSION = _make_session()


def _fetch_json(url: str, params: dict) -> dict:
    """One GET through the retrying session, then the checks Retry can't make:
    4xx / ArcGIS error body -> deterministic rejection (CropMonitorError, no
    fallback); non-JSON 200 (e.g. a maintenance page) -> ServiceUnreachable, so
    the last-good cache can answer instead."""
    try:
        r = _SESSION.get(url, params=params, timeout=_TIMEOUT, verify=_verify())
    except requests.RequestException as exc:       # incl. RetryError after exhaustion
        raise ServiceUnreachable(f"Could not reach the Crop Monitor service: {exc}") from exc
    if 400 <= r.status_code < 500:
        raise CropMonitorError(
            f"The Crop Monitor service rejected the request (HTTP {r.status_code}) at {url}")
    if r.status_code >= 500:                       # a 5xx the Retry policy let through
        raise ServiceUnreachable(f"HTTP {r.status_code} from {url}")
    try:
        data = r.json()
    except ValueError as exc:                      # e.g. an HTML maintenance page
        raise ServiceUnreachable(f"non-JSON response from {url}: {exc}") from exc
    if isinstance(data, dict) and "error" in data:
        raise CropMonitorError(f"ArcGIS error from {url}: {data['error']}")
    return data


def _cache_read(path: Path) -> dict | None:
    """The cached {"fetched_at", "data"} blob, or None. A corrupt/truncated file
    (crash mid-write, disk full) is a cache miss, not an error — delete and move on."""
    try:
        blob = json.loads(path.read_text())
        blob["fetched_at"]
        return blob
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        path.unlink(missing_ok=True)
        return None


def _cache_write(path: Path, data) -> None:
    """Atomic write (temp file + os.replace) so a concurrent reader or a crash can
    never observe a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"fetched_at": time.time(), "data": data}))
    os.replace(tmp, path)


def _cached(key_parts: list, fetch, check=None) -> tuple[dict | list, float | None]:
    """Disk-cached fetch. Returns (data, stale_at): stale_at is None when the data
    is live or within TTL; otherwise the epoch of the last good response, served
    only because the service is unreachable — callers MUST surface that.

    `check(data) -> bool` guards the cache: a degenerate-but-200 response (e.g. an
    empty layer list) is treated as unreachable and never overwrites last-good."""
    settings = get_settings()
    key = hashlib.sha1(json.dumps(key_parts).encode()).hexdigest()
    path = Path(settings.cache_dir) / "cropmonitor" / f"{key}.json"
    blob = _cache_read(path)
    ttl = settings.cropmonitor_cache_ttl_hours * 3600
    if blob and ttl > 0 and time.time() - blob["fetched_at"] < ttl:
        return blob["data"], None
    try:
        data = fetch()
        if check is not None and not check(data):
            raise ServiceUnreachable("The Crop Monitor service returned no usable data.")
    except ServiceUnreachable:
        if blob:                                   # last-good, however old — flagged
            return blob["data"], blob["fetched_at"]
        raise
    _cache_write(path, data)
    return data, None


def _get(url: str, params: dict, check=None) -> tuple[dict, float | None]:
    return _cached([url, sorted(params.items())],
                   lambda: _fetch_json(url, params), check)


def _query_features(url: str, params: dict) -> list[dict]:
    """Paginate one query live (resultOffset until the transfer limit clears).
    Deliberately uncached per-page: the assembled result is cached as one unit by
    the caller, so an outage can never splice pages from different fetches."""
    features: list[dict] = []
    offset = 0
    while True:
        page = _fetch_json(url, {**params, "resultOffset": offset})
        batch = page.get("features", [])
        features.extend(batch)
        # exceededTransferLimit sits top-level for f=json, under properties for f=geojson
        more = page.get("exceededTransferLimit") or (page.get("properties") or {}).get(
            "exceededTransferLimit")
        if not more or not batch:
            return features
        offset += len(batch)


def _layer_index() -> tuple[dict[str, int], float | None]:
    """({"YYYY-MM": layer_id}, stale_at) read live from the service's layer list."""
    data, stale_at = _get(
        get_settings().cropmonitor_url, {"f": "json"},
        check=lambda d: any(_LAYER_RE.match(l.get("name", ""))
                            for l in d.get("layers", []) if isinstance(l, dict)))
    index = {}
    for layer in data.get("layers", []):
        m = _LAYER_RE.match(layer.get("name", ""))
        if m:
            index[f"{m.group(1)}-{m.group(2)}"] = layer["id"]
    return index, stale_at


def _resolve_month(month: str | None) -> tuple[str, int, float | None]:
    index, stale_at = _layer_index()
    if month is None:
        latest = max(index)                        # zero-padded keys sort chronologically
        return latest, index[latest], stale_at
    m = _MONTH_RE.match(month.strip())
    mm = m and (m.group(2) or m.group(3))
    if not m or not 1 <= int(mm) <= 12:
        raise ValueError(f"month must be YYYY-MM (e.g. 2026-04), got {month!r}")
    key = f"{m.group(1)}-{int(mm):02d}"
    if key not in index:
        raise MonthNotAvailable(key, sorted(index), stale_at=stale_at)
    return key, index[key], stale_at


def resolve_month(month: str | None) -> tuple[str, int]:
    """Normalize 'YYYY-MM'/'YYYYMM' and map it to its layer id; None -> latest published."""
    key, layer_id, _ = _resolve_month(month)
    return key, layer_id


def _distinct_crops(layer_id: int) -> tuple[list[str], float | None]:
    # A published layer with zero crop names is a service hiccup, not truth — the
    # check keeps a degenerate 200 from overwriting the last good crop menu. (The
    # row query below carries no such check: an empty row result is a legitimate
    # answer there, so degeneracy is indistinguishable from a true empty.)
    data, stale_at = _get(f"{get_settings().cropmonitor_url}/{layer_id}/query", {
        "where": "1=1", "outFields": "Crop", "returnDistinctValues": "true",
        "returnGeometry": "false", "f": "json"},
        check=lambda d: any((f.get("attributes") or {}).get("Crop")
                            for f in d.get("features", []) if isinstance(f, dict)))
    names = {(f.get("attributes") or {}).get("Crop") for f in data.get("features", [])}
    return sorted(n for n in names if n and n.strip()), stale_at


def distinct_crops(layer_id: int) -> list[str]:
    """The season-level crop names the service assesses for that month's layer."""
    return _distinct_crops(layer_id)[0]


def _resolve_crop(crop: str, layer_id: int) -> tuple[list[str], float | None]:
    crops, stale_at = _distinct_crops(layer_id)
    wanted = crop.strip().lower()
    exact = [c for c in crops if c.lower() == wanted]
    matched = exact or [c for c in crops if wanted in c.lower()]
    if not matched:
        raise CropNotFound(crop, crops, stale_at=stale_at)
    return matched, stale_at


def resolve_crop(crop: str, layer_id: int) -> list[str]:
    """A user's crop word -> the season-level names ('wheat' -> Winter + Spring Wheat).
    Exact (case-insensitive) match wins; otherwise containment against the live list."""
    return _resolve_crop(crop, layer_id)[0]


def _sql_quote(value: str) -> str:
    """Escape a literal for an ArcGIS where clause (single quotes double)."""
    return value.replace("'", "''")


def conditions(month: str | None = None, crop: str | None = None,
               place: str | None = None, geometry: bool = False) -> dict:
    """Per-region condition assessments for a month (None -> latest published).

    `place` must equal a Crop Monitor Country or Region name (case-insensitive).
    With geometry=True the response also carries the service's GeoJSON
    FeatureCollection (the map layer); without it the query is attributes-only and
    light. Every response echoes the exact query it ran, and any response served
    from the last-good cache during an outage says so in `stale_data` — provenance
    is part of the payload.
    """
    crop = (crop or "").strip() or None            # "?place=%20" is no filter, not a
    place = (place or "").strip() or None          # match-nothing UPPER('') clause
    month_key, layer_id, index_stale = _resolve_month(month)
    where: list[str] = []
    crops_matched: list[str] | None = None
    crops_stale: float | None = None
    if crop:
        crops_matched, crops_stale = _resolve_crop(crop, layer_id)
        quoted = ", ".join(f"'{_sql_quote(c)}'" for c in crops_matched)
        where.append(f"Crop IN ({quoted})")
    if place:
        p = _sql_quote(place)
        where.append(f"(UPPER(Country) = UPPER('{p}') OR UPPER(Region) = UPPER('{p}'))")
    where_clause = " AND ".join(where) or "1=1"

    url = f"{get_settings().cropmonitor_url}/{layer_id}/query"
    params = {"where": where_clause, "outFields": ",".join(FIELDS),
              "returnGeometry": str(geometry).lower(),
              "f": "geojson" if geometry else "json"}
    features, query_stale = _cached(["query", url, sorted(params.items())],
                                    lambda: _query_features(url, params))

    def _props(f: dict) -> dict:
        return f.get("properties") or f.get("attributes") or {}

    def _clean(v):                                 # the service pads blanks (" ")
        return v.strip() if isinstance(v, str) else v

    records = [{k.lower(): _clean(_props(f).get(k)) for k in FIELDS} for f in features]
    # A blank Conditions value (live data: "" or " ") is the out-of-season marker.
    summary = Counter((r["conditions"] or "Out of season") for r in records)

    out: dict = {
        "source": "GEOGLAM Crop Monitor (CMET Global_SHP FeatureServer)",
        "as_of": month_key,
        "month": month_key,
        "crop": crop,
        "crops_matched": crops_matched,
        "place": place,
        "summary": dict(summary),
        "count": len(records),
        "records": records,
        "query": {"layer_id": layer_id,
                  "layer": f"Global_Synthesis_{month_key.replace('-', '')}",
                  "where": where_clause, "url": url},
    }
    if geometry:
        out["features"] = {"type": "FeatureCollection", "features": features}
    if not records:
        what = " / ".join(crops_matched) if crops_matched else "any crop"
        reasons = []
        if place:
            reasons.append("the place must exactly match a Crop Monitor Country or "
                           "Region name (case-insensitive)")
        reasons.append("the area may not be monitored")
        if crops_matched:
            reasons.append("the crop may be out of season there")
        out["note"] = (f"The GEOGLAM Crop Monitor returned no {what} assessment for "
                       f"{place or 'the requested filter'} in {month_key} — "
                       + "; ".join(reasons) + ".")
    stale_times = [t for t in (index_stale, crops_stale, query_stale) if t]
    if stale_times:                                # never present stale data as live
        out["stale_data"] = {
            "last_good_fetch": _iso(min(stale_times)),
            "message": "The Crop Monitor service could not be reached; this response "
                       "is served from the last successful fetch."}
    return out
