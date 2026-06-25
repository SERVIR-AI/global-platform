# BUILD_PLAN — layer-resolving risk agent

Technical tracker for building the risk agent described in `SOURCE_DATA_APPROACH.md`. This is the
living checklist. Build **one slice at a time**, each a meaningful demoable upgrade, tested on **real
data**. Update the status box as we go.

**Status:** ☐ todo · ◐ in progress · ☑ done · ⤬ blocked
**Current:** Layer 1 works (sample a precomputed tif at OSM assets). Next slice → **S1**.

---

## Target architecture

Keep the existing shape: FastAPI + hand-rolled LangGraph; LLM does routing/arg-fill/phrasing only;
**all GIS math is deterministic Python**. Computed risk grids are written into the per-AOI cache dir in
the **same 1–5 contract as a clipped hazard**, so `store._Severity` samples them and `viz` renders
them with no new code.

**Graph flow (target):**
`route → resolve (layer + asset) → plan → [approval] → fetch (+verify) → operate (align/reclass/combine/sample) → validate → finalize`

**New modules (all under `apps/api/src/app/graph/geo/`):**

| Module | Job | Slice |
|---|---|---|
| `conf/raster_schema.yml` + `schema.py` | per-file declared contract (role, dtype, range, units, scale, nodata, crs, pixel-size) | S1 |
| `verify.py` | open a file, confirm it matches its schema; **shared windowed-stats helper** | S1 |
| `align.py` | clip + reproject/resample any layer onto one AOI reference grid | S2 |
| `combine.py` | **Layer-2 engine**: weighted-sum vulnerability × hazard → 1–5 risk grid | S3 |
| `validate.py` | diff our grid vs ADPC `risk_<hazard>.tif` (the oracle) | S5 |
| `resolver.py` | choose L1→L2→L3→L4 by what exists; returns a `LayerPlan` | S6 |
| `asset_resolver.py` | map the asked asset → source (OSM / WorldPop / …) | S8 |
| `reclass.py` | **Layer-3 front-end**: raw continuous → 1–5 with declared cutoffs | S9 |
| `sources.py` | **Layer-4** hardcoded upstream provider registry | S10 |
| `plan.py` + graph edits | the human-readable plan + approval gate | S11 |

Already in place: `drive_tifs.py` (65 tif→Drive-id catalog), `ingest.source_raster(name)` (downloads
any by name), `ingest.hazard_clip`, `store.py` ops, `conf/tiffs.yml` (router menu for the 9 hazards).

---

## Cross-cutting rules (must hold for every slice)

These come from failure modes we've already hit or proven. Treat as invariants.

- **R1 — Verify before use.** No raster is used by any op until `verify_raster` passes its declared
  schema. Stats come from a **windowed/decimated read — never a full-array load** (`bldDensity` is
  182301×147780, `hazard_flood` 54691×44335 → a full read OOM-kills). One shared windowed-stats helper;
  every module reuses it.
- **R2 — One declared crossing rule.** `Risk = clip(round(Hazard × V / 5), 1, 5)` (or whatever we pick)
  lives in `conf`, validated by the oracle (S5) — **never hardcoded per call**, or multi-hazard
  widening and oracle agreement become meaningless.
- **R3 — Normalize scale before weighting.** Inputs are **not** uniformly 0–5 (population is 0–4).
  Rescale every input to a common 0–5 before the weighted sum, or it silently skews.
- **R4 — Two test tiers.** Tiny **synthetic 1–5 fixture grids** for fast math unit tests (align/combine/
  reclass); **real-data raster tests marked `slow`/`integration`** (cache is empty → first run does live
  Drive pulls, then caches). Both required per slice.
- **R5 — Naming is not uniform.** `hazard_landslides.tif` vs `risk_landslide.tif` (plural/singular);
  the resolver maps hazard↔risk↔vuln **explicitly**, never by shared stem.
- **R6 — Define nodata once.** How nodata in a vulnerability layer affects the weighted sum (skip / treat
  as 0 / propagate) is decided in S3 and applied stack-wide; output is nodata where the hazard is nodata.
  (`_Severity` already treats NaN as 0 and clamps negatives to 0 — stay consistent.)
- **R7 — Weights in conf, validated across AOIs.** Weights live in `conf`, not code. "Tuned" requires
  agreement across **several real AOIs** — no single-AOI overfit (guards S5's weight search).
- **R8 — Cache computed grids.** Write `risk_<hazard>_l2.tif` into the AOI dir and reuse by file
  existence (like `hazard_clip`), so repeated questions on a thread don't recompute.
- **R9 — Confirm the map path.** Before claiming "the map shows our risk surface," confirm
  `api/routes/raster.py` serves arbitrary AOI-dir keys (e.g. `risk_flood_l2.tif`); else add a small slice.

---

## Slice ladder

> Layer 2 spans **S1–S4** (verification → align → combine → wired into chat). S1 comes first because we
> proved files lie — building the combine on unverified files would bake in the mm/scale bugs.

### Foundation
- **☐ S1 — Per-file schema + windowed verification pass.**
  - *Goal:* every raster self-declares dtype/range/units/scale/nodata/crs/pixel-size and refuses use if
    the file on disk disagrees. The gate every later slice plugs into. Ships the shared windowed-stats helper (R1).
  - *Deliverable:* `conf/raster_schema.yml` for the flood-L2 set (`hazard_flood`, `vulnerability_reclass_blddensity`,
    `vulnerability_reclass_road`, `vulnerability_pop_all_total`, `risk_flood`) + the trap canary `global_pc_h100glob`;
    `geo/schema.py` (load/lookup, reuse `tiffs.yml` legend/band); `geo/verify.py` → `verify_raster(name)→VerifyReport`.
  - *Demo:* `python -m app.graph.geo.verify hazard_flood vulnerability_pop_all_total global_pc_h100glob`
    prints PASS/FAIL, auto-surfacing the int8 0–5 vs uint8 0–4 mismatch and `global_pc_h100glob` = uint32
    1–7180 → **millimetres** flagged against any "metres" claim.
  - *Tests:* `tests/test_verify.py` (slow) opens the REAL tifs via windowed reads — assert flood 0–5,
    pop 0–4, `global_pc_h100glob` uint32 max in thousands; one negative test declares `valid_max:5`/`units:metres`
    on the mm file and asserts verify **FAILs** with an explicit mismatch.

### Layer 2
- **☐ S2 — Grid-align two 1–5 rasters onto one AOI grid.**
  - *Goal:* isolate the alignment risk before arithmetic: bring any 1–5 tif onto the `hazard_flood` AOI
    grid with no shift or invented classes.
  - *Deliverable:* `geo/align.py`: `reference_grid(aoi, hazard)` (reuse `hazard_clip` transform/crs/shape);
    `align_to(ref, layer, aoi)` → clip + `rasterio.warp.reproject(resampling=nearest)` → `<aoi>/<layer>__aligned.tif`. Inputs pass verify (S1) first.
  - *Demo:* for a real AOI, `hazard_flood.tif` and `…blddensity__aligned.tif` print identical width/height/transform/crs.
  - *Tests:* real small AOI — assert `aligned.shape==flood.shape`, transforms/crs equal, values stay in
    declared class bounds after nearest resample, a known cell keeps a plausible value (no off-by-one).
- **☐ S3 — Layer-2 combine engine (flood only).**
  - *Goal:* the core deliverable, built first — pure cell-by-cell crossing of already-1–5 tifs into a risk
    grid; weights the only control; scale-normalized (R3); nodata rule decided (R6); crossing rule declared (R2).
  - *Deliverable:* `geo/combine.py`: `combine_l2(aoi, hazard='hazard_flood', vuln_weights, normalize=True)`
    → verify → align each vuln → rescale 0–4→0–5 → `V=Σ(wᵢ·vulnᵢ)` (weights sum to 1) → `Risk=clip(round(hazard·V/5),1,5)`
    → write `<aoi>/risk_flood_l2.tif` in the **1–5 clipped-hazard contract** so `_Severity` samples it unchanged. Default weights + crossing rule in `conf`.
  - *Demo:* `combine_l2` writes a real `risk_flood_l2.tif` we computed cell-by-cell (not ADPC's); print a class histogram.
  - *Tests:* real AOI — output 1–5 on the flood grid, nodata propagated, not all-zero; `weights=[1,0,0]`
    reproduces the single aligned layer; raising a weight monotonically shifts the histogram up; assert the 0–4 pop layer WAS rescaled.
- **☐ S4 — Wire L2 risk into the graph (end-to-end chat).**
  - *Goal:* first user-visible L1→L2 jump — "how much road is at flood **risk** in <place>?" answers off OUR grid.
  - *Deliverable:* `graph.fetch` builds the L2 grid + registers it in the AOI bundle; `operations.py`/`store.py`
    route "risk" questions to it; `prompts` learn the word "risk". (`_Severity` is already path-agnostic.)
  - *Demo:* CLI/chat returns a number with `by_severity`, sourced `risk_flood_l2 (Hazard × Vulnerability, weights=…)`;
    trace narrates verify → align → weighted_sum → cross → sample.
  - *Tests:* drive the graph with the StubClient for a risk question on a real AOI — assert source names the
    L2 grid and `length_km ∈ [0, total_road_km]`; endpoint integration test cites the l2 layer. **Guardrail: full suite still green.**

### Resolver
- **☐ S5 — Validation vs ADPC `risk_flood.tif` (oracle).**
  - *Goal:* earn trust — diff our grid vs ADPC's cell-by-cell; quantify agreement; recover effective weights.
  - *Deliverable:* `geo/validate.py`: `diff_against_risk(grid, 'flood', aoi)` → align `risk_flood.tif` to our
    grid → `{cell_agreement_pct, pct_within_1_class, mean_abs_class_diff, confusion_matrix, n_cells}` + a small weight search.
  - *Demo:* "L2 vs ADPC risk_flood: 87% within ±1 class, mean|diff|=0.4, best-fit weights=…".
  - *Tests:* grid-vs-self == 100%; default weights beat a floor (>70% within ±1); **skipping the 0–4→0–5
    rescale measurably worsens agreement** (proves R3 matters); confusion matrix sums to `n_cells`.
- **☐ S6 — Layer resolver (L1 vs L2).**
  - *Goal:* the brain — for a hazard, choose L1 (risk tif exists) or L2 (reclassed inputs exist); return a
    `LayerPlan {level, hazard_input, vuln_inputs, weights, oracle_tif, missing, rationale}`. Handles R5 naming.
  - *Deliverable:* `geo/resolver.py`; graph gets a resolve step before fetch that records the choice in State.
  - *Demo:* flood → trace "L1 available (risk_flood.tif) and L2 buildable from hazard_flood × [pop, blddensity, road]";
    a hazard missing reclassed vuln → "L2 unavailable: missing … (reason)".
  - *Tests:* (no network) `resolve_layer('flood')` sees L1+L2; forcing L2 picks combine inputs; a hazard with no
    vuln stack reports missing honestly; landslide naming mismatch resolves.
- **☐ S7 — Widen L2 to all reclassed hazards.**
  - *Goal:* prove the engine is hazard-agnostic — per-hazard vuln stacks + weight recipes in `conf`.
  - *Deliverable:* per-hazard L2 recipes in `conf/raster_schema.yml`; combine + resolver read them; verify schema
    per new input; each output validated (S5) against its `risk_<hazard>.tif` where one exists.
  - *Demo:* chat answers "roads at landslide risk" and "buildings at cyclone risk" off fresh L2 grids, each with an agreement number.
  - *Tests:* parametrised over ≥2 more hazards on a real AOI — each output 1–5 on the right grid, passes its S5 floor, verify PASSes.

### Layer 3
- **☐ S8 — Asset resolver (OSM today, WorldPop population next).**
  - *Goal:* generalize WHAT is asked — buildings/roads/hospitals→OSM (exists), population→WorldPop raster
    sampled like a hazard; replace the `registry.UNAVAILABLE['population']` stub.
  - *Deliverable:* `geo/asset_resolver.py`: `resolve_asset(name)→{kind:'osm'|'raster', source}`; population path sums
    persons by risk class over the L2 grid; population-exposure op in `operations.py`/`store.py`.
  - *Demo:* "how many PEOPLE are at flood risk in <place>?" — previously an honest refusal, now a population estimate by class.
  - *Tests:* real AOI — sample `pop_all_total_raw_SEA.tif` against the L2 grid, total persons > 0, plausible
    magnitude, equals the sum across class buckets; unsupported assets still refuse with a reason.
- **☐ S9 — Layer 3: reclass front-end over RAW tifs.**
  - *Goal:* combine ADPC raw continuous rasters by reclassing each to 1–5 first with **declared cutoffs**
    (encoding the mm-not-metres trap), then feeding the same combine engine.
  - *Deliverable:* `geo/reclass.py`: `reclass_to_1_5(name, cutoffs, units, scale)` (verified by S1 so mm stays mm);
    resolver gains an L3 branch (raw + cutoffs → reclass → combine); raw-layer cutoffs/units in `conf`. Return-period + $-damage hooks stubbed.
  - *Demo:* "flood risk using return-period cutoffs X" recomputes via L3 from `global_pc_h100glob.tif` (read as MM);
    change cutoffs → grid shifts; diff (S5) vs our L2 and ADPC.
  - *Tests:* real `global_pc_h100glob.tif` — output strictly 1–5 with mm scale (500 mm → the 0.5 m class, NOT a 1000× error);
    a metres-declared cutoff set yields a degenerate all-class-1 grid (proves units matter); an L3 combine passes the S5 floor.

### Layer 4
- **☐ S10 — Layer 4: hardcoded upstream sources.**
  - *Goal:* complete the ladder — when no usable tif exists, resolve to a **hardcoded provider list**, fetch
    where infra allows, honestly flag where it doesn't (GEE).
  - *Deliverable:* `geo/sources.py`: `{hazard/asset → {provider, endpoint, requires:'GEE'|'http'|'osm', freshness, required_input_schema}}`
    for JRC GloFAS, WorldPop, Google Open Buildings, OSM, ESA WorldCover; resolver L4 fallback; HTTP-reachable
    sources fetched + verified (S1) + reclassed (S9) + combined; GEE-only return a plan flagged `needs_infra`, never a crash.
  - *Demo:* "using L4: WorldPop fetched live" for a no-GEE provider; for a GEE-gated hazard, an honest plan: "would fetch JRC GloFAS — needs Earth Engine (not available yet)".
  - *Tests:* resolver falls through L1→L2→L3→L4 correctly; each source carries a complete required-input schema;
    hit ≥1 real reachable HTTP upstream + verify the fetched file (S1); GEE-only returns `needs_infra`.

### Approval UX
- **☐ S11 — Plan-for-approval (human gate before execute).**
  - *Goal:* after the resolver picks layers/assets/weights/cutoffs, return a **plan** and wait for approval
    before any download/compute.
  - *Deliverable:* a `plan` node between resolve and fetch; State gains `plan` + `awaiting_approval`/`approved`;
    the MemorySaver checkpointer interrupts at plan and resumes on a follow-up "approve" (same thread_id); auto-approve flag for tests.
  - *Demo:* a risk question first replies "Plan: flood→L2, inputs[…], weights[…], ~X MB, will verify each file +
    validate vs risk_flood (87% confidence). Approve?"; "yes" executes and returns the number.
  - *Tests:* two turns on one thread — turn 1 asserts NO side effects + a plan/awaiting_approval; turn 2 ("approve")
    runs fetch/operate and produces a grounded number; "no" aborts cleanly.

---

## Done
- **☑ L0 — Catalog port.** `drive_tifs.py` (all 65 tif→id), `source_raster` reaches every layer by name. (commit `efb32f7`)
- **☑ L0 — File-content verification spike.** Opened 7 real tifs; proved units/scale traps (mm-not-metres, 0–4 vs 0–5) → motivates R1/S1.
- **☑ Layer 1.** Sample a precomputed hazard/risk tif at OSM assets (pre-existing).
