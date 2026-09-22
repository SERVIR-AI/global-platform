"""Layer-2 combine: Risk = Hazard x weighted-sum(Vulnerability), all on a 1-5 scale.

Inputs are already-reclassed 1-5 rasters (verified against the schema), aligned onto
the hazard grid. The crossing rule and per-hazard weights live in conf/risk_l2.yml.

Per cell: V = weighted average of the vulnerability classes (a nodata vulnerability
cell is dropped and its weight renormalized); Risk = clip(round(Hazard*V/5), 1, 5);
nodata (0) where the hazard is nodata or every vulnerability layer is nodata there.
The result is written in the same 1-5 contract as a clipped hazard, so store._Severity
samples it and the map renders it with no new code.
"""
import os
import warnings

warnings.filterwarnings("ignore")
import numpy as np
import rasterio
import yaml

from ...config import get_settings
from . import align as align_mod


def _recipe():
    """The hand-authored recipe, with any hub-adjusted weights laid over it.

    Adjusted weights live in their own machine-owned file so the committed recipe —
    and the comments in it, which carry the reasoning — are never rewritten by code.
    """
    settings = get_settings()
    with open(settings.risk_l2_config_path) as f:
        base = yaml.safe_load(f) or {}
    try:
        with open(settings.risk_l2_contrib_path) as f:
            overlay = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return base
    weights = dict(base.get("weights") or {})
    for hazard, row in (overlay.get("weights") or {}).items():
        weights[hazard] = row
    base["weights"] = weights
    base["adjusted"] = overlay.get("adjusted") or {}
    return base


def adjustment_for(hazard):
    """Who last changed this hazard's weights and when, or None if they are the
    platform's own. A risk brief has to say which it is reading."""
    key = hazard[len("hazard_"):] if hazard.startswith("hazard_") else hazard
    import re
    base = re.sub(r"_rp\d+$", "", key)
    adj = _recipe().get("adjusted") or {}
    return adj.get(key) or adj.get(base)


def staged_weights_row(hazard):
    """The caller's own STAGED weights row for this hazard, or None.

    Exposed rather than inlined because the brief has to be able to SAY that its
    risk levels rest on an unreviewed proposal. While this was private, the pack
    used the staged weights and then described the approved ones, so a receipt
    reported "the weights are platform starting values" over numbers computed from
    somebody's pending 0.55/0.30/0.15 — the precise fact the provenance line exists
    to surface.
    """
    import re as _re
    key = hazard[len("hazard_"):] if hazard.startswith("hazard_") else hazard
    base = _re.sub(r"_rp\d+$", "", key)
    try:
        from ...contrib import staging
        for k in (key, base):
            row = staging.visible_staged_weights(k)
            if row and row.get("weights"):
                return row
    except Exception:
        return None
    return None


def weights_for(hazard):
    """Default {layer: weight} for a hazard from conf (e.g. 'hazard_flood' -> key 'flood').

    A return-period variant falls back to its base hazard's recipe: 'flood_rp100' is
    still flood, and the vulnerability that matters does not change with the return
    period. Without this, every return-period layer silently lost its risk levels."""
    import re
    key = hazard[len("hazard_"):] if hazard.startswith("hazard_") else hazard
    base = re.sub(r"_rp\d+$", "", key)
    # A staged adjustment is the CALLER's own preview: their risk levels use it,
    # nobody else's do, until a reviewer approves (contrib/staging.py).
    row = staged_weights_row(hazard)
    if row:
        return row["weights"]
    weights = _recipe().get("weights", {})
    if key in weights:
        return weights[key]
    return weights.get(base, {})


def _combine(hazard, vulns, weights, class_max=5):
    """Pure numpy core. `hazard`: 2-D int array (1-5, 0=nodata). `vulns`: list of 2-D
    arrays (1-5, 0=nodata) aligned to `hazard`. `weights`: list aligned to `vulns`.
    Returns a uint8 1-5 risk array (0=nodata)."""
    h = hazard.astype("float64")
    num = np.zeros_like(h)            # Σ wᵢ·vulnᵢ over the valid (non-nodata) layers per cell
    den = np.zeros_like(h)            # Σ wᵢ      over the valid layers per cell
    for arr, w in zip(vulns, weights):
        a = arr.astype("float64")
        valid = a > 0                 # 0 = nodata
        num += np.where(valid, w * a, 0.0)
        den += np.where(valid, w, 0.0)
    V = np.divide(num, den, out=np.zeros_like(h), where=den > 0)    # weighted avg ∈ [1,5]
    risk = np.clip(np.round(h * V / class_max), 1, class_max)
    ok = (h > 0) & (den > 0)          # need a hazard class and ≥1 vulnerability layer
    return np.where(ok, risk, 0).astype("uint8")


def _weights_tag(weights: dict) -> str:
    """A short, stable fingerprint of the weights a grid was computed from."""
    import hashlib
    payload = ";".join(f"{k}={float(v):.6g}" for k, v in sorted((weights or {}).items()))
    return hashlib.sha1(payload.encode()).hexdigest()[:8]


def _recipe_weights(hazard: str) -> dict:
    """The recipe's own weights for this hazard, ignoring any staged proposal."""
    import re as _re
    key = hazard[len("hazard_"):] if hazard.startswith("hazard_") else hazard
    base = _re.sub(r"_rp\d+$", "", key)
    w = _recipe().get("weights", {})
    return w.get(key) or w.get(base, {})


def combine_l2(aoi, hazard="hazard_flood", vuln_weights=None, recompute=False):
    """Compute the Layer-2 risk grid for `aoi` and write <aoi>/risk_<hazard>_l2.tif (the
    same 1-5 contract as a clipped hazard). Cached by file existence unless recompute."""
    weights = vuln_weights or weights_for(hazard)
    if not weights:
        raise ValueError(f"no Layer-2 weights for {hazard} (pass vuln_weights or add to conf)")
    adir = os.path.dirname(aoi["admin"])
    # The weights are PART OF THE IDENTITY of this grid. Keyed on (AOI, hazard)
    # alone, a cached grid computed from the platform recipe was returned to a
    # caller whose staged proposal had replaced it — and the brief printed the new
    # weights over numbers produced by the old ones. A receipt that names a method
    # its numbers did not come from is the failure this platform exists to prevent,
    # and it is invisible: every figure looks plausible because it IS a real risk
    # grid, just not the one described.
    out = os.path.join(adir, f"risk_{hazard.replace('hazard_', '')}"
                             f"_l2__{_weights_tag(weights)}.tif")
    legacy = os.path.join(adir, f"risk_{hazard.replace('hazard_', '')}_l2.tif")
    if os.path.exists(out) and not recompute:
        return out
    if os.path.exists(legacy) and not recompute and weights == _recipe_weights(hazard):
        return legacy                      # the default recipe's grid, already computed

    ref = align_mod.reference_grid(aoi, hazard)
    with rasterio.open(ref["path"]) as h:
        hazard_arr = h.read(1)
    layers, ws = [], []
    for layer, w in weights.items():
        with rasterio.open(align_mod.align_to(ref, layer, aoi)) as s:
            layers.append(s.read(1))
        ws.append(float(w))
    risk = _combine(hazard_arr, layers, ws, class_max=int(_recipe().get("class_max", 5)))

    prof = {"driver": "GTiff", "height": ref["height"], "width": ref["width"], "count": 1,
            "dtype": "uint8", "crs": ref["crs"], "transform": ref["transform"],
            "compress": "lzw", "nodata": 0}
    with rasterio.open(out, "w", **prof) as d:
        d.write(risk, 1)
    return out
