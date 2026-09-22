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


def place_time(country: str | None = None, crop: str | None = None,
               region: str | None = None, when: str | None = None,
               place: str | None = None) -> dict:
    """Resolve a place and/or a country-crop season into machine terms.

    A place is resolved by the platform's own gazetteer — the same one every
    hazard answer uses — so the two halves of "place and time" now both work.
    """
    month, err = _month(when)
    if err:
        return {"status": "declined", "note": err}

    # A place (or a region, which is a place named relative to its country) is
    # resolved for its own sake. This is the half of the bone that used to be a
    # declared Phase-2 gap while the resolver it needed was already shipping in
    # every hazard answer.
    asked_place = place or (f"{region}, {country}" if region and country else region)
    resolved_place = None
    if asked_place:
        from ..graph.geo import ingest
        resolved_place = ingest.resolve_place(asked_place)

    if not (country and crop):
        if resolved_place is None:
            return {"status": "declined",
                    "note": "pass a `place` to resolve an area, or `country` and "
                            "`crop` to resolve a season — this call had neither"}
        return {"status": resolved_place["status"], "asked_month": month,
                "month_name": _cal.month_name[month] if month else None,
                "place": resolved_place,
                "seasons_note": ("no country/crop given, so no season window was "
                                 "resolved — pass both to get one")}
    seasons = (fs_calendar.load().get((country or "").lower(), {})
               .get((crop or "").lower()))
    if not seasons:
        return {"status": "empty", "country": country, "crop": crop,
                "asked_month": month,
                **({"place": resolved_place} if resolved_place else {}),
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
            **({"place": resolved_place} if resolved_place else {}),
            "region_resolution": (
                resolved_place["how"] if resolved_place
                and resolved_place.get("status") == "ok"
                else resolved_place["note"] if resolved_place
                else "no region or place given — nothing to resolve")}
