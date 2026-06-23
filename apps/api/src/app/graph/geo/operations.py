"""The operations the route node offers the model (as OpenAI tool schemas) and the
dispatch into the deterministic store. The model picks an operation and extracts
`place`; the graph fetches the bundle for that place, then dispatch runs the op
over the bundle (so `place` itself is not passed on to the store).
"""
from . import registry, store

_PLACE = {"type": "string", "description": "City or district, e.g. 'Battambang' or 'Siem Reap, Cambodia'"}
_SEVERITY = {"type": "integer", "default": 1, "description": "only count flood severity >= this (1-5)"}

_TOOLS = [
    {"name": "count_features",
     "description": "Count hospitals or schools in a place.",
     "parameters": {"type": "object", "required": ["place", "layer"], "properties": {
         "place": _PLACE, "layer": {"type": "string", "enum": list(registry.COUNTABLE)}}}},
    {"name": "count_in_flood",
     "description": "Count hospitals or schools within the 100-year flood hazard of a place.",
     "parameters": {"type": "object", "required": ["place", "layer"], "properties": {
         "place": _PLACE, "layer": {"type": "string", "enum": list(registry.COUNTABLE)},
         "min_severity": _SEVERITY}}},
    {"name": "roads_in_flood",
     "description": "Length and share of road within the 100-year flood hazard of a place.",
     "parameters": {"type": "object", "required": ["place"], "properties": {
         "place": _PLACE, "min_severity": _SEVERITY}}},
]

# OpenAI tool-calling format, ready to hand to the route node's chat call.
SCHEMA = [{"type": "function", "function": t} for t in _TOOLS]

_DISPATCH = {"count_features": store.count_features,
             "count_in_flood": store.count_in_flood,
             "roads_in_flood": store.roads_in_flood}


def dispatch(operation, aoi, **op_args):
    return _DISPATCH[operation](aoi, **op_args)
