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


def corpus_state(name: str) -> dict:
    """Real state of a pack's document library, not a literal. Every pack's corpus
    is named, starts empty and fills by contribution."""
    try:
        from ..rag.store import Corpus
        docs = Corpus(name).documents()
    except Exception as exc:
        return {"status": "unavailable", "name": name,
                "reason": f"{type(exc).__name__}: {exc}"}
    if not docs:
        return {"status": "empty", "name": name, "documents": 0,
                "note": ("the library exists and accepts contributions; nothing has been "
                         "contributed yet, so no publication can be cited alongside the "
                         "computed numbers")}
    return {"status": "available", "name": name, "documents": len(docs)}


def _risk_corpus_state() -> dict:
    return corpus_state("risk")


def _gather_land_cover(target: dict, focus: str, trace: list,
                       extras: dict) -> tuple[list, list, dict]:
    from ..land_cover import synthesis
    return synthesis.gather_land_cover_evidence(target, focus, trace, extras)


def _lc_sections() -> list[str]:
    from ..land_cover import synthesis
    return list(synthesis.SECTIONS)


def _lc_manifest() -> dict:
    from ..land_cover import synthesis
    return synthesis.manifest()


def _is_synthetic(row: dict) -> bool:
    """A demonstration layer, by its own declaration."""
    blob = " ".join(str(row.get(k) or "") for k in
                    ("title", "description", "source")).lower()
    return "synthetic" in blob or "demonstration layer" in blob


def text(spec: dict, key: str) -> str:
    """A pack row's text field, which may be a callable that reads live state —
    the catalog, the registry, the library — so the sentence a consumer model
    routes on names what the platform holds TODAY, not what it held the day the
    row was typed. Hand-typed summaries never advertised the seven return-period
    flood layers, flash flood, a contributed hazard, or any country a hub added
    documents for."""
    v = spec.get(key)
    if callable(v):
        try:
            return str(v() or "")
        except Exception:
            return ""
    return str(v or "")


def _risk_hazard_words() -> str:
    """The hazards the catalog serves, in words: return-period floods collapsed to
    one clause, synthetic demonstration layers left out."""
    import re
    from ..graph.geo import tiffs
    try:
        cat = tiffs.catalog()
    except Exception:
        return "mapped hazards"
    stems, rps = [], []
    for k, row in cat.items():
        if not k.startswith("hazard_") or (row or {}).get("internal") or _is_synthetic(row or {}):
            continue
        stem = k[len("hazard_"):]
        m = re.match(r"^(.*)_rp(\d+)$", stem)
        if m:
            rps.append((m.group(1), int(m.group(2))))
            continue
        if stem not in stems:
            stems.append(stem)
    words = list(stems)
    by_base: dict = {}
    for base, n in rps:
        by_base.setdefault(base, []).append(n)
    for base, ns in by_base.items():
        words.append(f"{base} at {min(ns)}- to {max(ns)}-year return periods")
    return ", ".join(words) if words else "mapped hazards"


def _risk_asset_words() -> str:
    """What can be counted under a hazard: people when a count grid is landed, the
    built-in OSM classes, and every contributed point layer by its title."""
    from ..graph.geo import registry as geo_registry, tiffs, vectors
    words = []
    try:
        if any(k.startswith("population_") for k in tiffs.catalog()):
            words.append("people")
    except Exception:
        pass
    words += ["roads", "schools", "hospitals", "buildings"]
    try:
        vis = vectors.visible()
        for layer in geo_registry.countable():
            if layer in vis:
                words.append(str(vis[layer].get("title") or layer).lower())
    except Exception:
        pass
    return ", ".join(words[:-1]) + " and " + words[-1]


def _risk_summary() -> str:
    return (f"HAZARD RISK — {_risk_asset_words()} exposed to a mapped hazard ({_risk_hazard_words()}) "
            "for any place in Southeast Asia, plus vulnerability-weighted risk levels")


def _risk_keywords() -> str:
    return (f"hazard exposure and risk: {_risk_asset_words()} in {_risk_hazard_words()} zones "
            "(Southeast Asia)")


def _fs_coverage() -> tuple[str, str]:
    from . import registry
    try:
        c = registry.coverage()
    except Exception:
        c = {}
    crops = ", ".join(c.get("crops") or []) or "crop"
    where = " and ".join(c.get("countries") or []) or "the countries hubs contribute documents for"
    return crops, where


def _fs_summary() -> str:
    crops, where = _fs_coverage()
    return (f"FOOD SECURITY — {crops} outlooks for {where} and the climate drivers behind them "
            "(ENSO, IOD, live indices, a governed document library)")


def _fs_keywords() -> str:
    crops, where = _fs_coverage()
    return f"food security, {crops} outlooks, El Niño / ENSO and IOD drivers ({where})"


def _lc_stacks_words() -> tuple[str, str]:
    """(classes, where) from the registered stacks' legends, titles, years and the
    countries their rows declare — a contributed national mangrove stack names its
    country here the moment it lands."""
    try:
        from ..land_cover.synthesis import landcover_stacks
        from ..graph.geo import stacks as _stacks
        rows = landcover_stacks()
    except Exception:
        rows = {}
    if not rows:
        return "land-cover classes", "no land-cover stack is registered yet"
    classes, where = [], []
    for sid, row in rows.items():
        for _k, label in sorted(((int(k), v) for k, v in (row.get("legend") or {}).items()),
                                key=lambda kv: kv[0]):
            lab = str(label).lower()
            if lab not in classes:
                classes.append(lab)
        try:
            ys = _stacks.years(row)
            span = f"{min(ys)}-{max(ys)}" if ys else ""
        except Exception:
            span = ""
        title = str(row.get("title") or sid)
        countries = [str(c) for c in (row.get("countries") or []) if str(c).strip()]
        where.append(title + (f", {span}" if span and span not in title else "")
                     + (f" ({', '.join(countries)})" if countries else ""))
    return ", ".join(classes), "; ".join(where)


def _lc_where() -> list[str]:
    """Where the land-cover pack answers, from the registered stacks' declared
    countries (else their titles) — never a typed list."""
    try:
        from ..land_cover.synthesis import landcover_stacks
        rows = landcover_stacks()
    except Exception:
        rows = {}
    out = []
    for sid, row in rows.items():
        countries = [str(c) for c in (row.get("countries") or []) if str(c).strip()]
        out.append(", ".join(countries) if countries else str(row.get("title") or sid))
    return out or ["no land-cover stack is registered yet"]


def _lc_summary() -> str:
    classes, where = _lc_stacks_words()
    return (f"LAND COVER — how much of a district, province, park or country is {classes} in a "
            f"mapped year; change and transitions between years; fire detections by class; "
            f"today over {where}")


def _lc_keywords() -> str:
    classes, where = _lc_stacks_words()
    return (f"land cover: {classes}, land-cover change, deforestation and forest gain, "
            f"fire detections ({where})")


def target_shapes(sep: str = " | ") -> str:
    """'country+crop | place+hazard | place+year' — generated from every pack's
    required target keys, so a fourth pack appears without an edit."""
    parts = []
    for _pid, sp in PACKS.items():          # declaration order: the order the packs introduce themselves
        req = [k for k in sp["target_keys"] if k not in sp.get("optional_keys", ())]
        parts.append("+".join(req))
    return sep.join(parts)


def target_shapes_long() -> str:
    """'food security: country + crop; risk: place + hazard; land cover: place + year
    [+ year_to, classes, stack]' — generated."""
    parts = []
    for _pid, sp in PACKS.items():
        req = [k for k in sp["target_keys"] if k not in sp.get("optional_keys", ())]
        opt = [k for k in sp["target_keys"] if k in sp.get("optional_keys", ())]
        parts.append(f"{sp.get('display_name', _pid).replace(' Platform', '').lower()}: "
                     + " + ".join(req) + (f" [+ {', '.join(opt)}]" if opt else ""))
    return "; ".join(parts)


def _vulnerability_coverage_line() -> str:
    """How much of the regional indicator scheme the recipes in force represent —
    read from the recipe, so a hub's wider recipe changes this sentence."""
    from ..graph.geo import combine
    try:
        recipes = combine._recipe().get("weights") or {}
        layers = sorted({l for w in recipes.values() for l in (w or {})})
        covered, absent, _outside = combine.families_covered({l: 1 for l in layers})
        n = len(combine.indicator_families())
        return (f"vulnerability recipes use {len(layers)} layer(s) across hazards, representing "
                f"{len(covered)} of the {n} indicator families the regional scheme names"
                + (f" (absent: {', '.join(absent)})" if absent else ""))
    except Exception as exc:
        return f"vulnerability coverage could not be read from the recipe ({type(exc).__name__})"


def _risk_manifest() -> dict:
    """Honest v0: what the risk pack ships and what it does not. Real state where
    derivable, gaps declared in one place."""
    from ..graph.geo import tiffs
    cat = tiffs.catalog()
    all_hazards = sorted(k.removeprefix("hazard_") for k in cat if k.startswith("hazard_"))
    # A synthetic demonstration layer must not sit in a hazard MENU looking like a
    # mapped hazard. Its own description says "SYNTHETIC DEMONSTRATION LAYER — not a
    # scientific product", but a consumer reading a list of names cannot see that:
    # UAT caught a brief offering `heatdays` to an air-quality officer as one of
    # "all 16 mapped hazards" for their city.
    synthetic = {h for h in all_hazards if _is_synthetic(cat.get(f"hazard_{h}") or {})}
    hazards = [h for h in all_hazards if h not in synthetic]
    return {
        "id": "risk", "display_name": "Risk Platform", "version": "v0",
        "profile": "v0 — hazard exposure, vulnerability-weighted risk levels, and a "
                   "document library that starts empty and grows by contribution",
        "built_for": "asset exposure to a mapped hazard for one place, replayable",
        "output_contract": {
            "required_sections": _risk_sections(),
            "gate": "verify_groundedness — same blocking rules as every pack",
            "receipt": "record_receipt / publish_answer — pack carries the map viz",
        },
        "sources": {
            "corpus": _risk_corpus_state(),
            "rasters": {"status": "available", "hazards": hazards,
                        **({"demonstration_only": sorted(synthetic),
                            "demonstration_note": (
                                "synthetic layers built to exercise the contribution "
                                "gate. They are NOT scientific products and are not "
                                "offered as hazards; naming one explicitly still "
                                "works and the brief says what it is.")}
                           if synthetic else {}),
                        "note": ("only hazard_flood states lineage (ADPC, derived "
                                 "from JRC GLOFAS v2.1); vintages/licences are "
                                 "unrecorded for all — declared in every pack")},
            "assets": {"status": "available", "source": "OSM via Overpass",
                       "note": "no retrieval date recorded in the AOI bundle"},
        },
        "target": {"place": "geocodable place name", "hazard": "one of: " + ", ".join(hazards),
                   "min_severity": "optional, 1-5, default 1"},
        "gaps": [
            "L1 precomputed risk layers exist in the catalog but are not offered here",
            _vulnerability_coverage_line(),
            "raster vintages/licences unrecorded for the layers that predate the "
            "contribution gate",
            "BYOD uploads are session-scoped and cannot enter a pack",
        ],
        "compositions": {"risk.brief": "available via compose_run — the platform "
                                        "LLM parses, drafts and gates server-side; "
                                        "or run the assemble -> publish_answer "
                                        "loop with your own model"},
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
        "cites_sources": ("feeds", "tables"),
        "summary": lambda: _fs_summary(),
        "audience": "food-security analysts, hub teams and ministry advisors deciding whether to act months ahead",
        "keywords": lambda: _fs_keywords(),
        "default_focus": lambda t: f"{t.get('country', '')} {t.get('crop', '')}".strip(),
        "doctor_target": {"country": "kenya", "crop": "maize"},
    },
    "risk": {
        "display_name": "Risk Platform",
        "version": "v0",
        "target_keys": ("place", "hazard"),
        "target_doc": {"place": "geocodable place name",
                       "hazard": "hazard name, e.g. flood, fire, drought"},
        "gather": _gather_risk,
        "sections": _risk_sections,
        # Domain packs are meant to be parallel. Food security accepts documents and
        # risk did not, purely because this field said None — not for want of data or
        # any technical limit. Named, so the same contribution path works for both.
        "corpus": "risk",
        "manifest": _risk_manifest,
        "cites_sources": ("feeds", "tables"),
        "summary": lambda: _risk_summary(),
        "audience": "disaster-risk and preparedness planners and the hubs that support them",
        "keywords": lambda: _risk_keywords(),
        "default_focus": lambda t: f"{t.get('hazard', '')} exposure in {t.get('place', '')}".strip(),
        "default_layers_doc": "its recipe's vulnerability layers",
        "doctor_target": {"place": "battambang", "hazard": "flood"},
    },
    "land-cover": {
        "display_name": "Land Cover Platform",
        "version": "v0",
        # `year` is required: without it a bare `place` would satisfy this pack too
        # and every risk call without a hazard would become ambiguous.
        "target_keys": ("place", "year", "year_to", "classes", "stack"),
        "optional_keys": ("year_to", "classes", "stack"),
        "target_doc": {"place": "district, province or park name inside a registered "
                                "land-cover stack's footprint (or pass geometry; place is then its label)",
                       "year": "a mapped year, e.g. 2022",
                       "year_to": "optional later year — adds change and transitions",
                       "classes": "optional class names to detail, e.g. [\"Forest\"]",
                       "stack": "optional: which registered stack to read when more than one "
                                "covers the place (the pack names the alternatives in its gaps)"},
        "gather": _gather_land_cover,
        "sections": _lc_sections,
        "corpus": "land-cover",
        # Contributed TABLES bound to this pack are read: one whose columns map
        # `class` and `factor` is applied to the class areas (and loss/gain) the
        # pack counts — carbon per hectare, a value per hectare, any per-hectare
        # coefficient — and cited with the arithmetic shown. Any other table bound
        # here is cited as context.
        "cites_sources": ("tables",),
        "manifest": _lc_manifest,
        "summary": lambda: _lc_summary(),
        "audience": "foresters, land-use and conservation planners, and the hubs that map land cover",
        "keywords": lambda: _lc_keywords(),
        "subjects": ["land cover by class", "forest, cropland, grassland and built-up change "
                     "between mapped years"],
        "where": _lc_where,
        "default_focus": lambda t: (f"land cover of {t.get('place', '')} in {t.get('year', '')}"
                                    + (f" against {t['year_to']}" if t.get("year_to") else "")).strip(),
        "doctor_target": {"place": "Bagmati Province, Nepal", "year": "2022"},
        "default_layers_doc": "the year maps its class areas are counted from",
        "usage_notes": lambda: _lc_usage_notes(),
    },
}


def _lc_usage_notes() -> str:
    """The land-cover pack's guidance, from the stacks registered today: their cell
    sizes and whether any declares an accuracy — two sentences that were typed for
    the first stack and stayed wrong for every stack after it."""
    try:
        from ..land_cover.synthesis import landcover_stacks
        from ..graph.geo import schema as _schema
        rows = landcover_stacks()
    except Exception:
        rows = {}
    cells, declared = [], []
    for sid, row in rows.items():
        try:
            px = (_schema._doc().get("stacks") or {}).get(sid, {}).get("pixel_size_deg")
        except Exception:
            px = None
        if px:
            cells.append(f"{round(float(px) * 111_320)} m")
        acc = str(row.get("accuracy") or "").strip()
        if acc and acc.lower() not in ("unstated", "unknown", "none"):
            declared.append(str(row.get("title") or sid))
    cell = ("Pixel counts of land-cover maps (cells of " + ", ".join(sorted(set(cells))) + ")"
            if cells else "Pixel counts of registered land-cover maps")
    acc = (f"Accuracy is declared by {len(declared)} stack(s) ({'; '.join(declared[:3])}) and "
           "stated in the pack's gaps; where a stack declares none, say so."
           if declared else "No stack declares an accuracy figure: say so.")
    return (f"{cell}, not a model. Quote the figures with their year and the mask the pack "
            "declares; change between two years includes classifier disagreement, so read "
            f"trends over several years. {acc}")

# Load errors from dropped-in pack modules, surfaced by doctor and capabilities —
# a broken contribution must be visible, never a silent absence or a dead server.
EXT_ERRORS: dict[str, str] = {}


def _load_ext_packs() -> None:
    """Merge app.packs_ext.<id> modules (each exporting SPEC) into PACKS.
    Contributed packs are droppable files, like declarative feeds. A module that
    fails to import or collides records an error and is skipped."""
    import importlib
    import pkgutil

    try:
        from .. import packs_ext
    except ImportError:
        return
    for info in pkgutil.iter_modules(packs_ext.__path__):
        name = info.name
        try:
            mod = importlib.import_module(f"..packs_ext.{name}", __package__)
            spec = getattr(mod, "SPEC", None)
            if not isinstance(spec, dict):
                EXT_ERRORS[name] = "module has no SPEC dict"
                continue
            pack_id = spec.get("id") or name
            if pack_id in PACKS:
                EXT_ERRORS[name] = (f"pack id {pack_id!r} collides with an existing "
                                    "pack — contributed packs cannot shadow")
                continue
            PACKS[pack_id] = {k: v for k, v in spec.items() if k != "id"}
        except Exception as exc:                    # never crash the server for one bad module
            EXT_ERRORS[name] = f"{type(exc).__name__}: {exc}"


_load_ext_packs()


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
    if len(hits) > 1:
        # Packs may share a key (`place` serves risk and land cover). The call that
        # supplies every REQUIRED key of exactly one pack meant that pack; a call
        # that fully satisfies none, or several, stays ambiguous and declines.
        full = [pid for pid in hits
                if all(k in supplied for k in PACKS[pid]["target_keys"]
                       if k not in PACKS[pid].get("optional_keys", ()))]
        if len(full) == 1:
            return full[0]
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


def gaps_citation(citations: list, gaps: list) -> dict:
    """Declared gaps as the pack's LAST numbered citation, so the what's-missing
    section has something real to cite. Models kept writing honest gap paragraphs
    the gate then blocked as uncited — the pack declared gaps as content but gave
    them no citable identity. ALWAYS present: a zero-gap pack still needs the
    entry, because "no gaps were identified" is itself a declaration a model will
    honestly write (observed blocking an FS draft) — and absence of declared gaps
    must never read as proof of completeness. Any numbers inside a gap statement
    ride the citation text, so the number-scan can verify them like any other
    evidence."""
    n = max((int(c.get("n") or 0) for c in citations), default=0) + 1
    text = ("Declared gaps in this evidence pack: "
            + " ".join(f"({i}) {g}" for i, g in enumerate(gaps, 1))
            if gaps else
            "No evidence gaps were declared at assembly time for this pack's "
            "scope. Absence of declared gaps is not a completeness guarantee.")
    return {"n": n, "kind": "gaps", "source": "platform pack assembly",
            "title": "declared evidence gaps", "text": text,
            "validation": "declared-by-platform", "retrieval": "config"}
