# Hazard exposure agent

Ask a plain-English question about which infrastructure a hazard affects in a
Southeast-Asian city, and get an answer computed from **real data** — never invented
by the model:

```
$ python -m grp_mvp.run "how many km of road are flooded in Battambang?"
241.5 km of road in Battambang fall within the 100-year flood hazard (of 724.4 km).
Breakdown: 1 Very Low 49.5, 2 Low 63.4, 3 Moderate 121.3, 4 High 7.3, 5 Very High 0.0 km
Source: hazard_flood.tif × roads
   roads_in_hazard({'place': 'Battambang', 'hazard': 'flood'}) -> 241.5
   $0.02  grounded=True
```

It resolves the place, pulls the assets from OpenStreetMap, downloads the chosen
hazard raster from Google Drive by id and clips it to the place, then overlays them.
The LLM only routes and writes; **every number comes from a deterministic spatial tool.**

## What you can ask

A question combines four things; the model extracts them from your wording:

| Dimension | Options |
| --- | --- |
| **Hazard** | flood *(default)*, flashflood, drought, fire, landslide, cyclone, storm, tsunami, earthquake |
| **Asset** | roads (length km), hospitals, schools, buildings (counts) |
| **Severity** | a class 1–5, a qualitative term ("severe", "moderate", "extreme"), or `all` (full breakdown). If you don't say, it **asks and waits**. |
| **Place** | any SE-Asia place — its admin boundary if one is under 1,500 km², else a ~12 km box around its center |

So *"how many **buildings** are affected by **drought** in **Chiang Mai**"* or *"km of
road under **severe** **flooding** in **Battambang**"* both work.

## From OpenStreetMap — the assets (exposure)

Fetched live per place (Nominatim + Overpass), clipped to the boundary:

| Asset | OSM query | Used for |
| --- | --- | --- |
| Boundary (AOI) | Nominatim admin polygon | the place outline everything clips to |
| Roads | `highway=*` | length (km) affected, per severity class |
| Hospitals | `amenity=hospital` | count affected |
| Schools | `amenity=school` | count affected |
| Buildings | `building=*` | count affected |

## From the ADPC tifs — the hazards (the effect)

Each hazard is a severity raster (class 1–5), downloaded **by Drive id** from the
ASEAN data sheet's `notes` tab (the full 65-tif map lives in `hazards.py`), cached, and
clipped to the AOI:

| Hazard | tif | Severity legend |
| --- | --- | --- |
| Flood | `hazard_flood.tif` | depth 0–0.5 / 0.5–1 / 1–1.5 / 1.5–2 / >2 m → 1–5 |
| Cyclone | `hazard_cyclone.tif` | wind ≤61 / 62–88 / 89–117 / 118–156 / ≥157 km/h → 1–5 |
| Earthquake | `hazard_earthquake.tif` | PGA <25 / 25–50 / 50–150 / 150–300 / >300 → 1–5 |
| Flashflood, drought, fire, landslide, storm, tsunami | `hazard_*.tif` | generic 1 (Very Low) – 5 (Very High) |

The overlay = sample the hazard raster's class at each asset's location; for roads,
sum segment length per class. `risk_*.tif` (precomputed Hazard×Vulnerability) are also
in the map for later use.

## Install

Python 3.9+.

```
pip install -r requirements.txt
```

The agent talks to any **OpenAI-compatible** endpoint. Configure it in a `.env` file
beside the package (`grp_mvp/.env`, gitignored) — copy the template and fill in the key:

```
cp grp_mvp/.env.example grp_mvp/.env
```

| Provider | `GRP_MODEL` | `GRP_BASE_URL` | Key |
| --- | --- | --- | --- |
| OpenAI | `gpt-4o` | _(unset)_ | `OPENAI_API_KEY` |
| OpenRouter (Claude, Gemini, …) | `anthropic/claude-opus-4-8` | `https://openrouter.ai/api/v1` | `GRP_API_KEY` |
| Ollama (local) | `llama3.1` | `http://localhost:11434/v1` | any |
| vLLM (local) | `meta-llama/Llama-3.1-8B-Instruct` | `http://localhost:8000/v1` | any |

Shell env vars with the same names also work and take precedence over `.env`.

## Usage

```
python -m grp_mvp.run "how many km of road are flooded in Battambang?"
python -m grp_mvp.run "how many buildings are affected by an earthquake in Siem Reap?"
python -m grp_mvp.run "how many hospitals are in severe flooding in Phnom Penh?"
python -m grp_mvp.run --check "Battambang"      # validate the overlay; needs no API key
```

If you don't name a severity it **prompts and waits** — type `1`–`5` or `all`. The
first question about a city fetches + caches its OSM (~30–60 s); the first use of a
hazard downloads its tif once (~50 MB). After that it's instant. Cached data lives in
`cache/<city>/`, query traces in `traces/`.

## How it works

```
question → model (route + extract place, hazard, severity) → deterministic tool → model (write) → answer
                                          │
        place → Nominatim boundary → Overpass assets   hazard → Drive download → clip to AOI
```

1. **The model** picks a tool and pulls the place, hazard, layer, and severity from the question.
2. **ingest** turns the place into real data (Nominatim boundary, Overpass assets) and
   downloads + clips the hazard raster by Drive id.
3. **store** computes the answer — point-in-polygon and raster sampling via shapely and
   rasterio. The only place a number is produced.
4. **The model** writes the sentence, required to state the tool's number and cite its source.
5. **trace** records cost, tokens, and a groundedness check.

If the place can't be resolved, the hazard isn't one of the nine, or a layer isn't
available (population), it says so — it never guesses or silently substitutes.

## Validation

`--check` cross-checks `roads_in_hazard` (raster sampling per segment) against an
independent method (rasterize the flood, clip the road lines as vectors, measure
length). For Battambang the two agree within ~1.5%. This proves the overlay is
*self-consistent*, not that it matches an observed event — the rasters are model output.

## Limitations

- **Coverage is Southeast Asia** — the rasters span ~92–141°E, 11°S–29°N. A place
  outside them (London) still errors at the raster clip rather than answering.
- **AOI resolution** — a clean admin boundary under 1,500 km² is used as-is; otherwise
  (only a giant province like Sihanoukville, or a point-only village) it falls back to a
  ~12 km box around the place center. The box is an approximation — it can pull in
  neighbouring areas and straddle the coast, so its counts are a radius, not a jurisdiction.
- **Severity is a 1–5 class, not a physical value.** Bands are documented for flood,
  cyclone, earthquake; the other six use a generic 1–5.
- **OSM completeness varies** — buildings especially are sparse in SE Asia (~900 in
  Battambang vs Google Open Buildings ≈10×); counts undercount.
- **~30–100 m hazard resolution** sampled per point/segment — accurate in aggregate,
  coarse at edges.
- **Roads include footpaths/tracks** (all `highway=*`), not just drivable roads.
- **Population** exposure (WorldPop) and **routing** (which assets lose road access) are
  not implemented; `risk_*.tif` not yet used.

## Layout

```
hazards.py   the 65-tif Drive-id map + the 9-hazard registry (tif + severity legend)
ingest.py    place -> cached boundary + OSM assets; download + clip a hazard raster
store.py     the deterministic spatial operations (the only place a number is made)
tools.py     tool schemas + dispatch
llm.py       the model router and writer (OpenAI-compatible)
agent.py     conversation loop: route -> (ask user?) -> tool -> write -> trace
trace.py     cost, groundedness, JSON trace
registry.py  which assets are available vs knowingly absent
config.py    model endpoint + pricing (.env)
run.py       CLI
```
