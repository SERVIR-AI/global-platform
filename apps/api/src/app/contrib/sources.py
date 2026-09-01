"""X1: add document sources to a pack's corpus from a validated manifest.

The manifest is YAML: a list of entries (or {sources: [...]}). Every entry must
carry full provenance; a missing field is a REFUSAL naming everything wrong at
once, because a contributor fixing one field per attempt gives up by the third.
"""

from __future__ import annotations

import io
from pathlib import Path

import yaml

REQUIRED = ("pack", "url", "source", "title", "pub_date", "temporal", "validation")

# Live vocabulary read from the corpus 2026-09-01, plus the honest escape hatch:
# a contributor who cannot vouch for validation says so rather than inventing it.
TEMPORAL = ("forecast", "retrospective")
VALIDATION = ("multi-agency-consensus", "peer-reviewed", "single-agency",
              "official-statistic", "unvalidated")

OPTIONAL = ("event", "countries", "crops", "doc_type", "filename", "file",
            "usage_notes")


def validate_entry(e: dict) -> list[str]:
    """Every problem with one manifest entry, all at once. Empty list = valid."""
    fails = []
    if not isinstance(e, dict):
        return ["entry is not a mapping"]
    for k in REQUIRED:
        if not str(e.get(k) or "").strip():
            fails.append(f"missing required field '{k}'")
    if e.get("temporal") and e["temporal"] not in TEMPORAL:
        fails.append(f"temporal must be one of {TEMPORAL}, got {e['temporal']!r} — "
                     "'forecast' gates what may be cited as an outlook")
    if e.get("validation") and e["validation"] not in VALIDATION:
        fails.append(f"validation must be one of {VALIDATION}, got "
                     f"{e['validation']!r} — say 'unvalidated' rather than invent one")
    unknown = set(e) - set(REQUIRED) - set(OPTIONAL)
    if unknown:
        fails.append(f"unknown fields {sorted(unknown)} — every field is provenance; "
                     "there are no free-form extras")
    if e.get("file") and not Path(str(e["file"])).is_file():
        fails.append(f"file {e['file']!r} does not exist")
    from . import notes
    fails += notes.validate(e.get("usage_notes"))
    return fails


def _corpus_for(pack_id: str):
    """The pack's corpus, or (None, reason) — a corpusless pack declines honestly."""
    from ..mcp import packs
    if pack_id not in packs.PACKS:
        return None, (f"unknown pack {pack_id!r} — available: "
                      + ", ".join(packs.available()))
    name = packs.PACKS[pack_id].get("corpus")
    if not name:
        return None, (f"pack {pack_id!r} has no document corpus (a declared gap, "
                      "not an oversight) — document contributions land when one "
                      "exists; feeds and rasters are that pack's contribution paths")
    from ..rag.store import Corpus
    return Corpus(name), None


def _fetch(url: str) -> tuple[bytes, str]:
    """Bytes + filename for a URL. Browser UA: several upstreams 403 plain clients."""
    import requests
    r = requests.get(url, timeout=60, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
    r.raise_for_status()
    return r.content, url.rstrip("/").rsplit("/", 1)[-1] or "document"


def contribute(entries: list[dict], dry_run: bool = False) -> dict:
    """Validate every entry, then ingest the valid ones (unless dry_run).

    Returns {results: [...], ingested, declined, dry_run}. One bad entry never
    blocks a good one — each result names its entry and outcome."""
    from ..rag import docloader

    results = []
    for i, e in enumerate(entries):
        label = (e or {}).get("title") or f"entry {i + 1}"
        fails = validate_entry(e)
        if fails:
            results.append({"entry": label, "status": "declined", "failures": fails})
            continue
        corpus, why = _corpus_for(e["pack"])
        if corpus is None:
            results.append({"entry": label, "status": "declined", "failures": [why]})
            continue
        if dry_run:
            results.append({"entry": label, "status": "valid (dry-run, not ingested)"})
            continue
        try:
            if e.get("file"):
                raw = Path(e["file"]).read_bytes()
                fname = e.get("filename") or Path(e["file"]).name
            else:
                raw, fname = _fetch(e["url"])
                fname = e.get("filename") or fname
            text = docloader.extract_text(raw, fname)
            meta = {k: e[k] for k in
                    ("source", "title", "pub_date", "temporal", "validation", "url")}
            meta |= {k: e[k] for k in ("event", "countries", "crops", "doc_type",
                                        "usage_notes") if e.get(k)}
            out = corpus.ingest(text, meta, raw=raw, filename=fname)
            results.append({"entry": label, "status": "ingested",
                            "doc_id": out["doc_id"], "chunks": out["chunks"],
                            "already_ingested": out.get("already_ingested", False),
                            "passport": meta})
        except Exception as exc:                       # one entry, one failure line
            results.append({"entry": label, "status": "declined",
                            "failures": [f"{type(exc).__name__}: {exc}"]})
    ingested = sum(1 for r in results if r["status"] == "ingested")
    declined = sum(1 for r in results if r["status"] == "declined")
    return {"results": results, "ingested": ingested, "declined": declined,
            "dry_run": dry_run}


def load_manifest(path: str) -> list[dict]:
    """YAML manifest -> entry list. Accepts a bare list or {sources: [...]}."""
    data = yaml.safe_load(io.open(path, encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("sources"), list):
        return data["sources"]
    if isinstance(data, list):
        return data
    raise ValueError("manifest must be a YAML list of entries, or {sources: [...]}")
