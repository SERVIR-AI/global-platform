"""Declared per-raster contracts (conf/raster_schema.yml) + lookup.

Every raster we compute on must first pass verify.verify_raster against the schema
declared here — because filenames and the source sheet lie about units and scale (a
file the sheet called 'metres' held millimetres; a '1-5' layer was actually 0-4). The
schema is the ground truth a file is asserted against before any op uses it.
"""
import math

import yaml

from ...config import get_settings


def _doc():
    with open(get_settings().raster_schema_path) as f:
        doc = yaml.safe_load(f) or {}
    try:
        with open(get_settings().raster_schema_contrib_path) as f:
            contrib = (yaml.safe_load(f) or {}).get("layers") or {}
        layers = doc.setdefault("layers", {})
        layers.update({k: v for k, v in contrib.items() if k not in layers})
    except FileNotFoundError:
        pass
    try:                                        # staged contracts, caller-scoped
        from ...contrib import staging
        layers = doc.setdefault("layers", {})
        layers.update({k: v for k, v in staging.visible_staged_contracts().items()
                       if k not in layers})
        for k in staging.visible_staged_amends():
            if k in layers:
                layers[k] = staging.amended_row(k, layers[k], contract=True)
    except Exception:
        pass
    return doc


def schema_for(name):
    """Declared schema for a layer name/stem ('hazard_flood' or 'hazard_flood.tif'),
    merged over the file's `defaults`, or None if the layer isn't declared."""
    doc = _doc()
    layers = doc.get("layers", {})
    key = name[:-4] if name.endswith(".tif") else name
    entry = layers.get(key) if key in layers else layers.get(name)
    if entry is None:
        # A year of a raster STACK is contracted once, under `stacks:` — one legend,
        # one contract for every year (tiffs._expand_stacks tags the row with its stack).
        from . import tiffs
        row = tiffs.catalog().get(key) or {}
        if row.get("stack"):
            entry = (doc.get("stacks") or {}).get(row["stack"])
    if entry is None:
        return None
    return {**doc.get("defaults", {}), **entry}


def names():
    """Every layer name declared in the schema."""
    return list(_doc().get("layers", {}))


def roles():
    """{role: rule} — what a role's values mean when they are read back, e.g. whether
    0 is "no value". Data (conf/raster_schema.yml `roles`, plus any the contributed
    contracts file declares), so a role a contributor introduces states its own rule
    instead of inheriting a guess; the hand-authored rule wins on a clash."""
    out = dict(_doc().get("roles") or {})
    try:
        with open(get_settings().raster_schema_contrib_path) as f:
            contrib = (yaml.safe_load(f) or {}).get("roles") or {}
        out.update({k: v for k, v in contrib.items() if k not in out})
    except FileNotFoundError:
        pass
    return out


def role_for(name):
    """The role a layer's name gives it — the namespace whose prefix it carries — or None."""
    for role, rule in namespaces().items():
        if rule.get("prefix") and str(name or "").startswith(str(rule["prefix"])):
            return role
    return None


def namespaces():
    """{role: rule} for the namespaces a contributed raster may take (`namespaces` in
    conf/raster_schema.yml): its prefix, what its cells hold, whether it needs a legend,
    how a projected file is re-gridded. A schema that declares none (a stripped test or
    deployment schema) falls back to the platform's shipped table — the namespaces are
    data, never a list typed into code."""
    rows = _doc().get("namespaces") or _shipped().get("namespaces")
    return {str(k): dict(v or {}) for k, v in (rows or {}).items()}


def _shipped():
    """The platform's own conf/raster_schema.yml, whatever schema is configured."""
    from ...config import _REPO_ROOT
    try:
        with open(_REPO_ROOT / "conf" / "raster_schema.yml") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def same_nodata(declared, observed, tol=0.0001) -> bool:
    """Is a declared nodata the file's? Equal within `tol`, or equal once both are
    float32 — the file stores the fill -3.4e38 as -3.3999999521443642e+38, and a
    contributor declaring -3.4e38 means that very value. NaN matches NaN."""
    if declared is None or observed is None:
        return declared is None and observed is None
    try:
        d, o = float(declared), float(observed)
    except (TypeError, ValueError):
        return str(declared).strip() == str(observed).strip()
    if math.isnan(d) or math.isnan(o):
        return math.isnan(d) and math.isnan(o)
    if abs(d - o) <= tol:
        return True
    import numpy as np
    big = float(np.finfo(np.float32).max)
    if max(abs(d), abs(o)) > big * (1 + 1e-7):     # beyond float32 both would read as inf
        return False
    with np.errstate(over="ignore"):
        return bool(np.float32(d) == np.float32(o))


def values_kind(role):
    """What a role's cells hold — 'classes', 'count' or 'amount' — from the configured
    roles, else its namespace, else the platform's shipped roles; None when none says."""
    role = str(role or "")
    return ((roles().get(role) or {}).get("values")
            or (namespaces().get(role) or {}).get("values")
            or ((_shipped().get("roles") or {}).get(role) or {}).get("values"))
