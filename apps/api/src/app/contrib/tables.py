"""X2c: tabular data (CSV) as a first-class, citable source — the type scientists
actually have (station series, yield tables, field-visit numbers).

Landing copies the file into the platform's archive, records its sha256, and
writes a declarative feed row (adapter `generic_csv`). The feed serves the
LANDED copy — residency 'platform-hosted copy' — and refuses if the bytes have
changed since landing, because a table silently edited after citation is the
exact provenance failure this platform exists to prevent.
"""

from __future__ import annotations

import csv
import hashlib
import io
import shutil
from pathlib import Path

import yaml

REQUIRED = ("dataset", "file", "title", "description", "source", "validation",
            "license", "vintage", "cadence", "columns", "units")

VALIDATION = ("multi-agency-consensus", "peer-reviewed", "single-agency",
              "official-statistic", "unvalidated")


def validate_manifest(m: dict) -> list[str]:
    """Every problem at once; the column mapping is checked against the header."""
    fails = []
    if not isinstance(m, dict):
        return ["manifest is not a mapping"]
    for k in REQUIRED:
        if not m.get(k):
            fails.append(f"missing required field '{k}'")
    if m.get("validation") and m["validation"] not in VALIDATION:
        fails.append(f"validation must be one of {VALIDATION} — say 'unvalidated' "
                     "rather than invent one")
    ds = str(m.get("dataset") or "")
    if ds and not ds.replace("_", "").isalnum():
        fails.append("dataset must be a snake_case identifier")
    cols = m.get("columns")
    if cols is not None and not isinstance(cols, dict):
        fails.append("columns must map output field -> CSV column header")
    path = m.get("file")
    if path and not Path(str(path)).is_file():
        fails.append(f"file {path!r} does not exist")
    elif path and isinstance(cols, dict):
        try:
            header = next(csv.reader(io.open(path, encoding="utf-8-sig")))
            missing = [c for c in cols.values() if c not in header]
            if missing:
                fails.append(f"CSV header {header} is missing mapped column(s) "
                             f"{missing}")
        except (OSError, StopIteration) as exc:
            fails.append(f"file is not readable CSV: {exc}")
    return fails


def add(manifest: dict, dry_run: bool = False) -> dict:
    """Gate then land: validate -> archive the file + sha -> write the feed row."""
    from ..config import get_settings

    fails = validate_manifest(manifest)
    if fails:
        return {"status": "declined", "failures": fails}
    settings = get_settings()
    ds = manifest["dataset"]
    feed_yml = Path(settings.feeds_conf_dir) / f"{ds}.yml"
    if feed_yml.exists():
        return {"status": "declined",
                "failures": [f"dataset {ds!r} already landed — contributions add "
                             "tables, they do not overwrite them"]}
    raw = Path(manifest["file"]).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    n_rows = max(0, sum(1 for _ in csv.reader(io.StringIO(raw.decode("utf-8-sig")))) - 1)
    if dry_run:
        return {"status": "valid (dry-run, nothing written)",
                "rows": n_rows, "sha256": digest}

    dest = Path(settings.cache_dir) / "tables" / f"{ds}.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest["file"], dest)

    spec = {"dataset": ds, "title": manifest["title"],
            "description": manifest["description"], "source": manifest["source"],
            "validation": manifest["validation"],
            "residency": "platform-hosted copy",
            "cadence": manifest["cadence"], "adapter": "generic_csv",
            "license": manifest["license"], "vintage": manifest["vintage"],
            "fetch": {"path": str(dest), "sha256": digest,
                      "columns": manifest["columns"], "units": manifest["units"],
                      **({"as_of_field": manifest["as_of_field"]}
                         if manifest.get("as_of_field") else {})},
            "params": {"limit": "rows of series to return (default 12)"}}
    feed_yml.parent.mkdir(parents=True, exist_ok=True)
    feed_yml.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
    return {"status": "landed", "dataset": ds, "rows": n_rows, "sha256": digest,
            "archived": str(dest), "feed_row": str(feed_yml),
            "note": "restart the server (or reload feeds) to serve it",
            "passport": {k: manifest[k] for k in
                         ("source", "validation", "license", "vintage")}}
