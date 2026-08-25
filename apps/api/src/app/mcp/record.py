"""The record bone: mint a replayable receipt tying question + pack + verdict into
one resolvable id, and resolve it back. Completes rule 3 (replayable). The receipt
is claim-scoped (rule 6): it attests traceability to the pack, NOT the truth of the
underlying sources. Thin over mcp.store.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import feeds, loop, store, ui


def _abs(path: str) -> str:
    """Platform-absolute, from the same config the resolver uses."""
    if not path or path.startswith(("http://", "https://")):
        return path
    return ui.tokens()["product"]["resolver"]["base"].rstrip("/") + path


def _resolver_url(receipt_id: str) -> str:
    """Absolute so trust chrome resolves against the PLATFORM, never the consuming
    app's own origin. Config-driven: conf/ui_theme.json product.resolver."""
    r = ui.tokens()["product"]["resolver"]
    return r["base"] + r["receipt"].format(receipt_id=receipt_id)


_PULLED = ("conditions", "feed", "index")   # citation kinds fetched at pack time


def _is_pulled(c: dict) -> bool:
    """A live PULL, not an archived document. The distinction is load-bearing: an
    archived document can be re-read byte-for-byte forever, a pulled value cannot —
    re-running the same query next month returns a different number."""
    return c.get("kind") in _PULLED


def _sources(pack: dict) -> list[dict]:
    """The full passport per source. Previously this kept 5 fields and dropped
    pub_date, url, the literal query and staleness — so a receipt resting on a
    cache-served index was indistinguishable from one resting on fresh data.
    Rule 7 makes the receipt the artifact that outlives everything; it cannot be
    the lossiest one."""
    out = []
    for c in pack.get("citations", []):
        entry = {"n": c.get("n"), "source": c.get("source"), "title": c.get("title"),
                 "validation": c.get("validation"),
                 "archived_copy": c.get("archived_copy"),
                 # ABSOLUTE too. The relative form resolves against whatever origin
                 # is rendering — which for an MCP App iframe or a consumer's page is
                 # not us, so the archived copy 404s exactly where it matters most.
                 **({"archived_url": _abs(c["archived_copy"])}
                    if c.get("archived_copy") else {}),
                 "pub_date": c.get("pub_date"), "url": c.get("url"),
                 "temporal": c.get("temporal"), "score": c.get("score"),
                 "residency": c.get("residency"), "authority": c.get("authority"),
                 # how this evidence was obtained, and whether it can be re-read
                 "retrieval": "pulled-at-pack-time" if _is_pulled(c) else "archived-document",
                 "query_receipt": c.get("query")}
        stale = c.get("stale_data")
        if stale:
            # Carried either way: cadence/retrieved_at are real provenance. But only a
            # genuine fallback earns the caveat — see feeds.is_stale for why presence
            # is not the signal.
            entry["stale_data"] = stale
            if feeds.is_stale(stale):
                entry["caveat"] = ("served from last-good cache, not a live read — this "
                                   "value may pre-date the question")
        out.append({k: v for k, v in entry.items() if v is not None})
    return out


def _freshness(pack: dict) -> dict:
    """One place a reader can see, without parsing every source, whether this answer
    rested on live pulls and whether any of them were stale."""
    cites = pack.get("citations", [])
    pulled = [c for c in cites if _is_pulled(c)]
    stale = [c.get("n") for c in pulled if feeds.is_stale(c.get("stale_data"))]
    return {
        "pulled_sources": [c.get("n") for c in pulled],
        "archived_sources": [c.get("n") for c in cites if not _is_pulled(c)],
        "stale_sources": stale,
        "as_of": {c.get("n"): c.get("pub_date") for c in pulled if c.get("pub_date")},
        "note": ("Pulled sources were read at pack time and are a SNAPSHOT: re-running "
                 "the same query later can legitimately return different values, so this "
                 "receipt is not reproducible from the upstream — only from the stored pack."
                 + (f" Sources {stale} were served from cache, not a live read."
                    if stale else "")) if pulled else
                "All sources are archived documents and can be re-read unchanged.",
    }


def _claim_scope(pack: dict) -> str:
    """Scope wording that tells the truth about HOW the evidence was obtained."""
    base = ("verified = every cited claim is traceable to this evidence pack at mint "
            "time; NOT verified = the truth of the underlying sources")
    if any(_is_pulled(c) for c in pack.get("citations", [])):
        base += ("; live-feed values are a snapshot taken at pack time and are attested "
                 "as WHAT THE FEED SAID THEN, not as current")
    return base


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
        "sources": _sources(pack),
        "evidence_freshness": _freshness(pack),
        "queries": pack.get("queries"), "gaps": pack.get("gaps"),
        "claim_scope": _claim_scope(pack),
        "minted_at": datetime.now(timezone.utc).isoformat(),
    }
    rid = store.save_receipt(receipt)
    return {"status": "ok", "receipt_id": rid,
            "answer_status": loop.RECEIPT_NEEDS_SHOWING,
            "next_step": loop.after_record(rid),
            "resolve_with": "record_receipt(receipt_id=...)",
            # The HTTP resolver a browser can reach. Consumers MUST point trust chrome
            # here: a verdict a consuming app serves about itself attests nothing.
            # Still a declared gap: this is a local/deployed platform URL, not yet a
            # public link that survives copy-paste outside the network.
            "public_resolver": _resolver_url(rid),
            # HOW TO SHOW THIS, using the platform's own UI capability rather than
            # a visualisation the caller invents. `provenance_graph` is
            # receipt_bound and delivered as an EMBED, so it re-resolves its state
            # here at view time — which is the only way a verdict can be displayed
            # without being frozen into the page (rule 5).
            "render_with": {
                "provenance": {"tool": "ui_embed", "component": "provenance_graph",
                               "receipt_id": rid},
                "verdict": {"tool": "ui_embed", "component": "groundedness_strip",
                            "receipt_id": rid},
                "sources": {"tool": "ui_component", "component": "source_card",
                            "note": "one card per numbered source; copy-owned recipe"},
                "note": ("Do not hand-roll a chart or graph for this. Call ui_catalog to "
                         "see every component, then ui_embed for anything showing a "
                         "verdict and ui_component for anything you own outright."),
            },
            **receipt}
