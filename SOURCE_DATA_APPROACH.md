# Computing risk: three layers, depending on what data we have

**Scope:** how we produce a **risk** answer, and where the data comes from — for **any of the nine
hazards** (flood, flash flood, cyclone, earthquake, landslide, storm surge, tsunami, drought, fire).
We start with **flood** as the worked example, but the design is the same for all of them. Exposure
data — roads, hospitals, schools, buildings from OpenStreetMap — always stays as vectors (lists of
shapes) and isn't discussed here. This is only about the risk side.

The risk recipe is always the same: **take one hazard map (how dangerous), blend in the
vulnerability maps (who/what is here and how easily harmed), weighted by importance → a risk score
everywhere.** The three layers below differ only in *how much of that recipe is already cooked for
us.* Each is the fallback when the layer above isn't available or isn't enough.

| | Start from | What we do | What we control | Cost |
|---|---|---|---|---|
| **Layer 1** | `risk_<hazard>.tif` (precomputed) | sample it at our vectors → count buildings/roads by risk class | nothing — ADPC's recipe | trivial |
| **Layer 2** | `hazard_<hazard>.tif` + the `vulnerability_*.tif` set (all already 1–5) | apply our weights, combine → our own risk grid | **the weights** | small (just map algebra) |
| **Layer 3** | **no tifs** — raw upstream data | fetch, reclass, align, weight, combine | **everything** (weights, return period, cutoffs, timeframe, $ damage) | large (a real pipeline) |

All three are checked against ADPC's `risk_*.tif` (see *Validation*).

> **What changes between hazards, what doesn't.** The **vulnerability half and the whole pipeline are
> shared across all nine hazards** — same vulnerability layers, same clip → reclass → align → combine
> steps. Going from flood to any other hazard only swaps **two things**: the **hazard input** (a
> different source, units, and 1–5 cutoffs) and the **weight column** (each hazard has its own). So
> once flood works, each new hazard is "plug in its hazard layer + its weights," not a rebuild.

---

## Layer 1 — use ADPC's precomputed risk grid

`risk_<hazard>.tif` already has hazard multiplied by vulnerability, baked in across all of Southeast
Asia. We don't build anything — we **sample** it at each building/road location and count by risk
class. Same operation we already do with the hazard file (`store._Severity` doesn't care which tif
it reads); just point it at the risk file: `count_in_hazard(aoi, "risk_flood", "buildings")`.

- **Gives us:** "N buildings sit in high-risk areas," fast and free.
- **Note:** the class attached to a building is its **30 m neighborhood's** composite risk, not that
  specific building's construction. Two buildings in the same cell get the same class.
- **Limit:** we're stuck with ADPC's weights, their return period, their timeframe.

## Layer 2 — recompute risk from the reclassed tifs (our weights)

If we want to change the **weights** but nothing else, we skip `risk_<hazard>.tif` and start from the
already-1–5 hazard and vulnerability tifs, then run the formula ourselves:

`Risk = Hazard × Σ(weightᵢ · Vulnerabilityᵢ)`

ADPC already did the hard parts (unit handling, sorting values into 1–5). We just supply that
hazard's weight column and combine.

- **Gives us:** control of the weights — the one genuinely contested choice (see *Weights*).
- **Still required:** the reclassed tifs aren't all on the same grid (building density 30 m,
  distances 100 m, drought coarse…), so we **resample them onto one common grid**, then do the
  cell-by-cell math. That combined grid is our risk raster.
- **Skips:** raw units, reclassification, the data-quality snags in raw source.
- **Limit:** still inherits ADPC's reclass cutoffs, return period, and timeframe.

## Layer 3 — no tifs: rebuild from raw source

When nothing is pre-baked, we build every input from the original public datasets. The vulnerability
inputs and the pipeline are the **same for every hazard**; only the hazard layer differs.

### The vulnerability inputs (shared across all hazards)

For the 7-layer weight table, these are the seven, each fetched then reclassed to 1–5:

1. **Population density** — **WorldPop** (an estimate of people per ~100 m square, made by spreading
   census numbers using satellite imagery). → persons/ha → <10 / 10–50 / 50–150 / 150–300 / >300.
2. **Population age/gender** — **WorldPop age-sex grids** (separate maps for children / adults /
   elderly × male / female; the very young and old are more vulnerable). → category → level.
3. **Building density** — **Google Open Buildings** (Google's AI traced every building it could spot
   in satellite imagery; Earth Engine `GOOGLE/Research/open-buildings/v3/polygons`). → rasterize to
   **% built-up per 30 m cell** → <15 / 15–30 / 30–60 / 60–80 / >80 %.
4. **Building height** — **Global Building Atlas** or the public **GHSL building-height** layer
   (how tall buildings are; taller often = safer in a flood). → metres → <5 / 5–10 / 10–20 / 20–30 / >30 m.
5. **Distance to road** — **OpenStreetMap** roads (the free crowd-sourced world map; we already pull
   these via Overpass). → distance-to-nearest-road grid → 0–100 / 100–200 / 200–500 / 500–1000 / >1000 m.
6. **Distance to shelter** — **OpenStreetMap** (schools / stadiums as shelters). → distance grid →
   0–200 / 200–500 / 500–1000 / 1000–2000 / >2000 m.
7. **Land cover** — **ESA WorldCover** (public, 10 m) or ADPC's RLCMS land-cover map (what's on the
   ground: crops, urban, forest…). → category → level.

*(Some cutoffs vary by hazard — e.g. distance-to-road uses wider bands for drought/fire/earthquake.
Same layers, hazard-specific cutoffs and weights.)*

### The hazard input (one per hazard — this is the part that swaps)

We start from flood; the rest of the table is what to plug in for the others.

| Hazard | Raw source — what it actually is | Units → 1–5 cutoffs | Watch out |
|---|---|---|---|
| **Flood** ← start here | **JRC GloFAS Global River Flood Hazard v2.1.** The European Commission's Joint Research Centre runs a worldwide river-flood model called the Global Flood Awareness System (GloFAS); this layer maps **how deep river floodwater would reach** for a flood of a chosen rarity. Earth Engine: `JRC/CEMS_GLOFAS/FloodHazard/v2_1`. | water depth (m): 0–0.5 / 0.5–1 / 1–1.5 / 1.5–2 / >2 | pick a return period (100-year is the one ADPC productized) |
| **Flash flood** | **SERVIR Flash Flood Guidance System / Flash Flood Potential Index.** SERVIR is a joint NASA + USAID satellite program for regions like Southeast Asia. This layer **scores how prone each spot is to sudden floods from intense rainfall** — distinct from slow-rising river floods. | index score (raw range ~3.7–9.25) | **no cutoffs in the sheet** — derive (e.g. equal intervals over the range) |
| **Cyclone** | **Tropical-cyclone wind hazard** (SERVIR-Mekong). A map of the **strongest sustained wind speed** a place is expected to feel from a rare (~1-in-100-year) cyclone. Asset `Wind_CC_T100`. | wind speed (km/h): ≤61 / 62–88 / 89–117 / 118–156 / ≥157 | — |
| **Earthquake** | **Seismic ground-shaking hazard.** Gives **Peak Ground Acceleration (PGA)** — the standard measure of *how violently the ground shakes* — for a rare (1-in-250-year) earthquake. Asset `PGA_250y`. | PGA: <25 / 25–50 / 50–150 / 150–300 / >300 | — |
| **Landslide** | **Rainfall-triggered landslide susceptibility.** How likely a slope is to fail in heavy rain, already graded into five danger classes. Asset `landslides_rainfall_class`. | **already graded 1–5** | no reclass needed (use as-is) |
| **Storm surge** | **Coastal storm-surge heights.** How high the sea rises as a cyclone pushes water onshore. Delivered as a **scatter of points along the coastline**, not a grid. Asset `Storm_Surge_SEA`. | surge height (m): <0.5 / 0.5–1 / 1–2 / 2–3.5 / >3.5 | **rasterize the points** onto the grid first |
| **Tsunami** | **Modeled tsunami run-up heights.** How high a tsunami wave climbs onto land. Also a set of **coastal points**. Asset `Tsunami_hazard_SEA`. | run-up height (m): 0–0.5 / 0.5–2 / 2–5 / 5–10 / >10 | rasterize the points; clamp negative values to "no hazard" |
| **Drought** | **Climate-model dry-spell probability.** How often severe droughts are projected to occur, using the **SPEI** index (Standardized Precipitation-Evapotranspiration Index — it weighs rainfall against evaporation demand) under a high-emissions future scenario. Asset `drought_ssp585…` / SPEI-6. | probability (%): 0–5 / 5–10 / 10–15 / 15–25 / >25 | sheet stores the 5–10 & 10–15 cutoffs **as dates** — hardcode them |
| **Fire** | **Satellite fire-frequency map.** How often fires have actually burned at each spot, built from **25 years (2000–2024) of fire detections** by NASA's MODIS and VIIRS satellite sensors. Asset `fire_freq_sea_2000_2024`. | detection frequency (count) | **no cutoffs in the sheet** — derive |

### The pipeline (same for every hazard)

1. **Clip** every layer to the Area of Interest (the place asked about).
2. **Reclass** each to 1–5 using the cutoffs above.
3. **Align** — reproject + resample everything onto **one common grid** (e.g. 30 m, in the AOI's
   local metric coordinate system). Unavoidable, because the sources arrive at different resolutions
   and shapes — this is the step that makes the cell-by-cell math possible.
4. **Vulnerability** = weighted sum `Σ(weightᵢ · Vᵢ)` using that hazard's weight column. Flood
   Table-A weights: Pop density 0.20, Land cover 0.15, Pop age/gender 0.15, Bld density 0.15,
   Dist-shelter 0.15, Bld height 0.10, Dist-road 0.10.
5. **Risk** = Hazard(1–5) × Vulnerability.
6. **Write** the risk grid → validate against ADPC's `risk_<hazard>.tif`.

### What Layer 3 unlocks that 1 & 2 can't

We keep the hazard in its **raw units** (flood depth in metres, wind in km/h…), so beyond the 1–5
risk score we can feed real values into a published **damage curve** — for flood, a JRC / World Bank
**depth-damage function** for actual **$ damage**, impossible once depth has been squashed to 1–5.

---

## Validation (all three layers)

ADPC ships precomputed `risk_*.tif` for all 9 hazards, plus per-country land-cover truth
(`Stratified_Area_Estimation_<country>_2024`, Cambodia included). We **diff our result cell-by-cell
against `risk_<hazard>.tif`**, and sanity-check against a known past event (e.g. the 2020 flood):
does our high-risk area land where it actually hit? If "low risk" sits where reality was hit hard,
the weights are wrong — that's a finding, logged not auto-fixed.

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

None of this changes the **design** (the pipeline is identical for every hazard) — only the
**numbers** it produces. So weights are a config choice, defaulting to Table A, tuning deferred.

---

## Glossary

- **ADPC** — Asian Disaster Preparedness Center; provides the precomputed risk and reclass tifs.
- **AOI** — Area of Interest; the place the user is asking about.
- **Hazard** — how dangerous the event is at each place (flood depth, wind speed, ground shaking…).
- **Vulnerability** — how badly a place would suffer / how unable it is to cope.
- **Exposure** — what/who is in harm's way (counted from vectors; not risk on its own).
- **Raster / grid** — data as a grid of cells, each holding a value.
- **Vector** — data as shapes: points, lines, polygons.
- **Reclass** — sorting a continuous value (metres, %) into a small set of classes (here 1–5).
- **Return period** — how rare an event is; a "100-year flood" has a 1-in-100 chance per year.
- **JRC GloFAS** — the EU's global river-flood model; our raw flood-depth source.
- **WorldPop** — the source for population (people per grid cell).
- **Google Open Buildings** — AI-mapped building footprints; our building-density source.
- **OpenStreetMap** — free crowd-sourced map; source for roads and points of interest.
- **ESA WorldCover** — a public 10 m land-cover map.
- **MODIS / VIIRS** — NASA satellite sensors (used for fire sightings, vegetation indices).
