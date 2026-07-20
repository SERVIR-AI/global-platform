"""The verify bone: the deterministic groundedness gate over a persisted pack.
Draft + pack_id in; server-computed verdict + a persisted report_id out (rule 5:
the verdict is server-bound, never a client input). The report states its
evidence tier (rule 6). Thin over synthesis.check_grounded.
"""

from __future__ import annotations

from ..food_security import synthesis
from . import store


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
    citations = pack.get("citations", [])
    r = synthesis.check_grounded(draft, citations)
    report = {"pack_id": pack_id, "passed": r["passed"],
              "evidence_tier": "platform-registered",
              "checks": r, "draft_excerpt": draft[:280]}
    report_id = store.save_report(report)
    return {"status": "ok", "report_id": report_id, "passed": r["passed"],
            "evidence_tier": "platform-registered",
            "required_sections": list(synthesis.SECTIONS),
            "failures": r["failures"], "cited": r["cited"],
            "phantom_citations": r["phantom_citations"],
            "missing_sections": r["missing_sections"],
            "uncited_paragraphs": r["uncited_paragraphs"],
            "numbers_unverified_recorded": r["numbers_unverified"]}
