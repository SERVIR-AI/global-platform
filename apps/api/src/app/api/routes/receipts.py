"""The receipt resolver over HTTP — what makes a verdict re-resolvable.

An MCP tool call can't be made from a browser, so trust chrome (the groundedness
strip, and later the embeds) resolves state here instead. This is the difference
between a verdict frozen into a static page and one that is checked at view time.

Read-only: it serves what the platform already recorded. Ids are validated by the
store (16 hex), so an unknown or malformed id is a 404, never a path.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...mcp import store

router = APIRouter(prefix="/resolve")


@router.get("/receipt/{receipt_id}")
def resolve_receipt(receipt_id: str) -> dict:
    """The shareable proof: question, pack, verdict, sources, claim scope, and the
    hash of the exact text that passed. Claim-scoped on purpose — it attests
    traceability to the evidence pack, NOT the truth of the sources."""
    receipt = store.load_receipt(receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"unknown receipt {receipt_id!r}")
    return receipt


@router.get("/report/{report_id}")
def resolve_report(report_id: str) -> dict:
    """The gate's verdict and per-check detail, plus the full verified text."""
    report = store.load_report(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"unknown report {report_id!r}")
    return report


@router.get("/pack/{pack_id}")
def resolve_pack(pack_id: str) -> dict:
    """The evidence a claim was written from — the terminus of a trace-back."""
    pack = store.load_pack(pack_id)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"unknown pack {pack_id!r}")
    return pack
