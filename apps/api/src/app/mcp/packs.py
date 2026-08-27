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
