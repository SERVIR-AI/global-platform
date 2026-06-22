# API — FastAPI + LangGraph

FastAPI service that runs a LangGraph application and exposes it over HTTP.

## Layout

```
src/app/
├── main.py            # FastAPI app factory + CORS
├── config.py          # settings (env-driven)
├── schemas.py         # request/response models
├── api/routes/        # HTTP endpoints (health, chat)
├── llm/factory.py     # provider factory: claude | openai | gemini
└── graph/graph.py     # the LangGraph graph (single agent node, extend here)
```

## Where to build

- **The graph** (`graph/graph.py`) is the heart of the LangGraph app. Right now
  it's one node that calls the chat model. Add tool nodes, conditional edges,
  retrieval, subgraphs, human-in-the-loop, etc.
- **Providers** (`llm/factory.py`) — add a provider by registering a builder and
  extending `Provider` in `config.py`.
- **Memory** — the graph uses an in-process `MemorySaver` checkpointer keyed by
  `thread_id`. Swap it for a persistent checkpointer (Postgres/SQLite) for
  durability across restarts.

## Run

From the repo root:

```bash
uv sync
cp apps/api/.env.example apps/api/.env   # then fill in API keys
uv run uvicorn app.main:app --reload --app-dir apps/api/src
```

API: http://localhost:8000 · docs: http://localhost:8000/docs

## Smoke test

```bash
curl -s http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Say hi in one word."}]}' | jq
```
