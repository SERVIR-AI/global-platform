"""The record bone: mint a replayable receipt tying question + pack + verdict into
one resolvable id, and resolve it back. Completes rule 3 (replayable). The receipt
is claim-scoped (rule 6): it attests traceability to the pack, NOT the truth of the
underlying sources. Thin over mcp.store.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import store


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
            # a hosted, shareable verifier page (a link that survives copy-paste) is Phase 2
            "public_resolver": "declared gap: hosted shareable resolver is Phase 2",
            **receipt}
