"""Validate a computed Layer-2 risk grid against ADPC's precomputed risk_<hazard>.tif
(the oracle): how often do our classes agree? Quantifies trust, and a small weight
search finds the weighting that best matches the reference. (Slice S5.)

Comparison is over the *union* of active cells (ours>0 or oracle>0), so a cell where
ADPC sees risk but we don't (or vice-versa) counts as disagreement, not a free pass.
"""
import warnings

warnings.filterwarnings("ignore")
import numpy as np
import rasterio

from . import align as align_mod
from . import combine as combine_mod
from . import resolver


def _metrics(ours, oracle):
    """Agreement between two 1-5 (0=nodata) class arrays over the union of active cells."""
    active = (ours > 0) | (oracle > 0)
    n = int(active.sum())
    if n == 0:
        return {"n_cells": 0, "exact_pct": None, "within1_pct": None,
                "mean_abs_diff": None, "confusion": {}}
    a = ours[active].astype("int16")
    b = oracle[active].astype("int16")
    diff = np.abs(a - b)
    confusion = {}
    for ours_c in range(6):
        for orac_c in range(6):
            c = int(((a == ours_c) & (b == orac_c)).sum())
            if c:
                confusion[f"ours{ours_c}/oracle{orac_c}"] = c
    return {"n_cells": n,
            "exact_pct": round(100 * float((diff == 0).mean()), 1),
            "within1_pct": round(100 * float((diff <= 1).mean()), 1),
            "mean_abs_diff": round(float(diff.mean()), 3),
            "confusion": confusion}


def _oracle_on_grid(grid_path, hazard, aoi):
    """Align the precomputed risk tif (correct name via the resolver) onto `grid_path`'s grid."""
    risk_key = resolver.risk_key_for(hazard) or f"risk_{hazard.replace('hazard_', '')}"
    with rasterio.open(grid_path) as g:
        ref = {"transform": g.transform, "crs": g.crs, "width": g.width, "height": g.height}
    with rasterio.open(align_mod.align_to(ref, risk_key, aoi)) as o:
        return o.read(1)


def diff_against_risk(our_grid_path, hazard, aoi):
    """Agreement of our computed risk grid vs ADPC's precomputed risk tif."""
    with rasterio.open(our_grid_path) as g:
        ours = g.read(1)
    oracle = _oracle_on_grid(our_grid_path, hazard, aoi)
    m = _metrics(ours, oracle)
    m["oracle"] = (resolver.risk_key_for(hazard) or f"risk_{hazard.replace('hazard_', '')}") + ".tif"
    return m


def _candidates(layers):
    """A small deterministic set of weight vectors: equal, then each layer dominant."""
    n = len(layers)
    cands = [{l: round(1 / n, 3) for l in layers}]
    for dom in layers:
        c = {l: round(0.2 / (n - 1), 3) for l in layers}
        c[dom] = 0.8
        cands.append(c)
    return cands


def best_weights(aoi, hazard="hazard_flood", layers=None, candidates=None):
    """Try a few weight vectors over the recipe layers; return the one whose risk grid
    best matches risk_<hazard>.tif (highest within-±1 agreement). Alignment is done once;
    each candidate is a cheap numpy recombine."""
    layers = layers or list(combine_mod.weights_for(hazard).keys())
    ref = align_mod.reference_grid(aoi, hazard)
    with rasterio.open(ref["path"]) as h:
        hazard_arr = h.read(1)
    arrays = {l: rasterio.open(align_mod.align_to(ref, l, aoi)).read(1) for l in layers}
    oracle = _oracle_on_grid(ref["path"], hazard, aoi)

    best = None
    for w in (candidates or _candidates(layers)):
        ws = [w.get(l, 0.0) for l in layers]
        risk = combine_mod._combine(hazard_arr, [arrays[l] for l in layers], ws)
        m = _metrics(risk, oracle)
        if best is None or (m["within1_pct"] or 0) > (best["within1_pct"] or 0):
            best = {"weights": w, **m}
    return best
