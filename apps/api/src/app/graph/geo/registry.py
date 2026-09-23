"""What the agent can and cannot answer — the anchor for honest refusals."""

# OSM point layers we count (fetched per place)
COUNTABLE = ("hospitals", "schools", "buildings")     # built-in, from OSM


def countable() -> tuple:
    """Built-in point layers plus every contributed point layer this caller may see."""
    from . import vectors
    return COUNTABLE + tuple(l for l in vectors.visible() if l not in COUNTABLE)

# named in the use case but not ingested -> the agent must refuse, not guess
UNAVAILABLE = {
    "homeless": "no homeless-population layer",
    "population": "WorldPop not wired in",
    "evacuation_plan": "no evacuation layer",
    "ews": "no early-warning layer",
}
