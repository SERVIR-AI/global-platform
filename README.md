# Retreat Platform

Monorepo: a **FastAPI + LangGraph** backend and a frontend that consumes it.
Managed with [uv](https://docs.astral.sh/uv/) (a uv workspace).

```
.
├── apps/
│   ├── api/   # FastAPI service hosting the LangGraph app
│   └── web/   # frontend slot (stack TBD — see apps/web/README.md)
└── pyproject.toml   # uv workspace root
```

## Quickstart

```bash
uv sync                                   # install the workspace
cp apps/api/.env.example apps/api/.env    # add your provider API keys
uv run uvicorn app.main:app --reload --app-dir apps/api/src
```

- API: http://localhost:8000
- Interactive docs: http://localhost:8000/docs

Send a query:

```bash
curl -s http://localhost:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello!"}]}' | jq
```

## What's here

- **LangGraph app** — `apps/api/src/app/graph/graph.py`. A minimal single-node
  graph with conversation memory; extend it with tools, edges, retrieval, etc.
- **Multi-provider** — `apps/api/src/app/llm/factory.py` builds a chat model for
  `claude`, `openai`, or `gemini` behind one interface (LangChain). Default
  provider and per-provider models are set in `config.py` / `.env`.
- **Frontend-ready JSON** — `POST /api/chat` returns a structured assistant
  message plus metadata (provider, model, usage, thread id). Contract is
  documented in `apps/web/README.md`.

See `apps/api/README.md` for backend details.
