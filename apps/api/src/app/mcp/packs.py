"""The DOMAIN PACK registry — the seam that makes the second pack rows, not code.

ARCHITECTURE §2/§3: a new pack must add ZERO tools. Until now that was true for
feeds, adapters and compositions but false for the loop itself: `assemble_pack`
imported food_security.synthesis directly, so the assembler WAS the food-security
assembler. This registry is the fix: a pack is a row holding its gatherer, its
output contract (required_sections), its target schema and its manifest — and
assemble dispatches on the row.

Gatherers are LATE-BOUND (imported inside the function) because the domain modules
pull in llm/embedding dependencies that must not load just to read the registry —
same discipline as registry.pack_manifest's local import.
"""

from __future__ import annotations


def _gather_food_security(target: dict, focus: str, trace: list,
                          extras: dict) -> tuple[list, list, dict]:
    from ..food_security import synthesis
    parsed = {"country": target.get("country", ""), "crop": target.get("crop", ""),
              "focus": focus}
    return synthesis.gather_evidence(
        parsed, trace,
        calendar_override=extras.get("override"),
        calendar_target=(extras.get("override_country"), extras.get("override_crop")))


def _fs_sections() -> list[str]:
    from ..food_security import synthesis
    return list(synthesis.SECTIONS)


def _gather_risk(target: dict, focus: str, trace: list,
                 extras: dict) -> tuple[list, list, dict]:
    from ..risk import synthesis as risk_synthesis
    return risk_synthesis.gather_risk_evidence(target, focus, trace, extras)


def _risk_sections() -> list[str]:
    from ..risk import synthesis as risk_synthesis
    return list(risk_synthesis.SECTIONS)


def _risk_manifest() -> dict:
    """Honest v0: what the risk pack ships and what it does not. Real state where
    derivable, gaps declared in one place."""
    from ..graph.geo import tiffs
    cat = tiffs.catalog()
    hazards = sorted(k.removeprefix("hazard_") for k in cat if k.startswith("hazard_"))
    return {
        "id": "risk", "display_name": "Risk Platform", "version": "v0",
        "profile": "v0 — hazard exposure only; risk levels and corpus are declared gaps",
        "built_for": "asset exposure to a mapped hazard for one place, replayable",
        "output_contract": {
            "required_sections": _risk_sections(),
            "gate": "verify_groundedness — same blocking rules as every pack",
            "receipt": "record_receipt / publish_answer — pack carries the map viz",
        },
        "sources": {
            "corpus": {"status": "declared_gap",
                       "reason": "no risk document corpus exists yet"},
            "rasters": {"status": "available", "hazards": hazards,
                        "note": ("only hazard_flood states lineage (ADPC, derived "
                                 "from JRC GLOFAS v2.1); vintages/licences are "
                                 "unrecorded for all — declared in every pack")},
            "assets": {"status": "available", "source": "OSM via Overpass",
                       "note": "no retrieval date recorded in the AOI bundle"},
        },
        "target": {"place": "geocodable place name", "hazard": "one of: " + ", ".join(hazards),
                   "min_severity": "optional, 1-5, default 1"},
        "gaps": [
            "risk levels (L1/L2) not in the pack — exposure only, engine exists",
            "no risk corpus", "raster vintages/licences unrecorded",
            "BYOD uploads are session-scoped and cannot enter a pack",
        ],
        "compositions": {"risk.brief": "declared gap — runner not implemented; "
                                        "use the assemble -> publish_answer loop"},
    }


# id -> the pack row. `target_keys` name the assemble_pack parameters that select
# this pack; `build_target` validates them into the generic target dict every
# downstream consumer (record, publish, embeds) reads instead of country/crop.
PACKS: dict[str, dict] = {
    "food-security": {
        "display_name": "Food Security Platform",
        "version": "v0",
        "target_keys": ("country", "crop"),
        "target_doc": {"country": "country name", "crop": "crop name"},
        "gather": _gather_food_security,
        "sections": _fs_sections,
        "corpus": "food-security",
        "default_focus": lambda t: f"{t.get('country', '')} {t.get('crop', '')}".strip(),
    },
    "risk": {
        "display_name": "Risk Platform",
        "version": "v0",
        "target_keys": ("place", "hazard"),
        "target_doc": {"place": "geocodable place name",
                       "hazard": "hazard name, e.g. flood, fire, drought"},
        "gather": _gather_risk,
        "sections": _risk_sections,
        "corpus": None,
        "manifest": _risk_manifest,
        "default_focus": lambda t: f"{t.get('hazard', '')} exposure in {t.get('place', '')}".strip(),
    },
}


def available() -> list[str]:
    return sorted(PACKS)


def infer(pack: str | None, **params) -> str | None:
    """Which pack does this call want? Explicit `pack` wins; otherwise the pack
    whose target params were supplied. Ambiguous or empty -> None (decline)."""
    if pack:
        return pack
    supplied = {k for k, v in params.items() if v}
    hits = [pid for pid, spec in PACKS.items()
            if supplied & set(spec["target_keys"])]
    if len(hits) == 1:
        return hits[0]
    claimed = {k for spec in PACKS.values() for k in spec["target_keys"]}
    if supplied and not (supplied & claimed):
        return None                     # params no registered pack claims: say so
    if not hits and "food-security" in PACKS:
        return "food-security"          # historic default: bare calls meant FS
    return None


def build_target(pack_id: str, params: dict) -> tuple[dict | None, str | None]:
    """The generic target dict, or (None, reason) when required params are absent."""
    spec = PACKS[pack_id]
    target = {k: params.get(k) for k in spec["target_keys"] if params.get(k)}
    missing = [k for k in spec["target_keys"] if not target.get(k)
               and k not in spec.get("optional_keys", ())]
    if missing:
        return None, (f"pack {pack_id!r} needs {', '.join(missing)} "
                      f"(target params: {', '.join(spec['target_keys'])})")
    return target, None


def gaps_citation(citations: list, gaps: list) -> dict | None:
    """Declared gaps as the pack's LAST numbered citation, so the what's-missing
    section has something real to cite. Models kept writing honest gap paragraphs
    the gate then blocked as uncited — the pack declared gaps as content but gave
    them no citable identity. Any numbers inside a gap statement ride the citation
    text, so the number-scan can verify them like any other evidence."""
    if not gaps:
        return None
    n = max((int(c.get("n") or 0) for c in citations), default=0) + 1
    return {"n": n, "kind": "gaps", "source": "platform pack assembly",
            "title": "declared evidence gaps",
            "text": "Declared gaps in this evidence pack: "
                    + " ".join(f"({i}) {g}" for i, g in enumerate(gaps, 1)),
            "validation": "declared-by-platform", "retrieval": "config"}
