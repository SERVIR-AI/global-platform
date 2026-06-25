# Computing risk: four layers, by how much we recompute

**Scope:** how we produce a **risk** answer, and where the data comes from — for **any of the nine
hazards** (flood, flash flood, cyclone, earthquake, landslide, storm surge, tsunami, drought, fire).
We use **flood** as the worked example, but the design is the same for all of them. Exposure data —
roads, hospitals, schools, buildings from OpenStreetMap — always stays as vectors (lists of shapes)
and isn't discussed here. This is only about the risk side.

The risk recipe is always the same: **take one hazard map (how dangerous), blend in the
vulnerability maps (who/what is here and how easily harmed), weighted by importance → a risk score
everywhere.** The four layers differ only in *how much of that recipe is already cooked for us* —
and, crucially, **whether the tifs already hold 1–5 scores or still hold raw values.** Each layer is
the fallback when the one above isn't available or doesn't give us enough control.

| | Start from | What we do | What we control | Cost / needs |
|---|---|---|---|---|
| **Layer 1** | `risk_<hazard>.tif` (precomputed risk) | sample it at our vectors → count by risk class | nothing — ADPC's recipe | trivial · Drive |
| **Layer 2** | the **reclassed 1–5** tifs (`hazard_*.tif` + `vulnerability_reclass_*`) | weight + combine — **pure crossing, no reclass** | weights | small · Drive |
| **Layer 3** | ADPC's **raw continuous** tifs (`global_pc_h100glob.tif`, `*_raw_SEA…`) | **reclass each to 1–5**, then weight + combine | weights **+ reclass cutoffs + return period + $ damage** | medium · Drive |
| **Layer 4** | the **true upstream providers** (JRC, WorldPop, Open Buildings, OSM) — *outside* ADPC's repository | fetch, reclass, align, weight, combine | **everything** (+ data freshness, timeframe, global coverage) | large · Earth Engine / portals |

All four are checked against ADPC's `risk_*.tif` (see *Validation*).

> **What changes between hazards, what doesn't.** At every layer the **vulnerability half and the
> pipeline are shared across all nine hazards.** Going from flood to another hazard only swaps the
> **hazard input** and the **weight column**. So once flood works, each new hazard is a plug-in.

---

## Layer 1 — use ADPC's precomputed risk grid

`risk_<hazard>.tif` already has hazard multiplied by vulnerability, baked in across Southeast Asia.
We build nothing — we **sample** it at each building/road and count by risk class. Same operation we
already do with the hazard file; just point it at the risk file:
`count_in_hazard(aoi, "risk_flood", "buildings")`.

- **Gives us:** "N buildings sit in high-risk areas," fast and free.
- **Note:** a building's class is its **30 m neighborhood's** composite risk, not its own construction.
- **Limit:** stuck with ADPC's weights, return period, and timeframe.

## Layer 2 — combine the already-reclassed tifs (pure crossing)

Every input here is **already 1–5** — ADPC did the binning. So we just run the formula:

`Risk = Hazard × Σ(weightᵢ · Vulnerabilityᵢ)`

There is **no reclass step** — the tifs already hold scores, so the combine engine literally crosses
them cell-by-cell.

- **Gives us:** control of the **weights** — the one genuinely contested choice (see *Weights*).
- **Still required:** the reclassed tifs aren't all on the same grid (building density 30 m, distances
  100 m, drought coarse…), so we **resample them onto one common grid**, then cross. That combined
  grid is our risk raster.
- **Limit:** inherits ADPC's reclass cutoffs, return period, and timeframe.

**The Layer-2 files (left column) are the 1–5 versions:**

| Layer | **Layer 2** file (already 1–5) | **Layer 3** file (raw value) |
|---|---|---|
| Flood hazard | `hazard_flood.tif` | `global_pc_h100glob.tif` (depth, m) |
| Population | `vulnerability_pop_all_total.tif` | `pop_all_total_raw_SEA.tif` (persons) |
| Building density | `vulnerability_reclass_blddensity.tif` | `bldDensity.tif` (% built-up) |
| Building height | `vulnerability_reclass_bldheight.tif` | `bldHeight.tif` (m) |
| Distance to road | `vulnerability_reclass_road.tif` | `roadDistance.tif` (m) |
| Distance to shelter | *(no reclass on Drive — we'd build it)* | `vulnerability_shelter_CostDistance_100m.tif` (m) |
| Land cover | `RLCMS_2024_SEA.tif` (categorical → level) | *(same file)* |

## Layer 3 — combine ADPC's raw tifs (reclass first, then cross)

Same Drive, same download path — but now we pull the **raw, continuous** versions (the right column
above): flood **depth in metres**, population in **persons**, building density in **% built-up**,
distances in **metres**. You **cannot** just cross these — multiplying metres × persons × % is
meaningless. So each one is first **reclassed to 1–5**, *then* combined. **That reclass step is the
whole point of Layer 3** — it's the one knob ADPC baked shut (the cutoffs, the return period) that we
reopen.

### The reclass cutoffs — per hazard (this is what Layer 3 adds over Layer 2)

We start from flood; the rest of the table is what to plug in for the other hazards.

| Hazard | Raw ADPC file | What it measures → 1–5 cutoffs | Watch out |
|---|---|---|---|
| **Flood** ← start here | `global_pc_h100glob.tif` | depth (m): 0–0.5 / 0.5–1 / 1–1.5 / 1.5–2 / >2 | confirm it's continuous, not the `_class` sibling |
| **Flash flood** | `hazard_flashflood.tif` | index score (~3.7–9.25) | **no cutoffs in the sheet**; only a pre-processed file exists |
| **Cyclone** | `Wind_CC_T100.tif` | wind (km/h): ≤61 / 62–88 / 89–117 / 118–156 / ≥157 | — |
| **Earthquake** | `PGA_250y.tif` | ground shaking (PGA): <25 / 25–50 / 50–150 / 150–300 / >300 | — |
| **Landslide** | `n1_mosaic_wgs84_opt.tif` | **already graded 1–5** | no reclass needed |
| **Storm surge** | `raw_storm.tif` | surge (m): <0.5 / 0.5–1 / 1–2 / 2–3.5 / >3.5 | source is coastal **points** — rasterize first |
| **Tsunami** | `raw_tsunami.tif` | run-up (m): 0–0.5 / 0.5–2 / 2–5 / 5–10 / >10 | coastal **points**; clamp negatives to "no hazard" |
| **Drought** | *(sheet mislabels this — confirm)* | probability (%): 0–5 / 5–10 / 10–15 / 15–25 / >25 | sheet stores the 5–10 & 10–15 cutoffs **as dates**; raw file ref is wrong |
| **Fire** | `raw_fire.tif` | detection frequency (count) | **no cutoffs in the sheet** — derive |

### The pipeline (same for every hazard)

1. **Clip** every layer to the Area of Interest.
2. **Reclass** each to 1–5 using the cutoffs above (and the vulnerability cutoffs — population density
   <10/10–50/50–150/150–300/>300 pers/ha, etc.).
3. **Align** — resample everything onto **one common grid** (e.g. 30 m, in the AOI's local metric
   coordinate system). Unavoidable: the sources arrive at different resolutions.
4. **Vulnerability** = `Σ(weightᵢ · Vᵢ)` with that hazard's weight column. Flood Table-A weights: Pop
   density 0.20, Land cover 0.15, Pop age/gender 0.15, Bld density 0.15, Dist-shelter 0.15, Bld
   height 0.10, Dist-road 0.10.
5. **Risk** = Hazard(1–5) × Vulnerability.
6. **Write** the risk grid → validate against `risk_<hazard>.tif`.

### What Layer 3 unlocks that 1 & 2 can't

We hold the hazard in **raw units** (flood depth in metres). Beyond the 1–5 risk score, that lets us
feed real depth into a published **depth-damage function** (JRC / World Bank) for actual **$ damage**
— impossible once depth has been squashed to 1–5.

> **One caveat on the flood file.** There are two flood-raw candidates: `global_pc_h100glob.tif`
> (treated here as continuous depth) and `global_pc_h100glob_class.tif` (the `_class` suffix suggests
> it's pre-binned). The spreadsheet's `gdrive_raw` cell confusingly names the `_class` one. First
> build step: download `global_pc_h100glob.tif`, open it, check the value range — ~0–6 m = real
> continuous depth (use it); 1–5 = pre-classed (use the other). A check against the file, not the sheet.

## Layer 4 — go to the true upstream sources (outside ADPC's repository)

Layer 3 reuses ADPC's *exports*. Layer 4 leaves ADPC entirely and pulls each layer from the
**original provider** — the same datasets ADPC built theirs from, but fetched fresh and direct:

- **Flood** → **JRC GloFAS** (the EU's Global Flood Awareness System; Earth Engine
  `JRC/CEMS_GLOFAS/FloodHazard/v2_1`)
- **Population** → **WorldPop** · **Building density/height** → **Google Open Buildings** + **GHSL** ·
  **Distances** → **OpenStreetMap** · **Land cover** → **ESA WorldCover** · **Vegetation/biodiversity**
  → **MODIS** / **WDPA**

The pipeline (clip → reclass → align → weight → combine → validate) is **identical to Layer 3** — only
the *fetch* changes.

- **Gives us, beyond Layer 3:** fresher data, alternative **return periods** and **timeframes**
  (near-real-time / annualized / climate-adjusted), **global coverage** (not just Southeast Asia), and
  the latest model versions — i.e. everything ADPC froze at export time.
- **Costs:** access to **Google Earth Engine** (a cloud project + auth) or each provider's portal/API.
  This is the only layer that needs infrastructure beyond the Drive downloads we already have.

---

## Validation (all four layers)

ADPC ships precomputed `risk_*.tif` for all 9 hazards, plus per-country land-cover truth
(`Stratified_Area_Estimation_<country>_2024`, Cambodia included). We **diff our result cell-by-cell
against `risk_<hazard>.tif`**, and sanity-check against a known past event (e.g. the 2020 flood): does
our high-risk area land where it actually hit? If "low risk" sits where reality was hit hard, the
weights are wrong — that's a finding, logged not auto-fixed.

## Weights — what blocks the *numbers* (not the design)

The spreadsheet has **two conflicting weight tables**, each with a **column per hazard**:

- **Table A** (`Multihazard Weight` tab) — 7 layers, round numbers, each hazard's column adds to
  exactly 1.0. **Pin this one** for the prototype (the meeting decision).
- **Table B** (per-hazard tabs) — ~19 layers, fine-grained, grouped People / Assets / Economic / Nature.

Five errors to confirm with Daniel/Pin before trusting any risk number:
1. Which table is official (gates the whole risk product).
2. "Distance to road" has weight 0 everywhere in Table B — probably a mistake.
3. The children male/female rows point at each other's files (a swap).
4. The flood weights add to ~1.10 instead of 1.0.
5. The storm/tsunami tab is mislabeled "drought."

None of this changes the **design** (the pipeline is identical for every hazard and every layer) —
only the **numbers** it produces. So weights are a config choice, defaulting to Table A, tuning deferred.

---

## Glossary

- **ADPC** — Asian Disaster Preparedness Center; provides the precomputed risk, reclass, and raw tifs.
- **AOI** — Area of Interest; the place the user is asking about.
- **Hazard** — how dangerous the event is at each place (flood depth, wind speed, ground shaking…).
- **Vulnerability** — how badly a place would suffer / how unable it is to cope.
- **Exposure** — what/who is in harm's way (counted from vectors; not risk on its own).
- **Reclassed (1–5) tif** — values already sorted into severity classes (Layer 2 inputs).
- **Raw / continuous tif** — values still in physical units: metres, %, persons (Layer 3 inputs).
- **Reclass** — sorting a continuous value into the 1–5 classes (the step Layer 3 adds over Layer 2).
- **Return period** — how rare an event is; a "100-year flood" has a 1-in-100 chance per year.
- **Google Earth Engine** — Google's cloud platform hosting the upstream datasets (needed only at Layer 4).
- **JRC GloFAS** — the EU's global river-flood model; the upstream flood-depth source.
- **WorldPop** — the upstream source for population. **Google Open Buildings** — building footprints.
- **OpenStreetMap** — roads and points of interest. **ESA WorldCover** — a public 10 m land-cover map.
