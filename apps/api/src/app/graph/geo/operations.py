"""The operations the route node offers the model (as OpenAI tool schemas) and the
dispatch into the deterministic store. The model picks an operation, extracts
`place`, and — for operations that read a hazard raster — selects the hazard
layer(s) from the catalog by matching their descriptions. The graph fetches the
bundle for that place (and clips the selected hazards into it), then dispatch runs
the op over the bundle, passing the chosen hazard layer key as `hazard`.
"""
from . import registry, store

_PLACE = {"type": "string", "description": "City or district, e.g. 'Battambang' or 'Siem Reap, Cambodia'"}
_SEVERITY = {"type": "integer", "default": 1, "description": "only count hazard severity >= this (1-5)"}
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
        {"name": "count_in_hazard",
         "description": "Count hospitals or schools within a hazard zone of a place.",
         "parameters": {"type": "object", "required": ["place", "layer", "hazard_layers"], "properties": {
             "place": _PLACE, "layer": _POI, "hazard_layers": hazard, "min_severity": _SEVERITY}}},
        {"name": "roads_in_hazard",
         "description": "Length and share of road within a hazard zone of a place.",
         "parameters": {"type": "object", "required": ["place", "hazard_layers"], "properties": {
             "place": _PLACE, "hazard_layers": hazard, "min_severity": _SEVERITY}}},
    ]
    return [{"type": "function", "function": t} for t in tools]


_DISPATCH = {"count_features": store.count_features,
             "count_in_hazard": store.count_in_hazard,
             "roads_in_hazard": store.roads_in_hazard}


def dispatch(operation, aoi, hazard_layers=None, **op_args):
    """Run the op over the fetched bundle. Hazard ops get the chosen layer key as
    `hazard`; count_features takes no hazard."""
    if operation == "count_features":
        return store.count_features(aoi, **op_args)
    hazard = (hazard_layers or [None])[0]
    return _DISPATCH[operation](aoi, hazard, **op_args)
