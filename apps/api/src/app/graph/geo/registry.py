"""What the agent can and cannot answer — the anchor for honest refusals."""

# OSM point layers we count (fetched per place)
COUNTABLE = ("hospitals", "schools", "buildings")     # built-in, from OSM


def countable() -> tuple:
    """Built-in point layers plus every contributed POINT layer this caller may see.
    Line and polygon layers are measured, not counted (graph/geo/vectors.summarise)."""
    from . import vectors
    return COUNTABLE + tuple(l for l, e in vectors.visible().items()
                             if l not in COUNTABLE and vectors.family(e) == "points")

# named in the use case but not ingested -> the agent must refuse, not guess
UNAVAILABLE = {
    "homeless": "no homeless-population layer",
    "population": "WorldPop not wired in",
    "evacuation_plan": "no evacuation layer",
    "ews": "no early-warning layer",
}
