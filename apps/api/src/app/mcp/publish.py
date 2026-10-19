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
            **({"places": _insight_places(pack)} if _insight_places(pack) else {}),
            **({"layers": _insight_layers(pack)} if pack.get("layers") else {}),
            **({"vectors": _insight_vectors(pack)} if pack.get("vectors") else {}),
            "pack": pack.get("pack"), "target": pack.get("target"),
            **({"country": pack.get("country"), "crop": pack.get("crop")}
               if pack.get("country") or pack.get("crop") else {})}


def _insight_places(pack: dict, per_layer: int = 50) -> dict | None:
    """The places behind each count — name, coordinates, hazard class, risk
    level — as the pack recorded them, bounded per layer so the result stays
    proportionate; `truncated` and the totals say when the list is a sample."""
    assets = ((pack.get("stats") or {}).get("assets")) or {}
    out = {}
    for layer, a in assets.items():
        items = a.get("items") or []
        if not items:
            continue
        out[layer] = {"exposed": a.get("exposed"), "total": a.get("total"),
                      "name_field": a.get("name_field"),
                      "items": items[:per_layer],
                      "truncated": bool(a.get("truncated")) or len(items) > per_layer}
    return out or None


def _insight_vectors(pack: dict) -> list[dict]:
    """Each contributed vector layer the pack measured inside its area: what it is,
    how much of it is inside, by the contributor's group field, the largest named
    features — the numbers the citation states, in a shape a panel can table."""
    rows = []
    for name, rec in (pack.get("vectors") or {}).items():
        rows.append({
            "layer": name, "n": rec.get("n"), "title": rec.get("title"),
            "geometry": rec.get("geometry"), "unit": rec.get("unit"),
            "total": rec.get("total"), "total_ha": rec.get("total_ha"),
            "features_inside": rec.get("features_inside"),
            "features_in_layer": rec.get("features_in_layer"),
            "group_field": rec.get("group_field"), "by_group": rec.get("by_group"),
            "by_group_rest": rec.get("by_group_rest"),
            "name_field": rec.get("name_field"), "largest_named": rec.get("largest_named"),
            # a point layer names its most frequent names with counts (every point measures 1)
            "most_frequent_named": rec.get("most_frequent_named"),
            "distinct_names": rec.get("distinct_names"),
            "unnamed": rec.get("unnamed"), "truncated": bool(rec.get("truncated")),
            "source": rec.get("source"), "vintage": rec.get("vintage"),
            "license": rec.get("license"), "review": rec.get("review"),
            "features_sha256": rec.get("features_sha256")})
    return rows


def _insight_layers(pack: dict, budget: int = 12_000) -> list[dict]:
    """Each layer the pack read — what it is, what it holds over the area, and its
    grid — at panel size. Its own budget, so the map's shedding order is untouched;
    over it, grids are thinned, then dropped (defaults before requested), and every
    layer's row stays with a note: no layer disappears silently. The full grids stay
    in the pack."""
    import json as _json

    from ..graph.geo import layer_values
    rows = []
    for name, rec in (pack.get("layers") or {}).items():
        s = rec.get("summary") or {}
        rng = rec.get("declared_range")
        rows.append({
            "layer": name, "n": rec.get("n"), "title": rec.get("title"),
            "why": rec.get("why"), "unit": rec.get("unit_note"),
            "source": rec.get("source"), "vintage": rec.get("vintage"),
            "validation": rec.get("validation"), "staged": bool(rec.get("staged_by")),
            "review": rec.get("review"),
            "caveats": [c for c in (
                (rec.get("read_level") or {}).get("note"),
                ("grid downsampled from {}x{} cells".format(*rec["downsampled"]["from"])
                 if rec.get("downsampled") else None)) if c],
            "legend": rec.get("legend"), "scale": rec.get("scale"),
            "colors": rec.get("colors"),
            # a declared range is a validation limit; only a class layer is drawn across
            # it — a continuous layer (people per pixel) is shaded across what is there
            "range": ({"lo": rng[0], "hi": rng[1], "from": "declared"}
                      if rng and None not in rng and (rec.get("legend") or rec.get("unit") == "class")
                      else {"lo": s.get("min"), "hi": s.get("max"), "from": "observed"}),
            "summary": {k: s.get(k) for k in ("min", "max", "mean", "distinct", "uniform",
                                              "cells_with_value", "cells_in_area")},
            "grid": rec.get("grid")})
    size = lambda: len(_json.dumps(rows))
    while size() > budget and any(r.get("grid") and min(r["grid"]["w"], r["grid"]["h"]) > 8
                                  for r in rows):
        for r in rows:
            g = r.get("grid")
            if g and min(g["w"], g["h"]) > 8:
                r["grid"] = layer_values.thin(g)
                r["note"] = "grid thinned at panel size — the full grid is in the pack's `layers`"
    for requested in (False, True):
        for r in sorted(rows, key=lambda r: -len(_json.dumps(r.get("grid")))):
            if size() <= budget:
                break
            if r.get("grid") and (r.get("why") == "requested by name") == requested:
                r["grid"] = None
                r["note"] = "grid omitted at panel size — the full grid is in the pack's `layers`"
    return rows


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
         # a pack may say what the drawn layer IS instead of letting the panel
         # assume a hazard (the land-cover pack draws classes, not severities)
         "layer_kind": viz.get("layer_kind"), "layer_label": viz.get("layer_label"),
         "caption": viz.get("caption"),
         "bounds": viz.get("bounds"), "legend": viz.get("legend"),
         "aoi": _coarse(viz.get("aoi"), 2e-3),
         "hazard_grid": viz.get("hazard_grid"),
         "assets": viz.get("features"), "assets_layer": viz.get("layer"),
         "point_layers": viz.get("point_layers") or None, "metric": viz.get("metric")}
    # The panel copy carries only what the panel draws and names by. A hub's
    # evacuation list carries a dozen fields per point, and 84 secondary points
    # with their full properties pushed a district map past the cap — at which
    # point the old order dropped the SIX named hospitals of the headline before
    # touching the heavy secondary layers, and the hub got a map with nothing on
    # it but a boundary. Lean copies first; then shed by value: unnamed building
    # centroids, the pixel grid, the largest secondary layer at a time, and the
    # headline last of all. The full properties stay in the pack and the embed.
    big = lambda: len(_json.dumps(m)) > 34_000
    layer = m.get("assets_layer")
    n_head = len(((m.get("assets") or {}).get("features")) or [])
    if layer == "buildings" and n_head > 200:
        # A sample of unnamed centroids is a misleading picture, not a thinner
        # one: at panel size a dense buildings headline is counts only.
        m["assets"] = None
        m["assets_note"] = f"{n_head} building centroids omitted at panel size — counts remain"
    if m.get("assets"):
        lean, n, trunc = _lean(m["assets"], cap=200)
        m["assets"] = lean
        if trunc:
            m["assets_note"] = (f"200 of {n} {layer} drawn at panel size (most severe first) "
                                "— counts and stats.assets remain complete")
    if m.get("point_layers"):
        lean_layers = {}
        for k, fc in m["point_layers"].items():
            if k == "buildings":
                continue
            lean_layers[k], _n, _t = _lean(fc, cap=200)
        m["point_layers"] = lean_layers or None
    if big() and layer == "buildings":
        m["assets"] = None
        m["assets_note"] = "building centroids omitted at panel size — counts remain"
    if big():
        m["hazard_grid"] = None               # the pixel picture goes before any named point
    dropped = []
    while big() and m.get("point_layers"):
        k = max(m["point_layers"], key=lambda k: len(m["point_layers"][k]["features"]))
        dropped.append(f"{k} ({len(m['point_layers'][k]['features'])} points)")
        del m["point_layers"][k]
        if not m["point_layers"]:
            m["point_layers"] = None
    if dropped:
        m["point_layers_note"] = ("omitted at panel size — counts and stats.assets remain: "
                                  + ", ".join(dropped))
    if big() and m.get("assets"):
        lean, n, _t = _lean(m["assets"], cap=50)
        m["assets"] = lean
        m["assets_note"] = (f"50 of {n} {layer} drawn at panel size (most severe first) "
                            "— counts and stats.assets remain complete")
    if big() and m.get("assets"):
        m["assets"] = None
        m["assets_note"] = f"{layer} points omitted at panel size — counts remain"
    if big():
        return None
    return m


_PANEL_PROPS = ("name", "hazard_class", "risk_level", "severity")


def _lean(fc: dict, cap: int | None = None) -> tuple[dict, int, bool]:
    """(collection, total, truncated): the features with only the properties the
    panel draws and names by — plus the contributor's own id — most severe first,
    at most `cap`."""
    from ..graph.geo import viz as _viz
    feats = (fc or {}).get("features") or []
    out = []
    for f in feats:
        p = f.get("properties") or {}
        q = {k: p[k] for k in _PANEL_PROPS if k in p}
        ident = _viz.ident(p)
        if ident is not None:
            q["id"] = ident
        out.append({"type": "Feature", "geometry": f.get("geometry"), "properties": q})
    out.sort(key=lambda f: -((f["properties"].get("severity") or 0)))
    truncated = cap is not None and len(out) > cap
    return {"type": "FeatureCollection", "features": out[:cap] if cap else out}, len(feats), truncated


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
