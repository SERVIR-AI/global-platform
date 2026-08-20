"""Execution trace envelope for the Maize Situation Desk backend.

One HTTP request to /api/ask is one "turn". Each pipeline stage
(assemble_pack, draft_brief, verify_groundedness, record_receipt) is one
step, built immediately before that stage returns — only the stage itself
knows what it actually decided.

Builders here are pure functions of already-computed values: no I/O, no
global state. Assembling/persisting a trace must never be able to break the
actual response — callers wrap those calls in try/except (see server.py).
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone

TRACE_DIR = os.path.join(os.path.dirname(__file__), "traces")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def new_turn_id():
    return uuid.uuid4().hex


def make_step(name, t_start, t_end, started_at, ended_at, summary, why,
              inputs, outcome, error, llm=None, external=None, cache=None):
    """Build one step event. Both success and failure paths call this.

    `llm`      — {provider, model, tokens:{...}, cost_usd, priced_at} or None
                 if this step made no model call (never 0 — see design
                 principle 6: no call vs a call that used 0 tokens differ).
    `external` — {service, operation, endpoint, attempts, outcome_size} or
                 None if this step made no third-party/MCP call.
    `cache`    — {was_cached: bool} or None if caching does not apply here.
    """
    return {
        "step": None,  # index assigned at assembly time, by position
        "name": name,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": round((t_end - t_start) * 1000, 3),
        "summary": summary,
        "why": why,
        "inputs": inputs,
        "outcome": outcome,
        "error": error,
        "llm": llm,
        "external": external,
        "cache": cache,
    }


def assemble_envelope(turn_id, question, country, steps, final_status):
    """Assemble the envelope at the turn boundary. Steps are indexed by
    their position in the list, not by a counter read inside the step."""
    for i, s in enumerate(steps):
        s["step"] = i

    durations = [s["duration_ms"] for s in steps]
    total_duration_ms = round(sum(durations), 3) if durations else None

    # Header totals SKIP steps with no llm field rather than coercing to 0
    # (design principle 11) — a step that never called a model has nothing
    # to add, which is different from a model call that used zero tokens.
    llm_steps = [s for s in steps if s.get("llm") is not None]
    total_cost_usd = None
    if llm_steps:
        costs = [s["llm"].get("cost_usd") for s in llm_steps if s["llm"].get("cost_usd") is not None]
        total_cost_usd = round(sum(costs), 6) if costs else None

    return {
        "trace_id": turn_id,
        "created_at": now_iso(),
        "final_status": final_status,
        "question": question,
        "country": country,
        "n_steps": len(steps),
        "total_duration_ms": total_duration_ms,
        "total_cost_usd": total_cost_usd,
        "steps": steps,
    }


def persist_envelope(envelope):
    """Write the envelope to traces/<trace_id>.json. Never load-bearing —
    callers must swallow any exception this raises."""
    os.makedirs(TRACE_DIR, exist_ok=True)
    path = os.path.join(TRACE_DIR, f"{envelope['trace_id']}.json")
    with open(path, "w") as f:
        json.dump(envelope, f, indent=2)
    return path
