"""The platform map: what exists, what's a declared gap. Reads real state (corpus,
calendars) and never hides a gap — the honesty ethos applied to the server itself.
Pure/no-network; safe to call before anything else in a build session.
"""

from __future__ import annotations

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

# Bone availability by phase (ARCHITECTURE §8). "available" = callable now.
_BONES = [
    {"bone": "resolve", "tool": "resolve_place_time", "status": "phase2"},
    {"bone": "fetch", "tool": "corpus_search / corpus_document / feeds_query",
     "status": "partial", "note": "corpus available; live feeds phase2"},
    {"bone": "compute", "tool": "compute_run", "status": "phase2"},
    {"bone": "context", "tool": "context_get", "status": "available"},
    {"bone": "assemble", "tool": "assemble_pack", "status": "phase1"},
    {"bone": "verify", "tool": "verify_groundedness", "status": "phase1"},
    {"bone": "record", "tool": "record_receipt", "status": "phase1"},
    {"bone": "contribute", "tool": "contribute_submit", "status": "phase2"},
]


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


def capabilities() -> dict:
    """The honest platform map for a build session. Sources/calendars are real;
    gaps are declared, not omitted."""
    return {
        "server": {"name": "global-risk-platform", "version": VERSION,
                   "transport": "stdio"},
        "contract": CONTRACT,
        "bones": _BONES,
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
            "skills": {"status": "declared_gap", "reason": "compose-a-brief skill phase2"},
        }],
        "declared_deferrals": [
            "acreage-under-stress (needs crop-type/land-cover masks)",
            "climate feeds (pending hub data lists)",
            "compute/analytics registry (pending hub contribution)",
        ],
    }
