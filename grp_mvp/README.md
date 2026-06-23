# Flood exposure agent

Ask a plain-English flood question about a Southeast-Asian city and get an answer
computed from **real data** — never invented by the model:

```
$ python -m grp_mvp.run "how many km of road are flooded in Battambang?"
In Battambang, 241.4 km of road are affected by the 100-year flood hazard (out of 724.4 km).
Source: .../battambang/flood.tif × .../battambang/roads.geojson
   roads_in_flood({'place': 'Battambang'}) -> 241.4
   $0.0157  grounded=True
```

It resolves the place, pulls roads/hospitals/schools from OpenStreetMap, overlays
them on the ADPC flood-hazard raster, and lets the model phrase the result — but the
LLM only routes and writes; every number comes from a deterministic spatial tool.

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

`.env`:

```
GRP_MODEL=claude-opus-4-8
GRP_BASE_URL=https://api.anthropic.com/v1/
GRP_API_KEY=sk-...
```

Shell env vars with the same names also work and take precedence over `.env`.

## Usage

```
python -m grp_mvp.run "how many schools are exposed to the flood in Siem Reap?"
python -m grp_mvp.run "how many hospitals are in Battambang?"
python -m grp_mvp.run --check "Battambang"      # validate the overlay; needs no API key
```

The first question about a city fetches and caches its data (~30–60 s); after that
it is instant. Cached data lives in `cache/<city>/`, query traces in `traces/`.

## How it works

```
question → Claude (route + extract place) → deterministic tool → Claude (write) → answer
                                               │
              place → Nominatim boundary → Overpass roads/POIs → flood raster clip
```

1. **The model** picks a tool and the place name from the question.
2. **ingest** turns the place into real data: Nominatim for the boundary, Overpass
   for roads/hospitals/schools, and a window of the ADPC flood raster clipped to it.
3. **store** computes the answer — point-in-polygon and raster sampling via shapely
   and rasterio. This is the only place a number is produced.
4. **The model** writes the sentence, required to state the tool's number and cite its source.
5. **trace** records cost, tokens, and a groundedness check, and writes a JSON trace.

If the place can't be resolved, or a layer isn't available (e.g. buildings), it says
so — it never guesses or silently substitutes another location.

## Data sources

| Layer | Source |
| --- | --- |
| Boundary, roads, hospitals, schools | OpenStreetMap (Nominatim + Overpass) |
| Flood hazard (100-year, severity 1–5) | ADPC `hazard_flood.tif`, derived from JRC GLOFAS v2.1 |

## Validation

`--check` cross-checks `roads_in_flood` (raster sampling per road segment) against an
independent method (rasterize the flood, clip the road lines as vectors, measure
length). For Battambang the two agree within 1.4%.

This proves the overlay is *self-consistent*, not that it matches an observed flood —
the flood raster is itself a model output.

## Limitations

- **Coverage is Southeast Asia** — the flood raster spans ~92–141°E, 11°S–29°N. Cities
  outside it return an error rather than a wrong answer.
- **Flood severity is a 1–5 class, not depth in metres.**
- **~100 m hazard resolution** sampled per road segment — accurate in aggregate, coarse
  at individual flood edges.
- **OSM completeness varies** — counts are only as good as what the map contains.
- **Roads include footpaths/tracks** (all OSM `highway=*`), not just drivable roads.
- Building counts, population exposure, and routing (which assets lose road access) are
  not implemented.

## Layout

```
ingest.py    place -> cached boundary + roads + POIs + flood clip
store.py     the deterministic spatial operations
tools.py     tool schemas + dispatch
llm.py       Claude router and writer
agent.py     route -> tool -> write -> trace
trace.py     cost, groundedness, JSON trace
registry.py  which layers are available vs knowingly absent
config.py    model id + pricing
run.py       CLI
```
