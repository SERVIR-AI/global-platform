"""The operations the route node offers the model (as OpenAI tool schemas) and the
dispatch into the deterministic store. The model picks an operation, extracts
`place`, and — for operations that read a hazard raster — selects the hazard
layer(s) from the catalog by matching their descriptions. The graph fetches the
bundle for that place, then dispatch runs the op over the bundle (so neither
`place` nor `hazard_layers` are passed on to the store).
"""
from . import registry, store

_PLACE = {"type": "string", "description": "City or district, e.g. 'Battambang' or 'Siem Reap, Cambodia'"}
_SEVERITY = {"type": "integer", "default": 1, "description": "only count flood severity >= this (1-5)"}
_POI = {"type": "string", "enum": list(registry.COUNTABLE)}


def _hazard(layers):
    return {"type": "array", "items": {"type": "string", "enum": layers},
            "description": "Hazard layer key(s) this needs; choose from the catalog in the "
                           "system message by matching their descriptions."}


def schema(hazard_layers):
    """OpenAI tool schemas, with the available hazard-layer keys injected into the
    operations that read a hazard raster."""
    hazard = _hazard(hazard_layers)
    tools = [
        {"name": "count_features",
         "description": "Count hospitals or schools in a place.",
         "parameters": {"type": "object", "required": ["place", "layer"], "properties": {
             "place": _PLACE, "layer": _POI}}},
        {"name": "count_in_flood",
         "description": "Count hospitals or schools within the flood hazard of a place.",
         "parameters": {"type": "object", "required": ["place", "layer", "hazard_layers"], "properties": {
             "place": _PLACE, "layer": _POI, "hazard_layers": hazard, "min_severity": _SEVERITY}}},
        {"name": "roads_in_flood",
         "description": "Length and share of road within the flood hazard of a place.",
         "parameters": {"type": "object", "required": ["place", "hazard_layers"], "properties": {
             "place": _PLACE, "hazard_layers": hazard, "min_severity": _SEVERITY}}},
    ]
    return [{"type": "function", "function": t} for t in tools]


_DISPATCH = {"count_features": store.count_features,
             "count_in_flood": store.count_in_flood,
             "roads_in_flood": store.roads_in_flood}


def dispatch(operation, aoi, **op_args):
    return _DISPATCH[operation](aoi, **op_args)
