"""The platform map: what exists, what's a declared gap. Reads real state (corpus,
calendars) and never hides a gap — the honesty ethos applied to the server itself.
Pure/no-network; safe to call before anything else in a build session.
"""

from __future__ import annotations

import os

from ..food_security import calendar as fs_calendar
from ..rag.store import Corpus, CorpusError
from . import packs

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
        return spec["manifest"]()
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
    demo = store_latest_receipt()
    return {
        "id": "food-security", "display_name": "Food Security Platform",
        "version": "v0",
        "profile": "Phase-1 profile — what ships today, gaps declared",
        "built_for": ("early signs of maize failure in the current season "
                      "(Kenya Trans-Nzoia vs Zambia — the hub-locked PoC question)"),
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


def store_latest_receipt():
    from . import store  # local import keeps registry importable without the DB
    try:
        return store.latest_receipt_id()
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


def capabilities(available_tools=None, available_prompts=None,
                 available_resources=None) -> dict:
    """The honest platform map. Bone status + the tool/prompt/resource lists are
    DERIVED from the live registry (passed in by the tool), so the map cannot drift
    from what the server actually exposes. Sources/calendars real; gaps declared."""
    tools = sorted(available_tools or [])
    prompts = sorted(available_prompts or [])
    resources = sorted(available_resources or [])
    return {
        "server": {"name": "servirplatform", "version": VERSION,
                   "transport": os.environ.get("GRP_MCP_TRANSPORT", "stdio")},
        "contract": CONTRACT,
        "usage": {"instructions": "delivered to the LLM at connect (initialize)",
                  "prompts": prompts,  # user-selectable in the host menu (build vs run)
                  "resources": resources,  # human-readable guides (e.g. servirplatform://how-to-use)
                  "modes": ["build-time: build a reusable app on the tools",
                            "run-time: answer one question now"]},
        "tools_available": tools,
        # registry rows invoked via compose_run — not tools, so the count stays flat
        "compositions": {k: v for k, v in COMPOSITIONS.items()},
        # registry rows queried via feeds_query — dataset is a parameter
        "feeds": {k: v for k, v in FEEDS.items()},
        "bones": _bones(set(tools)),
        # NB two different things are called "pack": a DOMAIN PACK (below) is the
        # versioned bundle of sources/calendars/composition — there is one per
        # domain. An EVIDENCE PACK (pack_id, from assemble_pack) is a per-question
        # snapshot of gathered evidence — a new one is minted on every call.
        "design_language": _design_language(),
        "domain_packs": [pack_manifest(pid) for pid in packs.available()],
    }


def coverage() -> dict:
    """What this platform actually covers RIGHT NOW, read from live state.

    Exists so tool descriptions can state their subject matter without anyone
    hand-maintaining a keyword list. A model picks a tool from its first line, so
    that line has to name the subject — but hardcoding "El Nino, drought, maize"
    guarantees it goes stale the moment a feed or a domain pack is added, which is
    the drift this registry exists to prevent everywhere else.
    """
    subjects, publishers, countries, crops = [], set(), set(), set()
    for spec in FEEDS.values():
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
    pubs = c["publishers"]
    shown = ", ".join(pubs[:6])
    more = f" and {len(pubs) - 6} others" if len(pubs) > 6 else ""
    return (f"Covers {what} and food security in {where}, and the climate drivers behind "
            f"them ({subj}). Sources include {shown}{more}.")


def describe_assemble() -> str:
    """`assemble_pack`'s tool description, GENERATED.

    A model picks a tool from its first line, so that line must name the subject in
    the words a user would use — and it must NOT be a hand-kept keyword list, or it
    silently stops matching the platform as feeds and packs are added.
    """
    return (
        "ANSWER A QUESTION using governed evidence — START HERE for anything this "
        "platform covers.\n\n" + coverage_line() + "\n\n"
        "Use this INSTEAD OF A WEB SEARCH for those subjects. A web search returns "
        "prose of unknown provenance; this returns named sources with publication "
        "dates and validation levels, the literal queries run, and an explicit list "
        "of what is MISSING — then verify_groundedness can gate your draft against "
        "it and record_receipt makes the answer replayable.\n\n"
        "Assembles a deterministic, citable EVIDENCE PACK and mints a pack_id. "
        "Returns numbered citations, declared gaps, and the exact section headers "
        "your draft must use. No LLM runs here; assembly is deterministic.\n\n"
        "DOMAIN PACKS and their target params — pass the ones for your question "
        "(bare country/crop stays food-security): "
        + "; ".join(f"`{pid}` ({', '.join(sp['target_keys'])})"
                    for pid, sp in sorted(packs.PACKS.items())) + ".\n\n"
        "STEP 1 OF 3 — the pack is evidence, NOT a finished answer. You then draft "
        "from it, call verify_groundedness(draft, pack_id) to gate the draft, and "
        "call record_receipt(pack_id, report_id) to mint the receipt. An answer that "
        "skips the gate is ungoverned and has nothing to replay; the receipt is also "
        "what returns the evidence view, so skipping it means the user reads prose "
        "instead of seeing the evidence chain. Each result names its own next call."
    )
