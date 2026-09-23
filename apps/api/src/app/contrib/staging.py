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

KINDS = ("document", "table", "feed", "raster", "vector", "weights")
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
        "pack": "which domain pack may cite it: food-security or risk (default food-security)",
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
    unknown = set(base) - set(tables.REQUIRED) - {"as_of_field", "usage_notes", "pack"}
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
            "pack": m.get("pack", "food-security"),
            "cadence": m["cadence"], "adapter": "generic_csv", "license": m["license"],
            "vintage": m["vintage"], "status": "available",
            "fetch": {"path": path, "sha256": sha256, "columns": m["columns"],
                      "units": m["units"],
                      **({"as_of_field": m["as_of_field"]} if m.get("as_of_field") else {})},
            "params": {"limit": "rows of series to return (default 12)"},
            **({"usage_notes": m["usage_notes"]} if m.get("usage_notes") else {}),
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
    try:
        rasters = store.list_contributions(status="pending", kind="raster")
    except Exception:
        return
    for rec in rasters:
        pv = rec.get("preview") or {}
        if pv.get("layer") and pv.get("entry") and pv["layer"] not in STAGED_RASTERS:
            STAGED_RASTERS[pv["layer"]] = {"entry": pv["entry"], "contract": pv.get("contract") or {}}
    try:
        wts = store.list_contributions(status="pending", kind="weights")
    except Exception:
        return
    for rec in wts:
        pv = rec.get("preview") or {}
        if pv.get("hazard") and pv["hazard"] not in STAGED_WEIGHTS:
            STAGED_WEIGHTS[pv["hazard"]] = {
                "weights": pv.get("after") or {}, "staged_by": rec["contributor_id"],
                "contribution_id": rec["contribution_id"],
                "contributor_label": rec.get("contributor_label"),
                "rationale": (rec.get("manifest") or {}).get("rationale"),
                "usage_notes": (rec.get("manifest") or {}).get("usage_notes")}


def visible_staged_feed(dataset: str) -> dict | None:
    """The staged row for `dataset` if the current caller may see it, else None
    (an invisible staged feed is indistinguishable from an unknown one)."""
    _ensure_staged_loaded()
    row = STAGED_FEEDS.get(dataset)
    if row is None or not identity.current().may_see(row.get("staged_by")):
        return None
    return row


def visible_staged_feeds_for_pack(pack: str) -> dict:
    """Staged feed rows bound to `pack` that the current caller may see — so a
    contributor can preview their own feed inside a real answer before review."""
    _ensure_staged_loaded()
    caller = identity.current()
    return {ds: row for ds, row in STAGED_FEEDS.items()
            if row.get("pack") == pack and caller.may_see(row.get("staged_by"))}


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
                 "to the record list), fields (output field -> dot path in a record), "
                 "as_of_field (STRONGLY RECOMMENDED — the output field holding each "
                 "record's timestamp). Without it the platform cannot tell which end "
                 "of your feed is recent, has to serve the last records in publication "
                 "order, and reports no as_of. A USGS feed published newest-first was "
                 "answering 'the latest earthquakes' with the oldest of the month.}",
    },
    "optional": {
        "usage_notes": "a few lines the consuming analyst reads on every query (max 500 chars)",
        "pack": "which domain pack may cite it: food-security or risk (default food-security)",
        "countries": ("which countries this source is about, e.g. [Kenya]. Without "
                      "it the platform treats it as global and cites it in every "
                      "answer for this pack — a Battambang rainfall table was cited "
                      "into a Kenya maize brief before this was declarable."),
        "hazards": ("which hazards this feed speaks to, e.g. [earthquake, tsunami]. "
                    "Without it a risk feed is cited in EVERY risk answer — a flood "
                    "brief carried global earthquakes until these were declared. A "
                    "citation list is a claim about what the answer rests on, so an "
                    "unrelated feed in it dilutes the ones that matter."),
        "brief_role": ("'driver' if this is a seasonal DRIVER a food-security brief "
                       "should cite every time (ENSO/IOD-style indices). Without it a "
                       "contributed food-security feed is queryable but can never "
                       "appear in a brief: it stages, it answers feeds_query, and the "
                       "evidence assembler never reads it. A reviewer decides whether "
                       "a feed has earned a place in every brief."),
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
    if m.get("brief_role") not in (None, "driver"):
        problems.append("brief_role, if given, must be 'driver' — the only role a "
                        "brief selects on today")
    if m.get("brief_role") == "driver" and m.get("pack", "food-security") != "food-security":
        problems.append("brief_role 'driver' is a food-security brief concept; a risk "
                        "feed reaches a risk answer by its `pack` alone")
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
    row.update({"status": "available", "pack": m.get("pack", "food-security"),
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


# --------------------------------------------------------------------------- rasters

RASTER_FIELDS = {
    "required": {
        "layer": "namespaced name: hazard_<name> (classes 1-5), risk_<name>, "
                 "vulnerability_<name> (classes 1-5, weightable via the 'weights' kind), or "
                 "population_<name> (a COUNT grid: people per pixel, float; the pack sums it "
                 "inside each hazard class to report people exposed — no legend needed)",
        "url": "where the platform can fetch the GeoTIFF (public host; up to the size cap)",
        "title": "what the layer is",
        "description": "what a pixel value means and how the layer was produced",
        "source": "who produced it, derived from what",
        "license": "e.g. CC-BY-4.0, or 'unstated' (silence is not accepted)",
        "vintage": "when the layer was produced, YYYY-MM",
        "legend": "mapping class number -> label, e.g. {1: Very Low, ..., 5: Very High} "
                  "(not required for a population_* count grid)",
        "declared": "the CONTRACT the file is verified against: {dtype, valid_min, valid_max, "
                    "nodata?} — say what the file IS; the platform checks it before staging",
    },
    "optional": {
        "usage_notes": "a few lines the consuming analyst reads in the hazard passport (max 500 chars)",
    },
}
_RASTER_KNOWN = set(RASTER_FIELDS["required"]) | set(RASTER_FIELDS["optional"])

# Staged layers live here (and in the contribution records), never in the
# contrib catalog files; graph/geo/tiffs.py and schema.py overlay them for the
# caller who may see them.
STAGED_RASTERS: dict[str, dict] = {}


def _validate_raster(manifest) -> list[str]:
    from . import rasters
    if not isinstance(manifest, dict):
        return ["manifest must be a mapping of provenance fields"]
    m = dict(manifest)
    problems = []
    if m.pop("file", None):
        problems.append("'file' is not accepted over the MCP — the platform cannot read "
                        "your disk; give a url it can fetch")
    if not m.get("url"):
        problems.append("missing required field 'url'")
    base = {k: v for k, v in m.items() if k != "url"}
    problems += [f for f in rasters.validate_manifest({**base, "file": "-"})
                 if not f.startswith("file ")]
    unknown = set(m) - _RASTER_KNOWN
    if unknown:
        problems.append(f"unknown fields {sorted(unknown)} — every field is provenance")
    if m.get("url"):
        problems += fetch_policy.check_url(m["url"])
    return problems


def _purge_clips(layer: str) -> int:
    """Per-place clips are cached by layer NAME; a layer that changes identity
    (staged, landed, retired, removed) must not be served from an old clip."""
    from ..config import get_settings
    settings = get_settings()
    n = 0
    for clip in settings.cache_dir.glob(f"*/{layer}.tif"):
        if clip.parent.resolve() == settings.tiffs_dir.resolve():
            continue
        try:
            clip.unlink()
            n += 1
        except OSError:
            pass
    return n


def _raster_taken(layer: str) -> str | None:
    from ..graph.geo import tiffs
    _ensure_staged_loaded()
    if layer in tiffs.catalog(include_staged=False) or layer in STAGED_RASTERS:
        return f"layer {layer!r} is already in the catalog — contributions add layers, they do not overwrite them"
    return None


def _prepare_raster(m: dict) -> dict:
    import os
    import tempfile
    from ..config import get_settings
    from . import rasters
    taken = _raster_taken(m["layer"])
    if taken:
        raise Declined(taken)
    try:
        raw, _ = fetch_policy.fetch(m["url"])
    except fetch_policy.FetchRefused as exc:
        raise Declined(str(exc)) from None
    except Exception as exc:
        raise Declined(f"could not fetch {m['url']}: {type(exc).__name__}: {exc}") from None
    tiffs_dir = get_settings().tiffs_dir
    tiffs_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="staged-tmp-", suffix=".tif", dir=tiffs_dir)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    base = {k: v for k, v in m.items() if k != "url"}
    out = rasters.add({**base, "file": tmp}, dry_run=True)
    if out["status"] == "declined":
        os.unlink(tmp)
        raise Declined("; ".join(out["failures"]))
    return {"tmp": tmp, "observed": out.get("observed")}


def _stage_raster(rec: dict, prepared: dict) -> dict:
    import os
    from ..config import get_settings
    m = rec["manifest"]
    layer, cid = m["layer"], rec["contribution_id"]
    dest = get_settings().tiffs_dir / f"staged-{cid}.tif"
    os.replace(prepared["tmp"], dest)
    entry = {"local_path": f"tiffs/staged-{cid}.tif", "title": m["title"],
             "description": m["description"], "legend": m["legend"], "source": m["source"],
             "license": m["license"], "vintage": m["vintage"],
             **({"usage_notes": m["usage_notes"]} if m.get("usage_notes") else {}),
             "contributed": True, "staged_by": rec["contributor_id"], "contribution_id": cid,
             "contributor_label": rec["contributor_label"]}
    from . import rasters as _r
    contract = {**m["declared"], "role": _r._role(layer)}
    _ensure_staged_loaded()
    STAGED_RASTERS[layer] = {"entry": entry, "contract": contract}
    _purge_clips(layer)
    short = layer.split("_", 1)[1] if "_" in layer else layer
    return {"layer": layer, "observed": prepared["observed"], "staged_file": str(dest),
            "entry": entry, "contract": contract,
            "how_to_test": (f"assemble_pack(pack='risk', place=<a place the layer covers>, "
                            f"hazard={short!r}) — only you and reviewers can use it until it "
                            "is approved; the pack's hazard passport shows the contract check")}


def _land_raster(rec: dict) -> dict:
    import os
    from . import rasters
    m, preview = rec["manifest"], rec["preview"]
    manifest = {**{k: v for k, v in m.items() if k != "url"}, "file": preview["staged_file"]}
    _ensure_staged_loaded()
    STAGED_RASTERS.pop(m["layer"], None)          # so the gate's own taken-check sees the catalog only
    out = rasters.add(manifest)
    if out["status"] == "declined":
        STAGED_RASTERS[m["layer"]] = {"entry": preview["entry"], "contract": preview["contract"]}
        raise Declined("; ".join(out["failures"]))
    try:
        os.unlink(preview["staged_file"])
    except OSError:
        pass
    _purge_clips(m["layer"])
    return {"layer": out["layer"], "file": out["file"], "verified": out["verified"],
            "passport": out["passport"]}


def _retire_raster(rec: dict) -> dict:
    from pathlib import Path
    preview = rec.get("preview") or {}
    layer = preview.get("layer", "")
    _ensure_staged_loaded()
    STAGED_RASTERS.pop(layer, None)
    _purge_clips(layer)
    path = Path(preview.get("staged_file") or "")
    if path.is_file():
        retired = path.with_suffix(".tif.retired")
        path.rename(retired)
        return {"removed": True, "staged_file": f"moved aside to {retired.name}", "note": _HISTORY}
    return {"removed": False, "note": "no staged file remains"}


def visible_staged_rasters() -> dict:
    """{layer: catalog entry} for the staged layers the current caller may see."""
    _ensure_staged_loaded()
    caller = identity.current()
    return {layer: row["entry"] for layer, row in STAGED_RASTERS.items()
            if caller.may_see(row["entry"].get("staged_by"))}


def visible_staged_contracts() -> dict:
    _ensure_staged_loaded()
    caller = identity.current()
    return {layer: row["contract"] for layer, row in STAGED_RASTERS.items()
            if caller.may_see(row["entry"].get("staged_by"))}


# --------------------------------------------------------------------------- weights

WEIGHTS_FIELDS = {
    "required": {
        "hazard": "which hazard's recipe to adjust, e.g. flood (a return-period layer "
                  "inherits its base hazard's weights)",
        "weights": "mapping of vulnerability layer -> weight, summing to 1.0, e.g. "
                   "{vulnerability_pop_all_total: 0.5, vulnerability_reclass_blddensity: 0.3, "
                   "vulnerability_reclass_road: 0.2}",
        "rationale": "why these weights, in a sentence a reviewer can judge — this is the "
                     "only evidence the number rests on",
    },
    "optional": {
        "usage_notes": "a few lines the consuming analyst reads with every risk level "
                       "computed from these weights (max 500 chars)",
    },
}
_WEIGHT_TOLERANCE = 0.001

# Staged weight adjustments: the contributor's own risk answers use them, nobody
# else's do, until a reviewer approves.
STAGED_WEIGHTS: dict[str, dict] = {}


def available_vulnerability_layers() -> list[str]:
    """The reclassed vulnerability layers a weight may name, read from the live
    raster contracts rather than a hardcoded list."""
    try:
        from ..graph.geo import schema
        doc = schema._doc()
        return sorted(k for k, v in (doc.get("layers") or {}).items()
                      if (v or {}).get("role") == "vulnerability_reclass")
    except Exception:
        return []


def _validate_weights(manifest) -> list[str]:
    from ..graph.geo import combine, tiffs
    if not isinstance(manifest, dict):
        return ["manifest must be a mapping with hazard, weights and rationale"]
    m = dict(manifest)
    problems = []
    unknown = set(m) - set(WEIGHTS_FIELDS["required"]) - set(WEIGHTS_FIELDS["optional"])
    if unknown:
        problems.append(f"unknown fields {sorted(unknown)} — every field is provenance")
    hazard = str(m.get("hazard") or "").strip()
    if not hazard:
        problems.append("missing required field 'hazard'")
    else:
        key = hazard.removeprefix("hazard_")
        if not combine.weights_for(f"hazard_{key}"):
            known = sorted((combine._recipe().get("weights") or {}))
            problems.append(f"no risk recipe for hazard {key!r} — one of: {', '.join(known)}")
        if tiffs.resolve(key) is None:
            problems.append(f"no hazard layer named {key!r} in the catalog")
    if not str(m.get("rationale") or "").strip():
        problems.append("missing required field 'rationale' — a weight with no stated "
                        "reason cannot be reviewed, and a risk level is only as good "
                        "as the reason behind its weights")
    w = m.get("weights")
    if not isinstance(w, dict) or not w:
        problems.append("missing required field 'weights' (layer -> weight)")
    else:
        allowed = set(available_vulnerability_layers())
        for layer, val in w.items():
            if allowed and layer not in allowed:
                problems.append(f"unknown vulnerability layer {layer!r} — one of: "
                                + ", ".join(sorted(allowed)))
            try:
                f = float(val)
            except (TypeError, ValueError):
                problems.append(f"weight for {layer!r} is not a number")
                continue
            if not 0.0 <= f <= 1.0:
                problems.append(f"weight for {layer!r} is {f} — weights run 0 to 1")
        try:
            total = sum(float(v) for v in w.values())
            if abs(total - 1.0) > _WEIGHT_TOLERANCE:
                problems.append(f"weights sum to {total:.3f}, not 1.0 — they are shares of "
                                "one vulnerability score, so they must add up")
        except (TypeError, ValueError):
            pass
    from . import notes
    problems += notes.validate(m.get("usage_notes"))
    # A count grid is summed, never weighted: a population_* layer in a recipe
    # would be read as classes 1-5 and produce risk levels that mean nothing.
    _w = manifest.get("weights") if isinstance(manifest, dict) else None
    if isinstance(_w, dict):
        _bad = [k for k in _w if not str(k).startswith("vulnerability_")]
        if _bad:
            problems.append(f"weights may only name vulnerability_* class layers, not {_bad} "
                            "— a population_* count grid is summed, never weighted")
    return problems


def _prepare_weights(m: dict) -> dict:
    """Nothing to fetch; report what the change actually is, so the record and the
    reviewer both see the before and after rather than only the after."""
    from ..graph.geo import combine
    key = str(m["hazard"]).removeprefix("hazard_")
    before = dict(combine.weights_for(f"hazard_{key}"))
    after = {k: round(float(v), 4) for k, v in m["weights"].items()}
    moved = {k: (before.get(k), after.get(k)) for k in set(before) | set(after)
             if abs(float(after.get(k, 0)) - float(before.get(k, 0))) > _WEIGHT_TOLERANCE}
    if not moved:
        raise Declined("these are already the weights in force for that hazard")
    return {"hazard": key, "before": before, "after": after,
            "changed": {k: {"from": v[0], "to": v[1]} for k, v in moved.items()}}


def _stage_weights(rec: dict, prepared: dict) -> dict:
    _ensure_staged_loaded()
    STAGED_WEIGHTS[prepared["hazard"]] = {
        "weights": prepared["after"], "staged_by": rec["contributor_id"],
        "contribution_id": rec["contribution_id"],
        "contributor_label": rec["contributor_label"],
        "rationale": rec["manifest"].get("rationale"),
        "usage_notes": rec["manifest"].get("usage_notes")}
    return {**prepared,
            "how_to_test": (f"ask a risk question for a place and hazard {prepared['hazard']!r} — "
                            "your risk levels use these weights; everyone else's still use the "
                            "ones in force until a reviewer approves")}


def _land_weights(rec: dict) -> dict:
    import yaml
    from ..config import get_settings
    pv = rec["preview"]
    path = get_settings().risk_l2_contrib_path
    doc = {}
    if path.exists():
        doc = yaml.safe_load(path.read_text()) or {}
    doc.setdefault("weights", {})[pv["hazard"]] = pv["after"]
    doc.setdefault("adjusted", {})[pv["hazard"]] = {
        "by": rec["contributor_label"], "contribution_id": rec["contribution_id"],
        "rationale": rec["manifest"].get("rationale"),
        "replaced": pv["before"],
        **({"usage_notes": rec["manifest"]["usage_notes"]}
           if rec["manifest"].get("usage_notes") else {})}
    path.write_text(
        "# Hub-adjusted Layer-2 vulnerability weights \u2014 machine-owned, written by\n"
        "# the contribution gate on approval. Hand-edit conf/risk_l2.yml, never this.\n"
        + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    _ensure_staged_loaded()
    STAGED_WEIGHTS.pop(pv["hazard"], None)
    return {"hazard": pv["hazard"], "weights": pv["after"], "replaced": pv["before"],
            "file": str(path)}


def _retire_weights(rec: dict) -> dict:
    _ensure_staged_loaded()
    hz = (rec.get("preview") or {}).get("hazard", "")
    return {"removed": STAGED_WEIGHTS.pop(hz, None) is not None,
            "note": "nothing was written to the recipe for a staged adjustment"}


def visible_staged_weights(hazard: str) -> dict | None:
    """Staged weights for `hazard` if the current caller may see them. This is what
    makes a weight adjustment previewable: its author's own risk levels use it."""
    _ensure_staged_loaded()
    row = STAGED_WEIGHTS.get(str(hazard).removeprefix("hazard_"))
    if row is None or not identity.current().may_see(row.get("staged_by")):
        return None
    return row



# --------------------------------------------------------------------------- vectors

VECTOR_FIELDS = {
    "required": {
        "layer": "a short snake_case name the risk pack will count under, e.g. "
                 "evacuation_centres — becomes a countable asset beside hospitals/schools",
        "url": "where the platform can fetch a GeoJSON FeatureCollection of Point features "
               "in EPSG:4326 (lon, lat); public host, up to the size cap",
        "title": "what the points are",
        "description": "what one point represents, how the list was compiled, what it excludes",
        "source": "who maintains the list (agency), derived from what",
        "license": "e.g. CC-BY-4.0, or 'unstated' (silence is not accepted)",
        "vintage": "when the list was last updated, YYYY-MM",
    },
    "optional": {
        "countries": "which countries the layer covers, e.g. [Thailand]",
        "name_field": "the property holding each point's name, if any",
        "usage_notes": "a few lines the consuming analyst reads (max 500 chars)",
    },
}
_VECTOR_KNOWN = set(VECTOR_FIELDS["required"]) | set(VECTOR_FIELDS["optional"])
STAGED_VECTORS: dict[str, dict] = {}
_STAGED_VECTORS_LOADED = False


def _ensure_staged_vectors_loaded() -> None:
    global _STAGED_VECTORS_LOADED
    if _STAGED_VECTORS_LOADED:
        return
    _STAGED_VECTORS_LOADED = True
    try:
        pending = store.list_contributions(status="pending", kind="vector")
    except Exception:
        return
    for rec in pending:
        pv = rec.get("preview") or {}
        if pv.get("layer") and pv.get("entry") and pv["layer"] not in STAGED_VECTORS:
            STAGED_VECTORS[pv["layer"]] = pv["entry"]


def _vector_taken(layer: str) -> str | None:
    import re
    from ..graph.geo import ingest, vectors
    if not re.fullmatch(vectors.NAME_RE, layer or ""):
        return "layer must be snake_case: lowercase letters, digits, underscores, 3-40 chars"
    if layer in ingest.ASSET_LAYERS or layer in ("admin",):
        return f"layer {layer!r} is a built-in OSM layer — pick another name"
    _ensure_staged_vectors_loaded()
    if layer in vectors.landed() or layer in STAGED_VECTORS:
        return f"layer {layer!r} is already contributed — contributions add layers, they do not overwrite them"
    return None


def _validate_vector(manifest) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest must be a mapping of provenance fields"]
    m = dict(manifest)
    problems = []
    if m.pop("file", None):
        problems.append("'file' is not accepted over the MCP — the platform cannot read "
                        "your disk; give a url it can fetch")
    for k in VECTOR_FIELDS["required"]:
        if not m.get(k):
            problems.append(f"missing required field '{k}'")
    taken = _vector_taken(str(m.get("layer") or ""))
    if taken and m.get("layer"):
        problems.append(taken)
    unknown = set(m) - _VECTOR_KNOWN
    if unknown:
        problems.append(f"unknown fields {sorted(unknown)} — every field is provenance")
    from . import notes
    problems += notes.validate(m.get("usage_notes"))
    if m.get("url"):
        problems += fetch_policy.check_url(m["url"])
    return problems


def _prepare_vector(m: dict) -> dict:
    import os
    import tempfile
    from ..graph.geo import vectors
    taken = _vector_taken(m["layer"])
    if taken:
        raise Declined(taken)
    try:
        raw, _ = fetch_policy.fetch(m["url"])
    except fetch_policy.FetchRefused as exc:
        raise Declined(str(exc)) from None
    except Exception as exc:
        raise Declined(f"could not fetch {m['url']}: {type(exc).__name__}: {exc}") from None
    try:
        observed = vectors.inspect(raw)
    except ValueError as exc:
        raise Declined(f"{m['url']}: {exc}") from None
    vdir = vectors._dir()
    fd, tmp = tempfile.mkstemp(prefix="staged-tmp-", suffix=".geojson", dir=vdir)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return {"tmp": tmp, "observed": observed}


def _stage_vector(rec: dict, prepared: dict) -> dict:
    import os
    from ..graph.geo import vectors
    m = rec["manifest"]
    layer, cid = m["layer"], rec["contribution_id"]
    dest = vectors._dir() / f"staged-{cid}.geojson"
    os.replace(prepared["tmp"], dest)
    entry = {"local_path": f"vectors/staged-{cid}.geojson", "title": m["title"],
             "description": m["description"], "source": m["source"],
             "license": m["license"], "vintage": m["vintage"],
             "features": prepared["observed"]["features"],
             "bbox": prepared["observed"]["bbox"],
             **({"countries": m["countries"]} if m.get("countries") else {}),
             **({"name_field": m["name_field"]} if m.get("name_field") else {}),
             **({"usage_notes": m["usage_notes"]} if m.get("usage_notes") else {}),
             "contributed": True, "staged_by": rec["contributor_id"], "contribution_id": cid,
             "contributor_label": rec["contributor_label"]}
    _ensure_staged_vectors_loaded()
    STAGED_VECTORS[layer] = entry
    vectors.purge_clips(layer)
    return {"layer": layer, "observed": prepared["observed"], "staged_file": str(dest),
            "entry": entry,
            "how_to_test": (f"assemble_pack(pack='risk', place=<a place inside the layer's "
                            f"bbox>, hazard='flood') — the pack now counts {layer!r} against "
                            "the hazard beside hospitals and schools; only you and reviewers "
                            "see it until it is approved")}


def _land_vector(rec: dict) -> dict:
    import os
    from ..graph.geo import vectors
    m, preview = rec["manifest"], rec["preview"]
    layer = m["layer"]
    src = vectors.master_path(preview["entry"])
    dest = vectors._dir() / f"{layer}.geojson"
    if dest.exists():
        raise Declined(f"layer {layer!r} already landed")
    os.replace(src, dest)
    entry = {k: v for k, v in preview["entry"].items()
             if k not in ("staged_by", "contributor_label")}
    entry["local_path"] = f"vectors/{layer}.geojson"
    if rec.get("auto_approved"):
        entry["review"] = "auto-approved — no human reviewed this layer"
    vectors.register(layer, entry)
    _ensure_staged_vectors_loaded()
    STAGED_VECTORS.pop(layer, None)
    vectors.purge_clips(layer)
    return {"layer": layer, "file": str(dest), "features": entry.get("features")}


def _retire_vector(rec: dict) -> dict:
    from pathlib import Path
    from ..graph.geo import vectors
    preview = rec.get("preview") or {}
    layer = preview.get("layer", "")
    _ensure_staged_vectors_loaded()
    STAGED_VECTORS.pop(layer, None)
    vectors.unregister(layer)
    vectors.purge_clips(layer)
    path = Path(preview.get("staged_file") or "")
    if path.is_file():
        retired = path.with_suffix(".geojson.retired")
        path.rename(retired)
        return {"removed": True, "staged_file": f"moved aside to {retired.name}", "note": _HISTORY}
    return {"removed": False, "note": "no staged file remains"}


def visible_staged_vectors() -> dict:
    """{layer: entry} for the staged point layers the current caller may see."""
    _ensure_staged_vectors_loaded()
    caller = identity.current()
    return {layer: e for layer, e in STAGED_VECTORS.items()
            if caller.may_see(e.get("staged_by"))}

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
    "raster": {"validate": _validate_raster, "prepare": _prepare_raster,
               "stage": _stage_raster, "land": _land_raster, "retire": _retire_raster,
               "fields": RASTER_FIELDS,
               "title": lambda m: f"{m.get('title') or 'untitled layer'} ({m.get('layer')})"},
    "vector": {"validate": _validate_vector, "prepare": _prepare_vector,
               "stage": _stage_vector, "land": _land_vector, "retire": _retire_vector,
               "fields": VECTOR_FIELDS,
               "title": lambda m: f"{m.get('title') or 'untitled point layer'} ({m.get('layer')})"},
    "weights": {"validate": _validate_weights, "prepare": _prepare_weights,
                "stage": _stage_weights, "land": _land_weights, "retire": _retire_weights,
                "fields": WEIGHTS_FIELDS,
                "title": lambda m: f"vulnerability weights for {m.get('hazard')}"},
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
    """Why this caller cannot review, and what would actually change that.

    The old note named the environment variable and stopped, so a reviewer read
    "needs a reviewer identity (GRP_REVIEWERS)" and had no way to act on it. UAT
    found the queue unreachable from chat entirely: with an allowlist set, the only
    identity mechanism is an HTTP header no chat client can send, and nothing said
    so. A gate nobody can reach is not a gate, it is a wall.
    """
    from ..config import get_settings
    s = get_settings()
    listed = [r.strip() for r in s.grp_reviewers if r.strip()]
    how = []
    if listed:
        how.append(f"this instance recognises {', '.join(listed)} as reviewers, and "
                   f"you are {caller.label!r} (identified by: {caller.source})")
        if not s.grp_oauth_enabled:
            how.append("OAuth is off here, so an identity comes from the "
                       "`X-GRP-Dev-Identity` request header — which a chat client "
                       "cannot set. To review from a chat client on this instance, "
                       "add your identity to GRP_REVIEWERS and restart, or set "
                       "GRP_REVIEWERS empty so the local operator reviews")
        else:
            how.append("sign in as one of those identities; the reviewer is taken "
                       "from your token subject")
    else:
        how.append("GRP_REVIEWERS is empty, so the local operator reviews — you are "
                   f"being seen as {caller.label!r} via {caller.source}, which is not "
                   "the local operator")
    return {"status": "declined",
            "note": f"{caller.label} is not a reviewer. " + ". ".join(how) + ".",
            "reviewers_configured": listed or None,
            "you_are": {"id": caller.id, "label": caller.label, "source": caller.source}}


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
    from ..config import get_settings
    if get_settings().grp_auto_approve:
        # A live session: land it now, and let the record, the reply and the
        # citation all say that no human reviewed it. Landing failure leaves it
        # staged for a reviewer rather than pretending.
        try:
            landing = spec["land"]({**rec, "auto_approved": True})
        except Exception as exc:
            _notify(f"Auto-approve could not land {kind} {ident}: {exc}")
            return {**_public(rec), "status": "staged",
                    "auto_approve": f"staging succeeded but landing failed ({exc}); "
                                    "left staged for a reviewer"}
        rec = store.update_contribution(ident, {
            "status": "approved", "landing": landing, "reviewer_id": "auto-approve",
            "reviewer_label": "auto-approve (GRP_AUTO_APPROVE on — no human reviewed this)",
            "decision_note": "auto-approved: this deployment lands contributions without review"})
        _notify(f"New {kind} contribution AUTO-APPROVED for {caller.label}: "
                f"{rec['title']} (id {ident})")
        return {**_public(rec), "status": "approved",
                "review": "AUTO-APPROVED — this deployment is configured to land "
                          "contributions without human review; the record says so",
                "next": "it is live for every caller now — ask a question that uses it"}
    _notify(f"New {kind} contribution staged by {caller.label}: "
            f"{rec['title']} (id {ident}) — awaiting review")
    return {**_public(rec), "status": "staged",
            "next": ("test it now — only you and reviewers can see it; a reviewer "
                     "approves or rejects it, and contribute_status shows the decision")}


def _landed_state(rec: dict) -> dict:
    """Is this APPROVED contribution actually being served?

    An approved contribution whose landing file never appeared is silently
    ignored — the read paths swallow a missing file, and nothing reconciles the
    approval ledger against what the platform serves. So a reviewer approves
    something, the ledger says approved, and no answer ever changes. Nobody could
    even ask the question: the ledger is reviewer-only and per-id status is
    owner-only, while an approved contribution is public by definition.
    """
    kind, m = rec.get("kind"), rec.get("manifest") or {}
    try:
        if kind == "document":
            from ..rag.store import Corpus
            from . import sources
            corpus, why = sources._corpus_for(m.get("pack") or "food-security")
            if corpus is None:
                return {"live": False, "why": why}
            landing = rec.get("landing") or {}
            doc_id = landing.get("doc_id") or rec.get("doc_id")
            if doc_id:
                # The landing record names its own corpus; a document contributed
                # to risk must be looked for in the risk library, not in whichever
                # one the manifest's pack happened to resolve to.
                name = landing.get("corpus")
                if name:
                    from ..rag.store import Corpus as _C
                    try:
                        corpus = _C(name)
                    except Exception:
                        pass
                found = corpus.find(doc_id) is not None
                return {"live": found,
                        "why": None if found else
                               f"doc_id {doc_id} is not in the {name or 'target'} library"}
            return {"live": None, "why": "no landed doc_id recorded"}
        if kind in ("table", "feed"):
            from ..mcp import registry
            ds = m.get("dataset")
            row = registry.FEEDS.get(ds)
            return {"live": bool(row), "why": None if row else
                    f"no registry row named {ds!r} — the landing file is missing or unreadable"}
        if kind == "raster":
            from ..graph.geo import tiffs
            layer = m.get("layer")
            cat = tiffs.catalog(include_staged=False)
            return {"live": layer in cat, "why": None if layer in cat else
                    f"no catalog row named {layer!r}"}
        if kind == "weights":
            from ..graph.geo import combine
            adj = combine.adjustment_for("hazard_" + str(m.get("hazard") or ""))
            return {"live": bool(adj), "why": None if adj else
                    "no adjusted block is in force for this hazard"}
    except Exception as exc:                                # pragma: no cover
        return {"live": None, "why": f"could not check ({type(exc).__name__}: {exc})"}
    return {"live": None, "why": f"no reconciliation for kind {kind!r}"}


def reconcile(caller: identity.Caller | None = None) -> dict:
    """Every APPROVED contribution, and whether the platform is actually serving it.

    Open to anyone: an approved contribution is public, and "is what we approved
    being used?" is a question a hub lead must be able to ask without being a
    reviewer.
    """
    caller = caller or identity.current()
    rows = store.list_contributions(status="approved")
    live, inert, unknown = [], [], []
    for r in rows:
        st = _landed_state(r)
        # Open to anyone, so it must not enumerate people. _public carries the
        # contributor and reviewer labels — real identities on a deployment — and
        # the preview blob repeats them inside its passport.
        pub = {k: v for k, v in _public(r).items()
               if k not in ("contributor_label", "reviewer_label", "preview",
                            "contributor_id", "reviewer_id")}
        entry = {**pub, "serving": st.get("live"), "why": st.get("why")}
        (live if st.get("live") else inert if st.get("live") is False else unknown).append(entry)
    return {"status": "ok", "approved": len(rows),
            "serving": len(live), "approved_but_inert": len(inert),
            "unverifiable": len(unknown),
            "contributions": {"serving": live, "inert": inert, "unverifiable": unknown},
            "note": ("an approved contribution that is not being served changed "
                     "nothing: the approval succeeded and the landing did not"
                     if inert else
                     "every approved contribution is being served")}


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
              "staged, so a wrong index name or dead URL is refused with the adapter's reason. "
              "A raster is fetched and verified against its declared contract before staging; "
              "staged, it is a hazard the risk pack can assemble against for its owner."]
    return "\n".join(lines)
