"""Tool schemas the model picks from, and the dispatch to the deterministic store."""
from . import hazards, registry, store

_PLACE = {"type": "string", "description": "City or district, e.g. 'Battambang' or 'Siem Reap, Cambodia'"}
_LAYER = {"type": "string", "enum": list(registry.COUNTABLE)}
_HAZARD = {"type": "string", "enum": list(hazards.HAZARDS), "default": "flood",
           "description": "hazard type; defaults to flood"}
_SEVERITY = {"type": "integer", "default": 1, "description": "only count severity >= this (1-5)"}

TOOLS = [
    {"name": "count_features",
     "description": "Count hospitals or schools in a place (no hazard).",
     "parameters": {"type": "object", "required": ["place", "layer"],
                    "properties": {"place": _PLACE, "layer": _LAYER}}},
    {"name": "count_in_hazard",
     "description": "Count hospitals or schools within a hazard's severity zone in a place.",
     "parameters": {"type": "object", "required": ["place", "layer"], "properties": {
         "place": _PLACE, "layer": _LAYER, "hazard": _HAZARD, "min_severity": _SEVERITY}}},
    {"name": "roads_in_hazard",
     "description": "Length and share of road within a hazard's severity zone in a place.",
     "parameters": {"type": "object", "required": ["place"], "properties": {
         "place": _PLACE, "hazard": _HAZARD, "min_severity": _SEVERITY}}},
    {"name": "ask_user",
     "description": "Ask the user ONE clarifying question and wait for their reply. Use ONLY "
                    "when a required parameter (e.g. severity) is missing and you cannot infer "
                    "it. Include the options in the question.",
     "parameters": {"type": "object", "required": ["question"], "properties": {
         "question": {"type": "string", "description": "the clarifying question to show the user"}}}},
]

_DISPATCH = {"count_features": store.count_features,
             "count_in_hazard": store.count_in_hazard,
             "roads_in_hazard": store.roads_in_hazard}


def dispatch(name, args):
    return _DISPATCH[name](**args)
