"""The verify bone: the deterministic groundedness gate over a persisted pack.
Draft + pack_id in; server-computed verdict + a persisted report_id out (rule 5:
the verdict is server-bound, never a client input). The report states its
evidence tier (rule 6). Thin over synthesis.check_grounded.
"""

from __future__ import annotations

import hashlib

from ..food_security import synthesis
from . import loop, packs, store


def groundedness(draft: str, pack_id: str) -> dict:
    """Gate `draft` against the pack's numbered citations. Blocking failures:
    missing required sections, model-written Sources, no/phantom citations,
    uncited paragraphs, and any number that appears in no citation and in no
    platform-computed figure. Warned (not blocking): a number attributed to a
    citation that does not contain it.
    Persists a report_id resolvable later (feeds record_receipt)."""
    pack = store.load_pack(pack_id)
    if pack is None:
        return {"status": "declined",
                "note": f"no evidence pack with id {pack_id!r} — call assemble_pack first",
                "available_packs": packs.available()}
    # Sections come from the PACK, not a food-security import — so a second domain
    # pack is gated against ITS OWN contract, not this one's.
    # ABSENT contract (legacy pack, pre-2026-08-27): those were all food-security
    # by construction — gate against FS sections and SAY so; review found the
    # falsy-collapse version reported required_sections=[] while blocking on the
    # FS headers: contradictory guidance a drafter can loop on forever. An EMPTY
    # contract is a storage bug: decline, never gate against another domain's
    # headers.
    sections = pack.get("required_sections")
    if sections is None:
        sections = list(synthesis.SECTIONS)
    elif not sections:
        return {"status": "declined",
                "note": (f"pack {pack_id!r} carries an EMPTY required_sections — a "
                         "storage bug, not a gateable contract. Re-assemble the pack.")}
    citations = pack.get("citations", [])
    # The pack's own computed figures count as evidence: the area of interest, the
    # asset totals, the weights. Quoting the platform's number back at it is the
    # opposite of making one up.
    r = synthesis.check_grounded(draft, citations, sections=sections,
                                 extra_evidence=pack.get("stats"))
    # Store the FULL verified text + its hash: a receipt that can't show what
    # passed can't answer "is the circulating copy the one you verified?".
    digest = hashlib.sha256(draft.encode("utf-8")).hexdigest()
    report = {"pack_id": pack_id, "passed": r["passed"],
              "evidence_tier": "platform-registered",
              "checks": r, "draft": draft, "draft_sha256": digest,
              "draft_excerpt": draft[:280]}
    if pack.get("staged_by"):
        report["staged_by"], report["staged_note"] = pack["staged_by"], pack.get("staged_note")
    report_id = store.save_report(report)
    return {"status": "ok", "report_id": report_id, "passed": r["passed"],
            # A verdict is a waypoint, not a destination — say which way is on.
            "answer_status": (loop.VERDICT_IS_NOT_A_RECEIPT if r["passed"]
                              else loop.BLOCKED),
            "next_step": loop.after_verify(pack_id, report_id, r["passed"],
                                           r["failures"]),
            "evidence_tier": "platform-registered", "draft_sha256": digest,
            "required_sections": list(sections),
            "failures": r["failures"], "cited": r["cited"],
            "phantom_citations": r["phantom_citations"],
            "missing_sections": r["missing_sections"],
            "uncited_paragraphs": r["uncited_paragraphs"],
            "numbers_unverified_recorded": r["numbers_unverified"],
            # A share of a cited total, allowed and shown so a reader can check it.
            "numbers_derived": r.get("numbers_derived") or {},
            # WARNING, never blocking: the figure IS in the pack, but not in the
            # citation the paragraph points at. Existence is not attribution, and a
            # reader following a claim to its source lands in the wrong place.
            "numbers_attributed_elsewhere": r.get("numbers_attributed_elsewhere") or [],
            # WARNING: a local rainfall/crop/food-security claim resting only on
            # evidence that says it describes the ocean and nothing about any
            # particular place. The drafting rules forbid it; this is where anyone
            # can see when it happened anyway.
            "local_claims_on_driver_evidence": r.get("local_claims_on_driver_evidence") or []}
