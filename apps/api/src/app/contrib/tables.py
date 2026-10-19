"""X2c: tabular data (CSV) as a first-class, citable source — the type scientists
actually have (station series, yield tables, field-visit numbers).

Landing copies the file into the platform's archive, records its sha256, and
writes a declarative feed row (adapter `generic_csv`). The feed serves the
LANDED copy — residency 'platform-hosted copy' — and refuses if the bytes have
changed since landing, because a table silently edited after citation is the
exact provenance failure this platform exists to prevent.

The bytes are archived as given (the sha is the contributor's file's) and read in
the encoding they were written in: UTF-8, else Windows-1252 or Mac Roman — the
encoding is recorded on the row, so a Mac export is never a raw codec error.
"""

from __future__ import annotations

import csv
import hashlib
import io
import shutil
from pathlib import Path

import yaml

from . import catalog_io

REQUIRED = ("dataset", "file", "title", "description", "source", "validation",
            "license", "vintage", "cadence", "columns", "units")
# Where the rows speak: a table that declares neither is read for every place.
SCOPE = ("countries", "bbox")

VALIDATION = ("multi-agency-consensus", "peer-reviewed", "single-agency",
              "official-statistic", "unvalidated")

ENCODING_NAMES = {"utf-8": "UTF-8", "cp1252": "Windows-1252", "mac_roman": "Mac OS Roman"}


def decode(raw: bytes) -> tuple[str, str]:
    """(text, encoding) of a CSV file's bytes — see docloader.decode_text."""
    from ..rag.docloader import decode_text
    return decode_text(raw)


def count_rows(text: str) -> int:
    """Data rows as the csv reader sees them: a quoted field may hold line breaks,
    so counting newlines over-counts (16,740 for a 16,715-row table)."""
    return max(0, sum(1 for row in csv.reader(io.StringIO(text))
                      if any(c.strip() for c in row)) - 1)


def norm_countries(value) -> list[str] | None:
    """A declared country list as every source kind stores it: names, stripped,
    each once. A single name is a list of one. None when nothing is declared."""
    if value is None or value == [] or value == "":
        return None
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return None
    out = []
    for c in items:
        name = " ".join(str(c).split()) if isinstance(c, str) else ""
        if name and name not in out:
            out.append(name)
    return out or None


def scope_problems(m: dict) -> list[str]:
    """`countries` must be country names; `bbox` [minLon, minLat, maxLon, maxLat]."""
    fails = []
    c = m.get("countries")
    if c not in (None, [], ""):
        items = [c] if isinstance(c, str) else c
        if not isinstance(items, (list, tuple)) or not items or \
                any(not isinstance(x, str) or not x.strip() for x in items):
            fails.append(f"countries must be a list of country names, e.g. [Costa Rica] — "
                         f"got {c!r}")
    b = m.get("bbox")
    if b not in (None, [], ""):
        if bbox_of(b) is None:
            fails.append(f"bbox must be [minLon, minLat, maxLon, maxLat] in degrees (EPSG:4326), "
                         f"west < east and south < north, e.g. [-85.4, 9.6, -84.6, 10.4] — got {b!r}")
    return fails


def bbox_of(value) -> list[float] | None:
    """[minLon, minLat, maxLon, maxLat] as floats, or None when it is not one. A
    "a,b,c,d" string is read too."""
    if isinstance(value, str):
        value = value.replace(";", ",").split(",")
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        w, s, e, n = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        return None
    return [w, s, e, n]


def scope_fields(m: dict) -> dict:
    """The normalised scope keys a row carries: `countries`, `bbox` — only declared ones."""
    out = {}
    c = norm_countries(m.get("countries"))
    if c:
        out["countries"] = c
    b = bbox_of(m.get("bbox")) if m.get("bbox") not in (None, [], "") else None
    if b:
        out["bbox"] = b
    return out


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
    from . import notes
    fails += notes.validate(m.get("usage_notes"))
    fails += scope_problems(m)
    path = m.get("file")
    if path and not Path(str(path)).is_file():
        fails.append(f"file {path!r} does not exist")
    elif path and isinstance(cols, dict):
        try:
            text, _enc = decode(Path(str(path)).read_bytes())
            header = next(csv.reader(io.StringIO(text)))
            missing = [c for c in cols.values() if c not in header]
            if missing:
                fails.append(f"CSV header {header} is missing mapped column(s) "
                             f"{missing}")
        except StopIteration:
            fails.append("file is empty — a CSV needs a header row and at least one data row")
        except (OSError, csv.Error) as exc:
            fails.append(f"file is not readable CSV: {type(exc).__name__}: {exc}")
    return fails


def add(manifest: dict, dry_run: bool = False, review: str | None = None) -> dict:
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
    text, enc = decode(raw)
    n_rows = count_rows(text)
    if dry_run:
        return {"status": "valid (dry-run, nothing written)",
                "rows": n_rows, "sha256": digest, "encoding": enc}

    with catalog_io.LOCK:
        # checked again where it is written: two landings of one name at once both
        # passed the first check, and the second's copy replaced the first's file
        if feed_yml.exists():
            return {"status": "declined",
                    "failures": [f"dataset {ds!r} already landed — contributions add "
                                 "tables, they do not overwrite them"]}
        return _land(manifest, review, settings, ds, feed_yml, digest, enc, n_rows)


def _land(manifest, review, settings, ds, feed_yml, digest, enc, n_rows):
    dest = Path(settings.cache_dir) / "tables" / f"{ds}.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest["file"], dest)

    spec = {"dataset": ds, "title": manifest["title"],
            "description": manifest["description"], "source": manifest["source"],
            "validation": manifest["validation"],
            "residency": "platform-hosted copy",
            "cadence": manifest["cadence"], "adapter": "generic_csv",
            "license": manifest["license"], "vintage": manifest["vintage"],
            # Relative to the cache dir: an absolute path here is the staging
            # machine's and goes dark on every other host.
            "fetch": {"path": str(dest.relative_to(Path(settings.cache_dir))),
                      "sha256": digest,
                      "columns": manifest["columns"], "units": manifest["units"],
                      **({"encoding": enc} if enc != "utf-8" else {}),
                      **({"as_of_field": manifest["as_of_field"]}
                         if manifest.get("as_of_field") else {})},
            "params": params_doc(),
            # Which domain pack may cite it. Without this every contributed table
            # defaulted to food-security and could never enter a risk answer.
            "pack": manifest.get("pack", "food-security"),
            # Where its rows speak: a pack reads `countries` (and `bbox`) before
            # citing it, so a Gulf of Nicoya factor is not applied in Ecuador.
            **scope_fields(manifest),
            **({"review": review} if review else {}),
            **({"usage_notes": manifest["usage_notes"]}
               if manifest.get("usage_notes") else {}),
            # The marker remove-feed keys on: only rows the gate wrote are
            # removable without --force; shipped feeds are code changes.
            "contributed": True}
    feed_yml.parent.mkdir(parents=True, exist_ok=True)
    catalog_io.write_text(feed_yml, yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
    return {"status": "landed", "dataset": ds, "rows": n_rows, "sha256": digest,
            "encoding": enc,
            "archived": str(dest), "feed_row": str(feed_yml),
            "note": "restart the server (or reload feeds) to serve it",
            "passport": {k: manifest[k] for k in
                         ("source", "validation", "license", "vintage")}}


def params_doc() -> dict:
    """What feeds_query takes on a table — the adapter declines anything else."""
    return {"limit": "rows to return, from the end of the table (default 12)",
            "<column>": "equality filter on any column, e.g. {country: 'Costa Rica'} — "
                        "column names and values match case- and accent-insensitively; "
                        "a list matches any of its values",
            "bbox": "[minLon, minLat, maxLon, maxLat] — rows whose lat/lon columns fall "
                    "inside (only for a table with latitude and longitude columns)"}
