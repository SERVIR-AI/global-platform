"""Read the tiff catalog (conf/tiffs.yml): where each hazard raster lives, how to
fetch it if missing, and what it represents. The route node uses the descriptions
to pick which layer a question needs; ingest uses the entry to locate/download it.
"""
import yaml

from ...config import get_settings


def catalog():
    """Hand-authored rows + contributed rows (conf/tiffs.contrib.yml — machine-
    owned, written only by the contribution gate; contrib cannot shadow)."""
    with open(get_settings().tiffs_config_path) as f:
        cat = yaml.safe_load(f) or {}
    contrib_path = get_settings().tiffs_contrib_path
    try:
        with open(contrib_path) as f:
            contrib = yaml.safe_load(f) or {}
        cat.update({k: v for k, v in contrib.items() if k not in cat})
    except FileNotFoundError:
        pass
    return cat


def entry(layer):
    cat = catalog()
    if layer not in cat:
        raise ValueError(f"unknown tiff layer: {layer}")
    return cat[layer]


def descriptions():
    """{layer: description} — the only fields the route node sees when choosing a layer.
    `internal: true` entries (e.g. the precomputed risk offered via the L1/L2 question) are hidden."""
    return {name: (meta.get("description") or "").strip()
            for name, meta in catalog().items() if not meta.get("internal")}


def legend(layer):
    """{class:int -> 'label (range)'} for a hazard layer, or {} if it has none or isn't in
    the catalog (e.g. a user-uploaded BYOD layer — the caller falls back to a default ramp)."""
    try:
        leg = entry(layer).get("legend") or {}
    except ValueError:
        return {}
    return {int(k): str(v).strip() for k, v in leg.items()}


def resolve(name):
    """A loose hazard name ('flood', 'hazard_flood', 'landslide') -> a catalog key, or None."""
    if not name:
        return None
    cat = catalog()
    name = str(name).strip().lower()
    if name in cat:
        return name
    if f"hazard_{name}" in cat:
        return f"hazard_{name}"
    return next((k for k in cat if name in k), None)
