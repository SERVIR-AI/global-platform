"""The assemble bone: a deterministic, citable evidence pack with a pack_id — the
seam where the consumer's own LLM takes over (flow B). Structured country/crop in
(no LLM in the tool path); wraps synthesis.gather_evidence and persists the pack so
verify/record can resolve it by id. Declared gaps are content, not omissions.
"""

from __future__ import annotations

from ..food_security import synthesis
from ..llm import MissingAPIKey
from ..rag.store import CorpusError
from . import context, loop, store


def assemble(country: str, crop: str, focus: str | None = None,
             override: list[dict] | None = None,
             override_country: str | None = None,
             override_crop: str | None = None) -> dict:
    """Gather evidence (two retrieval slices + live conditions + calendar) into a
    numbered, gap-declaring pack; mint and persist a pack_id. A calendar override
    is target-pinned (mismatch dropped + declared, via gather_evidence)."""
    if override is not None and not context._valid_override(override):
        return {"status": "declined",
                "note": "override must be a list of {season, planting:[m,m], harvest:[m,m]}"}
    parsed = {"crop": (crop or "").strip(), "country": (country or "").strip(),
              "focus": (focus or f"{country} {crop}").strip()}
    trace: list[str] = []
    try:
        citations, gaps, stats = synthesis.gather_evidence(
            parsed, trace, calendar_override=override,
            calendar_target=(override_country, override_crop))
    except MissingAPIKey as exc:
        return {"status": "declined", "note": f"embedding key missing: {exc}"}
    except CorpusError as exc:
        return {"status": "declined", "note": str(exc)}
    pack = {"country": parsed["country"], "crop": parsed["crop"], "focus": parsed["focus"],
            "citations": citations, "gaps": gaps, "queries": stats.get("queries"),
            # the sections verify_groundedness will require — so a drafter conforms
            # in one shot, without a throwaway probe call to learn them
            "required_sections": list(synthesis.SECTIONS),
            "stats": {k: v for k, v in stats.items() if k != "queries"}, "trace": trace}
    pack_id = store.save_pack(pack)
    # The forward edge rides ABOVE the citations, not below them: a model that has
    # what it needs to write stops reading, and everything after a 17-item array is
    # effectively unread. `next_step` is not persisted into the pack — it is
    # guidance for this call, not evidence (see loop.py for why it exists at all).
    return {"status": "ok", "pack_id": pack_id,
            "answer_status": loop.PACK_IS_NOT_AN_ANSWER,
            "next_step": loop.after_assemble(pack_id, pack["required_sections"]),
            **pack,
            # Repeated at the TAIL deliberately — see loop.YOUR_NEXT_OUTPUT.
            "your_next_output": loop.YOUR_NEXT_OUTPUT.format(pack_id=pack_id)}
