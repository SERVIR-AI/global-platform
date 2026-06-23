"""Tool schemas the model picks from, and the dispatch to the deterministic store."""
from . import registry, store

_PLACE = {"type": "string", "description": "City or district, e.g. 'Battambang' or 'Siem Reap, Cambodia'"}
_SEVERITY = {"type": "integer", "default": 1, "description": "only count flood severity >= this (1-5)"}

TOOLS = [
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

_DISPATCH = {"count_features": store.count_features,
             "count_in_flood": store.count_in_flood,
             "roads_in_flood": store.roads_in_flood}


def dispatch(name, args):
    return _DISPATCH[name](**args)
