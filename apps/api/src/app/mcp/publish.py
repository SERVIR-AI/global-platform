"""One call to finish: gate the draft, mint the receipt, hand back how to show it.

This exists because of a measured failure, not a design preference. With the loop
split across `verify_groundedness` then `record_receipt`, no model tested completed
it from a plain question — not Sonnet at medium effort in Claude Desktop, not
gpt-4o, not gpt-4o-mini. Every one assembled the pack, wrote a good brief, and
emitted it as prose. Two of them even ANNOUNCED that they would verify next, then
did not: once a model holds a satisfying answer, further tool calls read as
optional ceremony, and no wording in a tool result changed that.

So the remaining work is collapsed into a single obvious finish. The composite is
not a shortcut around the contract — it IS the contract: the draft is gated first
and a receipt is minted only if it passes, so there is no path through this tool
that publishes something ungated. `verify_groundedness` and `record_receipt` stay
exactly as they were for callers that want the steps separately.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from . import loop, record, store, verify


def _insight(pack_id: str, draft: str) -> dict:
    """What the answer SAYS, plus the numbers behind it — so a rendering surface can
    show the insight, not only its provenance.

    Until now the app received the receipt alone: sources, dates, validation,
    freshness. Metadata ABOUT evidence, with no values and no brief in it, which is
    why it could only ever draw a provenance view. The series are preserved by
    `synthesis._series`; the brief is the text that just passed the gate.
    """
    pack = store.load_pack(pack_id) or {}
    series = [{"n": c.get("n"), "title": c.get("title"), "source": c.get("source"),
               "as_of": c.get("pub_date"), **c["series"]}
              for c in pack.get("citations", []) if c.get("series")]
    context = {c["kind"]: c.get("text") for c in pack.get("citations", [])
               if c.get("kind") in ("conditions", "calendar")}
    return {"brief": draft, "series": series, "context": context,
            **({"map": _panel_map(pack)} if _panel_map(pack) else {}),
            "pack": pack.get("pack"), "target": pack.get("target"),
            **({"country": pack.get("country"), "crop": pack.get("crop")}
               if pack.get("country") or pack.get("crop") else {})}


def _panel_map(pack: dict) -> dict | None:
    """A compact copy of the recorded viz, small enough to ride the tool result —
    so the MCP App can DRAW the map inline (SVG, no external origins, which the
    sandbox CSP forbids). The full-fidelity map stays the hazard_map embed; this
    is the recorded snapshot at panel scale. Capped: the panel result reaches the
    consumer LLM's context, and pixels must not cost more tokens than the words."""
    import json as _json
    viz = pack.get("viz")
    if not viz:
        return None
    hl = viz.get("hazard_layer") or {}
    m = {"place": viz.get("place"), "hazard": viz.get("hazard"),
         "bounds": viz.get("bounds"), "legend": viz.get("legend"),
         "aoi": _coarse(viz.get("aoi"), 2e-3),
         "hazard_grid": viz.get("hazard_grid"),
         "assets": viz.get("features"), "metric": viz.get("metric")}
    if len(_json.dumps(m)) > 34_000:
        m["hazard_grid"] = None               # keep AOI+assets; drop the heavy layer
        if len(_json.dumps(m)) > 34_000:
            return None
    return m


def _coarse(gj, tol: float):
    """Panel-scale geometry: ~500 px wide schematic needs nothing finer than
    ~1 km. Embed-grade detail stays on the pack; this copy is for an SVG."""
    if not gj:
        return gj
    try:
        from shapely.geometry import mapping, shape

        def one(feature):
            g = shape(feature["geometry"])
            # The size is RINGS, not vertices: a hazard class is thousands of
            # pixel islands, and simplify() keeps every one. A panel schematic
            # keeps only islands you could see at ~500 px wide.
            if g.geom_type == "MultiPolygon":
                parts = sorted(g.geoms, key=lambda q: q.area, reverse=True)
                keep = [q for q in parts if q.area >= tol * tol] or parts[:1]
                from shapely.geometry import MultiPolygon
                g = MultiPolygon(keep[:40])
            g = g.simplify(tol, preserve_topology=True)
            geom = mapping(g)
            def r(x):
                return [r(i) for i in x] if isinstance(x, (list, tuple)) else (
                    round(x, 3) if isinstance(x, float) else x)
            return {**feature, "geometry": {**geom, "coordinates": r(geom["coordinates"])}}

        if gj.get("type") == "FeatureCollection":
            return {**gj, "features": [one(f) for f in gj.get("features", [])]}
        if gj.get("type") == "Feature":
            return one(gj)
    except Exception:
        return gj
    return gj


def _loop_trace(pack: dict, publish_started: datetime,
                verify_ms: float, record_ms: float, receipt_id: str | None) -> dict:
    """The platform's own execution trace for this answer — the governed loop's
    counterpart to the web app's per-turn trace envelope. It attests ONLY what the
    platform executed: the gather, the gate, the mint. The drafting step between
    them runs in the consumer's assistant, so it is declared as unobserved rather
    than silently omitted — the boundary is the trust story, not a gap in it."""
    cits = pack.get("citations", [])
    grades: dict = {}
    for c in cits:
        grades[record._retrieval(c)] = grades.get(record._retrieval(c), 0) + 1
    graded = ", ".join(f"{n} {g.replace('-at-pack-time', ' at pack time')}"
                       for g, n in sorted(grades.items())) or "none"
    ex = pack.get("exec") or {}
    steps = [{"step": "assemble", "tool": "assemble_pack",
              "duration_ms": ex.get("gather_ms"), "at": ex.get("assembled_at"),
              "summary": (f"{len(cits)} citations gathered ({graded}) · "
                          f"{len(pack.get('gaps', []))} declared gap(s)"),
              "detail": list(pack.get("trace") or [])}]
    draft_step = {"step": "draft", "tool": None, "duration_ms": None,
                  "outside_platform": True,
                  "summary": ("drafted by the consumer's own assistant — the platform "
                              "does not execute or observe this step; it attests only "
                              "the hash of the text that came back")}
    if ex.get("assembled_at"):
        try:
            t0 = datetime.fromisoformat(ex["assembled_at"])
            draft_step["elapsed_ms"] = round(
                (publish_started - t0).total_seconds() * 1000, 1)
        except ValueError:
            pass
    steps.append(draft_step)
    steps.append({"step": "verify", "tool": "publish_answer",
                  "duration_ms": verify_ms,
                  "summary": (f"groundedness gate: every [n] resolved against the "
                              f"pack, {len(pack.get('required_sections', []))} "
                              "required sections enforced — passed")})
    steps.append({"step": "record", "tool": "publish_answer",
                  "duration_ms": record_ms,
                  "summary": (f"receipt {receipt_id} minted — replayable and "
                              "publicly resolvable")})
    return {"steps": steps,
            "note": ("platform execution only — timings cover what ran here; the "
                     "draft step is the consumer's and is declared, not measured")}


def answer(pack_id: str, draft: str, question: str | None = None) -> dict:
    """Gate then receipt. A blocked draft returns its failures and NO receipt —
    the caller must fix and call again, which is the gate doing its job."""
    publish_started = datetime.now(timezone.utc)
    t_v = time.perf_counter()
    v = verify.groundedness(draft, pack_id)
    verify_ms = round((time.perf_counter() - t_v) * 1000, 1)
    if v.get("status") != "ok":
        return v                                   # unknown pack — already explained

    if not v.get("passed"):
        return {"status": "blocked",
                "answer_status": loop.BLOCKED,
                "note": ("This draft did NOT pass the groundedness gate, so no receipt "
                         "was minted and it must not be shown to the user. Fix the "
                         "failures below and call publish_answer again."),
                "report_id": v["report_id"],
                "failures": v["failures"],
                "phantom_citations": v["phantom_citations"],
                "missing_sections": v["missing_sections"],
                "uncited_paragraphs": v["uncited_paragraphs"],
                "required_sections": v["required_sections"],
                "next_step": loop.after_verify(pack_id, v["report_id"], False,
                                               v["failures"])}

    t_r = time.perf_counter()
    r = record.record(pack_id=pack_id, report_id=v["report_id"], question=question)
    record_ms = round((time.perf_counter() - t_r) * 1000, 1)
    if r.get("status") != "ok":
        return r
    # The receipt already carries its own forward edge (show it via ui_embed), so
    # this returns the verdict fields alongside it rather than restating them.
    return {"status": "ok", "passed": True,
            # The gated text comes BACK to the caller: a model that publishes and
            # then has nothing to restate answers with a bare embed and two lines.
            "insight": _insight(pack_id, draft),
            "trace": _loop_trace(store.load_pack(pack_id) or {}, publish_started,
                                 verify_ms, record_ms, r.get("receipt_id")),
            "report_id": v["report_id"], "draft_sha256": v["draft_sha256"],
            "evidence_tier": v["evidence_tier"],
            "numbers_unverified_recorded": v["numbers_unverified_recorded"],
            **{k: val for k, val in r.items() if k != "status"}}


def describe() -> str:
    """Named and described as the FINISH action, because that is how a model
    decides. 'verify' reads as an optional quality check; 'publish' reads as the
    step that makes an answer real."""
    return (
        "FINISH AN ANSWER — call this before you show the user a brief. One call: "
        "it gates your draft against the evidence pack and, only if it passes, mints "
        "the replayable receipt and returns the components that display the evidence "
        "chain.\n\n"
        "This is the LAST step of the canonical loop (assemble_pack -> you draft -> "
        "publish_answer). Showing a brief without it means the answer was never "
        "checked for citations that do not exist in the pack, has no receipt, and "
        "cannot be replayed or shared.\n\n"
        "Pass the draft VERBATIM and in full — it is hashed, so the receipt can prove "
        "which exact text passed. Cite pack items as [n], use the pack's "
        "required_sections as your headers, and do not write your own '## Sources'.\n\n"
        "Returns: {status, passed, report_id, receipt_id, public_resolver, sources, "
        "evidence_freshness, claim_scope, render_with, next_step}. status \"blocked\" "
        "-> the gate refused it: `failures` says why, NO receipt is minted, and the "
        "draft must not be shown. Use verify_groundedness / record_receipt directly "
        "only if you need the two steps separately.")
