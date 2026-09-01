"""coastal-tides: the runbook's worked domain-pack example, complete.

Drop this file into apps/api/src/app/packs_ext/ , restart the server, and the
pack registers itself. Validate first:
    uv run --project apps/api python -m app.contrib.cli doctor coastal-tides
"""


def gather(target, focus, trace, extras):
    from app.mcp import feeds

    place = target["place"]
    trace.append(f"coastal-tides[{place}]")

    # refuse what you do not serve — a governed decline, not an error
    if place.lower() not in ("demo-place", "kep", "sihanoukville"):
        raise ValueError(f"coastal-tides has no tide table for {place!r}")

    soi = feeds.query("enso_soi", {"limit": 3})
    latest = soi["records"][-1]

    citations = [
        {   # EVERY number a drafter may state must appear LITERALLY in text —
            # the gate scans citation text, nothing else.
            "n": 1, "kind": "index", "retrieval": "pulled-at-pack-time",
            "source": soi["passport"]["source"],
            "title": "Southern Oscillation Index, latest monthly",
            "validation": soi["passport"]["validation"],
            "url": "https://psl.noaa.gov/data/correlation/soi.data",
            "text": (f"SOI for {latest['year']}-{latest['month']:02d}: "
                     f"{latest['value']} standardized anomaly "
                     f"({latest.get('classification', 'unclassified')})."),
        },
        {
            "n": 2, "kind": "measurement", "retrieval": "computed-at-pack-time",
            "source": "platform method registry",
            "title": f"demo tide class for {place}",
            "validation": "documented-method",
            "text": f"Computed demo tide class for {place}: 2 of 5.",
        },
    ]
    gaps = [
        "no observed tide-gauge series is registered for this coastline",
        "the tide class is a demonstration method, not a validated model",
    ]
    return citations, gaps, {"queries": None}


SPEC = {
    "id": "coastal-tides",
    "display_name": "Coastal Tides",
    "version": "v0",
    "target_keys": ("place",),
    "target_doc": {"place": "a coastal settlement name"},
    "sections": lambda: ["## What the numbers show", "## Method and validation",
                         "## What's missing and how to weigh it"],
    "corpus": None,
    "default_focus": lambda t: " ".join(str(v) for v in t.values()),
    "usage_notes": "Screening only — demo tide classes, not navigation guidance.",
    "gather": gather,
    "doctor_target": {"place": "demo-place"},
}
