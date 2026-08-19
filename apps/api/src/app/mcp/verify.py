"""The verify bone: the deterministic groundedness gate over a persisted pack.
Draft + pack_id in; server-computed verdict + a persisted report_id out (rule 5:
the verdict is server-bound, never a client input). The report states its
evidence tier (rule 6). Thin over synthesis.check_grounded.
"""

from __future__ import annotations

import hashlib

from ..food_security import synthesis
from . import loop, store


def groundedness(draft: str, pack_id: str) -> dict:
    """Gate `draft` against the pack's numbered citations. Blocking failures:
    missing required sections, model-written Sources, no/phantom citations,
    uncited paragraphs. Recorded (not blocking): numbers absent from evidence.
    Persists a report_id resolvable later (feeds record_receipt)."""
    pack = store.load_pack(pack_id)
    if pack is None:
        return {"status": "declined",
                "note": f"no evidence pack with id {pack_id!r} — call assemble_pack first",
                "required_sections": list(synthesis.SECTIONS)}
    # Sections come from the PACK, not a food-security import — so a second domain
    # pack is gated against ITS OWN contract, not this one's.
    sections = pack.get("required_sections") or list(synthesis.SECTIONS)
    citations = pack.get("citations", [])
    r = synthesis.check_grounded(draft, citations, sections=sections)
    # Store the FULL verified text + its hash: a receipt that can't show what
    # passed can't answer "is the circulating copy the one you verified?".
    digest = hashlib.sha256(draft.encode("utf-8")).hexdigest()
    report = {"pack_id": pack_id, "passed": r["passed"],
              "evidence_tier": "platform-registered",
              "checks": r, "draft": draft, "draft_sha256": digest,
              "draft_excerpt": draft[:280]}
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
            "numbers_unverified_recorded": r["numbers_unverified"]}
