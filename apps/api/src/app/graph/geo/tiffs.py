"""Read the tiff catalog (conf/tiffs.yml): where each hazard raster lives, how to
fetch it if missing, and what it represents. The route node uses the descriptions
to pick which layer a question needs; ingest uses the entry to locate/download it.
"""
import yaml

from ...config import get_settings


def catalog():
    """Return the full tiff catalog loaded from conf/tiffs.yml.

    Returns:
        dict: mapping of layer name to metadata dict (local_path, download_url, description, etc.)
    """
    with open(get_settings().tiffs_config_path) as f:
        return yaml.safe_load(f)


def entry(layer: str):
    """Return the catalog metadata dict for a hazard layer.

    Args:
        layer (str): key matching an entry in conf/tiffs.yml (e.g. 'hazard_flood')

    Returns:
        dict: catalog metadata with keys such as 'local_path', 'download_url', and 'description'

    Raises:
        ValueError: if `layer` is not a key in the tiff catalog
    """
    cat = catalog()
    if layer not in cat:
        raise ValueError(f"unknown tiff layer: {layer}")
    return cat[layer]


def descriptions():
    """Return a {layer: description} dict — the only fields the route node sees when choosing a layer.

    Returns:
        dict[str, str]: mapping of layer name to its human-readable description string
    """
    return {name: (meta.get("description") or "").strip() for name, meta in catalog().items()}
