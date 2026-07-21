"""The record bone: mint a replayable receipt tying question + pack + verdict into
one resolvable id, and resolve it back. Completes rule 3 (replayable). The receipt
is claim-scoped (rule 6): it attests traceability to the pack, NOT the truth of the
underlying sources. Thin over mcp.store.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import store, ui


def _resolver_url(receipt_id: str) -> str:
    """Absolute so trust chrome resolves against the PLATFORM, never the consuming
    app's own origin. Config-driven: conf/ui_theme.json product.resolver."""
    r = ui.tokens()["product"]["resolver"]
    return r["base"] + r["receipt"].format(receipt_id=receipt_id)


def record(pack_id: str | None = None, report_id: str | None = None,
           receipt_id: str | None = None, question: str | None = None) -> dict:
    """Resolve mode (receipt_id given) returns the stored receipt. Mint mode
    (pack_id given) ties the pack + groundedness report into a receipt and
    persists it. A receipt minted without a report_id records an ungated pack
    (passed=null)."""
    if receipt_id:
        r = store.load_receipt(receipt_id)
        if r is None:
            return {"status": "declined", "note": f"no receipt with id {receipt_id!r}"}
        return {"status": "ok", **r}

    pack = store.load_pack(pack_id) if pack_id else None
    if pack is None:
        return {"status": "declined",
                "note": "to mint a receipt pass a valid pack_id (from assemble_pack)"}
    report = store.load_report(report_id) if report_id else None
    receipt = {
        "question": question or f"{pack.get('crop')} in {pack.get('country')}",
        "country": pack.get("country"), "crop": pack.get("crop"),
        "pack_id": pack_id, "report_id": report_id,
        "passed": (report or {}).get("passed"),
        "evidence_tier": (report or {}).get("evidence_tier", "platform-registered"),
        # the fingerprint of the exact text that passed — hash a circulating copy
        # and compare to prove it IS the verified one (full text lives in the report)
        "draft_sha256": (report or {}).get("draft_sha256"),
        "verified_text": "full text stored in the linked report_id",
        "sources": [{"n": c.get("n"), "source": c.get("source"), "title": c.get("title"),
                     "validation": c.get("validation"),
                     "archived_copy": c.get("archived_copy")}
                    for c in pack.get("citations", [])],
        "queries": pack.get("queries"), "gaps": pack.get("gaps"),
        "claim_scope": ("verified = every cited claim is traceable to this evidence pack "
                        "at mint time; NOT verified = the truth of the underlying sources"),
        "minted_at": datetime.now(timezone.utc).isoformat(),
    }
    rid = store.save_receipt(receipt)
    return {"status": "ok", "receipt_id": rid,
            "resolve_with": "record_receipt(receipt_id=...)",
            # The HTTP resolver a browser can reach. Consumers MUST point trust chrome
            # here: a verdict a consuming app serves about itself attests nothing.
            # Still a declared gap: this is a local/deployed platform URL, not yet a
            # public link that survives copy-paste outside the network.
            "public_resolver": _resolver_url(rid),
            **receipt}
