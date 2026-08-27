"""The assemble bone: a deterministic, citable evidence pack with a pack_id — the
seam where the consumer's own LLM takes over (flow B). Structured target in (no
LLM in the tool path); dispatches on the PACKS registry so a second domain arrives
as a row, not an import; persists the pack so verify/record resolve it by id.
Declared gaps are content, not omissions.
"""

from __future__ import annotations

from ..llm import MissingAPIKey
from ..rag.store import CorpusError
from . import context, loop, packs, store


def assemble(country: str | None = None, crop: str | None = None,
             focus: str | None = None, pack: str | None = None,
             place: str | None = None, hazard: str | None = None,
             min_severity: int | None = None,
             override: list[dict] | None = None,
             override_country: str | None = None,
             override_crop: str | None = None) -> dict:
    """Gather one pack's evidence into a numbered, gap-declaring pack; mint and
    persist a pack_id. Back-compat: bare (country, crop) calls stay food-security,
    and FS packs keep top-level country/crop keys — Desktop configs and the embed
    resolver read them."""
    if override is not None and not context._valid_override(override):
        return {"status": "declined",
                "note": "override must be a list of {season, planting:[m,m], harvest:[m,m]}"}
    params = {"country": (country or "").strip(), "crop": (crop or "").strip(),
              "place": (place or "").strip(), "hazard": (hazard or "").strip(),
              "min_severity": min_severity}
    pack_id_name = packs.infer(pack, **params)
    if pack_id_name is None or pack_id_name not in packs.PACKS:
        return {"status": "declined",
                "note": (f"unknown pack {pack_id_name or pack!r} — available: "
                         + ", ".join(packs.available())
                         + ". Name one, or pass its target params: "
                         + "; ".join(f"{pid}: {', '.join(sp['target_keys'])}"
                                     for pid, sp in sorted(packs.PACKS.items()))),
                "available_packs": packs.available()}
    spec = packs.PACKS[pack_id_name]
    target, why = packs.build_target(pack_id_name, params)
    if target is None:
        return {"status": "declined", "note": why,
                "available_packs": packs.available()}
    focus = (focus or spec["default_focus"](target)).strip()
    trace: list[str] = []
    extras = {"override": override, "override_country": override_country,
              "override_crop": override_crop, "min_severity": min_severity}
    try:
        citations, gaps, stats = spec["gather"](target, focus, trace, extras)
    except MissingAPIKey as exc:
        return {"status": "declined", "note": f"embedding key missing: {exc}"}
    except ValueError as exc:
        # A gatherer refusing its target (unknown hazard, bad month...) is a
        # GOVERNED decline, not a transport error — rule 2, at the dispatch seam
        # so it holds for every pack.
        return {"status": "declined", "note": str(exc)}
    except CorpusError as exc:
        return {"status": "declined", "note": str(exc)}
    pack_body = {"pack": pack_id_name, "target": target, "focus": focus,
                 # FS keeps its historic top-level keys; other packs carry only
                 # `target` (the embed resolver reads pack.country/crop for FS)
                 **({"country": target.get("country"), "crop": target.get("crop")}
                    if pack_id_name == "food-security" else {}),
                 "citations": citations, "gaps": gaps,
                 "queries": stats.get("queries"),
                 # the sections verify_groundedness will require come from the
                 # PACK ROW — the contract is pack data, not imported code
                 "required_sections": list(spec["sections"]()),
                 "stats": {k: v for k, v in stats.items() if k != "queries"},
                 "trace": trace}
    if stats.get("viz") is not None:                # a pack may carry embed data
        pack_body["viz"] = stats["viz"]
        pack_body["stats"].pop("viz", None)
    pack_id = store.save_pack(pack_body)
    return {"status": "ok", "pack_id": pack_id,
            "answer_status": loop.PACK_IS_NOT_AN_ANSWER,
            "next_step": loop.after_assemble(pack_id, pack_body["required_sections"]),
            **pack_body,
            "your_next_output": loop.YOUR_NEXT_OUTPUT.format(pack_id=pack_id)}
