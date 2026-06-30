"""Per-query record: cost, a groundedness check, and a JSON trace on disk.

Two layers live here:

1. `record(...)` — the original compact per-query disk record (cost / grounded / tokens),
   written by the finalize node. Unchanged, for back-compat.
2. The structured **execution trace** the API exports to the frontend: a `TraceCollector`
   that captures the deep external-call / download events emitted inside `ingest` (so the
   `fetch` node can attach them to its step), and `build_envelope(...)` which assembles the
   per-turn envelope (metadata + ordered step events) returned as `ChatResponse.trace_events`.
"""
import contextvars
import json
import time
from datetime import datetime, timezone

from ...config import get_settings

# Collector for the fine-grained external-call/download events emitted deep inside ingest.
# Installed by the fetch node around its own ingest calls (set + read in the same thread, so
# it never depends on context propagating across LangGraph's executor), and a no-op otherwise.
_COLLECTOR: contextvars.ContextVar = contextvars.ContextVar("grp_trace_collector", default=None)


class TraceCollector:
    """Accumulates `{kind, ...}` io events (API calls, downloads, clips) for one node's run."""

    def __init__(self):
        self.io: list[dict] = []

    def emit(self, event: dict) -> None:
        self.io.append(event)

    def drain(self) -> list[dict]:
        io, self.io = self.io, []
        return io


def set_collector(collector: TraceCollector):
    """Install `collector` for the current context; returns a token for `reset()`."""
    return _COLLECTOR.set(collector)


def reset(token) -> None:
    _COLLECTOR.reset(token)


def emit(event: dict) -> None:
    """Record one external-call/download event if a collector is installed (else a no-op)."""
    c = _COLLECTOR.get()
    if c is not None:
        c.emit(event)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _derive_path(steps: list[dict]) -> str:
    """Classify the turn from its step events (see WORKFLOW_ANALYSIS.md §1.6/§1.7)."""
    if not steps:
        return "unknown"
    if steps[0].get("node") == "route" and steps[0].get("kind") == "apply_choice":
        return "resume"
    if any(s.get("node") == "resolve" and s.get("awaiting_choice_set") for s in steps):
        return "clarify_pause"
    if any(s.get("node") == "finalize" and s.get("kind") == "error_echo" for s in steps):
        return "refused"
    return "direct"


def _error_origin(steps: list[dict]):
    """Which node first set `error` (the refusal/failure origin), or None."""
    for s in steps:
        sc = s.get("state_changes")
        if isinstance(sc, dict) and sc.get("error"):
            return s.get("node")
    return None


def build_envelope(events, *, thread_id=None, turn_id=None, user_query="", provider=None,
                   model=None, mode=None, usages=None, result=None, answer=None) -> dict:
    """Assemble the per-turn trace envelope (a JSON object containing `steps[]`) returned to
    the frontend as `ChatResponse.trace_events` and persisted via `write_envelope`."""
    steps = list(events or [])
    usages = [u for u in (usages or []) if u]
    total_in = sum(u.get("in", 0) for u in usages)
    total_out = sum(u.get("out", 0) for u in usages)
    s = get_settings()
    cost = sum(u.get("in", 0) * s.price_in / 1e6 + u.get("out", 0) * s.price_out / 1e6 for u in usages)
    grounded = next((st.get("grounded") for st in steps if st.get("node") == "finalize"), None)
    return {
        "trace_version": 2,
        "thread_id": thread_id, "turn_id": turn_id,
        "path": _derive_path(steps), "mode": mode,
        "provider": provider, "model": model, "user_query": user_query,
        "started_at": steps[0].get("started_at") if steps else None,
        "ended_at": steps[-1].get("ended_at") if steps else None,
        "duration_ms": round(sum(st.get("duration_ms", 0) or 0 for st in steps), 1),
        "usage_total": {"in": total_in, "out": total_out, "total": total_in + total_out},
        "tokens": {"in": total_in, "out": total_out},   # back-compat with record()
        "cost_usd": round(cost, 6),
        "grounded": grounded,
        "error_origin": _error_origin(steps),
        "tool_result": result,                           # back-compat
        "answer": answer,
        "steps": steps,
    }


def write_envelope(envelope: dict):
    """Persist a full trace envelope next to the compact records (best-effort)."""
    settings = get_settings()
    settings.traces_dir.mkdir(parents=True, exist_ok=True)
    path = settings.traces_dir / f"{int(time.time() * 1000)}-trace.json"
    path.write_text(json.dumps(envelope, indent=2, default=str))
    return path


def record(question, answer, usages, result=None, args=None):
    """Write a per-query trace JSON to disk and return it: token cost (USD) and a
    groundedness check (whether the tool's number appears verbatim in the answer)."""
    usages = [u for u in usages if u]
    settings = get_settings()
    price_in, price_out = settings.price_in / 1_000_000, settings.price_out / 1_000_000
    cost = sum(u["in"] * price_in + u["out"] * price_out for u in usages)

    # grounded = the tool's number actually appears in the answer (no fabrication)
    grounded = True
    if result is not None:
        number = result.get("count", result.get("length_km"))
        grounded = str(number) in answer.replace(",", "")

    rec = {"question": question, "answer": answer, "tool_call": args, "tool_result": result,
           "grounded": grounded, "cost_usd": round(cost, 6),
           "tokens": {"in": sum(u["in"] for u in usages), "out": sum(u["out"] for u in usages)}}
    settings.traces_dir.mkdir(parents=True, exist_ok=True)
    path = settings.traces_dir / f"{int(time.time() * 1000)}.json"
    path.write_text(json.dumps(rec, indent=2))
    return rec
