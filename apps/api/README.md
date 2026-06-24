# API — FastAPI + LangGraph geospatial agent

A FastAPI service that answers natural-language **disaster-exposure** questions for
Southeast-Asian places, computed from **real data** — never invented by the model. An
LLM routes the question to a deterministic spatial tool; the tool overlays OpenStreetMap
exposure (roads, hospitals, schools, buildings) against an ADPC hazard severity raster
and returns a grounded, per-severity answer.

## What you can ask

A question combines four things; the model extracts them:

| Dimension | Options |
| --- | --- |
| **Hazard** | flood · flashflood · drought · fire · landslide · cyclone · storm · tsunami · earthquake |
| **Asset** | roads (length km) · hospitals · schools · buildings (counts) |
| **Severity** | a class 1–5, or omit it for the full per-class breakdown |
| **Place** | any SE-Asia place — its admin boundary if under ~1,500 km², else a 12 km box around its centre |

Examples: *"how many km of road are flooded in Battambang?"* · *"how many buildings are
exposed to an earthquake in Siem Reap?"* · *"km of road at risk from a cyclone in Battambang"*.

## How it works

```
POST /api/chat → route → fetch → operate → finalize → JSON
                  │       │        │          │
       LLM picks op +    OSM +    shapely/   LLM phrases the answer
       place + hazard    raster   rasterio   (quotes the number, cites source)
       (extraction only) clip     overlay
```

The LLM never computes a number — `graph/geo/store.py` is the only place one is born.
On a decline (no place / unavailable layer) or a fetch failure, the answer is returned
without a second LLM call. Multi-turn memory is keyed by `thread_id` (checkpointer).

## Layout

```
src/app/
├── main.py                 # FastAPI app factory + CORS
├── config.py               # settings (env-driven): providers, paths, pricing
├── schemas.py              # request/response models
├── api/routes/             # HTTP endpoints (health, chat)
├── llm/client.py           # per-request OpenAI-SDK client: claude | openai | gemini
└── graph/
    ├── graph.py            # the route → fetch → operate → finalize graph
    ├── prompts.py          # loads the grounding prompt (conf/prompts.yml)
    └── geo/
        ├── ingest.py       # place → OSM boundary/roads/POIs + hazard raster clip
        ├── store.py        # the deterministic spatial ops (the only numbers)
        ├── operations.py   # tool schemas + dispatch
        ├── tiffs.py        # the hazard catalog reader (conf/tiffs.yml)
        ├── registry.py     # countable vs knowingly-unavailable layers
        └── trace.py        # cost + groundedness trace
```

The 9-hazard catalog and the grounding prompt live in `conf/tiffs.yml` and
`conf/prompts.yml` at the repo root.

## Run

From the repo root:

```bash
uv sync
cp apps/api/.env.example apps/api/.env   # set DEFAULT_PROVIDER + the matching API key
uv run uvicorn app.main:app --reload --app-dir apps/api/src
```

API: http://localhost:8000 · interactive docs: http://localhost:8000/docs

> The first query for a new place fetches its OSM (~30–60 s) and downloads the hazard
> raster once (~50 MB), then caches both — subsequent queries are instant.

## Query it

```bash
curl -s localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"how many km of road are flooded in Battambang?"}]}' \
  | jq -r '.message.content'
```

Add `"verbose": true` to the request body to also get a `trace` — a step-by-step
narration of how the answer was produced (route → boundary → exposure → overlay), the
same thing the CLI printed with `-v`:

```bash
curl -s localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"km of road flooded in Battambang?"}],"verbose":true}' \
  | jq '.trace'
```

See **[DEMO.md](DEMO.md)** for an exhaustive set of example queries — every hazard, every
asset, severity, the boundary fallbacks, honest refusals, multi-turn, and verbose.
