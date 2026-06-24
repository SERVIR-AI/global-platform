"""The deterministic geospatial core, moved from grp_mvp.

`ingest` resolves a place to cached OSM data + a clipped hazard raster (a bundle
of file paths); `store` runs the spatial operations over that bundle; `registry`
records what's answerable vs knowingly absent. No LLM lives here — every number
the agent reports is computed in this package.
"""
