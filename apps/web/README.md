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

The request/response contract for `POST /api/chat` (both text and drawn-AOI modes, every
geo field, and the exposure/risk choice flow) is documented in **`apps/api/API_EXAMPLES.md`** —
the single source of truth. `src/lib/api.ts` and `src/types/chat.ts` mirror it.

> Deploying the SPA on a different origin and calling the API directly? Add that origin to
> the backend's `CORS_ORIGINS` (`apps/api/.env.example`).
