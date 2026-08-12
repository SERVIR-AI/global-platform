"""IRI/CCSR ENSO forecast — the model plume (JSON) and the monthly outlook (prose).

Two products from one publisher, both public and unauthenticated:

  plume   ensoforecast.iri.columbia.edu/plumes_json/{year}/{month0}
          per-model Nino-3.4 forecasts by lead season. Genuinely machine-readable.

  outlook iri.columbia.edu/.../enso/{year}-{Month}-quick-look/
          the monthly narrative. The OFFICIAL probability percentages live here as
          prose and are served VERBATIM — never regex-parsed into numbers. A brief
          may quote and attribute them; the platform will not re-type them as
          structured data it cannot defend. See registry `enso_probabilities`.

Three upstream traps this module exists to absorb:

  1. plumes_json takes a ZERO-BASED month (6 = July), mirroring the JavaScript that
     calls it. Callers here pass a normal 1-12 month and this converts.
  2. Asking plumes_json for a month not yet issued returns the PREVIOUS month's
     forecast with HTTP 200 and no warning. Every response is checked against what
     was asked for, and reports the issuance actually received.
  3. The narrative page for the current month appears days after the plume does, so
     "latest" walks back to the most recent published month rather than 404-ing.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests

from .climate_indices import IndexUnavailable, cached

_TIMEOUT = (10, 30)
_UA = {"User-Agent": "SERVIR-GRP/0.1"}
_MISSING = -998.0                # the plume writes -999 for "this model stops here"
_WALK_BACK = 6                   # months to search backwards for a published issue

_PLUME_URL = "https://ensoforecast.iri.columbia.edu/plumes_json/{year}/{month0}"
_OUTLOOK_URL = ("https://iri.columbia.edu/our-expertise/climate/forecasts/enso/"
                "{year}-{month}-quick-look/")

_MONTHS = ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]

# Lead-season labels per ISSUING month, mirrored from IRI's own figure scripts.
# The first two entries are observed (season, then month); the forecast seasons
# follow. Mirrored rather than derived so our labels match the published figure
# exactly — a season label that disagrees with IRI's own chart is a wrong citation.
_LABELS = {
    1:  ["OND", "Dec", "DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON"],
    2:  ["NDJ", "Jan", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND"],
    3:  ["DJF", "Feb", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ"],
    4:  ["JFM", "Mar", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ", "DJF"],
    5:  ["FMA", "Apr", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ", "DJF", "JFM"],
    6:  ["MAM", "May", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ", "DJF", "JFM", "FMA"],
    7:  ["AMJ", "Jun", "JJA", "JAS", "ASO", "SON", "OND", "NDJ", "DJF", "JFM", "FMA", "MAM"],
    8:  ["MJJ", "Jul", "JAS", "ASO", "SON", "OND", "NDJ", "DJF", "JFM", "FMA", "MAM", "AMJ"],
    9:  ["JJA", "Aug", "ASO", "SON", "OND", "NDJ", "DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ"],
    10: ["JAS", "Sep", "SON", "OND", "NDJ", "DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA"],
    11: ["ASO", "Oct", "OND", "NDJ", "DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS"],
    12: ["SON", "Nov", "NDJ", "DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO"],
}

# Which basis a plume row forecasts on. "Relative" rows carry RONI (Nino-3.4 minus
# the tropical mean), not absolute Nino-3.4, and the "* AVG" rows are typed "Other"
# and duplicate `averages`. Mixing any of them into a count is a category error.
_ABSOLUTE = ("Dynamical", "Statistical")
_BASIS = {"Dynamical": "absolute Nino-3.4", "Statistical": "absolute Nino-3.4",
          "Relative": "relative Nino-3.4 (RONI) — different basis, not comparable",
          "Other": "ensemble summary, duplicates `ensemble_mean`"}

_NOT_A_PROBABILITY = (
    "MODEL COUNTS, NOT PROBABILITIES. How many models fall in each band is not the "
    "same quantity as IRI's published probability forecast, which is produced by a "
    "separate objective-plus-subjective procedure. Do not convert these counts to a "
    "percentage and do not present them as a chance of anything. For the official "
    "probabilities use the `enso_outlook` narrative and quote it."
)


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _now() -> tuple[int, int]:
    n = datetime.now(timezone.utc)
    return n.year, n.month


def _get(url: str) -> requests.Response:
    try:
        r = requests.get(url, timeout=_TIMEOUT, headers=_UA)
    except requests.RequestException as exc:
        raise IndexUnavailable(f"{url} unreachable: {exc}") from exc
    return r


# --------------------------------------------------------------------------- plume

def _plume_once(year: int, month: int) -> dict:
    """One issuance. `month` is 1-12; the upstream wants it zero-based."""
    url = _PLUME_URL.format(year=year, month0=month - 1)
    r = _get(url)
    if r.status_code != 200:
        raise IndexUnavailable(f"{url} returned HTTP {r.status_code}")
    try:
        raw = r.json()
    except ValueError as exc:
        raise IndexUnavailable(f"{url} did not return JSON") from exc
    if not raw.get("models"):
        raise IndexUnavailable(f"{url} returned no models")

    # TRAP 2: an unissued month silently yields the previous one, HTTP 200. Trust
    # the payload's own month over the one we asked for.
    got = int(raw.get("month", month - 1)) + 1

    # hash_sn lists ten forecast seasons but the models only ever carry nine, so the
    # label list is trimmed to what the data actually spans. Reporting the tenth
    # would advertise a lead season no model answered for.
    span = max((len(m.get("data") or []) for m in raw["models"]), default=0)
    labels = _LABELS[got]
    seasons = labels[2:2 + span]
    models, agreement = [], []
    for m in raw["models"]:
        vals = [(seasons[i], v) for i, v in enumerate(m.get("data", []))
                if i < len(seasons) and v is not None and v > _MISSING]
        if vals:
            kind = m.get("type")
            models.append({"model": m.get("model"), "type": kind,
                           "basis": _BASIS.get(kind, "unknown"),
                           "counts_toward_agreement": kind in _ABSOLUTE,
                           "forecast": [{"season": s, "anomaly": round(v, 2)} for s, v in vals]})

    # Only models on the ABSOLUTE Nino-3.4 basis may vote. Two exclusions, both of
    # which silently corrupted the count before: the `* AVG` rows are typed "Other"
    # and are byte-identical to `averages` — letting the ensemble means vote in the
    # statistic that summarises them — and "Relative" models forecast RONI, which
    # cannot be thresholded against absolute +/-0.5 bands.
    voting = [m for m in models if m["counts_toward_agreement"]]
    for season in seasons:
        band = {"el_nino": 0, "neutral": 0, "la_nina": 0}
        for m in voting:
            v = next((f["anomaly"] for f in m["forecast"] if f["season"] == season), None)
            if v is None:
                continue
            band["el_nino" if v >= 0.5 else "la_nina" if v <= -0.5 else "neutral"] += 1
        total = sum(band.values())
        if total:
            agreement.append({"season": season, "models_reporting": total, **band})

    return {
        "product": "ENSO model plume",
        "long_name": "CCSR/IRI Nino-3.4 model prediction plume",
        "source": "IRI / Columbia Climate School",
        "url": url,
        "issued_for": f"{_MONTHS[got - 1]} {year}",
        "observed": [{"period": o.get("month"), "anomaly": round(o.get("data"), 2)}
                     for o in raw.get("observed", []) if o.get("data") is not None],
        # RONI, carried because CPC now LEADS with the relative index while IRI's
        # prose quotes the absolute one. Dropping it left no way to reconcile the
        # two, and three different numbers for "weekly Nino-3.4" are in circulation.
        "observed_relative": [{"period": o.get("month"), "anomaly": round(o.get("data"), 2)}
                              for o in raw.get("observed_roni", []) if o.get("data") is not None],
        "lead_seasons": seasons,
        "model_count": sum(1 for m in models if m["counts_toward_agreement"]),
        "models_listed": len(models),
        "models": models,
        "ensemble_mean": {k: [round(v, 2) for v in vals if v > _MISSING]
                          for k, vals in (raw.get("averages") or {}).items()},
        "ensemble_mean_basis": {"dynamical": "absolute Nino-3.4",
                                "statistical": "absolute Nino-3.4",
                                "relative": "RONI — different basis, not comparable",
                                "total": "absolute Nino-3.4"},
        "model_agreement": agreement,
        "model_agreement_caveat": _NOT_A_PROBABILITY,
    }


def _latest_at_or_before(what: str, once, year: int | None, month: int | None) -> dict:
    """Walk backwards to the newest issuance at or before (year, month).

    Walking back is NORMAL when the caller asked for "latest" — the newest month is
    not published until mid-month, so landing on last month is the right answer, not
    a stale one. It is only a SUBSTITUTION when the caller named a month and got a
    different one; flagging both alike would put a staleness caveat on every default
    call and train readers to ignore it.
    """
    now_y, now_m = _now()
    explicit = year is not None or month is not None
    year, month = int(year or now_y), int(month or now_m)
    if not 1 <= month <= 12:
        raise IndexUnavailable(f"month must be 1-12, got {month!r}")
    requested = f"{_MONTHS[month - 1]} {year}"

    errs = []
    for _ in range(_WALK_BACK):
        try:
            out = once(year, month)
            break
        except IndexUnavailable as exc:
            errs.append(str(exc))
            year, month = _prev_month(year, month)
    else:
        raise IndexUnavailable(
            f"no {what} issued in the {_WALK_BACK} months to {requested}: {errs[0]}")

    substituted = explicit and out["issued_for"] != requested
    return {**out, "requested": requested if explicit else "latest available",
            "substituted": substituted,
            "substitution_note": (
                f"{requested} is not published; answered with {out['issued_for']} instead"
            ) if substituted else None}


def plume(year: int | None = None, month: int | None = None) -> dict:
    """The most recent model plume at or before (year, month); defaults to latest."""
    return _latest_at_or_before("plume", _plume_once, year, month)


# ------------------------------------------------------------------------- outlook

class _Narrative(HTMLParser):
    """Headings and paragraphs inside IRI's `page-content` blocks, dropping nav,
    scripts and the month-picker chrome. Stdlib rather than an HTML library: bs4 is
    only a transitive dependency here and relying on it would be a latent break."""

    _KEEP = ("h1", "h2", "h3", "h4", "p", "li")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._divs: list[bool] = []
        self._tag: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "div":
            self._divs.append("page-content" in (dict(attrs).get("class") or ""))
        elif tag in self._KEEP and any(self._divs):
            self._tag, self._buf = tag, []

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._divs:
            self._divs.pop()
        elif tag == self._tag:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if text:
                self.blocks.append({"tag": self._tag, "text": text})
            self._tag, self._buf = None, []

    def handle_data(self, data: str) -> None:
        if self._tag:
            self._buf.append(data)


def _outlook_once(year: int, month: int) -> dict:
    url = _OUTLOOK_URL.format(year=year, month=_MONTHS[month - 1])
    r = _get(url)
    if r.status_code != 200:
        raise IndexUnavailable(f"{url} returned HTTP {r.status_code}")
    p = _Narrative()
    p.feed(r.text)
    blocks = p.blocks
    if not blocks:
        raise IndexUnavailable(f"{url} had no readable narrative — page layout changed")

    published = next((b["text"].split(":", 1)[1].strip() for b in blocks
                      if b["tag"] == "h4" and b["text"].lower().startswith("published")), None)
    # Sections, so a consumer can quote the ENSO discussion or the IOD one and say
    # which. Headings drive the split, so a new IRI section appears on its own.
    sections, current = [], None
    for b in blocks:
        if b["tag"] in ("h1", "h2", "h3"):
            current = {"heading": b["text"], "paragraphs": []}
            sections.append(current)
        elif b["tag"] in ("p", "li") and current is not None:
            current["paragraphs"].append(b["text"])
    sections = [s for s in sections if s["paragraphs"]]

    return {
        "product": "ENSO quick look",
        "long_name": "IRI monthly ENSO/IOD forecast narrative",
        "source": "IRI / Columbia Climate School",
        "url": url,
        "issued_for": f"{_MONTHS[month - 1]} {year}",
        "published": published,
        "sections": sections,
        "verbatim": True,
        "handling": (
            "This narrative is the OFFICIAL forecast text and carries the official "
            "probability percentages in prose. Quote it with attribution. Do NOT "
            "extract the percentages into structured fields — no machine-readable "
            "probability table exists, and a re-typed number cannot be traced back."
        ),
    }


def outlook(year: int | None = None, month: int | None = None) -> dict:
    """The most recent published quick look at or before (year, month)."""
    return _latest_at_or_before("quick look", _outlook_once, year, month)


# ------------------------------------------------------------------- CPC discussion

_DISCUSSION_URL = ("https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/"
                   "enso_advisory/ensodisc.txt")
_STATUS = "ENSO Alert System Status:"


def discussion() -> dict:
    """NOAA CPC's monthly ENSO Diagnostic Discussion, served VERBATIM.

    The second official narrative, and usually FRESHER than IRI's quick look. It is
    the only source for the formal ENSO Alert System Status (Watch / Advisory /
    Final Advisory), which no index can supply — a status is a declaration, not a
    measurement. Like `outlook`, its percentages are quoted, never parsed.

    Latin-1 on the wire: `requests` guesses UTF-8 and mangles the tilde in "El Nino".
    """
    r = _get(_DISCUSSION_URL)
    if r.status_code != 200:
        raise IndexUnavailable(f"{_DISCUSSION_URL} returned HTTP {r.status_code}")
    r.encoding = "latin-1"
    lines = [ln.strip() for ln in r.text.splitlines()]

    status = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith(_STATUS)), None)
    issued = next((ln for ln in lines[:8]
                   if re.fullmatch(r"\d{1,2} [A-Z][a-z]+ \d{4}", ln)), None)
    # Figure captions are boilerplate that would swamp the discussion itself.
    body, seen_synopsis = [], False
    for ln in lines:
        if ln.startswith("Figure "):
            break
        if ln.startswith("Synopsis:"):
            seen_synopsis = True
        if seen_synopsis and ln:
            body.append(ln)
    if not status or not body:
        raise IndexUnavailable(f"{_DISCUSSION_URL} did not parse — CPC layout changed")

    return {
        "product": "ENSO diagnostic discussion",
        "long_name": "NOAA CPC monthly ENSO Diagnostic Discussion",
        "source": "NOAA CPC", "url": _DISCUSSION_URL,
        "issued_for": issued or "unknown",
        "published": issued,
        "alert_status": status,
        "sections": [{"heading": f"CPC ENSO Diagnostic Discussion, {issued or 'undated'}"
                                 f" — Alert System Status: {status}",
                      "paragraphs": body}],
        "verbatim": True,
        "handling": (
            "Official CPC narrative. `alert_status` is the formal ENSO Alert System "
            "Status and may be reported as such. The percentages in the prose are "
            "quoted with attribution, never re-typed as structured numbers."),
    }


_PRODUCTS = {"enso_plume": plume, "enso_outlook": outlook,
             "enso_discussion": discussion}


def fetch(product: str, **kwargs) -> dict:
    """Cached read, sharing the last-good-stale behaviour of the index fetcher: these
    are MONTHLY products, so a value from this morning is still this month's."""
    return cached(f"{product}:{sorted(kwargs.items())}",
                  lambda: _PRODUCTS[product](**kwargs))
