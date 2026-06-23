# Web frontend

React 19 + TypeScript on Vite, styled with Tailwind v4 + DaisyUI. State via
TanStack Query (server) and Zustand (client); Radix UI primitives and
lucide-react for UI.

## Develop

```bash
cd apps/web
npm install
npm run dev      # http://localhost:5173
```

Other scripts: `npm run build`, `npm run preview`, `npm run lint`,
`npm run format`.

### Talking to the API

The backend is a separate cross-origin service (`apps/api` on
`http://localhost:8000`, all routes under `/api`). In dev, Vite proxies `/api`
to it (see `vite.config.ts`), so frontend code fetches relative paths
(`/api/chat`) and never needs CORS or a hardcoded host. Start the API
separately (see the root `README.md`).

> If you instead deploy the SPA on a different origin and call the API
> directly, add that origin to the backend's `CORS_ORIGINS`
> (`apps/api/.env.example`).

## Backend contract

The API runs at `http://localhost:8000` by default (configurable). All endpoints
are under `/api`. Responses are JSON.

### `POST /api/chat`

Request body:

```json
{
  "messages": [
    { "role": "user", "content": "Hello" }
  ],
  "provider": "claude",
  "model": null,
  "thread_id": null
}
```

- `messages` (required): standard LLM chat history; roles are `user`,
  `assistant`, or `system`.
- `provider` (optional): `claude` | `openai` | `gemini`. Falls back to the
  server default.
- `model` (optional): override the provider's default model.
- `thread_id` (optional): pass back the `thread_id` from a prior response to
  continue that conversation (the backend keeps history server-side).

Response body:

```json
{
  "id": "msg_...",
  "thread_id": "uuid",
  "message": { "role": "assistant", "content": "Hi! How can I help?" },
  "provider": "claude",
  "model": "claude-opus-4-8",
  "usage": { "input_tokens": 12, "output_tokens": 8, "total_tokens": 20 },
  "created_at": "2026-06-22T12:00:00Z"
}
```

Render `message.content`; persist `thread_id` to keep the conversation going.

### `GET /api/health`

Liveness check: `{ "status": "ok", ... }`.

### Errors

Non-2xx responses use `{ "detail": "..." }`. `400` = bad request (e.g.
unsupported provider); `502` = upstream LLM/runtime failure.

## CORS

The backend allows the origins listed in `CORS_ORIGINS` (see
`apps/api/.env.example`). Add your dev server's origin there.

Interactive API docs: `http://localhost:8000/docs`.
