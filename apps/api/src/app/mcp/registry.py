"""The platform map: what exists, what's a declared gap. Reads real state (corpus,
calendars) and never hides a gap — the honesty ethos applied to the server itself.
Pure/no-network; safe to call before anything else in a build session.
"""

from __future__ import annotations

import os

from ..food_security import calendar as fs_calendar
from ..rag.store import Corpus, CorpusError

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
     "planned": "planned (phase2)", "note": "corpus tools built; live feeds (feeds_query) phase2"},
    {"bone": "compute", "tools": ["compute_run"], "planned": "planned (phase2)"},
    {"bone": "context", "tools": ["context_get"], "planned": "planned (phase1)"},
    {"bone": "assemble", "tools": ["assemble_pack"], "planned": "planned (phase1)"},
    {"bone": "verify", "tools": ["verify_groundedness"], "planned": "planned (phase1)"},
    {"bone": "record", "tools": ["record_receipt"], "planned": "planned (phase1)"},
    {"bone": "contribute", "tools": ["contribute_submit"], "planned": "planned (phase2)"},
]


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


def pack_manifest() -> dict:
    """The Food-Security DOMAIN PACK, v0 — the versioned bundle a hub adopts: what
    ships, what it produces, and what is missing. Real state where derivable; every
    gap declared in one place rather than scattered."""
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
        "id": "food-security", "version": "v0",
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
            "climate_feeds": {"status": "declared_gap",
                              "reason": "CHIRPS/CHIRTS/ERA5 pending hub data lists"},
        },
        "calendars": _calendars(),
        "calendar_provenance": ("hub defaults are hand-authored approximations of "
                                "GEOGLAM/FAO baselines; a hub-provided passported "
                                "calendar is pending. Per-request overrides are honored "
                                "and labelled ADJUSTED."),
        "compositions": sorted(COMPOSITIONS),
        "guidance": {"instructions": "delivered at connect",
                     "prompts": ["build_a_tool", "run_analysis", "explain_platform"],
                     "resources": ["grp://how-to-use", "grp://pack/food-security"]},
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


def capabilities(available_tools=None, available_prompts=None,
                 available_resources=None) -> dict:
    """The honest platform map. Bone status + the tool/prompt/resource lists are
    DERIVED from the live registry (passed in by the tool), so the map cannot drift
    from what the server actually exposes. Sources/calendars real; gaps declared."""
    tools = sorted(available_tools or [])
    prompts = sorted(available_prompts or [])
    resources = sorted(available_resources or [])
    return {
        "server": {"name": "global-risk-platform", "version": VERSION,
                   "transport": os.environ.get("GRP_MCP_TRANSPORT", "stdio")},
        "contract": CONTRACT,
        "usage": {"instructions": "delivered to the LLM at connect (initialize)",
                  "prompts": prompts,  # user-selectable in the host menu (build vs run)
                  "resources": resources,  # human-readable guides (e.g. grp://how-to-use)
                  "modes": ["build-time: build a reusable app on the tools",
                            "run-time: answer one question now"]},
        "tools_available": tools,
        # registry rows invoked via compose_run — not tools, so the count stays flat
        "compositions": {k: v for k, v in COMPOSITIONS.items()},
        "bones": _bones(set(tools)),
        # NB two different things are called "pack": a DOMAIN PACK (below) is the
        # versioned bundle of sources/calendars/composition — there is one per
        # domain. An EVIDENCE PACK (pack_id, from assemble_pack) is a per-question
        # snapshot of gathered evidence — a new one is minted on every call.
        "domain_packs": [pack_manifest()],
    }
