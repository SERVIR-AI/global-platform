"""A7 — the adjustable crop calendar. Hub defaults live in conf/crop_calendar.yml;
a per-request override is marked ADJUSTED and cited in the brief — a changed
calendar must be visible in the output, never silently absorbed."""

from __future__ import annotations

import calendar as _cal

import yaml

from ..config import get_settings

_BASELINE_URL = "https://www.cropmonitor.org/baseline-data"


def load() -> dict:
    path = get_settings().crop_calendar_path
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def _in_window(month: int, start: int, end: int) -> bool:
    """Windows may wrap the year end, e.g. Oct..Feb."""
    return start <= month <= end if start <= end else (month >= start or month <= end)


def _phase(month: int, season: dict) -> str:
    p, h = season["planting"], season["harvest"]
    if _in_window(month, p[0], p[1]):
        return "planting window"
    if _in_window(month, h[0], h[1]):
        return "harvest window"
    if _in_window(month, p[1] % 12 + 1, (h[0] - 2) % 12 + 1):
        return "growing season"
    return "off-season"


def _window(w: list[int]) -> str:
    return f"{_cal.month_abbr[w[0]]}–{_cal.month_abbr[w[1]]}"


def citation(country: str, crop: str, asked_month: int,
             override: list[dict] | None = None) -> dict | None:
    """The calendar as one evidence entry (kind='calendar'), with the asked-in
    month's phase computed deterministically per season."""
    seasons = override or load().get((country or "").lower(), {}).get((crop or "").lower())
    if not seasons:
        # An ABSENT calendar is still evidence, and it has to be citable. The brief
        # must always carry a Season timing caveat, but every paragraph must carry a
        # citation — so with nothing to cite the model either invents a number or
        # writes an uncited paragraph and the gate kills the whole brief. Returning
        # the absence as a real entry lets it say "no calendar applies" and cite the
        # statement that says so.
        why = ("no crop was named in the question" if not crop
               else f"the calendar has no entry for {country or 'that country'} {crop}")
        return {"kind": "calendar", "retrieval": "config",
                "source": "Crop calendar (hub default, GEOGLAM/FAO-derived)",
                "title": f"No crop calendar for {country or 'unspecified'} "
                         f"{crop or 'unspecified crop'}",
                "validation": None, "url": _BASELINE_URL, "adjusted": None,
                "text": (f"NO CROP CALENDAR APPLIES: {why}, so season timing cannot be "
                         "pinned to a planting or harvest window. Any timing statement "
                         "must be attributed to a dated source, not to a crop season.")}
    adjusted = override is not None
    month_name = _cal.month_name[asked_month]
    lines = [f"{s['season']}: planting {_window(s['planting'])}, harvest "
             f"{_window(s['harvest'])} — {month_name} falls in the {_phase(asked_month, s)}"
             for s in seasons]
    header = (f"CROP CALENDAR for {country} {crop}, ADJUSTED by the requester for this "
              "run (not the hub default)" if adjusted else
              f"Crop calendar for {country} {crop} (hub default configuration)")
    return {"kind": "calendar", "retrieval": "config",
            "source": ("Requester-adjusted crop calendar" if adjusted
                       else "Crop calendar (hub default, GEOGLAM/FAO-derived)"),
            "title": f"{country} {crop} season windows",
            "validation": None, "url": None if adjusted else _BASELINE_URL,
            "adjusted": adjusted,
            "text": header + ": " + "; ".join(lines) + "."}
