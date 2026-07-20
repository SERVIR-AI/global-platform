"""The resolve bone (minimal): human place-and-time -> machine place-and-time.
Country/crop + "this season" -> the season windows and the phase the month falls
in, via the hub calendar. Sub-national gazetteer / admin geometries are Phase 2 —
the limitation is DECLARED, never silently faked.
"""

from __future__ import annotations

import calendar as _cal
from datetime import date

from ..food_security import calendar as fs_calendar

_MONTHS = {m.lower(): i for i, m in enumerate(_cal.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(_cal.month_abbr) if m})
_NOW = ("", "this season", "this month", "now", "current", "current season")


def _month(when) -> tuple[int | None, str | None]:
    if when is None or str(when).strip().lower() in _NOW:
        return date.today().month, None
    w = str(when).strip()
    if w.isdigit():
        m = int(w)
        return (m, None) if 1 <= m <= 12 else (None, f"month {m} is not 1-12")
    m = _MONTHS.get(w.lower())
    return (m, None) if m else (
        None, f"could not resolve time expression {when!r} — use a month name, 1-12, "
              "or 'this season'")


def place_time(country: str, crop: str, region: str | None = None,
               when: str | None = None) -> dict:
    """Resolve country/crop/when into the season windows and current phase."""
    month, err = _month(when)
    if err:
        return {"status": "declined", "note": err}
    seasons = (fs_calendar.load().get((country or "").lower(), {})
               .get((crop or "").lower()))
    if not seasons:
        return {"status": "empty", "country": country, "crop": crop,
                "asked_month": month,
                "note": f"no hub calendar for {country!r} {crop!r} — cannot resolve a "
                        "season window (see platform_capabilities for configured pairs)"}
    resolved = [{"season": s["season"], "phase": fs_calendar._phase(month, s),
                 "planting_months": s["planting"], "harvest_months": s["harvest"],
                 "planting": fs_calendar._window(s["planting"]),
                 "harvest": fs_calendar._window(s["harvest"])} for s in seasons]
    return {"status": "ok", "country": country, "crop": crop, "region": region,
            "asked_month": month, "month_name": _cal.month_name[month],
            "seasons": resolved,
            "active_seasons": [r for r in resolved if r["phase"] != "off-season"],
            "region_resolution": ("declared gap: sub-national gazetteer / admin "
                                  "geometries are Phase 2 — `region` is passed through "
                                  "unresolved")}
