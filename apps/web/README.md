# Web frontend

React 19 + TypeScript on Vite, styled with Tailwind v4 + DaisyUI. State via
TanStack Query (server) and Zustand (client); OpenLayers for the map and
lucide-react for icons.

## Develop

```bash
cd apps/web
npm install
npm run dev      # http://localhost:5173
```

Other scripts: `npm run build`, `npm run preview`, `npm run lint`, `npm run format`.

## Talking to the API

The backend (`apps/api`) runs separately on `http://localhost:8001`, all routes under
`/api`. In dev, Vite proxies `/api` to it (see `vite.config.ts`), so frontend code fetches
relative paths (`/api/chat`) and never needs CORS or a hardcoded host. Start the API
separately — see `apps/api/README.md`.

The API contract for both endpoints the app calls — `POST /api/chat` (text and drawn-AOI
modes, every geo field, the exposure/risk choice flow) and `POST /api/tiffs` (the bring-your-own-data
upload) — is documented in **`apps/api/API_EXAMPLES.md`**, the single source of truth.
`src/lib/api.ts` (`postChat`, `uploadTiff`) and `src/types/chat.ts` mirror it.

## Trace & observability

Every chat response carries a `trace_envelope` — a structured, per-step record of how the
answer was produced. It's rendered per turn by `src/components/Trace`, on top of the pure
selector layer in `src/lib/trace`.

**`src/lib/trace/README.md` is the reference**: what each field means to an end user, the
audience split, how missing data must be rendered, and how to rebuild the visualization in
a different shape without re-reading the backend.

> Deploying the SPA on a different origin and calling the API directly? Add that origin to
> the backend's `CORS_ORIGINS` (`apps/api/.env.example`).
