"""The assemble bone: a deterministic, citable evidence pack with a pack_id — the
seam where the consumer's own LLM takes over (flow B). Structured country/crop in
(no LLM in the tool path); wraps synthesis.gather_evidence and persists the pack so
verify/record can resolve it by id. Declared gaps are content, not omissions.
"""

from __future__ import annotations

from ..food_security import synthesis
from ..llm import MissingAPIKey
from ..rag.store import CorpusError
from . import context, store


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
            "stats": {k: v for k, v in stats.items() if k != "queries"}, "trace": trace}
    pack_id = store.save_pack(pack)
    return {"status": "ok", "pack_id": pack_id, **pack}
