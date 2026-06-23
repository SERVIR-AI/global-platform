"""Read the tiff catalog (conf/tiffs.yml): where each hazard raster lives, how to
fetch it if missing, and what it represents. The route node uses the descriptions
to pick which layer a question needs; ingest uses the entry to locate/download it.
"""
import yaml

from ...config import get_settings


def catalog():
    with open(get_settings().tiffs_config_path) as f:
        return yaml.safe_load(f)


def entry(layer):
    cat = catalog()
    if layer not in cat:
        raise ValueError(f"unknown tiff layer: {layer}")
    return cat[layer]


def descriptions():
    """{layer: description} — the only fields the route node sees when choosing a layer."""
    return {name: (meta.get("description") or "").strip() for name, meta in catalog().items()}
