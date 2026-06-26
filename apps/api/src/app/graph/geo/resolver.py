"""The layer resolver: for a hazard, decide whether to use Layer 1 (sample ADPC's
precomputed risk_<hazard>.tif) or Layer 2 (compute Hazard x Vulnerability ourselves),
by what's available + what the request asks for. Returns a LayerPlan the agent can
explain and act on. (Slice S6.)

Default preference is L1 (cheapest, already validated); L2 is chosen when explicitly
requested (e.g. custom weights / validation) or when no precomputed risk exists. The
hazard<->risk name map (conf/risk_l2.yml) handles the landslides/landslide mismatch (R5).
"""
from dataclasses import dataclass, field

import yaml

from ...config import get_settings
from . import combine, drive_tifs


@dataclass
class LayerPlan:
    hazard: str                       # logical hazard, e.g. "flood"
    level: int | None                 # 1 (precomputed) | 2 (computed) | None (can't)
    hazard_input: str | None = None   # hazard_<x> reclass tif (L2 only)
    vuln_inputs: list = field(default_factory=list)   # vulnerability layers (L2 only)
    weights: dict = field(default_factory=dict)       # their weights (L2 only)
    oracle_tif: str | None = None     # precomputed risk_<x> (L1 answer or L2 validation oracle)
    l2_available: bool = False        # could L2 be built?
    missing: list = field(default_factory=list)       # what's missing if it can't resolve
    rationale: str = ""               # human-readable choice, recorded in the trace


def _name_map():
    with open(get_settings().risk_l2_config_path) as f:
        return (yaml.safe_load(f) or {}).get("hazards", {})


def _logical(name):
    """Map 'flood' | 'hazard_flood' | 'risk_flood' | 'risk_flood_l2' | 'hazard_landslides'
    -> the logical hazard key ('flood', 'landslide'), or the bare stem if unknown."""
    nm = _name_map()
    if name in nm:
        return name
    stem = name
    for pre in ("hazard_", "risk_"):
        if stem.startswith(pre):
            stem = stem[len(pre):]
    if stem.endswith("_l2"):
        stem = stem[:-3]
    if stem in nm:
        return stem
    for key, v in nm.items():        # match the actual tif names (handles landslides->landslide)
        if name in (v.get("hazard"), v.get("risk")) or \
                f"hazard_{stem}" == v.get("hazard") or f"risk_{stem}" == v.get("risk"):
            return key
    return stem


def hazard_key_for(hazard):
    """The hazard_<x> reclass tif key for a hazard (handles landslides/landslide), or None."""
    nm = _name_map().get(_logical(hazard))
    return nm["hazard"] if nm else None


def risk_key_for(hazard):
    """The precomputed risk_<x> tif key for a hazard (handles landslides/landslide), or None."""
    nm = _name_map().get(_logical(hazard))
    return nm["risk"] if nm else None


def options_for(hazard):
    """The answer paths available for a hazard, in order: exposure (raw hazard), risk L1
    (precomputed), risk L2 (computed). Each is (key, layer, label); only available ones."""
    hz = _logical(hazard)
    nm = _name_map().get(hz)
    if not nm:
        return []
    hazard_key, risk_key = nm["hazard"], nm["risk"]
    opts = []
    if drive_tifs.drive_id(hazard_key):
        opts.append(("exposure", hazard_key,
                     f"**Exposure** — which assets sit in the {hz} zone (raw hazard, by severity). Fast."))
    if drive_tifs.drive_id(risk_key):
        opts.append(("risk-L1", risk_key,
                     f"**Risk, precomputed (L1)** — ADPC's official {hz} risk = Hazard × Vulnerability. Fast."))
    if resolve_layer(hz, requested_level=2).level == 2:
        opts.append(("risk-L2", f"{risk_key}_l2",
                     f"**Risk, recomputed (L2)** — Hazard × Vulnerability with our weights; tunable; ~94% match to L1."))
    return opts


def resolve_layer(hazard, requested_level=None):
    """Pick L1 vs L2 for `hazard` and return a LayerPlan. `requested_level` (1 or 2)
    forces a level; otherwise prefer L1 when a precomputed risk tif exists."""
    h = _logical(hazard)
    nm = _name_map().get(h)
    if not nm:
        return LayerPlan(h, None, missing=[f"unknown hazard '{hazard}'"],
                         rationale=f"cannot resolve '{hazard}': not a known hazard")
    hazard_key, risk_key = nm["hazard"], nm["risk"]
    weights = combine.weights_for(hazard_key)

    l1_ok = drive_tifs.drive_id(risk_key) is not None
    if not weights:
        l2_missing = [f"no L2 weight recipe for {h} in conf/risk_l2.yml"]
    else:
        l2_missing = [k for k in [hazard_key, *weights] if drive_tifs.drive_id(k) is None]
    l2_ok = bool(weights) and not l2_missing

    if requested_level == 2:
        level = 2 if l2_ok else None
    elif requested_level == 1:
        level = 1 if l1_ok else None
    else:
        level = 1 if l1_ok else (2 if l2_ok else None)

    oracle = risk_key if l1_ok else None
    plan = LayerPlan(h, level, oracle_tif=oracle, l2_available=l2_ok)
    if level == 1:
        plan.rationale = (f"L1: {risk_key}.tif exists -> sample the precomputed risk (cheapest)"
                          + (f"; L2 also buildable" if l2_ok else ""))
    elif level == 2:
        plan.hazard_input, plan.vuln_inputs, plan.weights = hazard_key, list(weights), weights
        plan.rationale = (f"L2: compute risk = {hazard_key} x weighted[{', '.join(weights)}]"
                          + (f"; validate vs {risk_key}.tif" if l1_ok else "; no L1 oracle"))
    else:
        plan.missing = l2_missing if requested_level == 2 else (l2_missing or [f"no {risk_key}.tif"])
        plan.rationale = f"cannot resolve {h} at level {requested_level or 'auto'}: missing {plan.missing}"
    return plan
