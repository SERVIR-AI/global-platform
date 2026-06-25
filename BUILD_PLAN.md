# BUILD_PLAN — layer-resolving risk agent

Technical tracker for the agent in `SOURCE_DATA_APPROACH.md`. Build **one slice at a time**, each a
demoable upgrade, every task tested on **real data**. Tick boxes as we go.

**Status:** ☐ todo · ◐ in progress · ☑ done · ⤬ blocked
**Now:** S1 done (verification pass green). Next task → **S2.1** (grid-align).

**Test tags:** `[fast]` synthetic-fixture unit test (no network) · `[slow]` real-data, live Drive pull,
cached after · `[stub]` graph driven by the existing StubClient · `[guardrail]` the existing suite must stay green.

---

## Target architecture

Keep the shape: FastAPI + hand-rolled LangGraph; LLM does routing/arg-fill/phrasing only; **all GIS
math is deterministic Python**. Computed risk grids are written into the per-AOI cache dir in the
**same 1–5 contract as a clipped hazard**, so `store._Severity` samples them and `viz` renders them with no new code.

**Graph flow (target):**
`route → resolve (layer + asset) → plan → [approval] → fetch (+verify) → operate (align/reclass/combine/sample) → validate → finalize`

**New modules** (all under `apps/api/src/app/graph/geo/`): `schema.py` + `conf/raster_schema.yml`,
`verify.py`, `align.py`, `combine.py`, `validate.py`, `resolver.py`, `asset_resolver.py`, `reclass.py`,
`sources.py`, `plan.py`. Already in place: `drive_tifs.py`, `ingest.source_raster`/`hazard_clip`, `store.py`, `conf/tiffs.yml`.

---

## Cross-cutting rules (invariants, hold for every slice)

- **R1 — Verify before use.** No raster is used until `verify_raster` passes its schema. Stats via a
  **windowed/decimated read — never full-load** (`bldDensity` 182301×147780, `hazard_flood` 54691×44335 → OOM). One shared helper, reused everywhere.
- **R2 — One declared crossing rule.** `Risk = clip(round(Hazard × V / 5), 1, 5)` lives in `conf`, validated by the oracle (S5) — never hardcoded per call.
- **R3 — Normalize scale before weighting.** Inputs aren't uniformly 0–5 (population is 0–4); rescale to a common 0–5 before the weighted sum.
- **R4 — Two test tiers.** Synthetic fixtures for math `[fast]`; real-data raster tests `[slow]`. Both per slice.
- **R5 — Naming isn't uniform.** `hazard_landslides.tif` vs `risk_landslide.tif`; resolver maps hazard↔risk↔vuln explicitly, never by stem.
- **R6 — Define nodata once.** Decide in S3 how a nodata vuln cell affects the sum (skip+renormalize / 0 / propagate); output nodata where hazard is nodata. (`_Severity` treats NaN as 0, clamps negatives — stay consistent.)
- **R7 — Weights in conf, validated across AOIs.** No single-AOI overfit.
- **R8 — Cache computed grids** by file existence per AOI (like `hazard_clip`).
- **R9 — Confirm the map path.** Verify `api/routes/raster.py` serves arbitrary AOI-dir keys (e.g. `risk_flood_l2.tif`) before claiming the map shows our surface.
- **R10 — Cost is in the plan.** Every plan (S11) states estimated download size + time; target <60 s synchronous, else it's an async job. (The chat-vs-report boundary.)

---

## Layer 2 — S1–S4

### ☑ S1 — Per-file schema + windowed verification pass
*Goal:* every raster self-declares its contract and is refused if the file disagrees. *Demo:* `verify hazard_flood vulnerability_pop_all_total global_pc_h100glob` prints PASS/FAIL and auto-flags the mm and 0–4/0–5 traps.
- ☑ **S1.1** Decide schema fields: `role, dtype, valid_min, valid_max, units, scale, nodata, crs, pixel_size_deg`
- ☑ **S1.2** `conf/raster_schema.yml` — declare `hazard_flood`, `vulnerability_reclass_blddensity`, `vulnerability_reclass_road`, `vulnerability_pop_all_total`, `risk_flood` + canary `global_pc_h100glob`
- ☑ **S1.3** `geo/schema.py` — `load()` + `schema_for(name)`, reusing `tiffs.yml` legend/band
- ☑ **S1.4** `geo/rasterstats.py` — `windowed_stats(path)` → dtype/min/max/nodata/crs/pixel_size via decimated read (the shared no-OOM helper, R1)
- ☑ **S1.5** `geo/verify.py` — `verify_raster(name)` → `VerifyReport{name, ok, observed, declared, mismatches[]}` (downloads via `source_raster`, calls `windowed_stats`)
- ☑ **S1.6** `verify.py` CLI — `python -m app.graph.geo.verify <name>…` prints a PASS/FAIL table
- ☑ **S1.T1** `[fast]` `windowed_stats` on a hand-built 1–5 fixture returns correct dtype/min/max/nodata
- ☑ **S1.T2** `[slow]` `verify_raster('hazard_flood')` → int8, 0–5, PASS
- ☑ **S1.T3** `[slow]` `verify_raster('vulnerability_pop_all_total')` → reports 0–4 (scale mismatch visible)
- ☑ **S1.T4** `[slow]` `verify_raster('global_pc_h100glob')` → uint32, max ~thousands, PASS against mm-declared schema
- ☑ **S1.T5** `[slow]` negative: declare `units: metres, valid_max: 5` on `global_pc_h100glob` → verify **FAILs** with explicit mismatch

### ☐ S2 — Grid-align a 1–5 raster onto one AOI grid
*Goal:* bring any 1–5 tif onto the `hazard_flood` AOI grid with no shift or invented classes. *Demo:* flood clip + aligned vuln print identical width/height/transform/crs.
- ☐ **S2.1** `geo/align.py` — `reference_grid(aoi, hazard)` → (transform, crs, shape) from `hazard_clip`
- ☐ **S2.2** `align_to(ref, layer, aoi)` — verify(layer) → clip to AOI → `rasterio.warp.reproject(resampling=nearest)` onto ref → write `<aoi>/<layer>__aligned.tif`
- ☐ **S2.3** Handle extent shortfall: layer not covering the AOI → nodata fill, not crash
- ☐ **S2.T1** `[fast]` reproject a synthetic grid to coarser+finer ref → shape/transform exactly match ref
- ☐ **S2.T2** `[fast]` nearest resample of a 1–5 grid invents no new class values
- ☐ **S2.T3** `[slow]` align `vulnerability_reclass_blddensity` to a real AOI flood clip → shape/transform/crs == flood
- ☐ **S2.T4** `[slow]` aligned values stay within declared class bounds; a known cell keeps a plausible value (no off-by-one)

### ☐ S3 — Layer-2 combine engine (flood only)
*Goal:* weighted-sum already-1–5 vuln × hazard → 1–5 risk grid; weights the only control. *Demo:* a real `risk_flood_l2.tif` we computed cell-by-cell; print a class histogram.
- ☐ **S3.1** Add the crossing rule (R2) + default flood weights to `conf`
- ☐ **S3.2** Decide the nodata-in-sum rule (R6) and record it in `conf`
- ☐ **S3.3** `geo/combine.py` — `combine_l2(aoi, hazard, vuln_weights, normalize=True)`: verify → align each vuln → rescale 0–4→0–5 (R3) → `V=Σ wᵢ·vulnᵢ` → `Risk=clip(round(hazard·V/5),1,5)` → write `<aoi>/risk_<hazard>_l2.tif` in the 1–5 contract
- ☐ **S3.4** Cache by file existence (R8) — skip recompute if the grid is present
- ☐ **S3.T1** `[fast]` two known 3×3 grids + known weights → hand-computed risk matches exactly
- ☐ **S3.T2** `[fast]` a 0–4 input is rescaled to 0–5 before weighting (assert it happened)
- ☐ **S3.T3** `[fast]` a nodata vuln cell follows the declared rule
- ☐ **S3.T4** `[slow]` real AOI → output 1–5 on the flood grid, not all-zero, nodata propagated
- ☐ **S3.T5** `[slow]` `weights=[1,0,0]` reproduces the single aligned layer
- ☐ **S3.T6** `[slow]` raising a weight monotonically shifts the histogram toward higher classes

### ☐ S4 — Wire L2 risk into the graph (end-to-end chat)
*Goal:* "how much road is at flood **risk** in <place>?" answers off our grid. *Demo:* number + `by_severity`, sourced `risk_flood_l2 (Hazard × Vulnerability, weights=…)`; trace narrates verify → align → weighted_sum → cross → sample.
- ☐ **S4.1** `graph.fetch` — on a "risk" op, call `combine_l2`, register `risk_<hazard>_l2.tif` in the AOI bundle
- ☐ **S4.2** `operations.py`/`store.py` — route "risk" questions to the L2 grid key (reuse `_Severity`)
- ☐ **S4.3** `prompts` — teach route to distinguish "risk" from raw "hazard"
- ☐ **S4.4** Add the verify→align→sum→cross→sample trace lines
- ☐ **S4.T1** `[stub]` risk question → `combine_l2` invoked; result source names the L2 grid
- ☐ **S4.T2** `[stub]` `length_km ∈ [0, total_road_km]`
- ☐ **S4.T3** `[slow]` endpoint integration: risk question returns an answer citing the l2 layer
- ☐ **S4.T4** `[guardrail]` full existing suite still green

---

## Resolver — S5–S7

### ☐ S5 — Validation vs ADPC `risk_flood.tif` (oracle)
*Goal:* diff our grid vs ADPC's; quantify agreement; recover effective weights. *Demo:* "L2 vs ADPC: 87% within ±1 class, mean|diff|=0.4, best-fit weights=…".
- ☐ **S5.1** `geo/validate.py` — `diff_against_risk(grid, hazard, aoi)`: align `risk_<hazard>.tif` to grid → `{cell_agreement_pct, pct_within_1_class, mean_abs_class_diff, confusion_matrix, n_cells}`
- ☐ **S5.2** Small weight search minimizing mean|diff|; record best-fit weights
- ☐ **S5.3** `--validate` path on combine or a CLI
- ☐ **S5.T1** `[fast]` grid-vs-self → 100% agreement, zero diff, confusion on the diagonal
- ☐ **S5.T2** `[fast]` confusion matrix sums to `n_cells`
- ☐ **S5.T3** `[slow]` default weights beat a floor (>70% within ±1) on a real AOI
- ☐ **S5.T4** `[slow]` skipping the 0–4→0–5 rescale measurably worsens agreement (proves R3)

### ☐ S6 — Layer resolver (L1 vs L2)
*Goal:* choose L1 (risk tif exists) or L2 (reclassed inputs exist); return a `LayerPlan`; handle naming. *Demo:* trace explains the choice; missing inputs reported honestly.
- ☐ **S6.1** hazard↔risk↔vuln name map (R5) in `conf`
- ☐ **S6.2** `geo/resolver.py` — `resolve_layer(hazard, requested_level=None)` → `LayerPlan{level, hazard_input, vuln_inputs, weights, oracle_tif, missing, rationale}` from `drive_tifs` + schema availability
- ☐ **S6.3** `graph` — resolve step before fetch; choice recorded in State + trace
- ☐ **S6.T1** `[fast]` `resolve_layer('flood')` → both L1+L2 available, returns the configured preference
- ☐ **S6.T2** `[fast]` forcing L2 selects the combine inputs + weights
- ☐ **S6.T3** `[fast]` a hazard with no reclassed vuln stack → `missing` populated with a reason
- ☐ **S6.T4** `[fast]` landslide naming mismatch resolves hazard↔risk correctly

### ☐ S7 — Widen L2 to all reclassed hazards
*Goal:* hazard-agnostic — per-hazard vuln stacks + weights in conf. *Demo:* "roads at landslide risk", "buildings at cyclone risk" off fresh grids, each with an agreement number.
- ☐ **S7.1** Per-hazard L2 recipes (vuln stack + weights) in `conf/raster_schema.yml`
- ☐ **S7.2** Add verify schema for each new vuln input file used
- ☐ **S7.3** combine + resolver read the per-hazard recipe
- ☐ **S7.T1** `[slow]` parametrised over ≥2 more hazards → output 1–5 on the right grid
- ☐ **S7.T2** `[slow]` each passes its S5 floor where a `risk_<hazard>.tif` exists
- ☐ **S7.T3** `[slow]` verify PASSes each new input file

---

## Layer 3 — S8–S9

### ☐ S8 — Asset resolver (OSM today, WorldPop population)
*Goal:* resolve the asked asset → source; add population exposure. *Demo:* "how many PEOPLE are at flood risk in <place>?" now answers (was a refusal).
- ☐ **S8.1** `geo/asset_resolver.py` — `resolve_asset(name)` → `{kind:'osm'|'raster', source}`
- ☐ **S8.2** Population path — sample `pop_all_total_raw_SEA.tif` (verified) → sum persons by risk class over the L2 grid
- ☐ **S8.3** Population-exposure op in `operations.py`/`store.py`; shrink `registry.UNAVAILABLE`
- ☐ **S8.T1** `[fast]` persons-by-class zonal sum over a known pop grid + known risk grid = hand value
- ☐ **S8.T2** `[slow]` real AOI: total persons > 0, plausible magnitude, equals the sum across class buckets
- ☐ **S8.T3** `[fast]` an unsupported asset still refuses with a reason

### ☐ S9 — Layer 3: reclass front-end over RAW tifs
*Goal:* reclass raw continuous → 1–5 with declared cutoffs (encoding mm-not-metres), then feed the same engine. *Demo:* flood risk recomputed from `global_pc_h100glob.tif` (read as MM); change cutoffs → grid shifts.
- ☐ **S9.1** Raw-layer cutoffs + units/scale in `conf` (encode mm)
- ☐ **S9.2** `geo/reclass.py` — `reclass_to_1_5(name, cutoffs, units, scale)` (verified by S1 so mm stays mm); define + document boundary inclusivity
- ☐ **S9.3** Resolver L3 branch: raw + cutoffs → reclass → combine
- ☐ **S9.4** Stub hooks: return-period + $-damage as plan controls (no impl yet)
- ☐ **S9.T1** `[fast]` cutoffs applied correctly to a known array; boundary inclusivity tested
- ☐ **S9.T2** `[slow]` `global_pc_h100glob`: a 500 mm cell → the 0.5 m class (NOT a 1000× error)
- ☐ **S9.T3** `[slow]` a metres-declared cutoff set on the mm file → degenerate all-class-1 grid (proves units matter)
- ☐ **S9.T4** `[slow]` an L3 combine passes the S5 floor

---

## Layer 4 — S10

### ☐ S10 — Hardcoded upstream sources
*Goal:* when no usable tif exists, fall to a hardcoded provider list; fetch HTTP, flag GEE honestly. *Demo:* "using L4: WorldPop fetched live"; a GEE hazard → honest `needs_infra` plan.
- ☐ **S10.1** `geo/sources.py` — registry `{hazard/asset → {provider, endpoint, requires:'GEE'|'http'|'osm', freshness, required_input_schema}}` for JRC GloFAS, WorldPop, Open Buildings, OSM, ESA WorldCover
- ☐ **S10.2** Resolver L4 fallback after L3
- ☐ **S10.3** HTTP-reachable path: fetch → verify (S1) → reclass (S9) → combine
- ☐ **S10.4** GEE-only path: return a plan flagged `needs_infra` (never crash)
- ☐ **S10.T1** `[fast]` resolver falls through L1→L2→L3→L4 when nothing local fits
- ☐ **S10.T2** `[fast]` each source entry carries a complete `required_input_schema`
- ☐ **S10.T3** `[slow]` hit ≥1 real reachable HTTP upstream + verify the fetched file (S1)
- ☐ **S10.T4** `[fast]` a GEE-only source returns `needs_infra`, not a crash

---

## Approval UX — S11

### ☐ S11 — Plan-for-approval (human gate before execute)
*Goal:* after resolving layers/assets/weights/cutoffs, return a plan and wait for approval before any download/compute. *Demo:* "Plan: flood→L2, inputs[…], weights[…], ~X MB / ~Y s, will verify each file + validate vs risk_flood (87% conf). Approve?" → "yes" runs.
- ☐ **S11.1** `geo/plan.py` — serialize LayerPlan + AssetPlan + verification expectations + S5 confidence + est. cost/time (R10) → human text
- ☐ **S11.2** `graph` — `plan` node between resolve and fetch; State `plan` + `awaiting_approval`/`approved`; interrupt at plan, resume on "approve" (same thread_id) via MemorySaver; auto-approve flag for tests
- ☐ **S11.3** `prompts` for plan phrasing; `chat.py` handles the approval turn
- ☐ **S11.T1** `[stub]` turn 1: NO fetch/compute side effects; returns plan + `awaiting_approval`
- ☐ **S11.T2** `[stub]` turn 2 "approve": fetch/operate run, grounded number produced
- ☐ **S11.T3** `[stub]` "no" aborts cleanly, no side effects

---

## Done
- **☑ Catalog port** — `drive_tifs.py` (65 tif→id); `source_raster` reaches every layer by name. (`efb32f7`)
- **☑ File-content verification spike** — opened 7 real tifs; proved the units/scale traps (mm-not-metres, 0–4 vs 0–5) → motivates R1/S1.
- **☑ Layer 1** — sample a precomputed hazard/risk tif at OSM assets (pre-existing).
- **☑ S1 — Verification pass.** `conf/raster_schema.yml` + `schema.py` + `rasterstats.windowed_stats` + `verify.py` (+ CLI). All 6 flood-L2 layers verified against the real files; 2 fast + 4 slow tests green; suite 18 passed / 5 skipped. Findings: 3 distinct grids (100 m / 30 m / 0.000833°), `global_pc_h100glob` confirmed millimetres, pop is 0–5 (earlier "0–4" was a 512-px sampling artifact).
