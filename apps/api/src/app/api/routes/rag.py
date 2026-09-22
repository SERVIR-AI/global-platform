"""The archived original, for ANY domain pack's library.

The archive mount was food-security-only, and every risk citation still carried
an `archived_copy` pointing at it. A reader following the trace-back on a flood
claim got a 404 — the link existed, looked right, and led nowhere, which is worse
than no link at all. The route is the same one, made honest about which library
it is serving.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ...rag.store import Corpus, CorpusError

router = APIRouter(prefix="/rag")


def _corpus_or_404(corpus_name: str) -> Corpus:
    """Only a library a pack actually declares. Never an arbitrary path."""
    from ...mcp import packs
    known = {p.get("corpus") for p in packs.PACKS.values() if p.get("corpus")}
    if corpus_name not in known:
        raise HTTPException(status_code=404, detail=f"unknown library {corpus_name!r}")
    try:
        return Corpus(corpus_name)
    except CorpusError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _refuse_unless_visible(corpus: Corpus, doc_id: str) -> None:
    """The staged-contribution visibility rule, in full.

    Checking only the index metadata was not enough and leaked: a REJECTED or
    withdrawn contribution has its index rows removed while the raw original is
    kept for replay, so `corpus.find` returns None and the file was served to
    anyone who asked. Measured: a rejected contribution's 674 KB original came
    back 200 to an unauthenticated caller here while the older route correctly
    404'd the same id. Absence from the index is not permission; the contribution
    ledger is what decides.
    """
    from ...contrib import identity
    found = corpus.find(doc_id)
    if found is not None:
        if not identity.visible(found["metadata"]):
            raise HTTPException(status_code=404, detail="unknown doc_id")
        return
    from ...mcp import store
    rec = next((r for r in store.list_contributions(kind="document")
                if (r.get("preview") or {}).get("doc_id") == doc_id
                or (r.get("landing") or {}).get("doc_id") == doc_id), None)
    if rec and rec.get("status") != "approved" \
            and not identity.current().may_see(rec.get("contributor_id")):
        raise HTTPException(status_code=404, detail="unknown doc_id")


@router.get("/{corpus_name}/document/{doc_id}")
def archived_document(corpus_name: str, doc_id: str):
    """Serve the archived original — the exact bytes the cited text was extracted
    from, immune to the source URL rotting or being overwritten upstream."""
    if not re.fullmatch(r"[0-9a-f]{16}", doc_id):
        raise HTTPException(status_code=404, detail="unknown doc_id")
    corpus = _corpus_or_404(corpus_name)
    _refuse_unless_visible(corpus, doc_id)
    path = corpus.raw_path(doc_id)
    if path is None:
        known = any(d["doc_id"] == doc_id for d in corpus.documents())
        raise HTTPException(status_code=404, detail=(
            "document is in the library but has no archived original (ingested "
            "before archiving existed) — re-ingest it, or fetch it from its "
            "source_url" if known else "unknown doc_id"))
    return FileResponse(path)
