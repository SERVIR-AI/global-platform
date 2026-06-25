"""Layer-2 combine: Risk = Hazard x weighted-sum(Vulnerability), all on a 1-5 scale.

Inputs are already-reclassed 1-5 rasters (verified by S1), aligned onto the hazard
grid (S2). The crossing rule and per-hazard weights live in conf/risk_l2.yml.

Per cell: V = weighted average of the vulnerability classes (a nodata vulnerability
cell is dropped and its weight renormalized, R6); Risk = clip(round(Hazard*V/5), 1, 5);
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
    with open(get_settings().risk_l2_config_path) as f:
        return yaml.safe_load(f) or {}


def weights_for(hazard):
    """Default {layer: weight} for a hazard from conf (e.g. 'hazard_flood' -> key 'flood')."""
    key = hazard[len("hazard_"):] if hazard.startswith("hazard_") else hazard
    return _recipe().get("weights", {}).get(key, {})


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


def combine_l2(aoi, hazard="hazard_flood", vuln_weights=None, recompute=False):
    """Compute the Layer-2 risk grid for `aoi` and write <aoi>/risk_<hazard>_l2.tif (the
    same 1-5 contract as a clipped hazard). Cached by file existence unless recompute (R8)."""
    weights = vuln_weights or weights_for(hazard)
    if not weights:
        raise ValueError(f"no Layer-2 weights for {hazard} (pass vuln_weights or add to conf)")
    adir = os.path.dirname(aoi["admin"])
    out = os.path.join(adir, f"risk_{hazard.replace('hazard_', '')}_l2.tif")
    if os.path.exists(out) and not recompute:
        return out

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
