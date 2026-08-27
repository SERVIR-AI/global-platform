"""The context bone: the hub-default crop calendar, with a human override that is
declared (ADJUSTED) and pinned to its target. A mismatched override is dropped AND
the drop is declared (contract rule 4) — a swap must never apply silently.
Thin over food_security.calendar.citation.
"""

from __future__ import annotations

from datetime import date

from ..food_security import calendar as fs_calendar


def _valid_override(override: list[dict]) -> bool:
    return all(isinstance(s, dict) and "season" in s
              and isinstance(s.get("planting"), list) and len(s["planting"]) == 2
              and isinstance(s.get("harvest"), list) and len(s["harvest"]) == 2
              for s in override)


def get(country: str, crop: str, asked_month: int | None = None,
        override: list[dict] | None = None,
        override_country: str | None = None, override_crop: str | None = None) -> dict:
    """The calendar for country/crop and the phase the asked month falls in.
    asked_month defaults to the current month ("this season"). An override is
    ADJUSTED + target-pinned; if override_country/crop mismatch the request, it is
    dropped and declared in `gaps`."""
    month = asked_month or date.today().month
    if not 1 <= month <= 12:
        return {"status": "declined", "note": f"asked_month {month} is not 1-12"}

    gaps: list[str] = []
    applied = override
    if override is not None:
        if not _valid_override(override):
            return {"status": "declined",
                    "note": "override must be a list of {season, planting:[m,m], harvest:[m,m]}"}
        tc, tcr = (override_country or "").lower(), (override_crop or "").lower()
        if (tc and tc != (country or "").lower()) or (tcr and tcr != (crop or "").lower()):
            gaps.append(
                f"a calendar adjustment made for {override_country or '?'} "
                f"{override_crop or '?'} was NOT applied — the request is about "
                f"{country} {crop}; the hub-default calendar was used instead")
            applied = None

    entry = fs_calendar.citation(country, crop, month, override=applied)
    if entry is None:
        return {"status": "empty", "country": country, "crop": crop, "asked_month": month,
                "note": f"No hub-default crop calendar configured for {country!r} {crop!r}."}
    return {"status": "ok", "country": country, "crop": crop, "asked_month": month,
            "adjusted": entry["adjusted"], "calendar": entry, "gaps": gaps}
