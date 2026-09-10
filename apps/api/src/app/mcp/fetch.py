"""The fetch bone: corpus retrieval with passports, and document trace-back.
Thin over rag.store.Corpus.

Return contract (uniform, so a consumer renders declines without triggering one):
  status: "ok" | "empty" | "declined"
  hits/inventory present on "ok"; when there are none, `note` states the CAUSE
  ("empty" = corpus fine but nothing to show; "declined" = a hard failure).
  Always surface `note` when status != "ok" — that is rule 2 (declines say why).
"""

from __future__ import annotations

from ..config import get_settings
from ..food_security.routes import _CORPUS
from ..llm import MissingAPIKey
from ..rag.store import Corpus, CorpusError


def _passport(metadata: dict, doc_id: str, archived: bool) -> dict:
    """A hit's provenance passport (ARCHITECTURE bone 2). Validation level is real;
    authority certification is a declared Phase-3 gap, not invented here."""
    return {
        "source": metadata.get("source"),
        "title": metadata.get("title"),
        "pub_date": metadata.get("pub_date"),
        "validation": metadata.get("validation"),
        "event": metadata.get("event"),
        "residency": "platform-hosted",
        "authority": "not-yet-certified (Phase-3 regime)",
        "source_url": metadata.get("url"),
        "usage_notes": metadata.get("usage_notes"),
        "archived_copy": f"/api/food-security/rag/document/{doc_id}" if archived else None,
        "doc_id": doc_id,
    }


def search(query: str, k: int = 5, country: str | None = None, crop: str | None = None,
           temporal: str | None = None, doc_type: str | None = None) -> dict:
    """Top-k passages with passports. status='empty' with a `note` naming which of
    the three causes applies (empty library / filters exclude / below the floor);
    status='declined' with a `note` for a hard failure (missing key / torn corpus)."""
    floor = get_settings().rag_min_relevance
    k = max(1, min(int(k), 50))          # bounded like the REST route (1..50)
    base = {"query": query, "corpus": _CORPUS, "min_relevance": floor, "hits": []}
    try:
        corpus = Corpus(_CORPUS)
        filters = {"countries": country, "crops": crop,
                   "temporal": temporal, "doc_type": doc_type}
        hits = corpus.search(query, k=k, **filters)
    except MissingAPIKey as exc:
        return {**base, "status": "declined", "note": f"embedding key missing: {exc}"}
    except CorpusError as exc:
        return {**base, "status": "declined", "note": str(exc)}
    if hits:
        return {**base, "status": "ok",
                "hits": [{"score": h["score"], "text": h["text"],
                          "passport": _passport(h["metadata"], h["doc_id"],
                                                corpus.raw_path(h["doc_id"]) is not None)}
                         for h in hits]}
    n = len(corpus.documents())
    if n == 0:
        note = "The library is empty."
    elif corpus.count(**filters) == 0:
        note = f"{n} document(s) ingested, but none match the filters."
    else:
        note = (f"No document is relevant to {query!r} above the floor ({floor}) — "
                "the honest answer is a decline, not a weak match.")
    return {**base, "status": "empty", "note": note}


def document(doc_id: str | None = None) -> dict:
    """No id: the library inventory with passports. With id: that document's
    passport + archived-copy link (the trace-back terminus). status='declined'
    with a `note` if the corpus is unreadable or the doc_id is unknown."""
    try:
        corpus = Corpus(_CORPUS)
    except CorpusError as exc:
        return {"status": "declined", "note": str(exc)}
    docs = corpus.documents()
    if doc_id is None:
        return {"status": "ok", "corpus": _CORPUS, "documents": len(docs),
                "inventory": [{"doc_id": d["doc_id"], "chunks": d["chunks"],
                               "passport": _passport(d["metadata"], d["doc_id"],
                                                     corpus.raw_path(d["doc_id"]) is not None)}
                              for d in docs]}
    match = next((d for d in docs if d["doc_id"] == doc_id), None)
    if match is None:
        return {"status": "declined", "note": f"unknown doc_id {doc_id!r}"}
    # The trace-back terminus over MCP: the EXTRACTED TEXT we actually chunked and
    # cited. The archived original bytes need the REST mount (declared below).
    text = "\n\n".join(c["text"] for c in corpus._chunks if c["doc_id"] == doc_id)
    return {"status": "ok", "doc_id": doc_id, "chunks": match["chunks"],
            "passport": _passport(match["metadata"], doc_id,
                                  corpus.raw_path(doc_id) is not None),
            "extracted_text": text,
            "archived_original": ("the raw original bytes are served by the REST mount at "
                                  "`archived_copy`; over MCP you get the extracted text "
                                  "above (the exact text that was chunked and cited)")}
