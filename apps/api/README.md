# API — FastAPI + LangGraph geospatial agent

A FastAPI service that answers natural-language **disaster-risk** questions for
Southeast-Asian places, computed from **real data** — never invented by the model. An
LLM routes the question to a deterministic spatial tool; the tool overlays OpenStreetMap
assets (roads, hospitals, schools, buildings) against a hazard severity raster and
returns a grounded, per-severity answer.

For any hazard the agent first asks how to answer it — **exposure** (raw hazard
footprint), **precomputed risk** (the agency's `risk_<hazard>` layer), or **recomputed
risk** (Hazard × Vulnerability rebuilt here, tunable) — and computes only once you choose.

## What you can ask

A question combines four things; the model extracts them:

| Dimension | Options |
| --- | --- |
| **Hazard** | flood · flashflood · drought · fire · landslide · cyclone · storm · tsunami · earthquake |
| **Asset** | roads (length km) · hospitals · schools · buildings (counts) |
| **Severity** | a class 1–5, or omit it for the full per-class breakdown |
| **Place** | any SE-Asia place — its admin boundary if under ~1,500 km², else a 12 km box around its centre |

Examples: *"how many km of road are flooded in Battambang?"* · *"how many buildings are
at risk from an earthquake in Siem Reap?"* · *"km of road at risk from a cyclone in Battambang"*.

## How it works

```
POST /api/chat → route → resolve → fetch → operate → finalize → JSON
                  │        │         │       │          │
       picks op +       asks       OSM +    overlay    phrases the answer
       place + hazard   exposure/  raster   (shapely/  (quotes the number,
       (extraction)     L1 / L2    clip     rasterio)  cites the source)
```

- **route** — the LLM picks the operation, place, and hazard. Extraction only; it computes nothing.
- **resolve** — offers exposure / precomputed-risk (L1) / recomputed-risk (L2), only the paths whose
  data exists, and pauses for the user's pick (resumed on the same `thread_id`).
- **fetch** — clips the OSM assets + the chosen hazard/risk raster to the AOI (L2 grids are computed here).
- **operate** — `graph/geo/store.py`: the only place a number is born (the spatial overlay).
- **finalize** — phrases the answer, quoting the number and citing the source.

On a decline (no place / unavailable layer) or a fetch failure, the answer is returned
without a second LLM call. Multi-turn memory is keyed by `thread_id` (in-process checkpointer).

The four-layer risk model behind `resolve` is described in the repo-root `SOURCE_DATA_APPROACH.md`.

## Layout

```
src/app/
├── main.py                 # FastAPI app factory + CORS
├── config.py               # settings (env-driven): providers, paths, pricing
├── schemas.py              # request/response models
├── api/routes/             # HTTP endpoints: health, chat, raster (clipped GeoTIFF), tiffs (BYOD upload)
├── llm/client.py           # per-request OpenAI-SDK client: claude | openai | gemini
└── graph/
    ├── graph.py            # the route → resolve → fetch → operate → finalize graph
    ├── prompts.py          # loads the grounding prompt (conf/prompts.yml)
    └── geo/
        ├── ingest.py       # place → OSM boundary/roads/POIs + hazard raster clip
        ├── resolver.py     # picks exposure vs L1 vs L2 from what's available
        ├── combine.py      # Layer-2 risk = Hazard × weighted vulnerability
        ├── align.py        # resample any 1–5 layer onto the hazard grid
        ├── verify.py / schema.py / rasterstats.py  # per-raster contracts + windowed checks
        ├── byod_verify.py / byod_registry.py  # verify + per-thread registry for user-uploaded layers
        ├── store.py        # the deterministic spatial ops (the only numbers)
        ├── operations.py   # tool schemas + dispatch
        ├── viz.py          # builds the map-visualization payload for the frontend
        ├── tiffs.py / drive_tifs.py  # hazard catalog (conf/tiffs.yml) + Drive ids
        ├── registry.py     # countable vs knowingly-unavailable layers
        └── trace.py        # cost + groundedness trace
```

Config lives at the repo root: the hazard catalog (`conf/tiffs.yml`), the grounding prompt
(`conf/prompts.yml`), the per-raster schema (`conf/raster_schema.yml`), and the Layer-2
weights + crossing rule (`conf/risk_l2.yml`).

## Bring your own data (BYOD)

`POST /api/tiffs` (multipart) lets a user supply their own hazard raster. The upload is
quarantined, run through a **verification gate** (`byod_verify.verify_upload` — single-band,
CRS present, finite bounds, values within the declared 0–5/1–5 severity scale; reuses the
windowed-read machinery, never full-loads), and **only on a PASS** registered for that
`thread_id` (`byod_registry`, in-process, per-thread). A registered layer is written into the
tiff cache so `ingest.source_raster` finds it locally, and `route()` merges it into the menu
for that thread — so the user can then ask about it in chat exactly like a built-in layer.
The curated `conf/tiffs.yml` is never mutated. See `API_EXAMPLES.md` for the request/response
contract.

## Run

From the repo root:

```bash
uv sync
cp apps/api/.env.example apps/api/.env   # set DEFAULT_PROVIDER + the matching API key
uv run uvicorn app.main:app --reload --app-dir apps/api/src --port 8001
```

API: http://localhost:8001 · interactive docs: http://localhost:8001/docs

> The first query for a new place fetches its OSM (~30–60 s) and downloads the hazard
> raster once (~50 MB), then caches both — subsequent queries are fast.

## Query it

```bash
curl -s localhost:8001/api/chat -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"how many km of road are flooded in Battambang?"}]}' \
  | jq -r '.message.content'
```

Add `"verbose": true` to also get a `trace` — a step-by-step narration of how the answer
was produced (route → resolve → boundary → exposure → overlay):

```bash
curl -s localhost:8001/api/chat -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"km of road flooded in Battambang?"}],"verbose":true}' \
  | jq '.trace'
```

See **[DEMO.md](DEMO.md)** for an exhaustive set of example queries, and
**[API_EXAMPLES.md](API_EXAMPLES.md)** for the full request/response contract the frontend uses.

## Test

Run from the repo root (the workspace owns pytest + its config):

```bash
uv run pytest                                  # fast suite (stubs, no network)
GRP_RUN_SLOW=1 uv run pytest                   # + slow real-data / Drive tests
```
