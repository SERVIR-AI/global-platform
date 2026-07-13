"""Food-security routes: health, the crop-conditions query (reported verbatim,
computed never), and the document-library endpoints over the shared RAG engine.
Coverage gaps decline honestly throughout."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
from fastapi import APIRouter, HTTPException, Query
from openai import OpenAIError
from pydantic import BaseModel, Field

from ..config import get_settings
from ..llm import MissingAPIKey
from ..rag import docloader
from ..rag.store import Corpus, CorpusError
from . import cropmonitor

router = APIRouter(prefix="/food-security")

_CORPUS = "food-security"     # the module's named, bounded document library
_MAX_DOC_BYTES = 50_000_000   # a bulletin PDF is a few MB; 50MB is already generous
_MAX_REDIRECTS = 3            # cropmonitor.org serves bulletins via one 302 hop


def _assert_public_http(url: str) -> None:
    """SSRF guard: http(s) only, no private/loopback ranges; re-checked per redirect
    hop. Resolve-then-connect race accepted at demo stage (egress proxy in prod)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400,
                            detail=f"only http(s) URLs can be ingested, got {parsed.scheme!r}")
    host = parsed.hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise HTTPException(status_code=502, detail=f"cannot resolve {host!r}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise HTTPException(status_code=400,
                                detail=f"{host!r} resolves to a non-public address")


def _fetch_document(url: str) -> tuple[bytes, str]:
    """Bounded fetch -> (bytes, final_url): per-hop redirect validation, size cap
    enforced while streaming (Content-Length is a claim, not a guarantee)."""
    for _ in range(_MAX_REDIRECTS + 1):
        _assert_public_http(url)
        try:
            r = requests.get(url, timeout=(5, 60), stream=True, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, r.headers.get("Location", ""))
                continue
            r.raise_for_status()
            declared = r.headers.get("Content-Length")
            if declared and int(declared) > _MAX_DOC_BYTES:
                raise HTTPException(status_code=413,
                                    detail=f"document exceeds {_MAX_DOC_BYTES} bytes")
            data = b""
            for block in r.iter_content(65536):
                data += block
                if len(data) > _MAX_DOC_BYTES:
                    raise HTTPException(status_code=413,
                                        detail=f"document exceeds {_MAX_DOC_BYTES} bytes")
            return data, url
        except requests.RequestException as exc:
            raise HTTPException(status_code=502,
                                detail=f"could not fetch {url}: {exc}") from exc
    raise HTTPException(status_code=502, detail=f"too many redirects fetching {url}")


@router.get("/health")
def health() -> dict:
    """Liveness for the food-security module + the upstream source it wraps."""
    return {"status": "ok", "module": "food-security",
            "source": get_settings().cropmonitor_url}


@router.get("/conditions")
def conditions(crop: str | None = None, month: str | None = None,
               place: str | None = None, geometry: bool = False) -> dict:
    """Per-region crop-condition assessments for a month (default: latest published)."""
    try:
        return cropmonitor.conditions(month=month, crop=crop, place=place,
                                      geometry=geometry)
    except ValueError as exc:                       # malformed month, not a coverage gap
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except cropmonitor.MonthNotAvailable as exc:
        raise HTTPException(status_code=404, detail={
            "error": "period_missing", "message": str(exc),
            "nearest_available": exc.nearest,
            # A span, not a promise of every month in it (December is skipped).
            "available": ({"from": exc.available[0], "to": exc.available[-1],
                           "note": "not every month in this span is published "
                                   "(the service skips December)"}
                          if exc.available else None)}) from exc
    except cropmonitor.CropNotFound as exc:
        raise HTTPException(status_code=404, detail={
            "error": "crop_not_found", "message": str(exc),
            "available": exc.available}) from exc
    except cropmonitor.CropMonitorError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


class IngestRequest(BaseModel):
    url: str | None = Field(default=None, description="Fetch this document (pdf/docx/txt/md).")
    text: str | None = Field(default=None, description="Or ingest raw text directly.")
    filename: str | None = Field(
        default=None, description="Overrides type sniffing when the URL has no extension.")
    metadata: dict = Field(
        default_factory=dict,
        description="Provenance tags — required: source, pub_date, title (or url); "
                    "optional: temporal, validation, countries, crops, doc_type, language.")


_TEMPORAL = ("forecast", "retrospective")
_VALIDATION = ("multi-agency-consensus", "single-agency", "model-output", "peer-reviewed")


def _require_provenance(metadata: dict, url: str | None) -> None:
    """A chunk without a source can never be cited honestly, so it must not enter."""
    missing = [f for f in ("source", "pub_date") if not metadata.get(f)]
    if not (metadata.get("title") or url):
        missing.append("title (or ingest by url)")
    if missing:
        raise HTTPException(status_code=422,
                            detail=f"metadata must carry provenance: missing {missing}")
    for field, allowed in (("temporal", _TEMPORAL), ("validation", _VALIDATION)):
        if metadata.get(field) and metadata[field] not in allowed:
            raise HTTPException(status_code=422,
                                detail=f"{field} must be one of {list(allowed)}, "
                                       f"got {metadata[field]!r}")


@router.post("/rag/ingest")
def rag_ingest(req: IngestRequest) -> dict:
    """Fetch/take one document -> extract -> chunk -> embed -> persist.
    Idempotent per document text."""
    metadata = dict(req.metadata)
    _require_provenance(metadata, req.url)
    if req.text is not None:
        text = req.text
    elif req.url:
        data, final_url = _fetch_document(req.url)
        name = req.filename or final_url.split("?")[0].rsplit("/", 1)[-1] or "document"
        try:
            text = docloader.extract_text(data, name)
        except docloader.DocLoadError as exc:  # typed, honest: OCR-needed, wrong format...
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        metadata.setdefault("url", req.url)
    else:
        raise HTTPException(status_code=400, detail="provide either url or text")
    try:
        return Corpus(_CORPUS).ingest(text, metadata)
    except docloader.DocLoadError as exc:      # e.g. whitespace-only inline text
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MissingAPIKey as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CorpusError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OpenAIError as exc:
        raise HTTPException(status_code=502, detail=f"embedding call failed: {exc}") from exc


@router.get("/rag/search")
def rag_search(q: str, k: int = Query(default=5, ge=1, le=50),
               country: str | None = None, crop: str | None = None,
               temporal: str | None = None, doc_type: str | None = None) -> dict:
    """Query the library; hits carry score + provenance. Empty results name their
    actual cause (empty library / filters / below the floor)."""
    try:
        corpus = Corpus(_CORPUS)
        filters = {"countries": country, "crops": crop,
                   "temporal": temporal, "doc_type": doc_type}
        hits = corpus.search(q, k=k, **filters)
    except MissingAPIKey as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CorpusError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except OpenAIError as exc:
        raise HTTPException(status_code=502, detail=f"embedding call failed: {exc}") from exc
    n_docs = len(corpus.documents())
    out = {"query": q, "corpus": _CORPUS, "documents": n_docs,
           "min_relevance": get_settings().rag_min_relevance, "hits": hits}
    if not hits:  # say which of the three truths applies, not one vague shrug
        if n_docs == 0:
            out["note"] = "The library is empty — no documents have been ingested yet."
        elif corpus.count(**filters) == 0:
            given = ", ".join(f"{k_}={v!r}" for k_, v in
                              [("country", country), ("crop", crop),
                               ("temporal", temporal), ("doc_type", doc_type)] if v)
            out["note"] = (f"{n_docs} document(s) ingested, but none match the "
                           f"filters ({given}).")
        else:
            out["note"] = (f"No document in the library is relevant to {q!r} — the "
                           "best match fell below the relevance floor "
                           f"({get_settings().rag_min_relevance}), so the honest answer "
                           "is a decline, not a weak match.")
    return out
