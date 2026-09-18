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

import os
import re

from ..graph.geo import combine, ingest, rasterstats, schema, store as geostore, tiffs, viz

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


# A computed risk grid carries no catalog legend; these are the platform's own
# class labels for the 1-5 risk scale (the same scale the hub's indicator scheme uses).
_RISK_LABELS = {1: "Very Low", 2: "Low", 3: "Moderate", 4: "High", 5: "Very High"}


def _l2_risk(aoi, hz, trace, gaps):
    """Compute the Layer-2 risk grid for this AOI: hazard crossed with weighted
    vulnerability (conf/risk_l2.yml). Returns (aoi key, weights) or (None, None)
    with a declared gap — a missing vulnerability layer must decline, not crash."""
    weights = combine.weights_for(hz)
    if not weights:
        gaps.append(f"{hz}: no Layer-2 vulnerability weights are configured, so this "
                    "answer is hazard exposure only, not a risk level")
        return None, None
    try:
        path = combine.combine_l2(aoi, hz)
    except Exception as exc:
        gaps.append(f"{hz}: risk level could not be computed ({type(exc).__name__}: {exc}) "
                    "— this answer is hazard exposure only")
        return None, None
    key = os.path.basename(path)[:-len(".tif")]          # risk_<hazard>_l2
    aoi[key] = path
    trace.append(f"risk_l2[{hz}] {' + '.join(f'{k}:{v}' for k, v in weights.items())}")
    return key, weights


def _effective_weights(aoi, hz, weights) -> tuple[dict, list]:
    """What the weights ACTUALLY were, cell by cell.

    The engine drops a no-data vulnerability cell and renormalises the rest (rule R6).
    That is correct, and silent: a layer that is empty over most of an AOI contributes
    almost nothing while the recipe still advertises its nominal weight. Measured here
    so the citation can state the effective weight and the coverage behind it —
    population cover was 1.6% of one real AOI, against a nominal 0.40.
    """
    import numpy as np
    import rasterio
    adir = os.path.dirname(aoi["admin"])
    with rasterio.open(aoi[hz]) as h:
        footprint = h.read(1) > 0
    cells = int(footprint.sum())
    cover, notes = {}, []
    ref_tag = os.path.splitext(os.path.basename(aoi[hz]))[0]
    for layer in weights:
        aligned = os.path.join(adir, f"{layer}__aligned__{ref_tag}.tif")
        try:
            with rasterio.open(aligned) as s:
                arr = s.read(1)
            cover[layer] = float((arr[footprint] > 0).mean()) if cells else 0.0
        except Exception:
            cover[layer] = 0.0
            notes.append(f"{layer}: coverage unreadable")
    total = sum(weights[l] * cover[l] for l in weights)
    effective = ({l: round(weights[l] * cover[l] / total, 3) for l in weights}
                 if total > 0 else {l: 0.0 for l in weights})
    for layer in weights:
        if cover[layer] < 0.5:
            notes.append(f"{layer} covers {cover[layer] * 100:.1f}% of the hazard footprint, so its "
                         f"nominal weight {weights[layer]} acts as {effective[layer]}")
    return {"cells": cells, "coverage": cover, "effective": effective}, notes


RISK_CORPUS = "risk"


def _document_hits(place, hz, focus, trace, gaps):
    """Document evidence for this place and hazard, mirroring the food-security pack:
    one forward-looking slice, one retrospective. An empty or unreadable library is a
    declared gap, never a failure — the computed numbers still stand on their own."""
    from ..llm import MissingAPIKey
    from ..rag.store import Corpus, CorpusError
    hazard = re.sub(r"_rp\d+$", "", hz.removeprefix("hazard_"))
    try:
        corpus = Corpus(RISK_CORPUS)
    except (CorpusError, MissingAPIKey) as exc:
        gaps.append(f"the risk document library could not be read ({exc}) — nothing "
                    "published is cited alongside these numbers")
        return None, []
    n_docs = len(corpus.documents())
    if n_docs == 0:
        gaps.append("the risk document library is empty — no published assessment, event "
                    "report or method note is cited alongside these numbers. It accepts "
                    "contributions: a document lands the same way it does for any pack")
        return corpus, []
    queries = ((f"{hazard} hazard risk assessment {place} {focus}".strip(), "forecast"),
               (f"past {hazard} event impact and damage in {place}".strip(), "retrospective"))
    hits = []
    for q, temporal in queries:
        try:
            got = corpus.search(q, k=3, temporal=temporal)
        except (CorpusError, MissingAPIKey) as exc:
            gaps.append(f"document retrieval failed ({exc})")
            return corpus, hits
        trace.append(f"retrieve[{temporal}] {q!r} -> {len(got)} hits")
        hits.extend(got)
    if not hits:
        gaps.append(f"the risk library holds {n_docs} document(s), none relevant to "
                    f"{hazard} in {place} above the relevance floor — a decline, not a "
                    "weak match")
    return corpus, hits


def _pack_feeds(place, trace, gaps):
    """Every feed bound to the risk pack, queried and returned with its passport.

    The risk gatherer read no feeds at all, so a contributed table or API feed could
    be landed, be queryable on its own, and still never reach a risk answer. A feed
    says which pack may cite it; this reads the ones that say risk.
    """
    from ..contrib import staging
    from ..mcp import feeds, registry
    rows = {k: v for k, v in registry.FEEDS.items()
            if v.get("pack") == "risk" and v.get("status") == "available"}
    try:                      # a contributor's own staged feed previews here too
        rows.update({k: v for k, v in staging.visible_staged_feeds_for_pack("risk").items()
                     if k not in rows})
    except Exception:
        pass
    out = []
    for ds in sorted(rows):
        res = feeds.query(ds, {"limit": 3})
        if res.get("status") != "ok":
            gaps.append(f"feed {ds} bound to this pack did not answer: "
                        f"{res.get('note', 'no reason given')}")
            continue
        trace.append(f"feed[{ds}] {res.get('count')} rows as of {res.get('as_of')}")
        out.append((ds, rows[ds], res))
    return out


def _layer_is_silent(clip_path: str) -> bool:
    """True when the clipped hazard holds no cell above 0 — the layer says nothing
    about this area, which is not the same as saying the area is safe."""
    try:
        import numpy as np
        import rasterio
        with rasterio.open(clip_path) as src:
            return not bool((src.read(1) > 0).any())
    except Exception:
        return False


def _severity_text(by_severity: dict, legend: dict, noun: str = "class") -> str:
    parts = []
    for cls in sorted(int(k) for k in (by_severity or {})):
        n = by_severity.get(cls, by_severity.get(str(cls), 0))
        if n:
            parts.append(f"{noun} {cls} ({legend.get(cls, 'unlabelled')}): {n}")
    return "; ".join(parts) or f"none in any {noun}"


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
    corpus, doc_hits = _document_hits(place, hz, focus, trace, gaps)
    # A layer that is silent over this area reports the same "0 exposed" as an area
    # that is genuinely safe, and the two mean opposite things. Found live: the JRC
    # 1 km return-period maps hold no flooded cell anywhere inside Battambang town
    # because their global model does not represent that catchment, while the 100 m
    # layer shows the town extensively flooded. Say which one is happening.
    silent = _layer_is_silent(aoi[hz])
    if silent:
        gaps.insert(0, (
            f"{hz} has NO cell above class 0 anywhere in {aoi.get('name', place)}. "
            "Every exposure count below is zero because the layer is silent here, not "
            "because the area is safe. Check the layer's resolution and coverage in its "
            "passport before reading any zero as an absence of hazard."))
        trace.append(f"silent[{hz}] no hazard cells in AOI")
    n = 0
    counts: dict = {}

    # --- what has been published about this place and hazard ------------------
    for h in doc_hits:
        m = h["metadata"]
        n += 1
        citations.append({
            "n": n, "kind": "document", "retrieval": "archived-document",
            "source": m.get("source"), "title": m.get("title"),
            "pub_date": m.get("pub_date"), "validation": m.get("validation"),
            "temporal": m.get("temporal"), "url": m.get("url"), "score": h["score"],
            "doc_id": h["doc_id"], "chunk_id": h["id"],
            "archived_copy": (f"/api/food-security/rag/document/{h['doc_id']}"
                              if corpus and corpus.raw_path(h["doc_id"]) else None),
            "usage_notes": m.get("usage_notes"),
            **({"staged_by": m["staged_by"], "contribution_id": m.get("contribution_id")}
               if m.get("staged_by") else {}),
            "text": h["text"]})
    if doc_hits:
        trace.append(f"documents[{len(doc_hits)}] cited")

    # --- feeds bound to this pack --------------------------------------------
    for ds, spec, res in _pack_feeds(place, trace, gaps):
        last = (res.get("records") or [{}])[-1]
        pp = res.get("passport") or {}
        n += 1
        bits = ", ".join(f"{k} {v}" for k, v in last.items() if v is not None)
        citations.append({
            "n": n, "kind": "index", "retrieval": "pulled-at-pack-time",
            "source": pp.get("source") or spec.get("source"),
            "title": spec.get("title", ds),
            "validation": pp.get("validation") or spec.get("validation", "unvalidated"),
            "url": pp.get("url"),
            **({"staged_by": spec["staged_by"],
                "contribution_id": spec.get("contribution_id")}
               if spec.get("staged_by") else {}),
            "text": (f"{spec.get('title', ds)} ({ds}), latest reading as of "
                     f"{res.get('as_of')}: {bits or 'no values returned'}. "
                     f"{res.get('summary', '')}"
                     + (f" Contributor guidance: {spec['usage_notes']}"
                        if spec.get("usage_notes") else "")),
        })

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
                     f"Method: {r['method']}."
                     + (f" NOTE: {hz} has no hazard cell anywhere in this area, so this "
                        "zero reports the layer's silence, not the absence of hazard."
                        if silent else "")),
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

    # --- Layer-2 risk: hazard crossed with weighted vulnerability -------------
    # The engine and its recipe already existed; this pack used to stop at hazard
    # exposure and declare risk levels a gap. It now computes them and says, in the
    # citations, exactly which vulnerability layers went in and how few they are.
    risk_key, risk_weights = _l2_risk(aoi, hz, trace, gaps)
    if risk_key:
        for layer in _ASSETS:
            rk = geostore.count_in_hazard(aoi, risk_key, layer, min_severity=min_sev)
            counts[layer]["at_risk"] = rk["count"]
            counts[layer]["by_risk"] = rk["by_severity"]
            n += 1
            cit = {
                "n": n, "kind": "risk_level", "retrieval": "computed-at-pack-time",
                "source": "platform Layer-2 engine (conf/risk_l2.yml)",
                "title": f"{layer} by risk level ({hz.removeprefix('hazard_')})",
                "validation": "documented-method",
                "text": (f"{rk['count']} of {counts[layer]['total']} {layer} in "
                         f"{aoi.get('name', place)} sit at risk level {min_sev} or higher "
                         f"once {hz.removeprefix('hazard_')} hazard is crossed with weighted "
                         f"vulnerability. By risk level — "
                         f"{_severity_text(rk['by_severity'], _RISK_LABELS, 'risk level')}. "
                         "Risk level is not hazard severity: a cell in a severe hazard class "
                         "with low vulnerability lands lower."),
                "method": "combine_l2",
            }
            sr = _series(f"risk_{layer}", rk["by_severity"], _RISK_LABELS,
                         f"{layer} by risk level")
            if sr:
                cit["series"] = sr
            citations.append(cit)
        rrk = geostore.roads_in_hazard(aoi, risk_key, min_severity=min_sev)
        counts["roads"]["at_risk_km"] = round(rrk["length_km"], 1)
        n += 1
        citations.append({
            "n": n, "kind": "risk_level", "retrieval": "computed-at-pack-time",
            "source": "platform Layer-2 engine (conf/risk_l2.yml)",
            "title": f"roads by risk level ({hz.removeprefix('hazard_')})",
            "validation": "documented-method",
            "text": (f"{rrk['length_km']:.1f} km of {rrk['total_road_km']:.1f} km of roads in "
                     f"{aoi.get('name', place)} sit at risk level {min_sev} or higher. "
                     "By risk level (km) — "
                     + ("; ".join(f"level {k}: {v:.1f}" for k, v in sorted(
                         (int(a), b) for a, b in (rrk["by_severity"] or {}).items()) if v)
                        or "none at any risk level")
                     + ". Each segment is attributed to the risk level at its midpoint."),
            "method": rrk["method"],
        })
        trace.append("risk_l2[counts] " + ", ".join(
            f"{k}:{v.get('at_risk')}" for k, v in counts.items() if "at_risk" in v)
            + f", roads:{rrk['length_km']:.1f}km")

        eff, eff_notes = _effective_weights(aoi, hz, risk_weights)
        if not silent:          # a silent layer has no footprint; the first gap says it
            gaps.extend(eff_notes)
        n += 1
        citations.append({
            "n": n, "kind": "method", "retrieval": "config",
            "source": "platform method registry", "title": "Layer-2 risk method",
            "validation": "documented-method",
            "text": ("Risk level = clip(round(hazard x V / 5), 1, 5), where V is the "
                     "weighted average of the vulnerability classes at that cell and a "
                     "no-data vulnerability layer is dropped with its weight "
                     "renormalised. Configured weights: "
                     + "; ".join(f"{lay} {w}" for lay, w in risk_weights.items())
                     + ". EFFECTIVE weights over this area, after each layer's real "
                     "coverage of the hazard footprint: "
                     + "; ".join(f"{lay} {eff['effective'][lay]} "
                                 f"(covers {eff['coverage'][lay] * 100:.1f}%)"
                                 for lay in risk_weights)
                     + ". Every input is on the same 1 to 5 class scale. The weights are "
                     "platform starting values, not calibrated against observed loss."),
        })

        n += 1
        vuln_bits = []
        for lay in risk_weights:
            try:
                m = tiffs.entry(lay)
            except Exception:
                m = {}
            vuln_bits.append(
                f"{lay} ({m.get('title') or 'no title recorded'}; source "
                f"{m.get('source') or 'unattributed'}; licence {m.get('license') or 'unstated'}; "
                f"vintage {m.get('vintage') or 'unrecorded'})")
        citations.append({
            "n": n, "kind": "vulnerability_layers", "retrieval": "computed-at-pack-time",
            "source": "platform raster catalog", "title": "vulnerability layers used",
            "validation": "unvalidated",
            "text": (f"{len(risk_weights)} vulnerability layers entered this risk level: "
                     + "; ".join(vuln_bits)
                     + ". The regional indicator scheme this platform is being built "
                     "against names 11 indicator families, so 8 are absent here: "
                     "population by age and sex, building height, land cover, distance "
                     "to shelter, distance to hospital, distance to school, GDP per "
                     "capita, and crop damage cost."),
        })

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
    elif meta.get("source"):
        passport_bits.append(f"Source: {meta['source']}.")
        if meta.get("license") or meta.get("vintage"):
            passport_bits.append(
                f"Licence {meta.get('license', 'unstated')}, vintage "
                f"{meta.get('vintage', 'unrecorded')}.")
        else:
            gaps.append(f"{hz} records a source but no licence or vintage")
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
        **({"staged_by": meta["staged_by"], "contribution_id": meta.get("contribution_id")}
           if meta.get("staged_by") else {}),
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
                 "1 (lowest) to 5 (highest). Hazard exposure answers WHERE the hazard "
                 "is; the risk-level citations above answer how bad it is once "
                 "vulnerability is weighted in."),
    })

    # --- what is missing, said as content --------------------------------------
    rp = re.search(r"_rp(\d+)$", hz)
    gaps[:0] = ([] if meta.get("vintage") else [
        "this raster records no publication date, version or licence"]) + [
        "risk levels use only 3 vulnerability layers (population, building density, "
        "distance to road) of the 11 indicator families the regional scheme defines, "
        "and the weights are platform starting values, uncalibrated against observed "
        "loss — treat a risk level as a screening signal, not an assessment",
        "no loss or damage estimate: nothing converts exposure or risk level into "
        "people affected, hectares, or cost",
    ] + ([f"this is the {rp.group(1)}-year return period — a "
          f"{100 / int(rp.group(1)):.1f}% chance in any year. Other return periods are "
          "separate layers; nothing here combines them into an annual expected loss"]
         if rp else
         ["no return period: this hazard layer is a single scenario, so nothing here "
          "is tied to an annual probability. Flood has return-period layers "
          "(flood_rp10 … flood_rp500); other hazards do not"]) + [
        "OSM asset data carries no retrieval date in the AOI bundle",
    ]

    if silent:                       # the loudest thing about this answer goes first
        for i, g in enumerate(gaps):
            if g.startswith(f"{hz} has NO cell"):
                gaps.insert(0, gaps.pop(i))
                break

    # The map shows the RISK grid when one was computed — that is what a planner
    # asked for — and falls back to the hazard clip when it was not. Both grids ride
    # along so a consumer can show either; the payload shape is unchanged.
    shown = risk_key or hz
    stats = {"queries": None, "place": aoi.get("name", place), "hazard": hz,
             "min_severity": min_sev, "counts": counts,
             "risk_layer": risk_key, "displayed_layer": shown,
             "viz": _bounded_viz(viz.build_payload(aoi, {
                 "hazard": shown, "method": "count_in_hazard", "layer": "hospitals",
                 "place": aoi.get("name", place), "min_severity": min_sev,
                 "count": counts["hospitals"].get("at_risk" if risk_key else "exposed"),
                 "by_severity": counts["hospitals"].get(
                     "by_risk" if risk_key else "by_severity")}))}
    stats["viz"]["layer_kind"] = "risk_level" if risk_key else "hazard_severity"
    stats["viz"]["hazard"] = hz              # always name the hazard, whatever is drawn
    grid = _severity_grid(aoi[hz])
    if grid:
        stats["viz"]["hazard_grid"] = grid
    if risk_key:
        rgrid = _severity_grid(aoi[risk_key])
        if rgrid:
            stats["viz"]["risk_grid"] = rgrid
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
