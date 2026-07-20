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
        "bones": _bones(set(tools)),
        "packs": [{
            "id": "food-security",
            "profile": "v0",
            "question": ("early signs of maize failure in the current season "
                         "(Kenya Trans-Nzoia vs Zambia)"),
            "sources": {
                "corpus": _corpus_summary(),
                "conditions": {"status": "available", "source": "GEOGLAM Crop Monitor"},
                "climate_feeds": {"status": "declared_gap",
                                  "reason": "CHIRPS/CHIRTS/ERA5 pending hub data lists"},
            },
            "calendars": _calendars(),
            "analytics": {"status": "declared_gap",
                          "reason": "threshold formulas pending hub contribution"},
            "widgets": {"status": "declared_gap",
                        "reason": "MCP Apps widgets phase2; web widgets exist in apps/web"},
            "skills": {"status": "partial",
                       "reason": "canonical loop shipped via server instructions + the "
                                 "build_a_tool prompt; a fuller skill/usage resource pending"},
        }],
        "declared_deferrals": [
            "acreage-under-stress (needs crop-type/land-cover masks)",
            "climate feeds (pending hub data lists)",
            "compute/analytics registry (pending hub contribution)",
        ],
    }
