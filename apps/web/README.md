# Web frontend (placeholder)

No frontend stack chosen yet. This directory reserves the slot in the monorepo;
drop in whatever you like (React + Vite, Next.js, SvelteKit, plain HTML — the
backend doesn't care). It only needs to speak the HTTP contract below.

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
