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
    contract = None
    try:
        contract = schema.schema_for(hz)
    except Exception:
        pass
    obs = None
    try:
        obs = rasterstats.windowed_stats(aoi[hz])
    except Exception:
        pass
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
    if obs:
        passport_bits.append(
            f"Observed on this AOI clip: dtype {obs.get('dtype')}, "
            f"range {obs.get('min')}-{obs.get('max')} (sampled).")
    passport_bits.append("Legend: "
                         + "; ".join(f"{k}={v}" for k, v in sorted(legend.items())))
    citations.append({
        "n": n, "kind": "hazard_layer", "retrieval": "computed-at-pack-time",
        "source": meta.get("source") or ("ADPC" if hz == "hazard_flood" else "catalog (unattributed)"),
        "title": meta.get("title", hz), "validation": "structural-contract-checked" if contract and obs else "unvalidated",
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

    stats = {"queries": None, "place": aoi.get("name", place), "hazard": hz,
             "min_severity": min_sev, "counts": counts,
             "viz": viz.build_payload(aoi, {
                 "hazard": hz, "method": "count_in_hazard", "layer": "hospitals",
                 "place": aoi.get("name", place), "min_severity": min_sev,
                 "count": counts["hospitals"]["exposed"],
                 "by_severity": counts["hospitals"]["by_severity"]})}
    return citations, gaps, stats
