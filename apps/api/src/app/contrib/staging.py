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

import re

import hashlib
import math
import os
from datetime import datetime, timedelta, timezone

from ..mcp import store
from . import fetch_policy, identity, sources

KINDS = ("document", "table", "feed", "raster", "vector", "weights", "amend")
STATUSES = ("pending", "approved", "rejected", "withdrawn", "failed")
PENDING_CAP = 20               # open contributions per contributor
# Submissions per contributor per rolling hour: GRP_CONTRIB_HOURLY_CAP (config.py).

_HISTORY = ("removal stops future use, it does not rewrite history — packs and "
            "receipts minted while the preview was live stay replayable")


class Declined(Exception):
    """A submission the platform will not stage; the message says why."""


# --------------------------------------------------------------------------- kinds

DOCUMENT_FIELDS = {
    "required": {
        "pack": "the domain pack whose library receives it (e.g. food-security)",
        "url": "where the platform can fetch the original — the PDF/DOCX/TXT file itself, not "
               "the page about it (http/https, public host; a Google Drive share link works "
               "when the file is shared with Anyone with the link, and so does a Google Docs "
               "document link shared that way — it is fetched as an export)",
        "source": "publisher, e.g. NOAA CPC",
        "title": "the document's title",
        "pub_date": "publication date as YYYY-MM or YYYY-MM-DD, quoted as text — or 'unstated' "
                    "when the document does not say (an honest 'unstated' beats a guessed date)",
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
    meta |= {k: m[k] for k in ("event", "countries", "crops", "doc_type", "usage_notes", "review")
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
    # Checked on the bytes before a parser is chosen by the name: a landing page, or
    # a challenge page served at a .pdf URL, read as "unreadable PDF ... Stream has
    # ended unexpectedly", which told the contributor nothing.
    if docloader.is_web_page(raw):
        raise Declined(f"{m['url']} returned a web page, not a document — give the PDF's own "
                       "link (the file, not the page about it), or download it and share it "
                       "from Google Drive (Anyone with the link). A web page whose text IS the "
                       "document is not accepted: save it as PDF or text and share that")
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
    return {"raw": raw, "fname": fname, "text": text, "doc_id": doc_id, "corpus": corpus,
            "emails": len(set(_EMAIL.findall(text)))}


# Personal data in a document's text: staff names come with their addresses.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def _stage_document(rec: dict, prepared: dict) -> dict:
    """Ingest with the staged tag; the record exists, so its id rides the metadata."""
    corpus = prepared["corpus"]
    meta = _document_meta(rec["manifest"]) | {"staged_by": rec["contributor_id"],
                                              "contribution_id": rec["contribution_id"]}
    out = corpus.ingest(prepared["text"], meta, raw=prepared["raw"], filename=prepared["fname"])
    # corpus_document reads one pack's library and defaults to food-security, so the
    # call must name the library this went to or it answers "unknown doc_id".
    return {"doc_id": out["doc_id"], "chunks": out["chunks"], "corpus": corpus.name,
            "passport": meta,
            "how_to_test": (f"ask a question the document should answer, or call "
                            f"corpus_document(doc_id={out['doc_id']!r}, "
                            f"pack={rec['manifest']['pack']!r}); only you and "
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
    note = _review_note(rec, "document")
    if note:
        meta["review"] = note
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
        "url": "where the platform can fetch the CSV instead of csv_text (the file's own link "
               "on a public host, or a Google Drive share link; UTF-8, Windows-1252 or Mac "
               "Roman text — the preview says which was read)",
        "countries": "which countries the table's rows speak to, e.g. [Costa Rica]. Without it "
                     "the table is treated as global and every answer of its pack reads it — a "
                     "Gulf of Nicoya soil-carbon factor was applied to Ecuador and Brazil before "
                     "this was declarable",
        "bbox": "[minLon, minLat, maxLon, maxLat] (EPSG:4326) of the area the rows were measured "
                "in, e.g. [-85.4, 9.6, -84.6, 10.4] for the Gulf of Nicoya",
        "as_of_field": "the output field holding the row date, so the feed reports as_of",
        "usage_notes": "a few lines the consuming analyst reads on every query (max 500 chars)",
        "pack": "which domain pack may cite it (default food-security): one of the packs that "
                "read tables — the decline text names the live list. A table bound to "
                "land-cover whose columns map `class` (a legend label) and `factor` (a number "
                "per hectare of that class; optional `unit`, `pool`, `basis`, `low`, `high`) "
                "is a FACTOR TABLE: every land-cover answer multiplies its class areas, losses "
                "and gains by it and cites the arithmetic",
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
    problems = _pack_binding_problems(m.get("pack"), "table")
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
    unknown = (set(base) - set(tables.REQUIRED) - set(tables.SCOPE)
               - {"as_of_field", "usage_notes", "pack"})
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
    from ..rag import docloader
    if str(m.get("csv_text") or "").strip():
        raw = m["csv_text"].encode("utf-8")
    else:
        try:
            raw, _ = fetch_policy.fetch(m["url"])
        except fetch_policy.FetchRefused as exc:
            raise Declined(str(exc)) from None
        except Exception as exc:
            raise Declined(f"could not fetch {m['url']}: {type(exc).__name__}: {exc}") from None
        if docloader.is_web_page(raw):
            raise Declined(f"{m['url']} returned a web page, not a CSV file — give the file's own "
                           "download link, or download it and share it from Google Drive "
                           "(Anyone with the link)")
    # Read in the encoding it was written in; a decode error must never escape as a
    # raw tool error ("'utf-8' codec can't decode byte 0x87") — Mac exports are common.
    text, enc = tables.decode(raw)
    fd, tmp = tempfile.mkstemp(prefix="staging-", suffix=".csv", dir=_staged_dir())
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    base = {k: v for k, v in m.items() if k not in ("csv_text", "url")}
    problems = tables.validate_manifest({**base, "file": tmp})
    if problems:
        os.unlink(tmp)
        raise Declined("; ".join(problems))
    return {"tmp": tmp, "sha256": hashlib.sha256(raw).hexdigest(), "base": base,
            "rows": tables.count_rows(text), "encoding": enc}


def _table_row(rec: dict, path: str, sha256: str, encoding: str = "utf-8") -> dict:
    """The registry row a staged table serves through — the same shape tables.add
    writes, pointed at the staged copy and tagged with its owner."""
    from . import tables
    m = rec["manifest"]
    return {"title": m["title"], "description": m["description"], "source": m["source"],
            "validation": m["validation"], "residency": "platform-hosted copy (staged)",
            "pack": m.get("pack", "food-security"),
            "cadence": m["cadence"], "adapter": "generic_csv", "license": m["license"],
            "vintage": m["vintage"], "status": "available",
            "fetch": {"path": path, "sha256": sha256, "columns": m["columns"],
                      "units": m["units"],
                      **({"encoding": encoding} if encoding != "utf-8" else {}),
                      **({"as_of_field": m["as_of_field"]} if m.get("as_of_field") else {})},
            "params": tables.params_doc(),
            # `countries` / `bbox`: where the rows speak — the packs read them first
            **tables.scope_fields(m),
            **({"usage_notes": m["usage_notes"]} if m.get("usage_notes") else {}),
            "staged_by": rec["contributor_id"], "contribution_id": rec["contribution_id"],
            "contributor_label": rec["contributor_label"]}


def _stage_table(rec: dict, prepared: dict) -> dict:
    import os
    from . import tables
    ds = rec["manifest"]["dataset"]
    dest = _staged_dir() / f"{rec['contribution_id']}.csv"
    os.replace(prepared["tmp"], dest)
    enc = prepared.get("encoding") or "utf-8"
    row = _table_row(rec, str(dest), prepared["sha256"], enc)
    _ensure_staged_loaded()
    STAGED_FEEDS[ds] = row
    hints = ([] if enc == "utf-8" else
             [f"the file is not UTF-8; it was read as {tables.ENCODING_NAMES.get(enc, enc)} "
              f"({enc}) — check that accented names read right in feeds_query, and say so "
              "if they do not"])
    if not tables.scope_fields(rec["manifest"]):
        hints.append("no countries or bbox declared, so every answer of its pack reads this "
                     "table wherever it is asked about — declare where its rows speak if they "
                     "are not global")
    return {"dataset": ds, "rows": prepared["rows"], "sha256": prepared["sha256"],
            "encoding": enc, "staged_copy": str(dest), "row": row,
            **({"hints": hints} if hints else {}),
            "how_to_test": (f"call feeds_query(dataset={ds!r}) — or filter it, e.g. "
                            f"feeds_query(dataset={ds!r}, params={{<column>: <value>, "
                            "limit: 5}}); only you and reviewers get "
                            "it until it is approved; then it is a platform feed for everyone")}


def _land_table(rec: dict) -> dict:
    from ..mcp import registry
    from . import tables
    preview = rec["preview"]
    manifest = {**{k: v for k, v in rec["manifest"].items() if k not in ("csv_text", "url")},
                "file": preview["staged_copy"]}
    out = tables.add(manifest, review=_review_note(rec, "table"))
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
            if pv["entry"].get("stack"):
                _stage_stack_row(pv["entry"], rec["contributor_id"], rec["contribution_id"])
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
    try:
        amends = store.list_contributions(status="pending", kind="amend")
    except Exception:
        return
    for rec in amends:
        pv = rec.get("preview") or {}
        if pv.get("layer") and pv["layer"] not in STAGED_AMENDS:
            STAGED_AMENDS[pv["layer"]] = {
                "target_kind": pv.get("target_kind"), "fields": pv.get("after") or {},
                "staged_by": rec["contributor_id"], "contribution_id": rec["contribution_id"],
                "contributor_label": rec.get("contributor_label"),
                "rationale": (rec.get("manifest") or {}).get("rationale")}


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
        "pack": "which domain pack may cite it (default food-security): one of the packs that "
                "read feeds — the decline text names the live list",
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


def _pack_binding_problems(pack, noun: str) -> list[str]:
    """`pack` names who may cite this source. Checked against the packs that exist
    AND cite sources: a feed bound to 'land-cover', to a contributed pack id, or to
    a typo ('Risk') used to stage, land, report live=true — and never be cited by
    anyone, with nothing saying why."""
    from ..mcp import packs as _packs
    pk = str(pack or "food-security").strip()
    citing = sorted(p for p, sp in _packs.PACKS.items() if sp.get("cites_sources"))
    if pk not in _packs.PACKS:
        return [f"unknown pack {pk!r} — one of: {', '.join(citing)}"]
    if pk not in citing:
        return [f"the {pk} pack cites no feeds or tables, so this {noun} would land and never "
                f"be read — bind it to one of: {', '.join(citing)}"]
    return []


def _review_note(rec: dict, noun: str) -> str | None:
    """The sentence an auto-approved contribution carries into every citation built
    on it. It used to be written for point layers only; an auto-approved hazard
    raster, table, feed, document or recipe cited with the same passport as a
    reviewed one."""
    return f"auto-approved — no human reviewed this {noun}" if rec.get("auto_approved") else None


def _validate_feed(manifest) -> list[str]:
    from . import feedspecs
    if not isinstance(manifest, dict):
        return ["manifest must be a mapping of feed settings"]
    m = dict(manifest)
    problems = _pack_binding_problems(m.get("pack"), "feed")
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
    note = _review_note(rec, "feed")
    spec = {**m, "contributed": True, **({"review": note} if note else {})}
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


# --------------------------------------------------------------------------- test hints

def _inside(bbox, lon, lat) -> bool:
    """Does an extent {lon: [w, e], lat: [s, n]} hold the centre of `bbox`?"""
    if not (lon and lat and bbox):
        return False
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    return lon[0] <= cx <= lon[1] and lat[0] <= cy <= lat[1]


def _where_to_test(name: str, bbox, arg: str, family: str | None = None) -> str:
    """The assemble_pack call that exercises a contributed layer WHERE IT LIES.

    A pack can only read a layer where the pack's own data reaches: the risk pack
    where a hazard layer in the catalog declares an extent holding the layer — and
    then with that real hazard — the land-cover pack where a registered stack's
    extent holds it. Both are read from live state, so a hub's hazard layer or stack
    extends where this points. The old hint sent a Costa Rica point layer to the
    risk pack with hazard='flood' (it failed after two minutes) and a risk_* raster
    in as its own "hazard" (unknown hazard). With neither, it says so."""
    box = [float(v) for v in bbox] if bbox and len(bbox) == 4 else None
    where = (f"lon {box[0]:.2f} to {box[2]:.2f}, lat {box[1]:.2f} to {box[3]:.2f}" if box
             else "the area the layer covers")
    area = (f"place=<a place inside {where}> (or geometry=<a bbox or polygon inside "
            f"{[round(v, 3) for v in box] if box else 'it'}>)")
    use = f"{arg}=[{name!r}]"
    if family == "points":
        does = (f" — the pack counts {name!r} against the hazard beside hospitals and schools")
    elif family in ("lines", "polygons"):
        does = (f" — the pack states the {'km of line' if family == 'lines' else 'km² (ha) of polygon'}"
                " inside its boundary")
    else:
        does = f" — the pack returns the layer's values over its area in layers.{name}"
    try:
        from ..graph.geo import tiffs
        from ..mcp.packs import _is_synthetic
        for k, row in tiffs.catalog().items():
            ext = (row or {}).get("extent") or {}
            if (k.startswith("hazard_") and not (row or {}).get("internal")
                    and not _is_synthetic(row or {})
                    and _inside(box, ext.get("lon"), ext.get("lat"))):
                return (f"assemble_pack(pack='risk', {area}, hazard={k.removeprefix('hazard_')!r}"
                        + ("" if family == "points" else f", {use}") + ")" + does)
    except Exception:
        pass
    try:
        from ..graph.geo import stacks as _stacks
        from ..land_cover.synthesis import landcover_stacks
        for sid, row in landcover_stacks().items():
            ext = row.get("extent") or {}
            if _inside(box, ext.get("lon"), ext.get("lat")):
                ys = sorted(_stacks.years(row) or row.get("years") or [])
                year = ys[-1] if ys else "<a mapped year>"
                return (f"assemble_pack(pack='land-cover', {area}, year={year}, stack={sid!r}, "
                        f"{use})" + (" — the pack counts it inside its area" if family == "points"
                                     else does))
    except Exception:
        pass
    return (f"no pack with an area reaches {where} yet: the risk pack reads a layer where a "
            "hazard layer in the catalog covers it, the land-cover pack where a land-cover "
            f"stack does, and none covers this one — {name!r} is stored and listed, and is "
            "read the day one does (contribute a stack or a hazard layer for the area)")


def _raster_bounds(path) -> list | None:
    try:
        from ..graph.geo import stacks as _stacks
        return _stacks.header(str(path))["bounds"]
    except Exception:
        return None


# --------------------------------------------------------------------------- rasters

def _raster_layer_text() -> str:
    """The `layer` field's text, derived from the schema's namespace table — the same
    table the gate enforces, so the form never lists a namespace the gate refuses or
    omits one it takes."""
    try:
        from . import rasters
        return "namespaced name, one of: " + rasters.namespace_text()
    except Exception:                             # the form text never fails an import
        return "namespaced name (see the namespaces table in conf/raster_schema.yml)"


RASTER_FIELDS = {
    "required": {
        "layer": _raster_layer_text(),
        "url": "where the platform can fetch the raster (GeoTIFF, or any GDAL-readable raster "
               "such as ERDAS .img or a GeoPackage — converted to GeoTIFF and reprojected to "
               "EPSG:4326 at ingest; public host or a Google Drive share link; up to the size "
               "cap). One band is read: a multi-band file is read as band 1, and a colour "
               "rendering (RGB/RGBA) is refused — give the single-band values",
        "title": "what the layer is",
        "description": "what a pixel value means and how the layer was produced",
        "source": "who produced it, derived from what",
        "license": "e.g. CC-BY-4.0, or 'unstated' (silence is not accepted)",
        "vintage": "when the layer was produced, YYYY-MM",
        "legend": "mapping class number -> label, e.g. {1: Very Low, ..., 5: Very High} "
                  "(not for a population_* count grid or a measure_* layer: their values are "
                  "amounts, and declared.units says what one means)",
        "declared": "the CONTRACT the file is verified against: {dtype, valid_min, valid_max, "
                    "nodata?, units?, scale_factor?, offset?} — say what the file IS; the "
                    "platform checks it before staging. valid_min/valid_max are the values AS "
                    "STORED, not counting nodata or outside_values. `units` (e.g. 'people per "
                    "pixel', 'class', 'Mg/ha', 'm', 't C/ha', 'degC', 'mm') is carried into every "
                    "pack that reads the layer's values and is REQUIRED for a measure_* layer; a "
                    "density per area (Mg/ha, t/ha, per km2) is also totalled over the area. "
                    "scale_factor/offset: when the file stores scaled integers (biomass x10 in "
                    "int16: scale_factor 0.1), value = stored x scale_factor + offset",
    },
    "optional": {
        "usage_notes": "a few lines the consuming analyst reads in the hazard passport (max 500 chars)",
        "stack": "for a landcover_* year: the stack it belongs to (landcover_<name>); the layer "
                 "must be named <stack>_<year>, and its legend must be the stack's",
        "year": "for a landcover_* year: the map year",
        "stack_title": "for the FIRST year of a new stack: the series' title",
        "outside_values": "pixel values that are not readings — outside the footprint, "
                          "unclassified, a fill such as -9999 or 255 over the sea; never a "
                          "legend class, and not checked against valid_min/valid_max. For a "
                          "landcover_* year the default is [0] plus declared.nodata; [] says "
                          "every cell is mapped",
        "layer_name": "for a file holding several rasters (a GeoPackage with several gridded "
                      "tables): the one to contribute",
        "authority": "who stands behind the map: national agency, regional hub product, ...",
        "accuracy": "documented accuracy, or 'unstated' (never invent one)",
        "colors": "optional {class: '#rrggbb'} for the map legend",
        "countries": "which countries the layer (or its stack) covers, e.g. [Costa Rica] — "
                     "the pack names them as where it answers",
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
    if _is_web_page(raw):
        raise Declined(_WEB_PAGE.format(url=m["url"], what="raster file (GeoTIFF, .img)"))
    tiffs_dir = get_settings().tiffs_dir
    tiffs_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="staged-tmp-", suffix=".tif", dir=tiffs_dir)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    base = {k: v for k, v in m.items() if k != "url"}
    out = rasters.add({**base, "file": tmp}, dry_run=True, keep_prepared=True)
    if out["status"] == "declined":
        os.unlink(tmp)
        for stray in (tmp + ".norm.tif",):
            if os.path.exists(stray):
                os.unlink(stray)
        raise Declined("; ".join(out["failures"]))
    # The gate may have reprojected or converted the file; what it verified is what
    # gets staged, and the raw upload goes.
    keep = out.get("file") or tmp
    if keep != tmp:
        os.unlink(tmp)
    return {"tmp": keep, "observed": out.get("observed")}


def _stage_raster(rec: dict, prepared: dict) -> dict:
    import os
    from ..config import get_settings
    m = rec["manifest"]
    layer, cid = m["layer"], rec["contribution_id"]
    dest = get_settings().tiffs_dir / f"staged-{cid}.tif"
    os.replace(prepared["tmp"], dest)
    entry = {"local_path": f"tiffs/staged-{cid}.tif", "title": m["title"],
             "description": m["description"], "legend": (m.get("legend") or {}), "source": m["source"],
             "license": m["license"], "vintage": m["vintage"],
             **({"usage_notes": m["usage_notes"]} if m.get("usage_notes") else {}),
             **({"prepared": prepared["observed"]["prepared"]}
                if (prepared.get("observed") or {}).get("prepared") else {}),
             "contributed": True, "staged_by": rec["contributor_id"], "contribution_id": cid,
             "contributor_label": rec["contributor_label"]}
    from . import rasters as _r
    rule = _r.namespace(layer)
    contract = {**m["declared"], "role": _r._role(layer)}
    # the values that are not readings ride the row every reader takes them from — as
    # the gate resolved them against the file (a stack year's default takes its nodata)
    obs = prepared.get("observed") or {}
    outside = obs["outside_values"] if "outside_values" in obs else _r.outside_values(m)
    if outside is not None:
        entry["outside_values"] = outside
    if m.get("countries"):
        entry["countries"] = list(m["countries"])
    _ensure_staged_loaded()
    if rule.get("stack"):
        from ..graph.geo import stacks as _stacks
        entry.update({"stack": str(m["stack"]), "year": int(m["year"]), "role": contract["role"],
                      "bounds": _stacks.header(str(dest))["bounds"],
                      **{k: m[k] for k in ("stack_title", "authority", "accuracy", "colors")
                         if m.get(k) is not None}})
        _stage_stack_row(entry, rec["contributor_id"], cid)
    STAGED_RASTERS[layer] = {"entry": entry, "contract": contract}
    _purge_clips(layer)
    short = layer.split("_", 1)[1] if "_" in layer else layer
    bounds = _raster_bounds(dest)
    if rule.get("stack"):
        how = (f"assemble_pack(pack='land-cover', place=<a place inside the map>, "
               f"year={int(m['year'])}) — or geometry=<a bbox or polygon the map covers>; "
               f"add year_to=<another year of {m['stack']}> for change and transitions, "
               "and classes=[<a legend label>] to detail one class")
    elif contract["role"] == "hazard":
        # the one namespace that IS a hazard: the risk pack reads it where it lies
        where = (f"a place inside lon {bounds[0]:.2f} to {bounds[2]:.2f}, lat {bounds[1]:.2f} "
                 f"to {bounds[3]:.2f}" if bounds else "a place the layer covers")
        how = (f"assemble_pack(pack='risk', place=<{where}>, "
               f"hazard={short!r}) — the pack's hazard passport shows the contract check")
    elif contract["role"] == "vulnerability":
        how = (f"risk_weights(hazard=<hazard>) to weigh {layer!r} into a recipe; "
               + _where_to_test(layer, bounds, "layers"))
    else:
        # measure_*, risk_*, population_*: values read over an area — never passed as a hazard
        how = _where_to_test(layer, bounds, "layers")
        if rule.get("values") == "amount":
            how += ("; a measure gives its mean, percentiles and, for a per-area density, its "
                    "total over the place, and the same per class of a land-cover map there")
    # What was done to the file and what it cannot do, in words at the top of the reply:
    # a band dropped, a projection changed, a stack that will never show change.
    said = [*(obs.get("prepared") or []), *(obs.get("hints") or [])]
    return {"layer": layer, "observed": prepared["observed"], "staged_file": str(dest),
            "entry": entry, "contract": contract,
            **({"note": " ".join(s[:1].upper() + s[1:].rstrip(".") + "." for s in said)}
               if said else {}),
            "how_to_test": how + "; only you and reviewers can use it until it is approved"}


def _land_raster(rec: dict) -> dict:
    import os
    from . import rasters
    m, preview = rec["manifest"], rec["preview"]
    manifest = {**{k: v for k, v in m.items() if k != "url"}, "file": preview["staged_file"]}
    _ensure_staged_loaded()
    STAGED_RASTERS.pop(m["layer"], None)          # so the gate's own taken-check sees the catalog only
    # the staged file was already converted; what was done to it stays on the landed row
    out = rasters.add(manifest, review=_review_note(rec, "layer"),
                      prepared_before=(preview.get("entry") or {}).get("prepared"))
    if out["status"] == "declined":
        STAGED_RASTERS[m["layer"]] = {"entry": preview["entry"], "contract": preview["contract"]}
        raise Declined("; ".join(out["failures"]))
    _unstage_stack_year(preview.get("entry") or {})
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
    _unstage_stack_year(preview.get("entry") or {})
    _purge_clips(layer)
    path = Path(preview.get("staged_file") or "")
    if path.is_file():
        retired = path.with_suffix(".tif.retired")
        path.rename(retired)
        return {"removed": True, "staged_file": f"moved aside to {retired.name}", "note": _HISTORY}
    return {"removed": False, "note": "no staged file remains"}


def _stage_stack_row(entry: dict, staged_by: str, cid: str) -> None:
    """A staged land-cover year also stages its STACK row (legend, years, extent), so
    the contributor's preview resolves the year through the same machinery the landed
    one will. Rebuilt from the year's entry after a restart; dropped with the year."""
    sid, year = str(entry["stack"]), int(entry["year"])
    b = entry.get("bounds") or [0, 0, 0, 0]
    row = STAGED_RASTERS.get(sid)
    if row is None or row["entry"].get("kind") != "stack":
        row = {"entry": {"kind": "stack", "role": "landcover", "internal": True,
                         "title": entry.get("stack_title") or f"{sid} land cover",
                         "description": entry.get("description", ""), "source": entry.get("source"),
                         "license": entry.get("license"), "legend": entry.get("legend") or {},
                         "vintage": entry.get("vintage"),
                         "outside_values": [],
                         **({"countries": list(entry["countries"])} if entry.get("countries") else {}),
                         "authority": entry.get("authority") or "contributed; authority unstated",
                         "accuracy": entry.get("accuracy") or "unstated",
                         **({"colors": entry["colors"]} if entry.get("colors") else {}),
                         "layer_pattern": f"{sid}_{{year}}", "years": [],
                         "extent": {"lon": [b[0], b[2]], "lat": [b[1], b[3]]},
                         "staged_by": staged_by, "contribution_id": cid},
               "contract": None}
        STAGED_RASTERS[sid] = row
    st = row["entry"]
    from . import rasters as _r
    # a year staged before outside values were resolved at the gate carries none: [0]
    # was the default then
    _r.merge_outside(st, [0] if entry.get("outside_values") is None else entry["outside_values"])
    st["years"] = sorted(set(int(y) for y in st.get("years") or []) | {year})
    ext = st["extent"]
    st["extent"] = {"lon": [min(ext["lon"][0], b[0]), max(ext["lon"][1], b[2])],
                    "lat": [min(ext["lat"][0], b[1]), max(ext["lat"][1], b[3])]}


def _unstage_stack_year(entry: dict) -> None:
    sid, year = entry.get("stack"), entry.get("year")
    row = STAGED_RASTERS.get(str(sid)) if sid else None
    if not row or row["entry"].get("kind") != "stack":
        return
    years = [y for y in row["entry"].get("years", []) if int(y) != int(year)]
    if years:
        row["entry"]["years"] = years
    else:
        STAGED_RASTERS.pop(str(sid), None)


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
            if row.get("contract") is not None          # a staged STACK row has no contract of its own
            and caller.may_see(row["entry"].get("staged_by"))}


# --------------------------------------------------------------------------- weights

WEIGHTS_FIELDS = {
    "required": {
        "hazard": "which hazard the weights are for, e.g. flood — any hazard_* layer in the "
                  "catalog, a contributed one included; a hazard with no recipe yet gets one "
                  "from this contribution (a return-period layer inherits its base hazard's)",
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


_VULNERABILITY_ROLES = ("vulnerability_reclass", "vulnerability")   # hand-authored | gate-assigned


def available_vulnerability_layers() -> list[str]:
    """The reclassed vulnerability layers a weight may name, read from the live
    raster contracts rather than a hardcoded list. The hand-authored contracts
    call the role `vulnerability_reclass`; the gate names a contributed layer's
    role after its namespace, `vulnerability` — two names for one role, five days
    apart, and the weights kind only knew the first, so every contributed
    vulnerability layer was "unknown" to the one kind the field doc promised."""
    try:
        from ..graph.geo import schema
        doc = schema._doc()
        return sorted(k for k, v in (doc.get("layers") or {}).items()
                      if (v or {}).get("role") in _VULNERABILITY_ROLES)
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
        # Any hazard in the catalog, contributed ones included. A hazard with no
        # recipe yet is not refused — this contribution becomes its recipe. The
        # old rule ("no risk recipe for hazard — one of: <the nine typed in
        # conf/risk_l2.yml>") meant a contributed hazard could never reach
        # Layer-2 through the one kind built to adjust it.
        if tiffs.resolve(key) is None:
            known = sorted(l.removeprefix("hazard_") for l in tiffs.catalog()
                           if l.startswith("hazard_"))
            problems.append(f"no hazard layer named {key!r} in the catalog — one of: "
                            + ", ".join(known))
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
        **({"review": _review_note(rec, "recipe")} if rec.get("auto_approved") else {}),
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
        "layer": "a short snake_case name, e.g. evacuation_centres, cr_rivers, cr_mangrove_map_2021. "
                 "A POINT layer becomes a countable asset beside hospitals/schools in every risk "
                 "answer; a LINE or POLYGON layer is measured inside any pack's area with "
                 "assemble_pack(..., vectors=[<layer>]) — km of line, km² (ha) of polygon",
        "url": "where the platform can fetch the layer: a GeoJSON FeatureCollection in EPSG:4326 "
               "(lon, lat), a ZIPPED SHAPEFILE (.shp/.shx/.dbf/.prj in one .zip) or a GEOPACKAGE "
               "(.gpkg) — shapefiles and GeoPackages in any CRS are reprojected to EPSG:4326 at "
               "ingest; Point, LineString or Polygon features (or their Multi forms; one family "
               "per layer); a Google Drive share link works; public host, up to the size cap",
        "title": "what the features are",
        "description": "what one feature represents, how the layer was compiled, what it excludes",
        "source": "who maintains the layer (agency), derived from what",
        "license": "e.g. CC-BY-4.0, or 'unstated' (silence is not accepted)",
        "vintage": "when the layer was last updated, YYYY-MM, or 'unstated' when the file does "
                   "not say (an honest 'unstated' beats a guessed month)",
    },
    "optional": {
        "countries": "which countries the layer covers, e.g. [Costa Rica]",
        "name_field": "the property holding each feature's name, if any. A POLYGON layer with a "
                      "name_field is also a gazetteer: its feature names resolve as `place` in "
                      "every pack and in resolve_place_time (the hub's own boundary, not a geocoder's)",
        "group_field": "a property to break the measure down by, e.g. clase or IUCN_CAT — "
                       "the citation states the km / km² / count per value",
        "layer_name": "for a GeoPackage with several feature layers, or a zip with several "
                      "shapefiles: which one to read",
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


_WEB_PAGE = ("{url} answered with a web page, not a {what}: it is a portal, catalogue or "
             "viewer page (ArcGIS Hub 'explore', a Google Earth Engine catalogue entry, a data "
             "portal item). Find the page's download link for the file itself — or download it "
             "and share it from Google Drive (Anyone with the link) — and give that link")


def _is_web_page(raw: bytes) -> bool:
    head = raw[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<?xml version")) and b"<html" in raw[:4096].lower()


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
        raw, fname = fetch_policy.fetch(m["url"])
    except fetch_policy.FetchRefused as exc:
        raise Declined(str(exc)) from None
    except Exception as exc:
        raise Declined(f"could not fetch {m['url']}: {type(exc).__name__}: {exc}") from None
    if _is_web_page(raw):
        raise Declined(_WEB_PAGE.format(url=m["url"], what="vector file (GeoJSON, zipped shapefile, GeoPackage)"))
    # A zipped shapefile or a GeoPackage is read into GeoJSON on EPSG:4326 here;
    # what lands is always GeoJSON, and the record says what ingest did.
    from ..graph.geo import vecio
    try:
        raw, prep_notes = vecio.to_geojson(raw, fname, m.get("layer_name"))
    except ValueError as exc:
        raise Declined(f"{m['url']}: {exc}") from None
    except Exception as exc:
        raise Declined(f"{m['url']}: could not be read as a vector file: {type(exc).__name__}: {exc}") from None
    try:
        observed = vectors.inspect(raw)
    except ValueError as exc:
        raise Declined(f"{m['url']}: {exc}") from None
    if prep_notes:
        observed["prepared"] = prep_notes
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
             "geometry": prepared["observed"].get("geometry", "points"),
             "bbox": prepared["observed"]["bbox"],
             **({"countries": m["countries"]} if m.get("countries") else {}),
             **({"name_field": m["name_field"]} if m.get("name_field") else {}),
             **({"group_field": m["group_field"]} if m.get("group_field") else {}),
             **({"prepared": prepared["observed"]["prepared"]}
                if prepared["observed"].get("prepared") else {}),
             **({"usage_notes": m["usage_notes"]} if m.get("usage_notes") else {}),
             "contributed": True, "staged_by": rec["contributor_id"], "contribution_id": cid,
             "contributor_label": rec["contributor_label"]}
    _ensure_staged_vectors_loaded()
    STAGED_VECTORS[layer] = entry
    vectors.purge_clips(layer)
    fam = entry["geometry"]
    how = _where_to_test(layer, entry.get("bbox"), "vectors", fam)
    if fam != "points" and entry.get("group_field") and how.startswith("assemble_pack"):
        how += f", broken down by {entry['group_field']}"
    if fam == "polygons" and entry.get("name_field"):
        how += (f"; the features' {entry['name_field']} values now resolve as `place` "
                "(resolve_place_time(place=<a feature's name>))")
    gp = m.get("group_field")
    props = prepared["observed"].get("properties") or []
    warn = []
    import re as _re
    vin = _re.match(r"(\d{4})", str(m.get("vintage") or ""))
    for note in prepared["observed"].get("prepared") or []:
        named = _re.search(r"layer (\S+)|shapefile (\S+)", note)
        years = _re.findall(r"(?<!\d)((?:19|20)\d\d)(?!\d)", " ".join(g for g in (named.groups() if named else ()) if g))
        if vin and years and vin.group(1) not in years:
            warn.append(f"the file's own layer name carries the year {', '.join(sorted(set(years)))} but the "
                        f"declared vintage is {m.get('vintage')} — confirm which version this is")
    if gp and gp not in props:
        warn.append(f"group_field {gp!r} is not a property of the features (properties: "
                    f"{', '.join(props[:12])}) — the breakdown will be empty")
    nf = m.get("name_field")
    if nf and nf not in props:
        warn.append(f"name_field {nf!r} is not a property of the features (properties: "
                    f"{', '.join(props[:12])}) — no feature will carry a name")
    return {"layer": layer, "observed": prepared["observed"], "staged_file": str(dest),
            "entry": entry, **({"hints": warn} if warn else {}),
            "how_to_test": how + "; only you and reviewers see it until it is approved"}


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

# --------------------------------------------------------------------------- amend

AMEND_FIELDS = {
    "required": {
        "layer": "an APPROVED contributed layer to correct — a raster (hazard_*, risk_*, "
                 "vulnerability_*, population_*, landcover_*, …) or a vector layer (points, "
                 "lines or polygons). Built-in layers are corrected through code review, not here",
        "fields": "only the corrected values: any of title, description, usage_notes, source, "
                  "license, vintage (and units, for a raster). The file and its contract do "
                  "not change — a different file is a new layer",
        "rationale": "what was wrong with the record, in a sentence a reviewer can check "
                     "(e.g. 'the data holds one value per district, not per sub-district')",
    },
    "optional": {},
}
_AMENDABLE = ("title", "description", "usage_notes", "source", "license", "vintage")
_AMENDABLE_CONTRACT = ("units",)

# Staged corrections: the contributor's own packs show the corrected record, nobody
# else's do, until a reviewer approves — the same preview rule as every other kind.
STAGED_AMENDS: dict[str, dict] = {}


def _amend_target(layer: str):
    """('raster' | 'vector', current row) for an approved contributed layer, else None.
    Read from the machine-owned landing files, never the merged catalog: a staged or
    hand-authored row is not something this kind may rewrite."""
    import yaml
    from ..config import get_settings
    path = get_settings().tiffs_contrib_path
    if path.exists():
        rows = yaml.safe_load(path.read_text()) or {}
        if layer in rows and isinstance(rows[layer], dict):
            row = dict(rows[layer])
            try:
                from ..graph.geo import schema as _schema
                doc = yaml.safe_load(get_settings().raster_schema_contrib_path.read_text()) or {}
                row["units"] = ((doc.get("layers") or {}).get(layer) or {}).get("units")
            except Exception:
                row["units"] = None
            return "raster", row
    from ..graph.geo import vectors
    entry = vectors.landed().get(layer)
    if entry:
        return "vector", dict(entry)
    return None


def _validate_amend(manifest) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest must be a mapping with layer, fields and rationale"]
    m = dict(manifest)
    problems = []
    unknown = set(m) - set(AMEND_FIELDS["required"]) - set(AMEND_FIELDS["optional"])
    if unknown:
        problems.append(f"unknown fields {sorted(unknown)} — layer, fields and rationale only")
    layer = str(m.get("layer") or "").strip()
    target = _amend_target(layer) if layer else None
    if not layer:
        problems.append("missing required field 'layer'")
    elif target is None:
        problems.append(f"{layer!r} is not an approved contributed layer — only those can be "
                        "corrected here; a built-in layer's record is corrected in code review, "
                        "and a pending contribution is withdrawn and resubmitted")
    fields = m.get("fields")
    if not isinstance(fields, dict) or not fields:
        problems.append("missing required field 'fields' — the corrected values, e.g. "
                        "{\"description\": \"…\", \"usage_notes\": \"…\"}")
    else:
        allowed = set(_AMENDABLE) | (set(_AMENDABLE_CONTRACT)
                                     if target and target[0] == "raster" else set())
        bad = sorted(set(fields) - allowed)
        if bad:
            problems.append(f"fields {bad} cannot be amended — one of: {', '.join(sorted(allowed))}. "
                            "The file and its declared contract do not change: a different file "
                            "is a new layer")
        empty = sorted(k for k, v in fields.items() if not str(v or "").strip())
        if empty:
            problems.append(f"fields {empty} are empty — a correction states the right value; "
                            "it does not erase the record")
        from . import notes
        problems += notes.validate(fields.get("usage_notes"))
    if not str(m.get("rationale") or "").strip():
        problems.append("missing required field 'rationale' — a correction with no stated reason "
                        "cannot be reviewed")
    if layer and target is not None:
        caller = identity.current()
        owner = _layer_contributor(layer, target[0])
        if not (caller.is_reviewer or (owner and caller.owns(owner))):
            problems.append(f"only the contributor of {layer!r} or a reviewer may correct its "
                            "record" + ("" if owner else " — it landed outside the contribution "
                                        "gate, so only a reviewer may"))
        if isinstance(fields, dict):
            from ..mcp.packs import _is_synthetic
            row = target[1]
            after = {**row, **{k: v for k, v in fields.items() if k in _AMENDABLE}}
            if _is_synthetic(row) and not _is_synthetic(after):
                problems.append(f"{layer!r} is declared a synthetic or demonstration layer; a "
                                "correction cannot remove that declaration — every answer built "
                                "on it leads with it")
    if layer:
        _ensure_staged_loaded()
        pending = STAGED_AMENDS.get(layer)
        if pending:
            caller = identity.current()
            mine = caller.is_reviewer or caller.owns(pending.get("staged_by"))
            problems.append(f"a correction of {layer!r} is already pending review"
                            + (f" ({pending.get('contribution_id')}) — withdraw it or wait for "
                               "the decision" if mine else ""))
    return problems


def _layer_contributor(layer: str, target_kind: str) -> str | None:
    """Who contributed `layer` through the gate (the approved record's contributor),
    or None when it landed some other way."""
    try:
        for rec in store.list_contributions(status="approved", kind=target_kind):
            if str((rec.get("manifest") or {}).get("layer") or "") == layer:
                return rec.get("contributor_id")
    except Exception:
        return None
    return None


def _prepare_amend(m: dict) -> dict:
    """The before and after of every field that actually changes — the record and the
    reviewer both see what the correction replaces, not only its new text."""
    layer = str(m["layer"]).strip()
    target_kind, row = _amend_target(layer)
    after = {k: str(v).strip() for k, v in m["fields"].items()}
    changed = {k: {"from": row.get(k), "to": v} for k, v in after.items()
               if str(row.get(k) or "").strip() != v}
    if not changed:
        raise Declined(f"the record of {layer!r} already says exactly this")
    return {"layer": layer, "target_kind": target_kind,
            "before": {k: row.get(k) for k in changed},
            "after": {k: after[k] for k in changed}, "changed": changed}


def _stage_amend(rec: dict, prepared: dict) -> dict:
    _ensure_staged_loaded()
    STAGED_AMENDS[prepared["layer"]] = {
        "target_kind": prepared["target_kind"], "fields": prepared["after"],
        "staged_by": rec["contributor_id"], "contribution_id": rec["contribution_id"],
        "contributor_label": rec["contributor_label"],
        "rationale": rec["manifest"].get("rationale")}
    target = _amend_target(prepared["layer"])
    row = (target or (None, {}))[1]
    if prepared["target_kind"] == "raster":
        from pathlib import Path
        from ..config import get_settings
        bounds = _raster_bounds(get_settings().tiffs_dir / Path(str(row.get("local_path") or "-")).name)
        how = _where_to_test(prepared["layer"], bounds, "layers")
    else:
        how = _where_to_test(prepared["layer"], row.get("bbox"), "vectors",
                             row.get("geometry", "points"))
    return {**prepared,
            "how_to_test": (how + " — your packs show the corrected record; everyone else's "
                            "show the current one until a reviewer approves")}


def _land_amend(rec: dict) -> dict:
    """Rewrite the landed row with the corrected values and keep what they replaced:
    `amended` is the row's own history, newest last."""
    import yaml
    from ..config import get_settings
    pv = rec["preview"]
    layer, after = pv["layer"], pv["after"]
    history = {"contribution_id": rec["contribution_id"], "by": rec["contributor_label"],
               "rationale": rec["manifest"].get("rationale"), "replaced": pv["before"],
               **({"review": _review_note(rec, "correction")} if rec.get("auto_approved") else {})}
    row_fields = {k: v for k, v in after.items() if k in _AMENDABLE}

    def reviewed(current: dict) -> dict:
        """Every citation reads the row's `review`; an unreviewed correction says so there."""
        if not rec.get("auto_approved"):
            return {}
        note = (f"record corrected by auto-approve ({rec['contribution_id']}) — no human "
                "reviewed the correction")
        prior = current.get("review")
        return {"review": f"{prior}; {note}" if prior else note}
    if pv["target_kind"] == "raster":
        s = get_settings()
        rows = yaml.safe_load(s.tiffs_contrib_path.read_text()) or {}
        if layer not in rows:
            raise Declined(f"{layer!r} is no longer a landed raster — nothing to correct")
        rows[layer] = {**rows[layer], **row_fields, **reviewed(rows[layer]),
                       "amended": [*(rows[layer].get("amended") or []), history]}
        s.tiffs_contrib_path.write_text(
            "# Contributed raster rows — machine-owned; written by the\n"
            "# contribution gate. Hand-edit conf/tiffs.yml, never this.\n"
            + yaml.safe_dump(rows, sort_keys=False, allow_unicode=True))
        if "units" in after:
            sp = s.raster_schema_contrib_path
            doc = (yaml.safe_load(sp.read_text()) or {}) if sp.exists() else {}
            doc.setdefault("layers", {}).setdefault(layer, {})["units"] = after["units"]
            sp.write_text("# Contributed raster contracts — machine-owned.\n"
                          + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    else:
        from ..graph.geo import vectors
        entry = vectors.landed().get(layer)
        if entry is None:
            raise Declined(f"{layer!r} is no longer a landed vector layer — nothing to correct")
        vectors.register(layer, {**entry, **row_fields, **reviewed(entry),
                                 "amended": [*(entry.get("amended") or []), history]})
    _ensure_staged_loaded()
    STAGED_AMENDS.pop(layer, None)
    return {"layer": layer, "corrected": after, "replaced": pv["before"]}


def _retire_amend(rec: dict) -> dict:
    _ensure_staged_loaded()
    layer = (rec.get("preview") or {}).get("layer", "")
    return {"removed": STAGED_AMENDS.pop(layer, None) is not None,
            "note": "nothing was written to the layer's record for a staged correction"}


def visible_staged_amends() -> dict:
    """{layer: staged correction} the current caller may see — its author's packs read
    the corrected record, as a preview."""
    _ensure_staged_loaded()
    caller = identity.current()
    return {layer: a for layer, a in STAGED_AMENDS.items()
            if caller.may_see(a.get("staged_by"))}


def amended_row(layer: str, row: dict, contract: bool = False) -> dict:
    """`row` with the caller's own staged correction laid over it, marked as unreviewed
    so any pack that reads it is a preview. `contract` picks the contract fields."""
    a = visible_staged_amends().get(layer)
    if not a:
        return row
    keys = _AMENDABLE_CONTRACT if contract else _AMENDABLE
    over = {k: v for k, v in (a.get("fields") or {}).items() if k in keys}
    if contract:
        return {**row, **over}
    # marked even when only contract fields (units) change: whatever reads this row
    # reads an unreviewed record, and the pack it lands in must be a preview
    return {**row, **over, "staged_by": a.get("staged_by"),
            "contribution_id": a.get("contribution_id"),
            "review": "STAGED correction of this record — not yet reviewed"}


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
    "amend": {"validate": _validate_amend, "prepare": _prepare_amend,
              "stage": _stage_amend, "land": _land_amend, "retire": _retire_amend,
              "fields": AMEND_FIELDS,
              "title": lambda m: f"correction of the record of {m.get('layer')}"},
}


# --------------------------------------------------------------------------- records

_PUBLIC = ("contribution_id", "kind", "status", "title", "contributor_label",
           "created_at", "updated_at", "preview", "landing", "decision_note",
           "reviewer_label", "problems", "held_for_review")


# A licence that keeps the platform from serving the data on: the platform hosts a
# copy and hands it to every caller, which IS redistribution. Non-commercial alone
# is not on this list — serving it for decision support is not commercial use.
_NO_REDISTRIBUTION = (
    # "no redistribution", "no sub-licensing or redistribution", "not for redistribution"
    # — a list of forbidden uses, never "no restrictions on redistribution"
    re.compile(r"\b(?:no|not\s+for)\s+(?:[\w-]+(?:\s+use)?\s*(?:,|\bor\b|\band\b|/)\s*){0,3}"
               r"(?:re-?distribution|redistributing)\b", re.I),
    re.compile(r"\b(?:not|never)\s+(?:be\s+)?(?:re-?distributed|redistribute)\b", re.I),
    re.compile(r"\bre-?distribution\b[^.;]{0,80}?\bnot\s+(?:allowed|permitted)\b", re.I),
    re.compile(r"\bre-?distribution\b[^.;]{0,40}?\b(?:is\s+)?(?:prohibited|forbidden)\b", re.I),
    re.compile(r"\b(?:prohibits?|forbids?)\s+(?:the\s+)?re-?distribution\b", re.I),
    re.compile(r"\ball\s+rights\s+reserved\b", re.I),
)
# "without (prior) (written) permission" restricts serving on only in a clause that
# forbids something AND is about passing the data on: "free to use without
# permission" grants, and "commercial use not allowed without permission" is the
# non-commercial clause, which is not a reason to hold.
_WITHOUT_PERMISSION = re.compile(r"\bwithout\s+(?:the\s+)?(?:prior\s+|express\s+)*(?:written\s+)?"
                                 r"(?:permission|consent|authori[sz]ation)\b", re.I)
_RESTRICTS = re.compile(r"\b(?:no|not|never|prohibited|forbidden|may\s+only|must\s+not)\b", re.I)
_PASSING_ON = re.compile(r"distribut|sub-?licens|\bshar(?:e|ed|ing)\b|\bpublish|\bcop(?:y|ies)\b"
                         r"|web\s+services?|third\s+part", re.I)


def _restricts_redistribution(licence) -> str | None:
    """The phrase in a declared licence that forbids serving the data on, or None."""
    text = str(licence or "")
    for rx in _NO_REDISTRIBUTION:
        m = rx.search(text)
        if m:
            return " ".join(m.group(0).split())
    for clause in re.split(r"[.;]", text):
        m = _WITHOUT_PERMISSION.search(clause)
        if m and _RESTRICTS.search(clause[:m.start()]) and _PASSING_ON.search(clause):
            return " ".join(clause.split())
    return None


def _needs_a_human(kind: str, manifest: dict, prepared: dict) -> str | None:
    """Why this submission must wait for a person even when auto-approve is on, in
    words its contributor reads — or None. Everything else lands as before."""
    licence = manifest.get("license")
    if kind == "amend":
        licence = (manifest.get("fields") or {}).get("license")
    said = _restricts_redistribution(licence)
    if said:
        return (f"its licence restricts redistribution ('{said}'), and serving it to every "
                "caller is redistribution — a reviewer checks that the platform has the "
                "permission the licence asks for before it goes live")
    n = int((prepared or {}).get("emails") or 0) if kind == "document" else 0
    if n >= 2:
        return (f"its text holds {n} e-mail addresses — personal data. The document's owner "
                "must confirm they may be served to every caller (or share a copy without "
                "them) before a reviewer lets it go live")
    return None


def _public(rec: dict) -> dict:
    return {k: rec[k] for k in _PUBLIC if rec.get(k) is not None}


def _caps(caller: identity.Caller) -> tuple[list[str], str | None]:
    """Check the caps and TAKE a slot in one write transaction: (problems, slot id).

    The check used to read the ledger, then fetch for up to two minutes, then write
    the record — so concurrent submissions all passed the same check and one reply
    read "32 submissions in the last hour — the cap is 30". The slot is held while
    the submission is prepared and released once its record is written (the record
    then counts in its place) or it is declined. BEGIN IMMEDIATE serialises the
    check across threads and processes; a slot a crashed worker left behind ages out
    with the hour. Under OAuth every signed-in participant is their own contributor
    (identity.py: the token subject); with OAuth off everyone on the box is one."""
    import sqlite3
    from ..config import get_settings
    cap = max(0, int(get_settings().grp_contrib_hourly_cap or 0))
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=1)).isoformat()
    try:
        con = store._connect()
    except sqlite3.OperationalError:
        return ["the contribution ledger is busy — submit again in a moment"], None
    con.isolation_level = None          # explicit transaction below
    try:
        con.execute("CREATE TABLE IF NOT EXISTS contribution_slots "
                    "(id TEXT PRIMARY KEY, contributor_id TEXT NOT NULL, at TEXT NOT NULL)")
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM contribution_slots WHERE at < ?", (since,))
        held = [r[0] for r in con.execute(
            "SELECT at FROM contribution_slots WHERE contributor_id = ?", (caller.id,))]
        pending = con.execute("SELECT COUNT(*) FROM contributions WHERE status = 'pending' "
                              "AND contributor_id = ?", (caller.id,)).fetchone()[0]
        recent = [r[0] for r in con.execute(
            "SELECT created_at FROM contributions WHERE contributor_id = ? AND created_at >= ?",
            (caller.id, since))]
        problems = []
        if pending + len(held) >= PENDING_CAP:
            problems.append(f"you have {pending + len(held)} contributions awaiting review — the "
                            f"cap is {PENDING_CAP}; wait for a decision or withdraw one")
        used = len(recent) + len(held)
        if cap and used >= cap:
            oldest = min(datetime.fromisoformat(t) for t in recent + held)
            mins = max(1, math.ceil((oldest + timedelta(hours=1) - now).total_seconds() / 60))
            shared = (f" — everyone reaching this instance without a sign-in submits as "
                      f"{caller.label!r}, so they share this budget; GRP_CONTRIB_HOURLY_CAP "
                      "raises it" if caller.source in ("local-dev", "anonymous") else "")
            problems.append(f"{used} submissions in the last hour — the cap is {cap} per "
                            f"contributor; the next slot frees in about {mins} min{shared}")
        if problems:
            con.execute("ROLLBACK")
            return problems, None
        slot = os.urandom(8).hex()
        con.execute("INSERT INTO contribution_slots(id, contributor_id, at) VALUES (?, ?, ?)",
                    (slot, caller.id, now.isoformat()))
        con.execute("COMMIT")
        return [], slot
    except sqlite3.OperationalError:     # the busy timeout ran out; closing rolls back
        return ["the contribution ledger is busy — submit again in a moment"], None
    finally:
        con.close()


def _release(slot: str | None) -> None:
    if not slot:
        return
    con = store._connect()
    try:
        with con:
            con.execute("DELETE FROM contribution_slots WHERE id = ?", (slot,))
    finally:
        con.close()


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

def _manifest_from_text(text: str) -> tuple[dict | None, str | None]:
    """A manifest pasted as YAML or JSON text — the same file a developer would put
    under conf/, submitted over the MCP instead of over a VPN. A top-level `kind`
    key may name the kind so the whole config travels as one block."""
    import yaml
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, f"manifest text is not valid YAML/JSON: {exc}"
    if not isinstance(doc, dict):
        return None, "manifest text must be a mapping of provenance fields"
    return doc, None


def submit(kind: str, manifest, caller: identity.Caller | None = None) -> dict:
    """Validate, store as pending, stage for preview. Declines name every problem."""
    caller = caller or identity.current()
    if isinstance(manifest, str):
        manifest, why = _manifest_from_text(manifest)
        if why:
            return {"status": "declined", "kind": kind, "problems": [why]}
    if isinstance(manifest, dict) and manifest.get("kind") and not kind:
        manifest = dict(manifest)
        kind = str(manifest.pop("kind"))
    elif isinstance(manifest, dict) and "kind" in manifest:
        manifest = {k: v for k, v in manifest.items() if k != "kind"}
    if manifest is None:
        return {"status": "declined", "kind": kind,
                "problems": ["missing manifest — a mapping of provenance fields, or the same "
                             "as YAML/JSON text with a top-level `kind`"]}
    if kind not in _KINDS:
        return {"status": "declined", "kind": kind,
                "problems": [f"unknown kind {kind!r} — over the MCP you can contribute: "
                             + ", ".join(KINDS)
                             + " (feeds, tables and rasters are coming; packs stay on the "
                             "developer path)"]}
    spec = _KINDS[kind]
    vtok = identity.bind(caller)       # a kind that checks WHO may submit sees the submitter
    try:
        problems = spec["validate"](manifest)
    finally:
        identity.unbind(vtok)
    if problems:
        return {"status": "declined", "kind": kind, "problems": problems,
                "fields": spec["fields"]}
    caps, slot = _caps(caller)
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
        _release(slot)                                # the record counts from here
        slot = None
        rec = store.load_contribution(ident)
        try:
            preview = spec["stage"](rec, prepared)
        except Exception as exc:                      # the record stays, marked, for audit
            store.update_contribution(ident, {"status": "failed", "problems": [str(exc)]})
            return {"status": "declined", "kind": kind, "contribution_id": ident,
                    "problems": [f"staging failed: {type(exc).__name__}: {exc}"]}
    finally:
        identity.unbind(token)
        _release(slot)
    hold = _needs_a_human(kind, manifest, prepared)
    rec = store.update_contribution(ident, {"preview": preview,
                                            **({"held_for_review": hold} if hold else {})})
    from ..config import get_settings
    if get_settings().grp_auto_approve and hold:
        # Auto-approve lands what a machine can vouch for. A licence that forbids
        # redistribution, or personal data in a document, needs a person to say yes.
        _notify(f"New {kind} contribution HELD for review despite auto-approve, from "
                f"{caller.label}: {rec['title']} (id {ident}) — {hold}")
        return {**_public(rec), "status": "staged",
                "review": f"HELD FOR A HUMAN REVIEWER — {hold}",
                "next": ("test it now — only you and reviewers can see it; it goes live when a "
                         "reviewer approves it, and contribute_status shows the decision")}
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
        preview = dict(rec.get("preview") or {})
        if isinstance(preview.get("how_to_test"), str):
            # the staged-preview tail is not true here: it landed for everyone
            preview["how_to_test"] = re.sub(
                r"\s*[;—-]\s*only you and reviewers[^.;]*?approved[^.;]*", "", preview["how_to_test"])
        rec = {**rec, "preview": preview}
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
        if kind == "vector":
            # Served = registered AND its master file is there and reads as GeoJSON;
            # every pack clips from that file, so a row without it serves nothing.
            from ..graph.geo import vectors
            layer = m.get("layer")
            entry = vectors.landed().get(layer)
            if not entry:
                return {"live": False, "why": f"no vector registry row named {layer!r}"}
            path = vectors.master_path(entry)
            try:
                with open(path, "rb") as f:
                    head = f.read(256).lstrip(b"\xef\xbb\xbf").lstrip()
            except OSError:
                return {"live": False, "why": f"{layer!r} is registered but its file "
                                              f"{entry.get('local_path')} is missing"}
            ok = head.startswith(b"{")
            return {"live": ok, "why": None if ok else
                    f"{layer!r} is registered but its file is not GeoJSON"}
        if kind == "weights":
            from ..graph.geo import combine
            adj = combine.adjustment_for("hazard_" + str(m.get("hazard") or ""))
            return {"live": bool(adj), "why": None if adj else
                    "no adjusted block is in force for this hazard"}
        if kind == "amend":
            pv = rec.get("preview") or {}
            target = _amend_target(str(pv.get("layer") or ""))
            if target is None:
                return {"live": False, "why": f"no landed layer named {pv.get('layer')!r}"}
            stale = [k for k, v in (pv.get("after") or {}).items()
                     if str(target[1].get(k) or "").strip() != str(v).strip()]
            if stale:
                hist = target[1].get("amended") or []
                ids = [h.get("contribution_id") for h in hist]
                cid = rec.get("contribution_id")
                later = [h for h in hist[ids.index(cid) + 1:]] if cid in ids else []
                by = [h.get("contribution_id") for h in later
                      if set(stale) & set((h.get("replaced") or {}))]
                if by:
                    return {"live": None, "why": f"superseded by a later correction ({by[-1]})"}
            return {"live": not stale, "why": None if not stale else
                    f"the record no longer says what this correction set for {stale}"}
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
    # The note counts what was checked: it said "every approved contribution is
    # being served" over a list where every vector sat under "unverifiable".
    counted = f"{len(live)} of {len(rows)} approved served"
    if inert:
        counted += f", {len(inert)} approved but NOT served"
    if unknown:
        counted += f", {len(unknown)} not checked (see each one's `why`)"
    return {"status": "ok", "approved": len(rows),
            "serving": len(live), "approved_but_inert": len(inert),
            "unverifiable": len(unknown),
            "contributions": {"serving": live, "inert": inert, "unverifiable": unknown},
            "note": (counted + (" — an approved contribution that is not being served changed "
                                "nothing: the approval succeeded and the landing did not"
                                if inert else
                                "" if unknown else " — every approved contribution is being served"))}


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

def _cap_words() -> str:
    from ..config import get_settings
    cap = max(0, int(get_settings().grp_contrib_hourly_cap or 0))
    return f"{cap} submissions" if cap else ""


def describe_submit() -> str:
    from ..config import get_settings
    auto = get_settings().grp_auto_approve
    lines = ["Contribute a source to the platform from this conversation — no server access, "
             "no config files. The submission is validated against the same gate the "
             "platform's own contribution commands use. "
             + ("This deployment AUTO-APPROVES: a clean submission lands at once and is live "
                "for every caller, and its record and every citation built on it say that no "
                "human reviewed it. (Human review is switched off here, not removed.) Two "
                "things still wait for a human reviewer, and the reply says which: a declared "
                "licence that restricts redistribution (no redistribution, not without "
                "written permission, all rights reserved — non-commercial alone is fine), and "
                "a document whose text holds two or more e-mail addresses."
                if auto else
                "A clean one is STAGED: stored for review and served to you (and to "
                "reviewers) on every query path — search, inventory, feeds, evidence packs — "
                "under the same relevance ranking as any other source, so you test exactly "
                "what an analyst will see. It goes live for everyone only when a reviewer "
                "approves it."),
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
              ("Returns {status: approved, contribution_id, preview, landing, review, next} — "
               "live now for every caller (if landing fails it is left {status: staged, "
               "auto_approve: why} for a reviewer) — or "
               if auto else
               "Returns {status: staged, contribution_id, preview{doc_id,...}, next} (the record "
               "then shows status pending in contribute_status until a reviewer decides) or ")
              + "{status: declined, problems: [...]} naming every problem at once — fix them all "
              "and resubmit. A url must answer with the FILE: a login page, a bot challenge or "
              "a web page about the file is declined in words, with what to do (usually: "
              "download it and share it from Google Drive, Anyone with the link). Each "
              "contributor may submit "
              + (f"{_cap_words()} per hour" if _cap_words() else "without an hourly cap")
              + ". Rules: contributions never overwrite an existing source; a "
              "document already in the library is declined by its doc_id; a table's or feed's "
              "dataset name must be new; `amend` corrects an approved layer's record, never its "
              "file or contract. A feed is test-queried once before it is accepted, so a wrong "
              "index name or dead URL is refused with the adapter's reason. A raster is fetched "
              "and verified against its declared contract before it is accepted. "
              + ("Accepted, a table or feed is queried with feeds_query(dataset) and a raster is "
                 "readable by every pack at once."
                 if auto else
                 "A staged table or feed is queried with feeds_query(dataset) like any feed and "
                 "appears under `staged_feeds` in platform_capabilities for its owner; staged, a "
                 "raster is a layer the packs read for its owner.")]
    return "\n".join(lines)
