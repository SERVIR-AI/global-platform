"""Declared per-raster contracts (conf/raster_schema.yml) + lookup.

Every raster we compute on must first pass verify.verify_raster against the schema
declared here — because filenames and the source sheet lie about units and scale (a
file the sheet called 'metres' held millimetres; a '1-5' layer was actually 0-4). The
schema is the ground truth a file is asserted against before any op uses it.
"""
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
    return doc


def schema_for(name):
    """Declared schema for a layer name/stem ('hazard_flood' or 'hazard_flood.tif'),
    merged over the file's `defaults`, or None if the layer isn't declared."""
    doc = _doc()
    layers = doc.get("layers", {})
    key = name[:-4] if name.endswith(".tif") else name
    entry = layers.get(key) if key in layers else layers.get(name)
    if entry is None:
        return None
    return {**doc.get("defaults", {}), **entry}


def names():
    """Every layer name declared in the schema."""
    return list(_doc().get("layers", {}))
