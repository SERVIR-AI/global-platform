"""The compositions bone: run a pre-wired pack composition end-to-end for consumers
with NO LLM of their own (dashboards, cron, automated bulletins).

Unlike the assemble -> YOUR-LLM -> verify loop, this runs the PLATFORM's LLM
server-side — the metered composite path. It persists pack + report + receipt so
the no-LLM path is replayable too (rule 3). Compositions are registry rows, so new
ones add zero tools.
"""

from __future__ import annotations

import hashlib

from ..food_security import synthesis
from ..llm import MissingAPIKey
from ..rag.store import CorpusError
from . import context, record, registry, store


def _run_foodsecurity_brief(question, override, override_country, override_crop,
                            provider, model) -> dict:
    """Runner for `foodsecurity.brief`. Runners return a synthesize-shaped dict:
    {declined, brief, citations, parsed, evidence, gaps, grounded, provider, model,
    trace} — compose.run does the persistence generically."""
    return synthesis.synthesize(question, provider=provider, model=model,
                                calendar=override,
                                calendar_target=(override_country, override_crop))


def _run_risk_brief(question, override, override_country, override_crop,
                    provider, model) -> dict:
    """Runner for `risk.brief`. The calendar override params are food-security
    concepts and are ignored here — a risk target has no crop calendar."""
    from ..risk import synthesis as risk_synthesis
    return risk_synthesis.synthesize(question, provider=provider, model=model)


# name -> runner. A composition needs BOTH a metadata entry (registry.COMPOSITIONS,
# for capabilities) and a runner here; a metadata entry without one is declined as
# not-implemented rather than silently running someone else's pipeline.
_RUNNERS = {"foodsecurity.brief": _run_foodsecurity_brief,
            "risk.brief": _run_risk_brief}


def run(composition: str = "foodsecurity.brief", question: str = "",
        override: list[dict] | None = None, override_country: str | None = None,
        override_crop: str | None = None, provider: str | None = None,
        model: str | None = None) -> dict:
    """Run a composition by id. Returns the governed brief plus the ids that make
    it replayable (pack/report/receipt)."""
    spec = registry.COMPOSITIONS.get(composition)
    if spec is None:
        return {"status": "declined", "note": f"unknown composition {composition!r}",
                "available": sorted(registry.COMPOSITIONS)}
    runner = _RUNNERS.get(composition)
    if runner is None:
        return {"status": "declined",
                "note": f"composition {composition!r} is registered but has no runner "
                        "(not implemented)"}
    if not (question or "").strip():
        return {"status": "declined", "note": "question is required"}
    if override is not None and not context._valid_override(override):
        return {"status": "declined",
                "note": "override must be a list of {season, planting:[m,m], harvest:[m,m]}"}
    try:
        out = runner(question, override, override_country, override_crop, provider, model)
    except MissingAPIKey as exc:
        return {"status": "declined", "note": f"LLM key missing: {exc}"}
    except CorpusError as exc:
        return {"status": "declined", "note": str(exc)}

    citations = out.get("citations") or []
    if out.get("declined"):
        return {"status": "declined", "note": out.get("decline_reason"),
                "citations": citations, "grounded": out.get("grounded"),
                "trace": out.get("trace")}

    parsed, stats = out.get("parsed") or {}, out.get("evidence") or {}
    brief = out.get("brief") or ""
    # The output contract comes from the RUNNER (each domain owns its sections);
    # the FS constant is only the fallback for the historic FS runner shape.
    sections = out.get("required_sections") or list(synthesis.SECTIONS)
    viz = stats.pop("viz", None) if isinstance(stats, dict) else None
    pack_id = store.save_pack({
        "country": parsed.get("country"), "crop": parsed.get("crop"),
        "focus": parsed.get("focus"), "citations": citations,
        "gaps": out.get("gaps") or [], "queries": stats.get("queries"),
        "required_sections": sections, "stats": stats,
        "trace": out.get("trace"), "composition": composition,
        **({"pack": out["pack"]} if out.get("pack") else {}),
        **({"target": out["target"]} if out.get("target") else {}),
        **({"viz": viz} if viz is not None else {})})
    grounded = out.get("grounded") or {}
    digest = hashlib.sha256(brief.encode("utf-8")).hexdigest()
    report_id = store.save_report({
        "pack_id": pack_id, "passed": grounded.get("passed"),
        "evidence_tier": "platform-registered", "checks": grounded,
        "draft": brief, "draft_sha256": digest, "draft_excerpt": brief[:280],
        "usage": out.get("usage")})
    rec = record.record(pack_id=pack_id, report_id=report_id, question=question)

    return {"status": "ok", "composition": composition, "brief": brief,
            "citations": citations, "gaps": out.get("gaps") or [],
            "grounded": grounded, "pack_id": pack_id, "report_id": report_id,
            "receipt_id": rec.get("receipt_id"), "draft_sha256": digest,
            # parity with /chat: the step trace and token usage must not be lost
            "trace": out.get("trace"), "usage": out.get("usage"),
            "provider": out.get("provider"), "model": out.get("model"),
            "note": "this composition ran the PLATFORM's LLM server-side (metered); "
                    "the assemble -> your-LLM -> verify loop uses YOUR model instead"}
