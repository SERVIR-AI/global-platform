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

KINDS = ("document",)          # tables, feeds and rasters follow (X4.3-X4.5)
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


def _stage_document(rec: dict) -> dict:
    """Fetch, extract, ingest with the staged tag. Raises Declined."""
    from ..rag import docloader
    m = rec["manifest"]
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
        if owner != rec["contributor_id"]:
            raise Declined("this document is already staged by another contributor")
    meta = _document_meta(m) | {"staged_by": rec["contributor_id"],
                                "contribution_id": rec["contribution_id"]}
    out = corpus.ingest(text, meta, raw=raw, filename=fname)
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
    out = corpus.ingest(text, meta, raw=raw, filename=fname)
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


_KINDS = {
    "document": {"validate": _validate_document, "stage": _stage_document,
                 "land": _land_document, "retire": _retire_document,
                 "fields": DOCUMENT_FIELDS,
                 "title": lambda m: str(m.get("title") or "untitled document")},
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
    rec = {"kind": kind, "status": "pending", "manifest": dict(manifest),
           "title": spec["title"](manifest),
           "contributor_id": caller.id, "contributor_label": caller.label}
    ident = store.save_contribution(rec)
    rec = store.load_contribution(ident)
    try:
        preview = spec["stage"](rec)
    except Declined as exc:
        store.update_contribution(ident, {"status": "failed", "problems": [str(exc)]})
        return {"status": "declined", "kind": kind, "contribution_id": ident,
                "problems": [str(exc)]}
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
             "review and immediately visible to you (and to reviewers) in every query, so "
             "you can test exactly what an analyst will see. It goes live for everyone only "
             "when a reviewer approves it.",
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
              "Returns {status: staged, contribution_id, preview{doc_id,...}, next} or "
              "{status: declined, problems: [...]} naming every problem at once — fix them all "
              "and resubmit. Rules: contributions never overwrite an existing source; a "
              "document already in the library is declined by its doc_id."]
    return "\n".join(lines)
