"""The platform map: what exists, what's a declared gap. Reads real state (corpus,
calendars) and never hides a gap — the honesty ethos applied to the server itself.
Pure/no-network; safe to call before anything else in a build session.
"""

from __future__ import annotations

import os
import re
import threading

from ..food_security import calendar as fs_calendar
from ..rag.store import Corpus, CorpusError
from . import budget, packs

VERSION = "0.2.0-phase1"
_FS_CORPUS = "food-security"

# The seven-rule contract, in one line each — so an agent building on the server
# learns the rules it must build by (full text: docs/mcp/ARCHITECTURE.md §1).
CONTRACT = [
    "evidence attached to every insight",
    "declines say why",
    "everything replayable via a receipt",
    "human input declared (ADJUSTED, target-pinned)",
    "verdicts are server-bound (no client-set green tick)",
    "receipts state their evidence tier",
    "receipts outlive everything",
]

# Bone -> its tool(s) + where it's headed if not built yet. STATUS IS DERIVED from
# the live tool registry (see _bones), so the map can never drift from reality.
_BONE_DEFS = [
    {"bone": "resolve", "tools": ["resolve_place_time"], "planned": "planned (phase2)"},
    {"bone": "fetch", "tools": ["corpus_search", "corpus_document", "feeds_query"],
     "planned": "planned (phase2)",
     "note": "corpus + GEOGLAM conditions built; CHIRPS/CHIRTS/ERA5 are declared feed rows"},
    {"bone": "compute", "tools": ["compute_run"], "planned": "planned (phase2)"},
    {"bone": "context", "tools": ["context_get"], "planned": "planned (phase1)"},
    {"bone": "assemble", "tools": ["assemble_pack"], "planned": "planned (phase1)"},
    {"bone": "verify", "tools": ["verify_groundedness"], "planned": "planned (phase1)"},
    {"bone": "record", "tools": ["record_receipt"], "planned": "planned (phase1)"},
    {"bone": "contribute", "tools": ["contribute_submit"], "planned": "planned (phase2)"},
]


# Live feeds are REGISTRY ROWS queried through the one `feeds_query` tool — dataset
# is a parameter, so CHIRPS/CHIRTS/ERA5 land as entries here (zero new tools) when
# the hub data lists arrive. Pending feeds are declared, not hidden.
FEEDS = {
    "geoglam_conditions": {
        "status": "available",
        "adapter": "cropmonitor_conditions",   # which code talks to this upstream
        "description": "Per-region crop-condition assessments for a month.",
        "params": {"crop": "optional crop name",
                   "place": "optional country or region",
                   "month": "optional YYYYMM (default: latest published)"},
        "source": "GEOGLAM Crop Monitor (CMET)",
        "validation": "multi-agency-consensus",
        "residency": "external call-out",
    },
    # --- Phase 1 of the food-security use case: the ENSO / driver signal. The four
    # rows below are the doc's Data Inventory, 1:1. Three share ONE adapter because
    # they are the same shape (a monthly index series) — that is the entry+adapter
    # rule paying off: three feeds, no new dispatch code.
    "enso_oni": {
        "status": "available",
        "title": "Oceanic Nino Index (ONI)",
        "brief_role": "driver",
        "adapter": "climate_index",
        "index": "oni",
        "description": "Oceanic Nino Index — 3-month running Nino-3.4 SST anomaly; "
                       "detects and classifies ENSO onset and strength.",
        "params": {"limit": "optional number of recent seasons (default 12)"},
        "source": "NOAA CPC",
        "sst_basis": "ERSSTv5, 1991-2020 base",   # IRI restates OISST — see below
        "validation": "single-agency",
        "residency": "external call-out",
        "cadence": "monthly, ~5-day availability",
    },
    "iod_dmi": {
        "status": "available",
        "title": "Dipole Mode Index (IOD)",
        "brief_role": "driver",
        "adapter": "climate_index",
        "index": "dmi",
        "description": "Dipole Mode Index — the Indian Ocean Dipole, a co-driver that "
                       "can amplify or offset the ENSO signal.",
        "params": {"limit": "optional number of recent months (default 12)"},
        "source": "NOAA PSL (HadISST)",
        "validation": "single-agency",
        "residency": "external call-out",
        "cadence": "monthly",
    },
    "enso_event_history": {
        "status": "available",
        "title": "Historical ENSO event catalogue (1950-present)",
        "brief_role": "driver",
        "adapter": "climate_index",
        "index": "enso_events",
        "description": "Historical ENSO event catalogue (1950-present) for analogue "
                       "years and back-testing. DERIVED from the ONI series using CPC's "
                       "own definition, not a separately published product.",
        "params": {"min_seasons": "optional run length threshold (default 5)"},
        "source": "derived from NOAA CPC ONI",
        "validation": "single-agency",
        "residency": "external call-out",
        "cadence": "monthly (follows ONI)",
    },
    # WAS available and working until 2026-08-17, when IRI retired its JSON API.
    # The adapter and parser are kept intact (mcp/enso_forecast.py) because nothing
    # is wrong with them: flip `status` back to "available" the day a data endpoint
    # returns. Reproduce the break in three commands, see the reason below.
    "enso_plume": {
        "status": "declared_gap",
        "title": "CCSR/IRI ENSO model prediction plume",
        "adapter": "enso_forecast",
        "product": "enso_plume",
        "source": "IRI / Columbia Climate School",
        "reason": "RETIRED UPSTREAM, 2026-08-17. IRI moved this service from "
                  "ensoforecast.iri.columbia.edu to ensoforecast2 (the old host 301s to "
                  "the new one) and, in the move, replaced the machine-readable JSON API "
                  "with server-rendered images. Every data route now returns HTTP 403 "
                  "(plumes_json, plume_models, select_plumes, figure4_options), while the "
                  "new figure4_plot / figure7_plot / figure9_plot routes return 200 "
                  "image/svg+xml. Confirmed not an IP or user-agent block: 403 from two "
                  "separate networks, with and without a browser UA, Referer and Origin. "
                  "The SVG is NOT parseable back into data — it carries 211 <path> "
                  "elements, ZERO <text> elements and no data attributes, because the "
                  "labels are rendered as glyph outlines. Recovering per-model values "
                  "would mean OCR-ing vector outlines and reverse-engineering the axis "
                  "transform, which is the same fabrication risk we refuse for "
                  "enso_probabilities. Closing this needs IRI to restore a data endpoint. "
                  "Use enso_discussion for official status and probability wording, and "
                  "enso_oni for the observed index.",
        "params": {"year": "int, optional", "month": "int 1-12, optional"},
    },
    "enso_outlook": {
        "status": "available",
        "title": "IRI monthly ENSO/IOD quick look",
        "brief_role": "driver",
        "adapter": "enso_forecast",
        "product": "enso_outlook",
        "source": "IRI / Columbia Climate School",
        "sst_basis": "OISST, 1991-2020 base",
        "validation": "single-agency",
        "residency": "external call-out",
        "cadence": "monthly (issued mid-month)",
        "description": "IRI monthly ENSO/IOD quick look — the official forecast "
                       "narrative served VERBATIM by section. This is where the official "
                       "probability percentages live, as prose: quote and attribute them, "
                       "never re-type them as structured numbers.",
        "params": {"year": "int, optional — defaults to the latest published",
                   "month": "int 1-12, optional — defaults to the latest published"},
    },
    "enso_discussion": {
        "status": "available",
        "title": "CPC ENSO Diagnostic Discussion",
        "brief_role": "driver",
        "adapter": "enso_forecast",
        "product": "enso_discussion",
        "source": "NOAA CPC",
        "validation": "multi-agency-consensus",
        "residency": "external call-out",
        "cadence": "monthly (2nd Thursday)",
        "description": "NOAA CPC monthly ENSO Diagnostic Discussion, served VERBATIM. "
                       "The ONLY source for the formal ENSO Alert System Status "
                       "(Watch / Advisory / Final Advisory) — a status is a declaration, "
                       "not a measurement, so no index can supply it. Usually fresher "
                       "than the IRI quick look.",
        "params": {},
    },
    "enso_probabilities": {
        "status": "declared_gap",
        "reason": "A STRUCTURED probabilistic ENSO forecast (El Nino / neutral / La Nina "
                  "by lead season, as numbers). No machine-readable table is published: "
                  "IRI carries the percentages only in prose, CPC publishes them as a PNG, "
                  "and the IRI Data Library path is not public. Regex-ing prose for "
                  "probabilities would put a silently-wrong number in a governed brief. "
                  "Use `enso_outlook` and quote the narrative, or `enso_plume` for model "
                  "counts — counts are not probabilities and must not be presented as such. "
                  "Closing this needs a structured feed or an authenticated dataset path.",
        "params": {"lead": "?"},
    },
    "iod_dmi_bom": {
        "status": "declared_gap",
        "reason": "The Australian BoM's own IOD index. The use-case doc names BoM FIRST "
                  "for the DMI and we serve NOAA PSL instead, so the substitution is "
                  "declared rather than left implicit. BoM publishes no machine-readable "
                  "series — bom.gov.au/climate/enso/indices.shtml is ~58 KB of HTML with "
                  "the value in prose and charts (iod_1.txt: HTTP 404). Regex-ing a number "
                  "out of that page is the same antipattern refused for enso_probabilities, "
                  "so it is NOT done. Live consequence to be aware of: PSL lags materially "
                  "(2026-05 while ONI is at MJJ 2026), so the co-driver reads older than the "
                  "driver. Closing this needs a BoM data path, not a scraper.",
        "params": {},
    },
    # --- Phase 2 of the use case, declared now rather than left silent. These are
    # named in the hub's Data Inventory but had NO row at all, which is precisely the
    # hiding this registry exists to prevent: a reader saw no gap because nothing
    # said one existed.
    "icpac_ghacof": {
        "status": "declared_gap",
        "reason": "ICPAC GHACOF regional seasonal consensus outlook for the Greater Horn "
                  "of Africa — the regionally-authoritative downscaling of the ENSO signal, "
                  "and the source a Kenya-specific claim should rest on rather than a global "
                  "index. Published as PDF statements; needs a machine-readable path or "
                  "ingestion into the corpus.",
        "params": {"place": "?", "season": "?"},
    },
    "nmme_seasonal": {
        "status": "declared_gap",
        "reason": "North American Multi-Model Ensemble seasonal forecast (precip/temp). "
                  "Grids are public at ftp.cpc.ncep.noaa.gov/NMME/realtime_anom/ but carry "
                  "no SST, so nothing here substitutes for the ENSO plume; needs an "
                  "extraction step to reach a place-level number.",
        "params": {"place": "?", "variable": "?", "lead": "?"},
    },
    "ecmwf_seas5": {
        "status": "declared_gap",
        "reason": "ECMWF SEAS5 seasonal forecast. Public products are charts (opencharts "
                  "serves a CC-BY-4.0 PNG), not per-place numbers; the data path is the "
                  "Climate Data Store, which needs an account and licence acceptance.",
        "params": {"place": "?", "variable": "?", "lead": "?"},
    },
    "tamsat_rainfall": {
        "status": "declared_gap",
        "reason": "TAMSAT satellite rainfall estimates + soil moisture for Africa — the "
                  "Africa-specific complement to CHIRPS. Pending hub data lists.",
        "params": {"place": "?", "date_range": "?"},
    },
    "chirps_rainfall": {
        "status": "declared_gap",
        "reason": "rainfall + ~15-day forecast — pending hub data lists. NOTE: Phase 2 of "
                  "the use case, not Phase 1.",
        "params": {"place": "?", "date_range": "?"},
    },
    "chirts_temperature": {
        "status": "declared_gap",
        "reason": "daily max/min temperature (heat stress) — pending hub data lists",
        "params": {"place": "?", "date_range": "?"},
    },
    "era5_agromet": {
        "status": "declared_gap",
        "reason": "agro-met reanalysis — pending hub data lists",
        "params": {"place": "?", "date_range": "?", "variable": "?"},
    },
    "hub_s2s_forecast": {
        "status": "declared_gap",
        "reason": "the East-Africa hub's seasonal-to-sub-seasonal pipeline — pending hub contribution",
        "params": {"place": "?", "lead_time": "?"},
    },
}

# Every current feed belongs to the food-security pack; a row may override.
for _spec in FEEDS.values():
    _spec.setdefault("pack", "food-security")

# Bumped by every reload of the declarative rows: the connect-time text built from an older
# generation is stale (server._signature).
FEEDS_GEN = 0
_FEEDS_LOCK = threading.Lock()
_FEEDS_STAMP: tuple | None = None       # (folder, its *.yml files) as this process last read it


def _feeds_stamp() -> tuple:
    from ..config import get_settings
    d = str(get_settings().feeds_conf_dir)
    try:
        return d, tuple(sorted((e.name, e.stat().st_mtime_ns, e.stat().st_size)
                               for e in os.scandir(d) if e.name.endswith(".yml")))
    except OSError:
        return d, None


# Declarative rows (conf/feeds/*.yml) merge in beside the code rows — a feed of a
# known shape is a YAML file, no Python. Invalid specs register as status
# "invalid" with their reasons; collisions with code rows refuse loudly.
def _reload() -> None:
    """Parse first, then swap in one short step (new rows added at once, gone ones dropped), so
    a reader never sees the folder half-read. Caller holds _FEEDS_LOCK."""
    global FEEDS_GEN, _FEEDS_STAMP
    from ..config import get_settings
    from ..contrib import feedspecs
    stamp = _feeds_stamp()
    rows = feedspecs.load_dir(get_settings().feeds_conf_dir)
    code = {k: v for k, v in list(FEEDS.items()) if not v.get("declarative")}
    merged = dict(code)
    feedspecs.merge_into(merged, rows)
    fresh = {k: v for k, v in merged.items() if k not in code}
    for spec in fresh.values():
        spec.setdefault("pack", "food-security")
    FEEDS.update(fresh)
    for name in [k for k, v in list(FEEDS.items()) if v.get("declarative") and k not in fresh]:
        FEEDS.pop(name, None)
    FEEDS_GEN += 1
    _FEEDS_STAMP = stamp


def reload_declarative_feeds() -> list[str]:
    """Re-read conf/feeds/*.yml into the live registry without a restart — what
    an approved contribution needs. A row no longer in the folder is dropped."""
    with _FEEDS_LOCK:
        _reload()
    return sorted(k for k, v in list(FEEDS.items()) if v.get("declarative"))


def sync_declarative_feeds() -> bool:
    """Reload when another process (a uvicorn worker) changed the folder this one last read;
    a different folder (a test's own) is left to whoever switched to it."""
    now = _feeds_stamp()
    if _FEEDS_STAMP is None or now[0] != _FEEDS_STAMP[0] or now == _FEEDS_STAMP:
        return False
    with _FEEDS_LOCK:
        if _feeds_stamp() == _FEEDS_STAMP:
            return False
        _reload()
    return True


reload_declarative_feeds()


# Compositions are REGISTRY ROWS invoked through the one `compose_run` tool — so a
# new pack/composition adds zero tools (the count discipline, ARCHITECTURE §2).
COMPOSITIONS = {
    "foodsecurity.brief": {
        "description": "Question -> governed 4-section cited brief: evidence assembled, "
                       "drafted, groundedness-gated, receipt minted.",
        "params": ["question", "override?", "override_country?", "override_crop?",
                   "provider?", "model?"],
        "runs_llm_server_side": True,
        "llm": ("the PLATFORM's model and key; `provider`/`model` choose among the "
                "platform's configured providers, NOT your own credentials. To use "
                "YOUR model, run the loop instead: assemble_pack -> you draft -> "
                "verify_groundedness (same evidence, same gate, same receipt)."),
        "for": ("consumers with no model credentials or no place to run code "
                "(dashboards, cron, REST) — the accompanied path"),
    },
    "risk.brief": {
        "description": "Question -> governed 4-section cited hazard-exposure "
                       "brief for a place x hazard: deterministic geo evidence "
                       "assembled, drafted, groundedness-gated, receipt minted.",
        "params": ["question", "provider?", "model?"],
        "runs_llm_server_side": True,
        "llm": ("the PLATFORM's model and key; the LLM only parses the target and "
                "phrases the brief — every number is computed deterministically. "
                "To use YOUR model, run the loop instead: assemble_pack(pack="
                "'risk', place=..., hazard=...) -> you draft -> publish_answer."),
        "for": ("consumers with no model credentials or no place to run code "
                "(dashboards, cron, REST) — the accompanied path"),
    },
    "landcover.brief": {
        "description": "Question -> governed 4-section cited land-cover brief for a place "
                       "x year (x a later year): class areas, change and transitions "
                       "counted deterministically, drafted, groundedness-gated, receipt minted.",
        "params": ["question", "provider?", "model?"],
        "runs_llm_server_side": True,
        "llm": ("the PLATFORM's model and key; the LLM only parses the target and "
                "phrases the brief — every figure is a pixel count. To use YOUR model, "
                "run the loop instead: assemble_pack(pack='land-cover', place=..., "
                "year=..., year_to=...) -> you draft -> publish_answer."),
        "for": ("consumers with no model credentials or no place to run code "
                "(dashboards, cron, REST) — the accompanied path"),
    },
}


def _bones(available: set[str]) -> list[dict]:
    """Status derived from which tools are actually registered — never hand-set."""
    out = []
    for b in _BONE_DEFS:
        built = [t for t in b["tools"] if t in available]
        status = ("available" if len(built) == len(b["tools"])
                  else "partial" if built else b["planned"])
        row = {"bone": b["bone"], "tools": b["tools"], "status": status}
        if b.get("note"):
            row["note"] = b["note"]
        out.append(row)
    return out


@budget.once
def _corpus_summary() -> dict:
    """Real doc/chunk counts + event windows, or a declared reason it's unavailable."""
    try:
        corpus = Corpus(_FS_CORPUS)
        docs = corpus.documents()
        events = sorted({d["metadata"].get("event") for d in docs
                         if d["metadata"].get("event")})
        return {"status": "available", "documents": len(docs),
                "chunks": corpus.count(), "events": events}
    except CorpusError as exc:
        return {"status": "unavailable", "reason": str(exc)}
    except Exception as exc:  # never let the map crash on a missing corpus
        return {"status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}


def _calendars() -> list[dict]:
    """Real hub-default calendars available for context/override."""
    cal = fs_calendar.load()
    return [{"country": c, "crops": sorted(crops)} for c, crops in sorted(cal.items())]


def pack_manifest(pack_id: str = "food-security") -> dict:
    """A DOMAIN PACK manifest — the versioned bundle a hub adopts: what ships,
    what it produces, and what is missing. The food-security body is authored
    here (the v0 pattern); other packs supply `manifest` on their PACKS row."""
    if pack_id != "food-security":
        spec = packs.PACKS.get(pack_id)
        if spec is None:
            return {"id": pack_id, "error": "unknown pack",
                    "available": packs.available()}
        builder = spec.get("manifest")
        if builder is None:
            return {"id": pack_id,
                    "display_name": spec.get("display_name", pack_id),
                    "version": spec.get("version", "v0"),
                    "target": spec.get("target_doc", {}),
                    **({"usage_notes": packs.text(spec, "usage_notes")}
                       if packs.text(spec, "usage_notes") else {}),
                    "note": "this pack registers no manifest — target params only"}
        try:
            return builder()
        except Exception as exc:   # same discipline as _corpus_summary: the map
            return {"id": pack_id,  # never crashes on one pack's bad day
                    "display_name": spec.get("display_name", pack_id),
                    "status": "manifest unavailable",
                    "reason": f"{type(exc).__name__}: {exc}"}
    from ..food_security import synthesis  # local: avoids importing llm deps at module load
    corpus = _corpus_summary()
    gaps = [
        "climate feeds (CHIRPS/CHIRTS/ERA5) — pending hub data lists",
        "analytics / threshold formulas — pending hub contribution (compute bone is phase2)",
        "acreage-under-stress — needs crop-type/land-cover masks (deferred)",
        "MCP Apps widgets — phase2 (web widgets exist in apps/web)",
        "sub-national regions are not resolved to geometries — gazetteer is phase2",
        "archived ORIGINAL bytes need the REST mount; over MCP corpus_document "
        "returns the extracted text that was cited",
    ]
    if corpus.get("status") != "available":
        gaps.append(f"corpus unavailable: {corpus.get('reason')}")
    demo = store_latest_receipt("food-security")
    return {
        "id": "food-security", "display_name": "Food Security Platform",
        "version": "v0",
        "profile": "Phase-1 profile — what ships today, gaps declared",
        "built_for": (f"{packs._fs_coverage()[0]} outlooks for {packs._fs_coverage()[1]} — built "
                      "around the hub-locked PoC question (early signs of maize failure in the "
                      "current season, Kenya Trans-Nzoia vs Zambia)"),
        "output_contract": {
            "required_sections": list(synthesis.SECTIONS),
            "gate": ("verify_groundedness — BLOCKING on: required sections present, "
                     "no self-written Sources, citations resolve to the pack, every "
                     "paragraph cited. Recorded not blocking: numbers absent from evidence."),
            "receipt": "record_receipt — question + pack + verdict + verified-text hash",
        },
        "sources": {
            "corpus": corpus,
            "conditions": {"status": "available", "source": "GEOGLAM Crop Monitor",
                           "note": "staleness is flagged in the evidence when served "
                                   "from last-good cache"},
            "feeds": {k: {"status": v["status"],
                          **({"source": v["source"]} if v.get("source") else {}),
                          **({"reason": v["reason"]} if v.get("reason") else {})}
                      for k, v in FEEDS.items()
                      if v.get("pack", "food-security") == "food-security"},
        },
        "calendars": _calendars(),
        "calendar_provenance": ("hub defaults are hand-authored approximations of "
                                "GEOGLAM/FAO baselines; a hub-provided passported "
                                "calendar is pending. Per-request overrides are honored "
                                "and labelled ADJUSTED."),
        "compositions": sorted(COMPOSITIONS),
        "guidance": {"instructions": "delivered at connect",
                     "prompts": ["build_a_tool", "run_analysis", "explain_platform"],
                     "resources": ["servirplatform://how-to-use", "servirplatform://pack/food-security"]},
        "gaps": gaps,
        "worked_example": ({"receipt_id": demo,
                            "resolve_with": f"record_receipt(receipt_id='{demo}')",
                            "note": "a real receipt from this server — resolve it "
                                    "instead of inventing a sample payload"}
                           if demo else
                           {"receipt_id": None,
                            "note": "none yet — run compose_run (or the "
                                    "assemble→verify→record loop) to mint one"}),
    }


def store_latest_receipt(pack: str | None = None):
    from . import store  # local import keeps registry importable without the DB
    try:
        return store.latest_receipt_id(pack)
    except Exception:
        return None


def _design_language() -> dict:
    """So a builder learns from platform_capabilities that the design language is
    live, rather than only discovering it if they happen to call ui_catalog."""
    try:
        from . import figma
        c = figma.read_components()
        return {"source": "figma-live", "file": c["provenance"]["file"],
                "last_modified": c["provenance"]["last_modified"],
                "brand_component_sets": [s["name"] for s in c["sets"]],
                "tools": ["ui_design", "ui_catalog", "ui_component", "ui_embed"],
                "note": ("Colour, type and brand assets are read from the design file at "
                         "call time. ui_design falls back to the committed theme if it is "
                         "unreachable and always states which source you got.")}
    except Exception:
        return {"source": "config",
                "tools": ["ui_design", "ui_catalog", "ui_component", "ui_embed"],
                "note": ("The design file is not reachable, so the committed theme is "
                         "served. It is a maintained copy, not the live design file.")}


def _skills_available() -> list[str]:
    from . import skills
    return skills.available()


def _methods_listed() -> dict:
    try:
        from ..contrib import methods
        return methods.catalog()
    except Exception:                       # the map never crashes on a method file
        return {}


def _staged_feeds() -> dict:
    try:
        from ..contrib import staging
        return staging.staged_feeds_for_caller()
    except Exception:                       # the map never crashes on staging
        return {}


def capabilities(available_tools=None, available_prompts=None,
                 available_resources=None) -> dict:
    """The honest platform map. Bone status + the tool/prompt/resource lists are
    DERIVED from the live registry (passed in by the tool), so the map cannot drift
    from what the server actually exposes. Sources/calendars real; gaps declared."""
    tools = sorted(available_tools or [])
    prompts = sorted(available_prompts or [])
    resources = sorted(available_resources or [])
    layers = raster_layers()
    return {
        "server": {"name": "servirplatform", "version": VERSION,
                   "transport": os.environ.get("GRP_MCP_TRANSPORT", "stdio")},
        "contract": CONTRACT,
        "usage": {"instructions": "delivered to the LLM at connect (initialize)",
                  "skills": {n: f"servirplatform://skill/{n}" for n in _skills_available()},
                  "prompts": prompts,  # user-selectable in the host menu (build vs run)
                  "resources": resources,  # human-readable guides (e.g. servirplatform://how-to-use)
                  "modes": ["build-time: build a reusable app on the tools",
                            "run-time: answer one question now"]},
        # Contributed logic first: what people mean by "methods" here. The listing used
        # to bury it past 125 KB of feed rows, where no client read it (MGV-517).
        "methods_note": ("METHODS are logic people contributed (formulas, thresholds, rules): "
                         "every answer of their pack computes them and cites the result. They "
                         "are not tools. To use one in something you build, read its result "
                         "from a pack (assemble_pack, then publish_answer for the receipt) and "
                         "show that figure; never copy its steps into your code. A method gives "
                         "one figure per area, not a verdict per unit. When the user's request "
                         "is a judgement (a verdict, a priority, a 'good candidate'), do not "
                         "silently adopt an existing method as its rule: suggest it, or another, "
                         "for them to accept or reject first. Add one with contribute_submit "
                         "kind=method."),
        "methods": _methods_listed(),
        # Coverage BY NAME — the opening of the instructions carries counts only — and every
        # raster layer in the catalog, whole, built-in and contributed alike.
        "coverage": coverage_by_name(layers),
        "raster_layers": layers,
        "tools_available": tools,
        # Payload-carried on purpose: connect-time INSTRUCTIONS lose to whatever
        # rides the tool results (measured — render_with was honored to the letter
        # in the same builds that ignored an instructions-only observability rule).
        # A builder reads THIS payload at build time, so the requirement lands here.
        "builder_requirements": {
            "note": ("requirements for anything you BUILD on these tools (a script, "
                     "an app, a page) — part of the capability contract, not advice; "
                     "a build that skips one ships unfinished"),
            "observability": {
                "rule": ("instrument your build: every run must emit its own "
                         "execution trace — a trace_id, per-step duration_ms, a "
                         "one-line summary per step, and token usage for any step "
                         "that calls a model"),
                **({"read_first": "servirplatform://skill/trace-emit"}
                   if "trace-emit" in _skills_available() else {}),
                "surface": ("publish_answer returns `trace` — the platform's own "
                            "loop execution (assemble/draft/verify/record). Show it "
                            "in what you build; do not swallow it"),
                "prove": ("a finished build includes one real captured trace from "
                          "a real run"),
            },
        },
        # registry rows invoked via compose_run — not tools, so the count stays flat
        "compositions": {k: v for k, v in COMPOSITIONS.items()},
        # registry rows queried via feeds_query — dataset is a parameter. One summary per
        # feed: the full rows (fetch specs, long descriptions) made this map ~125 KB, too
        # large for a client to read whole; feeds_query returns any feed in full.
        "feeds_note": ("one summary per feed or table; feeds_query(dataset=...) returns its "
                       "records, its full passport and its parameters"),
        "feeds": {k: _feed_brief(v) for k, v in FEEDS.items()},
        # the caller's own staged contributions (reviewers: all) — queryable by
        # them alone until approved; never a platform feed to anyone else
        "staged_feeds": _staged_feeds(),
        "bones": _bones(set(tools)),
        # NB two different things are called "pack": a DOMAIN PACK (below) is the
        # versioned bundle of sources/calendars/composition — there is one per
        # domain. An EVIDENCE PACK (pack_id, from assemble_pack) is a per-question
        # snapshot of gathered evidence — a new one is minted on every call.
        "design_language": _design_language(),
        "domain_packs": [_pack_brief(pid, pack_manifest(pid)) for pid in packs.available()],
    }


_FEED_KEEP = ("title", "status", "pack", "source", "cadence", "validation", "license",
              "countries", "bbox", "contributed")


def _feed_brief(row: dict) -> dict:
    """A feed as the map shows it: what it is, its state and how to ask for it."""
    out = {k: row[k] for k in _FEED_KEEP if row.get(k) not in (None, "", [], {})}
    if isinstance(out.get("source"), str) and len(out["source"]) > 140:
        out["source"] = out["source"][:140] + "…"
    try:
        from . import feeds as _feeds
        params = _feeds.params_doc(row)
    except Exception:
        params = row.get("params") if isinstance(row.get("params"), dict) else {}
    if params:
        out["params"] = {str(k): str(v) for k, v in params.items()}   # name -> what it takes
    if row.get("status") != "available" and row.get("reason"):
        out["declared_gap"] = str(row["reason"])
    if row.get("description"):
        out["about"] = str(row["description"])     # whole: a table's caveat is a gate field
    # the field names each record carries, so "what does it hold" needs no records read
    fetch = row.get("fetch") or {}
    names = fetch.get("fields") or fetch.get("columns")
    if isinstance(names, dict) and names:
        out["fields"] = list(names)
    return out


def _pack_brief(pid: str, manifest: dict) -> dict:
    """A domain pack as the map shows it: its sources counted, the full list one
    resource away (the land-cover pack alone listed 32 KB of them here)."""
    out = dict(manifest)
    srcs = out.pop("sources", None)
    if isinstance(srcs, list):
        out["sources"] = {"count": len(srcs), "full_list": f"servirplatform://pack/{pid}"}
    return out


@budget.once
def coverage() -> dict:
    """What this platform actually covers RIGHT NOW, read from live state (once per snapshot).

    Exists so tool descriptions can state their subject matter without anyone
    hand-maintaining a keyword list. A model picks a tool from its first line, so
    that line has to name the subject — but hardcoding "El Nino, drought, maize"
    guarantees it goes stale the moment a feed or a domain pack is added, which is
    the drift this registry exists to prevent everywhere else.
    """
    subjects, publishers, countries, crops = [], set(), set(), set()
    for spec in list(FEEDS.values()):
        if spec.get("status") != "available":
            continue
        if spec.get("title"):
            subjects.append(spec["title"])
        if spec.get("source"):
            publishers.add(spec["source"])
    try:
        corpus = Corpus(_FS_CORPUS)
        for ch in corpus._chunks:
            m = ch.get("metadata") or {}
            if m.get("staged_by"):          # a preview is nobody's coverage yet
                continue
            countries.update(m.get("countries") or [])
            crops.update(m.get("crops") or [])
            if m.get("source"):
                publishers.add(m["source"])
    except (CorpusError, AttributeError):
        pass                                    # coverage degrades, never raises
    return {"subjects": subjects,
            "countries": sorted(countries), "crops": sorted(crops),
            "publishers": sorted(publishers)}


def coverage_line() -> str:
    """One sentence naming what we cover, generated. Drop into a tool description
    so it re-states itself as the platform grows."""
    c = coverage()
    where = " and ".join(c["countries"]) if c["countries"] else "East and Southern Africa"
    what = ", ".join(c["crops"]) or "crops"
    # NOT truncated. Silently dropping the 6th subject would reintroduce exactly the
    # staleness this function exists to prevent: a capability we added but no longer
    # advertise is one a model will never pick us for.
    subj = "; ".join(c["subjects"])
    pubs = publisher_names()
    shown = ", ".join(pubs[:6])
    more = f" and {len(pubs) - 6} others" if len(pubs) > 6 else ""
    # Lead with every DOMAIN, one clause each from its pack row (`summary`), so a
    # model asked about a forest or a flood sees itself in the first sentence. A
    # Desktop model with web search enabled skipped these tools for a land-cover
    # question when this line opened with maize and ENSO. Packs without a summary
    # still get their subjects/where; the live feed list follows for food security.
    domains = []
    for pid, sp in sorted(packs.PACKS.items()):
        if packs.text(sp, "summary"):
            domains.append(packs.text(sp, "summary"))
        elif sp.get("subjects"):
            where = sp.get("where")
            where = where() if callable(where) else where
            domains.append(f"{', '.join(sp['subjects'])} in "
                           f"{' and '.join(where or ['its registered footprint'])}")
    lead = ("Covers " + "; ".join(domains) + ". " if domains else
            f"Covers {what} and food security in {where}. ")
    return (lead + f"Food-security specifics: {what} in {where}, and the climate drivers "
            f"behind them ({subj}). Sources include {shown}{more}.")


def coverage_by_name(layers=None) -> dict:
    """What the platform covers, by name, derived: each pack's own summary, the live feeds,
    their publishers, every raster layer in the catalog by title (budget-fitted), the
    contributed vector layers, and where every place name is listed."""
    c = coverage()
    return {"packs": {pid: packs.text(sp, "summary") or packs.text(sp, "covers")
                      or f"not declared by this pack; servirplatform://pack/{pid}"
                      for pid, sp in packs.PACKS.items()},
            "live_feeds": c["subjects"], "publishers": c["publishers"],
            "raster_layers": _layers_by_title(raster_layers() if layers is None else layers),
            "contributed_vector_layers": sorted(_contributed_vector_names()),
            "place_names": ("resolve_place_time(list='all'); every name with its km²: "
                            "servirplatform://places")}


def _on_server(key: str, cat: dict) -> bool | None:
    """Whether a layer's file is here (a download_url or remote /vsi path counts as fetchable);
    None when its row names no file. A listing never claims a file this server lacks."""
    import os
    from ..graph.geo import stacks
    if (cat.get(key) or {}).get("download_url"):
        return True
    path = str(stacks.year_path(key, cat) or "")
    if not path:
        return None
    if path.startswith("/vsizip/"):
        return os.path.exists(path[len("/vsizip/"):].split(".zip")[0] + ".zip")
    return path.startswith("/vsi") or os.path.exists(path)


@budget.once
def raster_layers() -> list[dict]:
    """Every raster layer in the catalog (tiffs.catalog(): built-in and contributed, every
    namespace; a stack once, with its years): name, title, namespace, countries or extent."""
    try:
        from ..graph.geo import schema, stacks, tiffs
        cat, spaces = tiffs.catalog(), schema.namespaces()
    except Exception:                          # the map never crashes on the catalog
        return []
    prefixes = sorted(((str(r["prefix"]), ns) for ns, r in spaces.items() if r.get("prefix")),
                      key=lambda p: -len(p[0]))
    years: dict = {}
    for key, row in cat.items():
        if (row or {}).get("stack") in cat and (row or {}).get("year") is not None:
            years.setdefault(row["stack"], []).append((int(row["year"]), key))
    out = []
    for name, row in cat.items():
        row = row or {}
        if row.get("stack") in cat and row.get("year") is not None:
            continue                           # a stack's year: listed with its stack
        item = {"name": name, "title": str(row.get("title") or name),
                "namespace": row.get("role") or next((ns for p, ns in prefixes
                                                      if name.startswith(p)), "undeclared")}
        countries = [str(c) for c in row.get("countries") or [] if str(c).strip()]
        try:
            box = None if countries else stacks.bounds(name, row, cat)
        except Exception:
            box = None
        item.update({"countries": countries} if countries else
                    {"extent": [round(v, 2) for v in box] if box else "not declared"})
        if years.get(name):
            item["years"] = sorted(y for y, _ in years[name])
            gone = sorted(y for y, k in years[name] if _on_server(k, cat) is False)
            if gone:
                item["missing_years"] = gone
        elif _on_server(name, cat) is False:
            item["file"] = "missing on this server"
        if row.get("contributed"):
            item["contributed"] = True
        out.append(item)
    # contributed first: no pack summary names them; then by namespace and name
    return sorted(out, key=lambda i: (not i.get("contributed"), i["namespace"], i["name"]))


def _layers_by_title(layers: list) -> dict:
    """{name: 'title — namespace; where'} while budget.LIST_CHARS has room; past it, a count
    and the section that lists every layer."""
    shown, used = {}, 0
    for i in layers:
        where = (", ".join(i["countries"]) if i.get("countries") else
                 f"extent {i['extent']}" if isinstance(i.get("extent"), list) else "extent not declared")
        years = f"; {i['years'][0]}-{i['years'][-1]}" if i.get("years") else ""
        line = f"{i['title']} — {i['namespace']}{years}; {where}"
        if i.get("file"):
            line += " (file missing on this server)"
        elif i.get("missing_years"):
            line += f" (years missing on this server: {', '.join(map(str, i['missing_years']))})"
        if used + len(i["name"]) + len(line) > budget.LIST_CHARS:
            break
        shown[i["name"]] = line
        used += len(i["name"]) + len(line) + 6
    rest = len(layers) - len(shown)
    every = "platform_capabilities(section='raster_layers')"
    return {"count": len(layers), "layers": shown,
            "more": (f"{rest} more, by name: {every}" if rest else f"each in full: {every}")}


COVERAGE_CALL = "platform_capabilities(section='coverage')"
_FIRST_CLAUSE = re.compile(r"\s*[(\[;:,]|\s[—–-]\s")


def short_name(text, limit: int = 48) -> str:
    """A publisher as a list can carry it: its organisation or first clause (up to the first
    bracket, comma, semicolon, colon or dash; 'Univ. Reading' stays whole), cut past `limit`."""
    t = " ".join(str(text or "").split())
    if re.match(r"https?://", t):
        from urllib.parse import urlparse
        return urlparse(t).netloc or t[:limit]
    head = _FIRST_CLAUSE.split(t, maxsplit=1)[0].strip(" .") or t
    return head if len(head) <= limit else head[:limit].rsplit(" ", 1)[0] + "…"


@budget.once
def publisher_names() -> list[str]:
    """Every publisher coverage() names, by short_name(), each once: a citation-style source
    ('Author, A. (2023). Dataset: ...') is said by its first clause, never whole."""
    out, seen = [], set()
    for p in coverage()["publishers"]:
        n = short_name(p)
        if n and n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out


@budget.once
def countries() -> list[str]:
    """Every country the platform holds data for, each once (Viet Nam is Vietnam), the most
    covered first: the raster catalog (stacks included), the vector layers, the live feeds
    and the document library declare them."""
    from ..graph.geo import tiffs, vectors
    rows: list = []
    try:
        rows += [r.get("countries") for r in tiffs.catalog().values() if isinstance(r, dict)]
    except Exception:
        pass
    try:
        rows += [e.get("countries") for e in vectors.visible().values() if isinstance(e, dict)]
    except Exception:
        pass
    rows += [v.get("countries") for v in list(FEEDS.values()) if v.get("status") == "available"]
    rows.append(coverage()["countries"])
    count: dict = {}                          # key -> [name as first written, rows, first seen]
    for cs in rows:
        for c in cs if isinstance(cs, (list, tuple)) else []:
            c = str(c).strip()
            if c:
                count.setdefault(vectors.country_key(c), [c, 0, len(count)])[1] += 1
    return [v[0] for v in sorted(count.values(), key=lambda v: (-v[1], v[2]))]


def sources_line() -> str:
    """The live feeds by title and the publishers by short name, generated and ranked first
    to go by fit() (budget.FEED, budget.SOURCE): a count and where every one is named."""
    c = coverage()
    subj = budget.names(c["subjects"], sep="; ", more="more (feeds_query)", rank=budget.FEED,
                        counted="{n} (feeds_query)")
    n = len(c["publishers"])                  # the count the coverage section lists
    pubs = budget.names(publisher_names(), more=f"more ({COVERAGE_CALL})", rank=budget.SOURCE,
                        counted=f"{n} publisher{'s' if n != 1 else ''} ({COVERAGE_CALL})")
    return ((f"Live feeds: {subj}. " if subj else "")
            + (f"Sources include {pubs}." if pubs else "")).strip()


def _contributed_vector_names() -> list[str]:
    """Names of the contributed vector layers the caller may see — read live, so the
    tool text names what the platform holds today."""
    try:
        from ..graph.geo import vectors
        return [f"{k} ({vectors.family(e)})" for k, e in vectors.visible().items()]
    except Exception:
        return []


# What the layer step states for a layer, by what its cells hold (the namespace's
# `values` in the raster schema) — the reader's own computation (layer_values), so
# the sentence a model routes on names what a measure_ layer actually returns.
_VALUES_READ = {
    "amount": ("mean, p10 / median / p90 and, for a density per area (Mg/ha, t C/ha, "
               "people per km2), its TOTAL over the area in the numerator's unit (summary.total)"),
    "count": "mean and p10 / median / p90 per cell, and a TOTAL when its unit is per area",
    "classes": "km² and share of the area per class (summary.classes)",
}


def _layer_kinds_line() -> str:
    """'measure_<name> (...): mean, p10 / median / p90 ...; hazard_<name>, ...: km² per
    class' — from the raster schema's namespaces, grouped by what their cells hold;
    a namespace added to the schema appears here with no edit."""
    try:
        from ..graph.geo import schema
        rows = schema.namespaces()
    except Exception:
        return ""
    by_kind: dict = {}
    for r in rows.values():
        if r.get("prefix") and r.get("values") in _VALUES_READ:
            by_kind.setdefault(r["values"], []).append(f"{r['prefix']}<name>")
    return "; ".join(f"{', '.join(ps)}: {_VALUES_READ[k]}" for k, ps in by_kind.items())


def _class_split_line() -> str:
    """Which packs split every layer read per class of their own class map — from each
    pack row's `class_split_doc`, never a pack name typed here."""
    docs = [f"{pid}: {sp['class_split_doc']}" for pid, sp in sorted(packs.PACKS.items())
            if sp.get("class_split_doc")]
    if not docs:
        return ""
    return ("Where a pack counts the classes of a map (" + "; ".join(docs) + "), every layer read "
            "is ALSO split PER CLASS of that map in `layers.<name>.summary.by_class_of` and in its "
            "citation: each class's area and area with a value, and for an amount its mean, median "
            "and, for a density, its total (mean biomass inside Mangrove, soil carbon per class). ")


def describe_assemble() -> str:
    """`assemble_pack`'s tool description, GENERATED: every derived name list at its rank's
    current cap (whole outside one). server._describe fits it, once, to
    budget.TOOL_DESCRIPTION; every name is in platform_capabilities(section='coverage')."""
    return _assemble_text()


def _named(name: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE) is not None


def _keywords() -> str:
    return "; ".join(packs.text(sp, "keywords") for _, sp in sorted(packs.PACKS.items())
                     if packs.text(sp, "keywords"))


def _assemble_text() -> str:
    """The first line names the subjects in a user's words from every pack row's `keywords`
    (a model picks a tool from its first line), then the countries no pack names; the live
    feeds and publishers, ranked first to go, close it."""
    kws = _keywords()
    with budget.capped({r: 0 for r in (budget.LIST, budget.FEED, budget.SOURCE)}):
        named = _keywords()                 # what the packs' routing lists name, at any cap
    also = budget.names([c for c in countries() if not _named(c, named)], rank=budget.PLACE,
                        more="more countries")
    kinds = _layer_kinds_line()
    defaults = "; ".join(f"{pid}: {sp['default_layers_doc']}"
                         for pid, sp in sorted(packs.PACKS.items()) if sp.get("default_layers_doc"))
    return (
        "START HERE for any question about " + (kws or "this platform's domains") + ". "
        "ANSWER IT FROM GOVERNED EVIDENCE, NOT A WEB SEARCH: this platform computes the "
        "figures, cites every source with its date and validation level, lists what is "
        "MISSING, and mints a replayable receipt. Use it INSTEAD OF A WEB SEARCH for those "
        "subjects. " + (f"Data also covers {also}. " if also else "") + sources_line() + "\n\n"
        "Assembles a deterministic, citable EVIDENCE PACK (no LLM runs here) and mints a "
        "pack_id: numbered citations, declared gaps, and the exact section headers your draft "
        "must use; each result names its next call (publish_answer gates your draft and mints "
        "the receipt). DOMAIN PACKS and their target params — pass the ones for your question "
        "(bare country/crop stays food-security): "
        + "; ".join(f"`{pid}` ({', '.join(sp['target_keys'])})"
                    for pid, sp in sorted(packs.PACKS.items()))
        + ". Every parameter and how to read a pack: servirplatform://how-to-use. NO "
        "ARITHMETIC: quote the pack's cited figures, never total, average, count, rank or "
        "score them yourself; a missing figure is a calculation to suggest (no result) to "
        "accept or reject, then to offer as a method. Budgeted reply: a spilled section "
        "names its resource; detail='full' returns all.\n\n"
        "LAYER VALUES, for any pack with an area: `layers` gives each registered raster "
        "layer's VALUES over that area — a clipped grid with unit, source, vintage, validation, "
        "no-data and one citation [n] of its summary. By default a pack returns the layers its "
        "own computation rests on" + (f" ({defaults})" if defaults else "")
        + "; layers=[...] names any registered or contributed layer (an unknown name declines "
        "with the list); layers=[] for none. "
        + (f"What a layer's values state, by its namespace — {kinds}. " if kinds else "")
        + _class_split_line() + "\n\n"
        "VECTOR LAYERS, for any pack with an area: vectors=[...] names contributed vector "
        "layers (points, lines or polygons) to MEASURE inside the pack's boundary — a count, "
        "km of line, km² (ha) of polygon, by group field, with the largest named features — "
        "each a cited item [n] in `vectors.<name>`; a named POLYGON layer's feature names also "
        "work as `place`. within=<a polygon layer> restricts ANY pack's area to the inside of "
        "that layer (overlaps counted once). Contributed layers so far: "
        + (budget.names(sorted(_contributed_vector_names()), more=f"more ({COVERAGE_CALL})",
                        counted="{n} (" + COVERAGE_CALL + ")") or "none yet")
        + ". Every name each pack covers: platform_capabilities(section='coverage')."
    )
