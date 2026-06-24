"""What the agent can and cannot answer — the anchor for honest refusals."""

# OSM point layers we count (fetched per place)
COUNTABLE = ("hospitals", "schools", "buildings")

# named in the use case but not ingested -> the agent must refuse, not guess
UNAVAILABLE = {
    "homeless": "no homeless-population layer",
    "population": "WorldPop not wired in",
    "evacuation_plan": "no evacuation layer",
    "ews": "no early-warning layer",
}
