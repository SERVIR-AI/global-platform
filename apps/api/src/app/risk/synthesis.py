"""The RISK domain pack's gatherer: deterministic hazard-exposure evidence.

Mirrors food_security.synthesis in role only — there is no LLM here and no corpus
(none exists; that is a DECLARED GAP, not an omission). Every number a drafter may
state is written literally into citation text, because the groundedness gate's
number scan reads citation text. Exposure results are `computed-at-pack-time`:
reproducible from the named inputs and method, unlike a live pull (different next
month) or an archived document (re-readable bytes).

The pack also carries a `viz` payload (AOI, severity-tagged assets, vectorized
hazard polygons, legend) — persisted WITH the pack so the hazard_map embed can
resolve everything it renders from the receipt, which is what makes a risk answer
replayable as a picture and not only as prose.
"""

from __future__ import annotations

from ..graph.geo import ingest, rasterstats, schema, store as geostore, tiffs, viz

SECTIONS = (
    "## What the numbers show",
    "## Method and validation",
    "## What's missing and how to weigh it",
    "## Reading the severity scale",
)

_ASSETS = ("hospitals", "schools", "buildings")

# Only hazard_flood's lineage is stated in conf/tiffs.yml; announcing that the
# other eight are unattributed is part of the evidence, not a footnote.
_FLOOD_LINEAGE = "ADPC hazard_flood.tif, derived from JRC GLOFAS v2.1"


def _severity_text(by_severity: dict, legend: dict) -> str:
    parts = []
    for cls in sorted(int(k) for k in (by_severity or {})):
        n = by_severity.get(cls, by_severity.get(str(cls), 0))
        if n:
            parts.append(f"class {cls} ({legend.get(cls, 'unlabelled')}): {n}")
    return "; ".join(parts) or "none in any hazard class"


def _series(sid: str, by_severity: dict, legend: dict, unit: str) -> dict | None:
    pts = []
    for cls in range(1, 6):
        v = (by_severity or {}).get(cls, (by_severity or {}).get(str(cls)))
        pts.append({"t": f"class {cls}", "v": int(v or 0),
                    "c": legend.get(cls, "")})
    if sum(p["v"] for p in pts) == 0:
        return None
    return {"id": sid, "points": pts, "unit": unit, "categorical": True}


def gather_risk_evidence(target: dict, focus: str, trace: list,
                         extras: dict) -> tuple[list, list, dict]:
    """Citations + declared gaps + stats(viz) for one place x hazard."""
    place = target["place"]
    hz = tiffs.resolve(target["hazard"])
    if hz is None or not hz.startswith("hazard_"):
        raise ValueError(
            f"unknown hazard {target['hazard']!r} — available: "
            + ", ".join(sorted(k.removeprefix("hazard_")
                               for k in tiffs.catalog() if k.startswith("hazard_"))))
    min_sev = int(extras.get("min_severity") or 1)
    legend = tiffs.legend(hz)

    aoi = ingest.ensure_aoi(place=place)
    trace.append(f"aoi[{aoi.get('name', place)}] {aoi.get('area_km2')} km2 via {aoi.get('how')}")
    aoi[hz] = ingest.hazard_clip(aoi, hz)
    trace.append(f"clip[{hz}]")

    citations, gaps = [], []
    n = 0
    counts: dict = {}

    # --- exposure: one citation per asset class, numbers IN the text ----------
    for layer in _ASSETS:
        total = geostore.count_features(aoi, layer)["count"]
        r = geostore.count_in_hazard(aoi, hz, layer, min_severity=min_sev)
        counts[layer] = {"exposed": r["count"], "total": total,
                         "by_severity": r["by_severity"]}
        n += 1
        sid = f"exposure_{layer}"
        cit = {
            "n": n, "kind": "exposure", "retrieval": "computed-at-pack-time",
            "source": r["source"], "title": f"{layer} vs {hz}",
            "validation": "deterministic-computation",
            "text": (f"{r['count']} of {total} {layer} in {aoi.get('name', place)} fall in "
                     f"{hz.removeprefix('hazard_')} hazard class >= {min_sev}. "
                     f"By severity — {_severity_text(r['by_severity'], legend)}. "
                     f"Method: {r['method']}."),
            "method": r["method"],
        }
        sr = _series(sid, r["by_severity"], legend, f"{layer} by hazard class")
        if sr:
            cit["series"] = sr
        citations.append(cit)
        trace.append(f"exposure[{layer}] {r['count']}/{total}")

    rr = geostore.roads_in_hazard(aoi, hz, min_severity=min_sev)
    counts["roads"] = {"exposed_km": round(rr["length_km"], 1),
                       "total_km": round(rr["total_road_km"], 1)}
    n += 1
    cit = {
        "n": n, "kind": "exposure", "retrieval": "computed-at-pack-time",
        "source": rr["source"], "title": f"roads vs {hz}",
        "validation": "deterministic-computation",
        "text": (f"{rr['length_km']:.1f} km of {rr['total_road_km']:.1f} km of roads in "
                 f"{aoi.get('name', place)} fall in {hz.removeprefix('hazard_')} hazard "
                 f"class >= {min_sev}. By severity (km) — "
                 + "; ".join(f"class {k}: {v:.1f}" for k, v in sorted(
                     (int(a), b) for a, b in (rr["by_severity"] or {}).items()) if v)
                 + f". Method: {rr['method']}."),
        "method": rr["method"],
    }
    citations.append(cit)
    trace.append(f"exposure[roads] {rr['length_km']:.1f}km")

    # --- the hazard layer's passport: declared contract vs observed clip ------
    meta = tiffs.entry(hz)
    contract, obs, check_notes = None, None, []
    try:
        contract = schema.schema_for(hz)
    except Exception as exc:
        check_notes.append(f"declared contract unreadable ({type(exc).__name__})")
        gaps.append(f"{hz}: raster contract could not be read — layer unvalidated")
    try:
        obs = rasterstats.windowed_stats(aoi[hz])
    except Exception as exc:
        check_notes.append(f"clip stats unreadable ({type(exc).__name__})")
        gaps.append(f"{hz}: clip statistics could not be read — layer unvalidated")
    n += 1
    passport_bits = [f"Hazard layer {hz}: {meta.get('title', hz)}."]
    if hz == "hazard_flood":
        passport_bits.append(f"Lineage: {_FLOOD_LINEAGE}.")
    else:
        gaps.append(f"{hz} carries no stated lineage in the catalog — provider unattributed")
    if contract:
        passport_bits.append(
            f"Declared contract: dtype {contract.get('dtype')}, valid "
            f"{contract.get('valid_min')}-{contract.get('valid_max')} {contract.get('units')}.")
    if obs and obs.get("sampled_min") is not None:
        passport_bits.append(
            f"Observed on this AOI clip: dtype {obs.get('dtype')}, range "
            f"{obs['sampled_min']:g}-{obs['sampled_max']:g} across "
            f"{obs.get('sampled_distinct')} distinct values (sampled).")
    if meta.get("usage_notes"):
        passport_bits.append(f"Contributor guidance: {meta['usage_notes']}")
    passport_bits.append("Legend: "
                         + "; ".join(f"{k}={v}" for k, v in sorted(legend.items())))
    # "checked" must mean CHECKED: compare declared vs observed, never co-print
    # them under a passing label (adversarial review).
    validation = "unvalidated"
    if contract and obs and obs.get("sampled_min") is not None:
        mism = []
        if str(obs.get("dtype")) != str(contract.get("dtype")):
            mism.append(f"dtype {obs.get('dtype')} != declared {contract.get('dtype')}")
        lo_ok = contract.get("valid_min") is None or obs["sampled_min"] >= contract["valid_min"]
        hi_ok = contract.get("valid_max") is None or obs["sampled_max"] <= contract["valid_max"]
        if not (lo_ok and hi_ok):
            mism.append(f"observed range {obs['sampled_min']:g}-{obs['sampled_max']:g} "
                        f"outside declared {contract.get('valid_min')}-{contract.get('valid_max')}")
        if mism:
            validation = "structural-contract-FAILED"
            passport_bits.append("CONTRACT MISMATCH: " + "; ".join(mism) + ".")
            gaps.append(f"{hz} failed its structural contract: " + "; ".join(mism))
        else:
            validation = "structural-contract-checked"
    if check_notes:
        passport_bits.append("Verification notes: " + "; ".join(check_notes) + ".")
    citations.append({
        "n": n, "kind": "hazard_layer", "retrieval": "computed-at-pack-time",
        "source": meta.get("source") or ("ADPC" if hz == "hazard_flood" else "catalog (unattributed)"),
        "title": meta.get("title", hz), "validation": validation,
        "text": " ".join(passport_bits),
    })

    # --- method, as citable configuration -------------------------------------
    n += 1
    citations.append({
        "n": n, "kind": "method", "retrieval": "config",
        "source": "platform method registry", "title": "exposure overlay method",
        "validation": "documented-method",
        "text": ("Exposure = asset location sampled against the clipped hazard raster; "
                 "a point's class is the raster value at its coordinates (0 = no data / "
                 "no hazard); road exposure attributes each segment's haversine length "
                 "to the class at its midpoint. Severity classes are the provider's, "
                 "1 (lowest) to 5 (highest). This is a HAZARD overlay, not a risk "
                 "level: vulnerability weighting (L2) is not part of this pack yet."),
    })

    # --- what is missing, said as content --------------------------------------
    gaps[:0] = [
        "no risk document corpus exists — no publications, assessments or reports "
        "can be cited for this domain yet",
        "raster vintages unknown: no publication date, version or licence is "
        "recorded for any catalog raster",
        "risk levels (L1 precomputed / L2 vulnerability-weighted) are not in this "
        "pack — exposure vs hazard severity only; the compute engine exists",
        "OSM asset data carries no retrieval date in the AOI bundle",
    ]

    grid = _severity_grid(aoi[hz])
    stats = {"queries": None, "place": aoi.get("name", place), "hazard": hz,
             "min_severity": min_sev, "counts": counts,
             "viz": _bounded_viz(viz.build_payload(aoi, {
                 "hazard": hz, "method": "count_in_hazard", "layer": "hospitals",
                 "place": aoi.get("name", place), "min_severity": min_sev,
                 "count": counts["hospitals"]["exposed"],
                 "by_severity": counts["hospitals"]["by_severity"]}))}
    if grid:
        stats["viz"]["hazard_grid"] = grid
    return citations, gaps, stats


def _severity_grid(clip_path: str, cells: int = 56) -> dict | None:
    """The clip downsampled to a small severity grid — the honest way to show a
    raster small. Vectorized polygons simplified to panel scale turned into
    abstract shards (holes distort first); pixels stay pixels. ~3 KB as a
    row-major digit string."""
    try:
        import rasterio
        with rasterio.open(clip_path) as src:
            h = max(1, min(cells, src.height))
            w = max(1, min(cells, src.width))
            arr = src.read(1, out_shape=(h, w))
            b = src.bounds
        vals = "".join(str(min(9, max(0, int(v)))) for row in arr for v in row)
        return {"w": w, "h": h, "cells": vals,
                "bounds": [b.left, b.bottom, b.right, b.top]}
    except Exception:
        return None


def _bounded_viz(v: dict, tol: float = 2e-3, geojson_cap: int = 400_000) -> dict:
    """Keep the recorded map payload proportionate: simplify the vectorized hazard
    polygons (pixel-edge unions at full float precision measured 842 KB for one
    town), and if still over the cap drop the geojson — the raster_url remains and
    the embed renders from it. Persisted state should cost what it is worth.

    Tolerance is ~2 raster pixels (3 arcsec pixels = 8.3e-4 deg): the first pass
    used 5e-4 — SMALLER than one pixel — which simplified nothing and silently
    dropped every real town's polygons over the cap."""
    import json as _json

    from shapely.geometry import mapping, shape as _shape

    hl = v.get("hazard_layer") or {}
    gj = hl.get("geojson")
    if gj and gj.get("features"):
        simplified = []
        for f in gj["features"]:
            try:
                g = _shape(f["geometry"]).simplify(tol, preserve_topology=True)
                geom = mapping(g)
            except Exception:
                geom = f["geometry"]
            simplified.append({**f, "geometry": _round_coords(geom)})
        gj = {**gj, "features": simplified}
        if len(_json.dumps(gj)) > geojson_cap:
            hl = {**hl, "geojson": None,
                  "note": "vectorized polygons exceeded the recorded-size cap; "
                          "the embed renders from raster_url"}
        else:
            hl = {**hl, "geojson": gj}
        v = {**v, "hazard_layer": hl}
    return v


def _round_coords(geom: dict, nd: int = 5) -> dict:
    def r(x):
        if isinstance(x, (list, tuple)):
            return [r(i) for i in x]
        return round(x, nd) if isinstance(x, float) else x
    return {**geom, "coordinates": r(geom.get("coordinates", []))}


# ---- risk.brief: the accompanied path (platform LLM drafts server-side) -------

_PARSE_SYSTEM = (
    "Extract the risk-analysis target from the question. Call set_risk_target "
    "with the place (a city/district/province name) and the hazard. If the "
    "question is not about hazard exposure or risk for a place, do not call the "
    "tool — reply with one sentence saying why you cannot brief on it.")

_PARSE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "set_risk_target",
        "description": "Record the place and hazard the question asks about.",
        "parameters": {
            "type": "object",
            "properties": {
                "place": {"type": "string", "description": "named place"},
                "hazard": {"type": "string",
                           "description": "hazard type, e.g. flood, cyclone, "
                                          "earthquake, drought, fire"},
                "min_severity": {"type": "integer", "minimum": 1, "maximum": 5},
                "focus": {"type": "string"},
            },
            "required": ["place", "hazard"],
        },
    },
}]

_SYNTH_SYSTEM_TMPL = """You write hazard-exposure briefs for risk analysts.

Non-negotiable rules:
- Use ONLY the numbered evidence provided. Every paragraph must carry at least one citation marker like [3]. Never use a citation number that is not in the evidence list.
- Never state a number that does not appear in the evidence text.
- The declared-gaps evidence entry is citable: cite it in the what's-missing section.
- Exposure numbers are deterministic computations, not model predictions — attribute them to the named hazard layer and method, never to yourself.
- If the evidence cannot support an answer, write no sections; reply with one paragraph starting "DECLINE:" naming exactly what is missing.

Write EXACTLY these markdown sections and nothing else:
{sections}

Do not write a Sources section — the system appends it."""


def synthesize(question: str, provider: str | None = None,
               model: str | None = None) -> dict:
    """The accompanied pipeline for one risk question: LLM parse -> deterministic
    gather -> platform-LLM draft -> groundedness gate (retry x1). Returns the
    synthesize-shaped dict compose.run persists, plus required_sections/pack/
    target so the generic persistence stops assuming food-security."""
    from ..config import get_settings
    from ..food_security import synthesis as fs
    from ..llm import build_client, default_model
    from ..mcp import packs as mcp_packs

    settings = get_settings()
    provider = provider or settings.default_provider
    model = model or default_model(provider)
    client = build_client(provider)
    trace, usage = [f'question: "{question}"'], []
    base = {"provider": provider, "model": model,
            "required_sections": list(SECTIONS), "pack": "risk"}

    resp = client.chat.completions.create(
        model=model, max_tokens=300, tools=_PARSE_TOOLS,
        messages=[{"role": "system", "content": _PARSE_SYSTEM},
                  {"role": "user", "content": question}])
    usage.append(fs._usage(resp))
    msg = resp.choices[0].message
    if not msg.tool_calls:
        trace.append("parse -> out of scope (no tool call)")
        return fs._declined(msg.content or "This question is outside the risk "
                            "brief's scope.", trace=trace, usage=usage) | base
    import json
    try:
        args = json.loads(msg.tool_calls[0].function.arguments)
        if not isinstance(args, dict):
            raise ValueError("tool arguments are not an object")
    except (ValueError, TypeError) as exc:
        trace.append(f"parse -> malformed tool arguments ({exc})")
        return fs._declined("The question could not be parsed reliably — please "
                            "rephrase it.", trace=trace, usage=usage) | base
    target = {"place": (args.get("place") or "").strip(),
              "hazard": (args.get("hazard") or "").strip()}
    focus = (args.get("focus") or question).strip()
    trace.append(f"parse -> {target}  [the model extracts the target; it fetches nothing]")

    try:
        citations, gaps, stats = gather_risk_evidence(
            target, focus, trace, {"min_severity": args.get("min_severity")})
    except ValueError as exc:
        return fs._declined(str(exc), trace=trace, usage=usage) | base
    except (OSError, RuntimeError) as exc:
        # Same contract as assemble_pack for the identical gather: upstream
        # infrastructure failing (geocoder down, Overpass mirrors exhausted,
        # raster unreadable) is a GOVERNED decline, never a raw tool error.
        return fs._declined(
            f"evidence gathering failed: {type(exc).__name__}: {exc}. This is an "
            "upstream/infrastructure failure, not a coverage gap — retrying later "
            "may succeed.", trace=trace, usage=usage) | base
    citations = [*citations, mcp_packs.gaps_citation(citations, gaps)]

    system = _SYNTH_SYSTEM_TMPL.format(sections="\n".join(SECTIONS))
    user_msg = (f"Question: {question}\n"
                f"Parsed target: place={target['place']}, hazard={target['hazard']}\n\n"
                "Numbered evidence (the ONLY permissible sources):\n\n"
                + fs._render_pack(citations))
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user_msg}]
    check = None
    for attempt in (1, 2):
        resp = client.chat.completions.create(model=model, max_tokens=3000,
                                              messages=messages)
        usage.append(fs._usage(resp))
        draft = (resp.choices[0].message.content or "").strip()
        if getattr(resp.choices[0], "finish_reason", None) == "length":
            trace.append(f"synthesis attempt {attempt} -> TRUNCATED at max_tokens; "
                         "the draft is incomplete, not ungrounded")
        if draft.startswith("DECLINE:"):
            trace.append(f"synthesis attempt {attempt} -> model declined")
            return fs._declined(draft, trace=trace, usage=usage,
                                citations=citations, stats=stats) | base
        check = fs.check_grounded(draft, citations, sections=SECTIONS)
        check["attempts"] = attempt
        trace.append(f"groundedness attempt {attempt} -> "
                     + ("PASS" if check["passed"]
                        else "FAIL: " + "; ".join(check["failures"])))
        if check["passed"]:
            brief = draft + "\n\n## Sources\n" + fs._sources_md(citations)
            return {"declined": False, "brief": brief, "citations": citations,
                    "parsed": target, "target": target, "evidence": stats,
                    "gaps": gaps, "grounded": check, "trace": trace,
                    "usage": usage} | base
        messages += [{"role": "assistant", "content": draft},
                     {"role": "user", "content":
                      "Your draft failed the groundedness check — "
                      + "; ".join(check["failures"])
                      + ". Rewrite following the rules exactly."}]
    return fs._declined(
        "The draft could not be grounded in the evidence after a retry — refusing "
        "to ship an uncited brief.", trace=trace, usage=usage, citations=citations,
        check=check, stats=stats) | base
