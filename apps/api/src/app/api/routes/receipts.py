"""The receipt resolver over HTTP — what makes a verdict re-resolvable.

An MCP tool call can't be made from a browser, so trust chrome (the groundedness
strip, and later the embeds) resolves state here instead. This is the difference
between a verdict frozen into a static page and one that is checked at view time.

Read-only: it serves what the platform already recorded. Ids are validated by the
store (16 hex), so an unknown or malformed id is a 404, never a path.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from ...mcp import record, store

router = APIRouter(prefix="/resolve")


def _readable_anywhere(response: Response) -> None:
    """A receipt is public proof, so it has to resolve from ANY origin.

    The consuming app lives on its own origin — that is the whole point of a
    verdict checked at view time rather than frozen into the page. Without this
    the trust chrome fails closed on every page the platform did not serve
    itself, and a builder's work always reads "unverified". The routes here are
    read-only and carry nothing user-specific, so a wildcard costs nothing;
    credentials never ride one, and these need none.
    """
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Vary"] = "Origin"


@router.get("/receipt/{receipt_id}")
def resolve_receipt(receipt_id: str, response: Response) -> dict:
    """The shareable proof: question, pack, verdict, sources, claim scope, and the
    hash of the exact text that passed. Claim-scoped on purpose — it attests
    traceability to the evidence pack, NOT the truth of the sources."""
    _readable_anywhere(response)
    receipt = store.load_receipt(receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"unknown receipt {receipt_id!r}")
    # A shared receipt has to arrive with its map and its evidence views, exactly
    # as it did when it was minted. These are derived, not stored, so they stay
    # live rather than freezing a view into the row.
    return {**receipt, **record.replay_view(receipt)}


@router.get("/report/{report_id}")
def resolve_report(report_id: str, response: Response) -> dict:
    """The gate's verdict and per-check detail, plus the full verified text."""
    _readable_anywhere(response)
    report = store.load_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"unknown report {report_id!r}")
    return report


@router.get("/pack/{pack_id}")
def resolve_pack(pack_id: str, response: Response) -> dict:
    """The evidence a claim was written from — the terminus of a trace-back."""
    _readable_anywhere(response)
    pack = store.load_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"unknown pack {pack_id!r}")
    return pack
