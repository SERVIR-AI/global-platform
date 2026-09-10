"""The assemble bone: a deterministic, citable evidence pack with a pack_id — the
seam where the consumer's own LLM takes over (flow B). Structured target in (no
LLM in the tool path); dispatches on the PACKS registry so a second domain arrives
as a row, not an import; persists the pack so verify/record resolve it by id.
Declared gaps are content, not omissions.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

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
    t_gather = time.perf_counter()
    try:
        citations, gaps, stats = spec["gather"](target, focus, trace, extras)
    except MissingAPIKey as exc:
        return {"status": "declined", "note": f"embedding key missing: {exc}"}
    except ValueError as exc:
        # A gatherer refusing its target (unknown hazard, bad month...) is a
        # GOVERNED decline, not a transport error — rule 2, at the dispatch seam
        # so it holds for every pack.
        return {"status": "declined", "note": str(exc)}
    except (OSError, RuntimeError) as exc:
        # Upstream infrastructure failing mid-gather (geocoder down, mirrors
        # exhausted, raster unreadable) must also decline with the cause. OSError
        # covers requests' connection errors and rasterio's IO errors alike.
        return {"status": "declined",
                "note": f"evidence gathering failed: {type(exc).__name__}: {exc}. "
                        "This is an upstream/infrastructure failure, not a coverage "
                        "gap — retrying later may succeed."}
    except CorpusError as exc:
        return {"status": "declined", "note": str(exc)}
    gaps_cit = packs.gaps_citation(citations, gaps)
    citations = [*citations, gaps_cit]
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
                 "trace": trace,
                 # execution provenance for the loop trace publish_answer surfaces
                 "exec": {"assembled_at": datetime.now(timezone.utc).isoformat(),
                          "gather_ms": round((time.perf_counter() - t_gather) * 1000, 1)}}
    if stats.get("viz") is not None:                # a pack may carry embed data
        pack_body["viz"] = stats["viz"]
        pack_body["stats"].pop("viz", None)
    pack_id = store.save_pack(pack_body)
    # The viz payload is PERSISTED for the embed resolver, never returned inline:
    # 850 KB of hazard polygons in a tool result is 850 KB in the consumer LLM's
    # context, paying tokens to carry pixels it cannot see (adversarial review).
    response = {k: v for k, v in pack_body.items() if k != "viz"}
    if "viz" in pack_body:
        response["viz_recorded"] = ("map payload recorded with the pack — rendered "
                                    "by the hazard_map embed for this receipt")
    return {"status": "ok", "pack_id": pack_id,
            # pack-level contributor guidance, read at the moment of use
            **({"usage_notes": spec["usage_notes"]}
               if spec.get("usage_notes") else {}),
            "answer_status": loop.PACK_IS_NOT_AN_ANSWER,
            "next_step": loop.after_assemble(pack_id, pack_body["required_sections"],
                                             gaps_citation_n=gaps_cit["n"]),
            **response,
            "your_next_output": loop.YOUR_NEXT_OUTPUT.format(pack_id=pack_id)}
