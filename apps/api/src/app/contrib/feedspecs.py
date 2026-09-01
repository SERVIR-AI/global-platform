"""X2: declarative feed rows — conf/feeds/*.yml become registry.FEEDS entries.

A feed of a known shape needs NO Python: the YAML names a generic adapter and a
fetch spec. Invalid specs are REGISTERED as status "invalid" with every failure
named — queryable gives an honest decline, capabilities shows the row — because a
silently dropped file is a contributor debugging in the dark.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REQUIRED = ("dataset", "title", "description", "source", "validation",
            "residency", "cadence", "adapter")

GENERIC_ADAPTERS = ("generic_table", "generic_json", "generic_csv")

_FETCH_REQUIRED = {"generic_table": ("url", "index_name", "units"),
                   "generic_json": ("url", "records_path", "fields"),
                   "generic_csv": ("path", "columns", "units")}


def validate_spec(spec: dict) -> list[str]:
    """Every problem with one feed spec, all at once. Empty list = valid."""
    fails = []
    if not isinstance(spec, dict):
        return ["spec is not a mapping"]
    for k in REQUIRED:
        if not str(spec.get(k) or "").strip():
            fails.append(f"missing required field '{k}'")
    adapter = spec.get("adapter")
    if adapter and adapter not in GENERIC_ADAPTERS:
        fails.append(f"adapter must be one of {GENERIC_ADAPTERS} for a declarative "
                     f"feed, got {adapter!r} — a new shape needs a dev and a code "
                     "adapter, which is a registry row away")
    fetch = spec.get("fetch")
    if not isinstance(fetch, dict):
        fails.append("missing 'fetch' mapping (url + shape details)")
    elif adapter in _FETCH_REQUIRED:
        for k in _FETCH_REQUIRED[adapter]:
            if not fetch.get(k):
                fails.append(f"fetch.{k} is required for {adapter}")
    return fails


def load_dir(conf_dir: Path) -> dict:
    """All conf/feeds/*.yml -> {dataset: row}. Invalid rows carry status='invalid'
    + reason; a file that will not parse becomes a row named after the file."""
    rows: dict = {}
    d = Path(conf_dir)
    if not d.is_dir():
        return rows
    for f in sorted(d.glob("*.yml")):
        try:
            spec = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            rows[f.stem] = {"status": "invalid", "declarative": str(f.name),
                            "reason": f"unparseable YAML: {exc}"}
            continue
        fails = validate_spec(spec)
        name = (spec or {}).get("dataset") or f.stem
        if fails:
            rows[name] = {"status": "invalid", "declarative": str(f.name),
                          "reason": "spec failures: " + "; ".join(fails)}
            continue
        row = {k: v for k, v in spec.items() if k != "dataset"}
        row.setdefault("status", "available")
        row["declarative"] = str(f.name)
        rows[name] = row
    return rows


def merge_into(feeds: dict, rows: dict) -> None:
    """Merge declarative rows into the live FEEDS dict. A YAML row shadowing a
    code row is a mistake, not an override — it registers invalid, saying so."""
    for name, row in rows.items():
        if name in feeds:
            feeds[name + ".yml"] = {
                "status": "invalid", "declarative": row.get("declarative"),
                "reason": (f"dataset name {name!r} collides with a built-in feed — "
                           "declarative rows cannot shadow code rows; rename it")}
            continue
        feeds[name] = row
