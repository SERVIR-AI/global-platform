"""Food-security spinoff (Workstream A) — a self-contained module.

Routes + services for the food-security capability: the GEOGLAM Crop Monitor
conditions query now; the bulletin-RAG synthesis engine lands here next. It
reuses the shared settings/cache (and later the shared LLM client), but stays
decoupled from the risk agent so it demos on its own and later ports cleanly
onto the config-driven harness.
"""
