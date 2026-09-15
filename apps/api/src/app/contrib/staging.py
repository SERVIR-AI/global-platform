"""Contributions over the MCP: staged, previewable by their contributor, live only
after a reviewer says so.

The gate is the same one the CLI runs (sources/tables/rasters validators) — a
submission with problems is refused with every problem named and nothing is
stored. A clean submission is stored as `pending` AND staged for preview: the
artefact is really there, tagged `staged_by`, and the visibility rule
(contrib/identity.py) shows it to its contributor and to reviewers only. So a
contributor tests exactly what an analyst will see, and nobody else sees it
until approval lands it through the same gate without the tag.

Removal semantics follow contrib/removal.py: rejecting or withdrawing stops
future use; a document's raw archive is kept so any receipt the contributor
minted while previewing stays replayable.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from ..mcp import store
from . import fetch_policy, identity, sources

KINDS = ("document", "table", "feed")  # rasters follow (X4.5)
STATUSES = ("pending", "approved", "rejected", "withdrawn", "failed")
PENDING_CAP = 20               # open contributions per contributor
HOURLY_CAP = 30                # submissions per contributor per hour

_HISTORY = ("removal stops future use, it does not rewrite history — packs and "
            "receipts minted while the preview was live stay replayable")


class Declined(Exception):
    """A submission the platform will not stage; the message says why."""


# --------------------------------------------------------------------------- kinds

DOCUMENT_FIELDS = {
    "required": {
        "pack": "the domain pack whose library receives it (e.g. food-security)",
        "url": "where the platform can fetch the original (http/https, public host)",
        "source": "publisher, e.g. NOAA CPC",
        "title": "the document's title",
        "pub_date": "publication date as YYYY-MM or YYYY-MM-DD, quoted as text",
        "temporal": "forecast | retrospective — gates what may be cited as an outlook",
        "validation": "multi-agency-consensus | peer-reviewed | single-agency | "
                      "official-statistic | unvalidated (say unvalidated rather than invent one)",
    },
    "optional": {
        "usage_notes": "a few lines the consuming analyst reads when citing it (max 500 chars)",
        "doc_type": "free tag, e.g. enso-status, bulletin, assessment",
        "event": "event window tag, e.g. elnino-2023-24",
        "countries": "list of country names the document covers",
        "crops": "list of crops the document covers",
        "filename": "override for the archived filename (extension decides the parser)",
    },
}


def _validate_document(manifest) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest must be a mapping of provenance fields"]
    m = dict(manifest)
    problems = []
    if m.pop("file", None):
        problems.append("'file' is not accepted over the MCP — the platform cannot read "
                        "your disk; give a 'url' it can fetch instead")
    problems += sources.validate_entry(m)
    if m.get("url") and not any("'url'" in p for p in problems):
        problems += fetch_policy.check_url(m["url"])
    return problems


def _document_meta(m: dict) -> dict:
    meta = {k: m[k] for k in ("source", "title", "pub_date", "temporal", "validation", "url")}
    meta |= {k: m[k] for k in ("event", "countries", "crops", "doc_type", "usage_notes")
             if m.get(k)}
    return meta


def _prepare_document(m: dict) -> dict:
    """Fetch, extract, duplicate-check — BEFORE anything is stored. Raises Declined."""
    from ..rag import docloader
    corpus, why = sources._corpus_for(m["pack"])
    if corpus is None:
        raise Declined(why)
    try:
        raw, fname = fetch_policy.fetch(m["url"])
    except fetch_policy.FetchRefused as exc:
        raise Declined(str(exc)) from None
    except Exception as exc:
        raise Declined(f"could not fetch {m['url']}: {type(exc).__name__}: {exc}") from None
    fname = m.get("filename") or fname
    try:
        text = docloader.extract_text(raw, fname)
    except Exception as exc:
        raise Declined(f"could not extract text from {fname!r}: "
                       f"{type(exc).__name__}: {exc}") from None
    doc_id = hashlib.sha1(text.encode()).hexdigest()[:16]
    existing = corpus.find(doc_id)
    if existing:
        owner = existing["metadata"].get("staged_by")
        if not owner:
            raise Declined(f"this document is already in the {corpus.name} library as "
                           f"doc_id {doc_id} — nothing to contribute")
        if owner != identity.current().id:
            raise Declined("this document is already staged by another contributor")
        raise Declined(f"you already staged this document as contribution "
                       f"{existing['metadata'].get('contribution_id')} — withdraw it "
                       "first, or wait for the review")
    return {"raw": raw, "fname": fname, "text": text, "doc_id": doc_id, "corpus": corpus}


def _stage_document(rec: dict, prepared: dict) -> dict:
    """Ingest with the staged tag; the record exists, so its id rides the metadata."""
    corpus = prepared["corpus"]
    meta = _document_meta(rec["manifest"]) | {"staged_by": rec["contributor_id"],
                                              "contribution_id": rec["contribution_id"]}
    out = corpus.ingest(prepared["text"], meta, raw=prepared["raw"], filename=prepared["fname"])
    return {"doc_id": out["doc_id"], "chunks": out["chunks"], "corpus": corpus.name,
            "passport": meta,
            "how_to_test": (f"ask a question the document should answer, or call "
                            f"corpus_document(doc_id={out['doc_id']!r}); only you and "
                            "reviewers can see it until it is approved")}


def _land_document(rec: dict) -> dict:
    """Approval: the same document, same doc_id, tag removed — visible to all."""
    from ..rag import docloader
    m, preview = rec["manifest"], rec["preview"]
    corpus, why = sources._corpus_for(m["pack"])
    if corpus is None:
        raise Declined(why)
    doc_id = preview["doc_id"]
    raw_path = corpus.raw_path(doc_id)
    if raw_path is not None:
        raw, fname = raw_path.read_bytes(), raw_path.name
    else:  # archive missing (should not happen): re-fetch under the same policy
        raw, fname = fetch_policy.fetch(m["url"])
        fname = m.get("filename") or fname
    text = docloader.extract_text(raw, fname)
    meta = _document_meta(m)
    out = corpus.ingest(text, meta, raw=raw, filename=fname, replace_staged=True)
    if out["doc_id"] != doc_id:
        raise Declined(f"re-extraction produced doc_id {out['doc_id']}, not the staged "
                       f"{doc_id} — the archive no longer matches the preview; reject and resubmit")
    return {"doc_id": out["doc_id"], "chunks": out["chunks"], "corpus": corpus.name,
            "passport": meta}


def _retire_document(rec: dict) -> dict:
    """Rejection or withdrawal: drop the staged index rows; never touch a live doc."""
    m, preview = rec["manifest"], rec.get("preview") or {}
    corpus, _ = sources._corpus_for(m["pack"])
    doc_id = preview.get("doc_id")
    if corpus is None or not doc_id:
        return {"removed": False, "note": "nothing was staged"}
    existing = corpus.find(doc_id)
    if not existing or existing["metadata"].get("staged_by") != rec["contributor_id"]:
        return {"removed": False, "note": "no staged copy of this document remains"}
    out = corpus.remove(doc_id)
    return {"removed": out["found"], "removed_chunks": out["removed_chunks"],
            "raw_archive_kept": out.get("raw_archive_kept", False), "note": _HISTORY}


# --------------------------------------------------------------------------- tables

TABLE_FIELDS = {
    "required": {
        "dataset": "snake_case identifier, e.g. sangkae_river_stage (becomes the feed name)",
        "title": "what the table is",
        "description": "what it holds, how it was produced, and any caveat (say SYNTHETIC if it is)",
        "source": "who produced it (agency, station network, or 'hub demonstration series')",
        "validation": "multi-agency-consensus | peer-reviewed | single-agency | "
                      "official-statistic | unvalidated",
        "license": "e.g. CC-BY-4.0, CC0-1.0, or 'unstated' (silence is not accepted)",
        "vintage": "when the data was produced, YYYY-MM",
        "cadence": "monthly | daily | annual | irregular",
        "columns": "mapping output field -> CSV column header, e.g. {month: Month, stage_m: Stage_m}",
        "units": "units of the numeric columns, in words",
    },
    "optional": {
        "csv_text": "the CSV content itself, header row first — for tables pasted into the "
                    "conversation (up to 200 KB); give this OR url",
        "url": "where the platform can fetch the CSV instead of csv_text (public host)",
        "as_of_field": "the output field holding the row date, so the feed reports as_of",
        "usage_notes": "a few lines the consuming analyst reads on every query (max 500 chars)",
    },
}
CSV_TEXT_CAP = 200 * 1024

# Staged feed rows live here — never in conf/feeds/ — and are rebuilt from the
# contribution records on first use after a restart. feeds.query and
# capabilities consult them through the visibility rule.
STAGED_FEEDS: dict[str, dict] = {}
_STAGED_LOADED = False


def _staged_dir():
    from ..config import get_settings
    d = get_settings().cache_dir / "tables" / "staged"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _validate_table(manifest) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest must be a mapping of provenance fields"]
    m = dict(manifest)
    problems = []
    if m.pop("file", None):
        problems.append("'file' is not accepted over the MCP — the platform cannot read "
                        "your disk; paste the table as csv_text or give a url")
    has_text, has_url = bool(str(m.get("csv_text") or "").strip()), bool(m.get("url"))
    if not (has_text or has_url):
        problems.append("give the table as csv_text (header row first) or a url to fetch it")
    if has_text and has_url:
        problems.append("give csv_text OR url, not both")
    if has_text and len(m["csv_text"].encode()) > CSV_TEXT_CAP:
        problems.append(f"csv_text is larger than {CSV_TEXT_CAP // 1024} KB — give a url instead")
    if has_url:
        problems += fetch_policy.check_url(m["url"])
    from . import tables
    base = {k: v for k, v in m.items() if k not in ("csv_text", "url")}
    problems += [f for f in tables.validate_manifest({**base, "file": "-"})
                 if not f.startswith("file ")]        # a fetched file is checked in prepare()
    unknown = set(base) - set(tables.REQUIRED) - {"as_of_field", "usage_notes"}
    if unknown:
        problems.append(f"unknown fields {sorted(unknown)} — every field is provenance")
    if has_text and isinstance(m.get("columns"), dict):   # pasted: check the header now
        import csv
        import io
        try:
            header = next(csv.reader(io.StringIO(m["csv_text"].lstrip("\ufeff"))))
        except StopIteration:
            header = []
        missing = [c for c in m["columns"].values() if c not in header]
        if missing:
            problems.append(f"CSV header {header} is missing mapped column(s) {missing}")
    return problems


def _table_name_taken(ds: str) -> str | None:
    from ..config import get_settings
    from ..mcp import registry
    _ensure_staged_loaded()
    if ds in registry.FEEDS or (get_settings().feeds_conf_dir / f"{ds}.yml").exists():
        return f"dataset name {ds!r} is already a platform feed — pick another name"
    if ds in STAGED_FEEDS:
        return f"dataset name {ds!r} is already staged — pick another name"
    return None


def _prepare_table(m: dict) -> dict:
    """Get the bytes, check the header against the mapping, check the name."""
    import os
    import tempfile
    from . import tables
    taken = _table_name_taken(m["dataset"])
    if taken:
        raise Declined(taken)
    if str(m.get("csv_text") or "").strip():
        raw = m["csv_text"].encode("utf-8")
    else:
        try:
            raw, _ = fetch_policy.fetch(m["url"])
        except fetch_policy.FetchRefused as exc:
            raise Declined(str(exc)) from None
        except Exception as exc:
            raise Declined(f"could not fetch {m['url']}: {type(exc).__name__}: {exc}") from None
    fd, tmp = tempfile.mkstemp(prefix="staging-", suffix=".csv", dir=_staged_dir())
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    base = {k: v for k, v in m.items() if k not in ("csv_text", "url")}
    problems = tables.validate_manifest({**base, "file": tmp})
    if problems:
        os.unlink(tmp)
        raise Declined("; ".join(problems))
    return {"tmp": tmp, "sha256": hashlib.sha256(raw).hexdigest(), "base": base,
            "rows": max(0, raw.decode("utf-8-sig").count("\n") - (0 if raw.endswith(b"\n") else -1) - 1)}


def _table_row(rec: dict, path: str, sha256: str) -> dict:
    """The registry row a staged table serves through — the same shape tables.add
    writes, pointed at the staged copy and tagged with its owner."""
    m = rec["manifest"]
    return {"title": m["title"], "description": m["description"], "source": m["source"],
            "validation": m["validation"], "residency": "platform-hosted copy (staged)",
            "cadence": m["cadence"], "adapter": "generic_csv", "license": m["license"],
            "vintage": m["vintage"], "status": "available",
            "fetch": {"path": path, "sha256": sha256, "columns": m["columns"],
                      "units": m["units"],
                      **({"as_of_field": m["as_of_field"]} if m.get("as_of_field") else {})},
            "params": {"limit": "rows of series to return (default 12)"},
            **({"usage_notes": m["usage_notes"]} if m.get("usage_notes") else {}),
            "pack": "food-security",
            "staged_by": rec["contributor_id"], "contribution_id": rec["contribution_id"],
            "contributor_label": rec["contributor_label"]}


def _stage_table(rec: dict, prepared: dict) -> dict:
    import os
    ds = rec["manifest"]["dataset"]
    dest = _staged_dir() / f"{rec['contribution_id']}.csv"
    os.replace(prepared["tmp"], dest)
    row = _table_row(rec, str(dest), prepared["sha256"])
    _ensure_staged_loaded()
    STAGED_FEEDS[ds] = row
    return {"dataset": ds, "rows": prepared["rows"], "sha256": prepared["sha256"],
            "staged_copy": str(dest), "row": row,
            "how_to_test": (f"call feeds_query(dataset={ds!r}) — only you and reviewers get "
                            "it until it is approved; then it is a platform feed for everyone")}


def _land_table(rec: dict) -> dict:
    from ..mcp import registry
    from . import tables
    preview = rec["preview"]
    manifest = {**{k: v for k, v in rec["manifest"].items() if k not in ("csv_text", "url")},
                "file": preview["staged_copy"]}
    out = tables.add(manifest)
    if out["status"] == "declined":
        raise Declined("; ".join(out["failures"]))
    registry.reload_declarative_feeds()
    _ensure_staged_loaded()
    STAGED_FEEDS.pop(preview["dataset"], None)
    try:
        import os
        os.unlink(preview["staged_copy"])            # the landed copy is the archive now
    except OSError:
        pass
    return {k: out[k] for k in ("dataset", "rows", "sha256", "archived", "feed_row", "passport")}


def _retire_table(rec: dict) -> dict:
    from pathlib import Path
    preview = rec.get("preview") or {}
    _ensure_staged_loaded()
    STAGED_FEEDS.pop(preview.get("dataset", ""), None)
    path = Path(preview.get("staged_copy") or "")
    if path.is_file():
        retired = path.with_suffix(".retired")
        path.rename(retired)
        return {"removed": True, "staged_copy": f"moved aside to {retired.name}", "note": _HISTORY}
    return {"removed": False, "note": "no staged copy remains"}


def _ensure_staged_loaded() -> None:
    """Rebuild the staged feed rows from pending records after a restart."""
    global _STAGED_LOADED
    if _STAGED_LOADED:
        return
    _STAGED_LOADED = True
    try:
        pending = (store.list_contributions(status="pending", kind="table")
                   + store.list_contributions(status="pending", kind="feed"))
    except Exception:
        return
    for rec in pending:
        row = (rec.get("preview") or {}).get("row")
        ds = (rec.get("preview") or {}).get("dataset")
        if row and ds and ds not in STAGED_FEEDS:
            STAGED_FEEDS[ds] = row


def visible_staged_feed(dataset: str) -> dict | None:
    """The staged row for `dataset` if the current caller may see it, else None
    (an invisible staged feed is indistinguishable from an unknown one)."""
    _ensure_staged_loaded()
    row = STAGED_FEEDS.get(dataset)
    if row is None or not identity.current().may_see(row.get("staged_by")):
        return None
    return row


def staged_feeds_for_caller() -> dict:
    """What capabilities shows: the caller's own staged feeds (reviewers: all)."""
    _ensure_staged_loaded()
    caller = identity.current()
    return {ds: {"title": row.get("title"), "contribution_id": row.get("contribution_id"),
                 "contributor": row.get("contributor_label"), "status": "staged — awaiting review",
                 "usage_notes": row.get("usage_notes")}
            for ds, row in STAGED_FEEDS.items() if caller.may_see(row.get("staged_by"))}


# --------------------------------------------------------------------------- feeds

FEED_FIELDS = {
    "required": {
        "dataset": "snake_case identifier, e.g. nino12_sst (becomes the feed name)",
        "title": "what the feed is, e.g. Nino 1+2 SST anomaly (monthly)",
        "description": "what it measures and where it comes from",
        "source": "publisher, e.g. NOAA PSL",
        "validation": "multi-agency-consensus | peer-reviewed | single-agency | "
                      "official-statistic | unvalidated",
        "residency": "external call-out (the platform reads the upstream at query time)",
        "cadence": "monthly | daily | annual | irregular",
        "adapter": "generic_table (NOAA year-by-12-months text series) | generic_json "
                   "(a JSON API with a record list)",
        "fetch": "adapter settings — generic_table: {url, index_name, units, missing_below?, "
                 "bands?: [{min?, max?, label}]}; generic_json: {url, records_path (dot path "
                 "to the record list), fields (output field -> dot path in a record)}",
    },
    "optional": {
        "usage_notes": "a few lines the consuming analyst reads on every query (max 500 chars)",
        "license": "upstream licence, e.g. public-domain (US government), or 'unstated'",
        "vintage": "version or date of the upstream product, if it has one",
        "sst_basis": "for SST-based indices: the dataset the anomalies rest on (ERSSTv5, OISST)",
    },
}
_FEED_ADAPTERS = ("generic_table", "generic_json")
_FEED_KNOWN = set(FEED_FIELDS["required"]) | set(FEED_FIELDS["optional"])


def _validate_feed(manifest) -> list[str]:
    from . import feedspecs
    if not isinstance(manifest, dict):
        return ["manifest must be a mapping of feed settings"]
    m = dict(manifest)
    problems = []
    if m.pop("file", None) or (isinstance(m.get("fetch"), dict) and m["fetch"].get("path")):
        problems.append("a feed reads an upstream URL; for a file you have, contribute it as a "
                        "table (kind table, csv_text or url)")
    if m.get("adapter") and m["adapter"] not in _FEED_ADAPTERS:
        problems.append(f"adapter must be one of {_FEED_ADAPTERS} over the MCP "
                        "(generic_csv is what a table contribution produces)")
    problems += [f for f in feedspecs.validate_spec(m)
                 if not f.startswith("adapter must be one of")]     # reported above
    unknown = set(m) - _FEED_KNOWN
    if unknown:
        problems.append(f"unknown fields {sorted(unknown)} — every field is provenance")
    url = (m.get("fetch") or {}).get("url") if isinstance(m.get("fetch"), dict) else None
    if url:
        problems += fetch_policy.check_url(url)
    return problems


def _feed_row(rec: dict) -> dict:
    m = rec["manifest"]
    row = {k: v for k, v in m.items() if k != "dataset"}
    # File-loaded rows carry `declarative` (their filename); the adapters key
    # their fetch cache on it, so a staged row names its contribution instead.
    row.update({"status": "available", "pack": "food-security",
                "declarative": f"staged:{rec['contribution_id']}",
                "staged_by": rec["contributor_id"], "contribution_id": rec["contribution_id"],
                "contributor_label": rec["contributor_label"]})
    return row


def _prepare_feed(m: dict) -> dict:
    """The spec must actually answer before it is staged: one live query through
    the adapter, so a wrong index name or a dead URL is refused, not staged."""
    from ..mcp import feeds
    taken = _table_name_taken(m["dataset"])
    if taken:
        raise Declined(taken)
    adapter = feeds.ADAPTERS.get(m["adapter"])
    if adapter is None:
        raise Declined(f"no adapter named {m['adapter']!r}")
    probe = {k: v for k, v in m.items() if k != "dataset"}
    probe.update({"status": "available", "declarative": f"probe:{m['dataset']}"})
    try:
        res = adapter({}, probe)
    except feeds.FeedDecline as exc:
        raise Declined(f"the feed did not answer through {m['adapter']}: {exc.note}") from None
    except Exception as exc:
        raise Declined(f"the feed did not answer through {m['adapter']}: "
                       f"{type(exc).__name__}: {exc}") from None
    if not res.get("records"):
        raise Declined("the feed answered but returned no records — check fetch settings")
    return {"sample": {"as_of": res.get("as_of"), "count": res.get("count"),
                       "summary": res.get("summary"), "last": res["records"][-1]}}


def _stage_feed(rec: dict, prepared: dict) -> dict:
    ds = rec["manifest"]["dataset"]
    _ensure_staged_loaded()
    STAGED_FEEDS[ds] = _feed_row(rec)
    return {"dataset": ds, "sample": prepared["sample"], "row": STAGED_FEEDS[ds],
            "how_to_test": (f"call feeds_query(dataset={ds!r}) — only you and reviewers get it "
                            "until it is approved; then it is a platform feed for everyone")}


def _land_feed(rec: dict) -> dict:
    import yaml
    from ..config import get_settings
    from ..mcp import registry
    m = rec["manifest"]
    url = (m.get("fetch") or {}).get("url")
    problems = fetch_policy.check_url(url) if url else []
    if problems:
        raise Declined("; ".join(problems))
    taken = _table_name_taken(m["dataset"]) if m["dataset"] not in STAGED_FEEDS else None
    if taken:
        raise Declined(taken)
    spec = {**m, "contributed": True}
    path = get_settings().feeds_conf_dir / f"{m['dataset']}.yml"
    if path.exists():
        raise Declined(f"{path.name} already exists — contributions never overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
    registry.reload_declarative_feeds()
    _ensure_staged_loaded()
    STAGED_FEEDS.pop(m["dataset"], None)
    return {"dataset": m["dataset"], "feed_row": str(path),
            "passport": {k: m.get(k) for k in ("source", "validation", "residency", "cadence")}}


def _retire_feed(rec: dict) -> dict:
    _ensure_staged_loaded()
    ds = (rec.get("preview") or {}).get("dataset", "")
    removed = STAGED_FEEDS.pop(ds, None) is not None
    return {"removed": removed, "note": "nothing was written to disk for a staged feed"}


_KINDS = {
    "document": {"validate": _validate_document, "prepare": _prepare_document,
                 "stage": _stage_document, "land": _land_document,
                 "retire": _retire_document, "fields": DOCUMENT_FIELDS,
                 "title": lambda m: str(m.get("title") or "untitled document")},
    "table": {"validate": _validate_table, "prepare": _prepare_table,
              "stage": _stage_table, "land": _land_table, "retire": _retire_table,
              "fields": TABLE_FIELDS,
              "title": lambda m: f"{m.get('title') or 'untitled table'} ({m.get('dataset')})"},
    "feed": {"validate": _validate_feed, "prepare": _prepare_feed,
             "stage": _stage_feed, "land": _land_feed, "retire": _retire_feed,
             "fields": FEED_FIELDS,
             "title": lambda m: f"{m.get('title') or 'untitled feed'} ({m.get('dataset')})"},
}


# --------------------------------------------------------------------------- records

_PUBLIC = ("contribution_id", "kind", "status", "title", "contributor_label",
           "created_at", "updated_at", "preview", "landing", "decision_note",
           "reviewer_label", "problems")


def _public(rec: dict) -> dict:
    return {k: rec[k] for k in _PUBLIC if rec.get(k) is not None}


def _caps(caller: identity.Caller) -> list[str]:
    pending = store.list_contributions(status="pending", contributor_id=caller.id)
    problems = []
    if len(pending) >= PENDING_CAP:
        problems.append(f"you have {len(pending)} contributions awaiting review — the cap "
                        f"is {PENDING_CAP}; wait for a decision or withdraw one")
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    recent = store.list_contributions(contributor_id=caller.id, since=since)
    if len(recent) >= HOURLY_CAP:
        problems.append(f"{len(recent)} submissions in the last hour — the cap is "
                        f"{HOURLY_CAP}; try again later")
    return problems


def _not_reviewer(caller: identity.Caller) -> dict:
    return {"status": "declined",
            "note": (f"{caller.label} is not a reviewer — approving and rejecting "
                     "contributions needs a reviewer identity (GRP_REVIEWERS)")}


# --------------------------------------------------------------------------- API

def submit(kind: str, manifest: dict, caller: identity.Caller | None = None) -> dict:
    """Validate, store as pending, stage for preview. Declines name every problem."""
    caller = caller or identity.current()
    if kind not in _KINDS:
        return {"status": "declined", "kind": kind,
                "problems": [f"unknown kind {kind!r} — over the MCP you can contribute: "
                             + ", ".join(KINDS)
                             + " (feeds, tables and rasters are coming; packs stay on the "
                             "developer path)"]}
    spec = _KINDS[kind]
    problems = spec["validate"](manifest)
    if problems:
        return {"status": "declined", "kind": kind, "problems": problems,
                "fields": spec["fields"]}
    caps = _caps(caller)
    if caps:
        return {"status": "declined", "kind": kind, "problems": caps}
    token = identity.bind(caller)
    try:
        try:
            prepared = spec["prepare"](manifest)      # fetches and checks; stores nothing
        except Declined as exc:
            return {"status": "declined", "kind": kind, "problems": [str(exc)]}
        rec = {"kind": kind, "status": "pending", "manifest": dict(manifest),
               "title": spec["title"](manifest),
               "contributor_id": caller.id, "contributor_label": caller.label}
        ident = store.save_contribution(rec)
        rec = store.load_contribution(ident)
        try:
            preview = spec["stage"](rec, prepared)
        except Exception as exc:                      # the record stays, marked, for audit
            store.update_contribution(ident, {"status": "failed", "problems": [str(exc)]})
            return {"status": "declined", "kind": kind, "contribution_id": ident,
                    "problems": [f"staging failed: {type(exc).__name__}: {exc}"]}
    finally:
        identity.unbind(token)
    rec = store.update_contribution(ident, {"preview": preview})
    _notify(f"New {kind} contribution staged by {caller.label}: "
            f"{rec['title']} (id {ident}) — awaiting review")
    return {**_public(rec), "status": "staged",
            "next": ("test it now — only you and reviewers can see it; a reviewer "
                     "approves or rejects it, and contribute_status shows the decision")}


def status(contribution_id: str | None = None, caller: identity.Caller | None = None) -> dict:
    """No id: the caller's own contributions (reviewers: everything pending).
    With an id: that record, if the caller may see it."""
    caller = caller or identity.current()
    if contribution_id:
        rec = store.load_contribution(contribution_id)
        if rec is None or not (caller.is_reviewer or caller.owns(rec["contributor_id"])):
            return {"status": "declined", "note": f"unknown contribution {contribution_id!r}"}
        return {"status": "ok", "contribution": _public(rec)}
    mine = [_public(r) for r in store.list_contributions(contributor_id=caller.id)]
    out = {"status": "ok", "you": caller.label, "reviewer": caller.is_reviewer,
           "contributions": mine}
    if caller.is_reviewer:
        out["pending_review"] = [_public(r) for r in store.list_contributions(status="pending")]
    return out


def withdraw(contribution_id: str, caller: identity.Caller | None = None) -> dict:
    caller = caller or identity.current()
    rec = store.load_contribution(contribution_id)
    if rec is None or not caller.owns(rec["contributor_id"]):
        return {"status": "declined", "note": f"unknown contribution {contribution_id!r}"}
    if rec["status"] != "pending":
        return {"status": "declined",
                "note": f"contribution {contribution_id} is {rec['status']}, not pending"}
    retired = _KINDS[rec["kind"]]["retire"](rec)
    rec = store.update_contribution(contribution_id, {"status": "withdrawn", "retired": retired})
    return {**_public(rec), "status": "withdrawn", "retired": retired}


def review_list(caller: identity.Caller | None = None, status_filter: str = "pending") -> dict:
    caller = caller or identity.current()
    if not caller.is_reviewer:
        return _not_reviewer(caller)
    if status_filter not in (*STATUSES, "all"):
        return {"status": "declined",
                "note": f"unknown filter {status_filter!r} — one of {', '.join(STATUSES)}, all"}
    rows = store.list_contributions(status=None if status_filter == "all" else status_filter)
    return {"status": "ok", "filter": status_filter, "contributions": [_public(r) for r in rows]}


def approve(contribution_id: str, caller: identity.Caller | None = None,
            note: str | None = None) -> dict:
    caller = caller or identity.current()
    if not caller.is_reviewer:
        return _not_reviewer(caller)
    rec = store.load_contribution(contribution_id)
    if rec is None:
        return {"status": "declined", "note": f"unknown contribution {contribution_id!r}"}
    if rec["status"] != "pending":
        return {"status": "declined",
                "note": f"contribution {contribution_id} is {rec['status']}, not pending"}
    try:
        landing = _KINDS[rec["kind"]]["land"](rec)
    except Declined as exc:
        return {"status": "declined", "note": f"landing failed: {exc}"}
    except Exception as exc:
        return {"status": "declined", "note": f"landing failed: {type(exc).__name__}: {exc}"}
    rec = store.update_contribution(contribution_id, {
        "status": "approved", "landing": landing, "reviewer_id": caller.id,
        "reviewer_label": caller.label, "decision_note": note or "approved"})
    _notify(f"Contribution approved by {caller.label}: {rec['title']} (id {contribution_id})")
    return {**_public(rec), "status": "approved"}


def reject(contribution_id: str, note: str, caller: identity.Caller | None = None) -> dict:
    caller = caller or identity.current()
    if not caller.is_reviewer:
        return _not_reviewer(caller)
    if not str(note or "").strip():
        return {"status": "declined", "note": "a rejection needs a note the contributor can act on"}
    rec = store.load_contribution(contribution_id)
    if rec is None:
        return {"status": "declined", "note": f"unknown contribution {contribution_id!r}"}
    if rec["status"] != "pending":
        return {"status": "declined",
                "note": f"contribution {contribution_id} is {rec['status']}, not pending"}
    retired = _KINDS[rec["kind"]]["retire"](rec)
    rec = store.update_contribution(contribution_id, {
        "status": "rejected", "retired": retired, "reviewer_id": caller.id,
        "reviewer_label": caller.label, "decision_note": note})
    _notify(f"Contribution rejected by {caller.label}: {rec['title']} (id {contribution_id}) — {note}")
    return {**_public(rec), "status": "rejected", "retired": retired}


def _notify(text: str) -> None:
    """Mattermost incoming webhook when configured; never raises, never blocks long."""
    from ..config import get_settings
    url = get_settings().grp_mattermost_webhook.strip()
    if not url:
        return
    try:
        import requests
        requests.post(url, json={"text": text}, timeout=3)
    except Exception:
        pass


# --------------------------------------------------------------------------- tool text

def describe_submit() -> str:
    lines = ["Contribute a source to the platform from this conversation — no server access, "
             "no config files. The submission is validated against the same gate the "
             "platform's own contribution commands use; a clean one is STAGED: stored for "
             "review and served to you (and to reviewers) on every query path — search, "
             "inventory, feeds, evidence packs — under the same relevance ranking as any other "
             "source, so you test exactly what an analyst will see. It goes live for everyone "
             "only when a reviewer approves it.",
             "",
             "kind: one of " + ", ".join(KINDS) + ".",
             "manifest: the provenance fields for that kind. Ask the contributor for anything "
             "missing BEFORE calling; do not invent provenance — 'unvalidated' is an honest "
             "value, a guessed publisher is not."]
    for kind in KINDS:
        fields = _KINDS[kind]["fields"]
        lines.append(f"\n{kind} — required: " + "; ".join(f"{k} ({v})" for k, v in fields["required"].items()))
        lines.append(f"{kind} — optional: " + "; ".join(f"{k} ({v})" for k, v in fields["optional"].items()))
    lines += ["",
              "Returns {status: staged, contribution_id, preview{doc_id,...}, next} (the record "
              "then shows status pending in contribute_status until a reviewer decides) or "
              "{status: declined, problems: [...]} naming every problem at once — fix them all "
              "and resubmit. Rules: contributions never overwrite an existing source; a "
              "document already in the library is declined by its doc_id; a table's or feed's "
              "dataset name must be new. A staged table or feed is queried with "
              "feeds_query(dataset) like any feed and appears under `staged_feeds` in "
              "platform_capabilities for its owner. A feed is test-queried once before it is "
              "staged, so a wrong index name or dead URL is refused with the adapter's reason."]
    return "\n".join(lines)
