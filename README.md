# SERVIR Global Platform

The SERVIR Global Platform is a governed **MCP server** for decision support. Hubs
connect the AI assistant they already use (Claude Desktop, Claude Code or any MCP client)
and ask questions in three domains: **food security**, **disaster risk** and **land
cover**. The platform computes the evidence itself, the assistant writes the answer, and a
deterministic gate checks the answer against that evidence before it gets a **receipt**
anyone can open. No LLM runs on the evidence path. The model is told to do no arithmetic
of its own on platform data, and the gate accepts only one kind it can check: a share of a
cited total that states both figures.

The same FastAPI app also serves a REST API (the receipt resolver, contributions) and a
React web app with embeddable live views, all from one origin.

## How a question becomes a governed answer

```
question → start_here → assemble_pack → the client's LLM drafts, citing [n] → publish_answer → receipt
```

1. **`start_here(question)`** routes the question to a domain pack and fills in its target.
   It is deterministic: word scoring against each pack's routing words, no LLM.
2. **`assemble_pack(target)`** computes an evidence pack: figures, numbered citations and
   declared gaps. It reads maps, vector layers, feeds, the document library and contributed
   methods, and stores the pack under a `pack_id`.
3. The client's **own model drafts** a brief in the pack's required sections, citing the
   pack as `[1]`, `[2]` …
4. **`publish_answer(pack_id, draft)`** runs the groundedness gate. It blocks a draft that
   misses a required section, writes its own Sources section, cites nothing or cites an
   `[n]` the pack does not hold, has a paragraph with no citation, states a figure of 10 or
   more (years aside) that does not trace to the evidence, writes a figure in words the
   gate cannot read, or links somewhere the pack does not serve. Rounding, unit conversion
   and a share of a cited total count as tracing. A pass mints a receipt; a fail returns
   the reasons and no receipt.
5. The **receipt** resolves publicly at `/api/resolve/receipt/<id>`: the question, the
   evidence, and the SHA-256 of the exact text that passed. It attests that the text traces
   to the evidence, not that the sources are right.

`compose_run` runs the same loop on the server with a configured LLM, for callers that
have no model of their own.

## Domain packs

| Pack | Target | Evidence |
| --- | --- | --- |
| Food security | country + crop | ENSO and IOD indices, GEOGLAM crop conditions, crop calendars, a document library |
| Risk | place + hazard | hazard maps, OpenStreetMap assets, population and vulnerability layers, risk weights |
| Land cover | place + year (and a later year for change) | yearly land-cover stacks: class areas, change and transitions, per-hectare factor tables |

A pack is a row in `PACKS` (`apps/api/src/app/mcp/packs.py`): its target, a `gather`
function and the sections a brief must contain. Everything after `gather` (contributed
layers, vectors and methods, the gate, receipts) is shared by every pack. A new pack can be
dropped in as `apps/api/src/app/packs_ext/<id>.py` exporting a `SPEC`.

## MCP tools

| Group | Tools |
| --- | --- |
| Answer | `start_here`, `assemble_pack`, `publish_answer`, `verify_groundedness`, `record_receipt`, `compose_run` |
| Look up | `platform_capabilities`, `resolve_place_time`, `corpus_search`, `corpus_document`, `feeds_query`, `context_get`, `risk_weights` |
| Build an interface | `ui_design`, `ui_catalog`, `ui_component`, `ui_embed` |
| Contribute | `contribute_submit`, `contribute_status`, `contribute_review` |

The server also exposes four prompts (`build_a_tool`, `run_analysis`, `contribute_logic`,
`explain_platform`) and `servirplatform://` resources: pack manifests, the full rulebook
(`servirplatform://how-to-use`), the bundled skills, and any reply section too large to
return inline. Tool descriptions are generated from the live registries and fitted to a
character budget (`apps/api/src/app/mcp/budget.py`), so they always name what is actually
served.

## Contributing data and logic

Everything enters through `contribute_submit`. Pass `kind` alone to get that kind's form.
The kinds are `document`, `table`, `feed`, `raster`, `vector`, `weights`, `amend` and
`method`. Submit the link as it is (a file, a Google Drive folder, a zip, an ArcGIS
service, an API): the platform fetches it through one guarded fetcher, prepares it (clip,
mosaic, recode, rasterise), checks it and lands it. For a raster, vector or method,
`preview: true` shows what would land and stores nothing; the other kinds have no preview
yet.

- Each kind's form lists its required fields. Provenance fields (title, description,
  source, licence, vintage, countries) are the contributor's own words: when a required one
  is missing, the submission is declined with a note to ask the contributor, never to
  compose it.
- `GRP_AUTO_APPROVE` is **on by default**, so a clean submission lands without human
  review. It still waits for a reviewer when its licence restricts redistribution, when a
  document holds personal e-mail addresses, or, with `GRP_METHOD_REVIEW=1`, when it is a
  method. Set `GRP_AUTO_APPROVE=0` to hold every submission for the reviewers listed in
  `GRP_REVIEWERS`, who decide in `contribute_review`.
- A landing writes data under `cache/` and entries in the gate-owned config:
  `conf/*.contrib.yml`, feed and table entries in `conf/feeds/`, and one file per method in
  `conf/methods/`. Running workers pick it up without a restart.
- A **method** is a calculation in a small expression language (`count`, `sum`, `mean`,
  `where` …). The server evaluates it, never executes it, for every pack it is bound to,
  and cites the result like any other source.

The web app also accepts a session-scoped hazard GeoTIFF (`POST /api/tiffs`). It is checked
(single band, georeferenced, values on the declared 0–5/1–5 severity scale) before the chat
agent can use it.

## Repository layout

```
apps/
├── api/                      FastAPI app (the uv workspace member)
│   ├── src/app/
│   │   ├── main.py           app factory: /mcp, /api and the web build on one origin
│   │   ├── mcp/              MCP server and tools; assemble, verify, publish, record; budgets; SQLite store
│   │   ├── contrib/          contribution gate: kinds, fetch policy, preparation ops, methods, jobs
│   │   ├── food_security/    food-security pack: gatherer, crop calendar, Crop Monitor, REST routes;
│   │   │                     its synthesis.py also holds the gate (check_grounded) every pack uses
│   │   ├── land_cover/       land-cover pack: gatherer in synthesis.py
│   │   ├── risk/             risk pack: gatherer in synthesis.py
│   │   ├── packs_ext/        drop-in packs
│   │   ├── graph/            geo engine (graph/geo) and the web app's LangGraph chat agent
│   │   ├── rag/              document libraries and search
│   │   ├── llm/              OpenAI-compatible provider client
│   │   └── api/routes/       REST: receipts (/api/resolve), contribute, chat, raster, tiffs, rag, health
│   ├── tests/                the test suite (hermetic by default)
│   └── scripts/              operator scripts: WorkOS invitations, MCP checks
└── web/                      React 19 + Vite + OpenLayers: web app, ?embed= views, runbook
conf/                         catalogs and contracts as data: tiffs.yml, raster_schema.yml, feeds/, ui_theme.json …
                              (*.contrib.yml are written by the contribution gate; never edit them by hand)
deploy/                       container deployment (Cloud Run): deploy.sh, entrypoint.sh, litestream.yml
cache/                        runtime state, not in git: rasters, vectors, corpora, cache/mcp/grp.db (SQLite)
```

## Run locally

```bash
git clone git@github.com:SERVIR-AI/global-platform.git && cd global-platform
uv sync                                    # backend (uv installs Python too)
cp apps/api/.env.example apps/api/.env     # optional: LLM and embedding keys
uv run uvicorn app.main:app --reload --app-dir apps/api/src --port 8001
```

The MCP endpoint is `http://127.0.0.1:8001/mcp`. To connect Claude Code to it:

```bash
claude mcp add --transport http servirplatform-local http://127.0.0.1:8001/mcp
```

The web app runs in a second terminal. Vite proxies `/api` to the backend on `:8001`, so
it works with no CORS setup:

```bash
cd apps/web && npm install && npm run dev   # http://localhost:5173
```

To run it the way production does, from one origin, build the web app and point the
backend at it. The runbook is then served at `/runbook/`:

```bash
cd apps/web && npm run build && cd ../..
GRP_WEB_DIST=$PWD/apps/web/dist GRP_PUBLIC_BASE=http://127.0.0.1:8001 \
  uv run uvicorn app.main:app --app-dir apps/api/src --port 8001
```

`GRP_PUBLIC_BASE` makes receipt and embed links point at the origin you serve from.

Local runs are **open by default**: no login is asked for, and the MCP transport accepts
anonymous calls. Enabling AuthKit is per deployment; see
[Access & authentication](#access--authentication). **No LLM keys are required**: keyless,
you still get the platform map, the live feeds and the complete risk-pack loop, from
evidence through the gate to a minted receipt. Add provider keys (see
`apps/api/.env.example`) for the server-side drafting paths. Data files (rasters, vectors,
corpora) live in `cache/`, outside git; the risk pack's source rasters download from Google
Drive on first use. The food-security document library has to be built once, with an
embeddings key set:

```bash
PYTHONPATH=apps/api/src uv run python -m app.food_security.seed_corpus
```

## Test

```bash
uv run pytest                                              # backend: stubs, no network (the suite turns OAuth off itself)
GRP_RUN_SLOW=1 uv run pytest                               # + slow real-data and Drive tests
cd apps/web && npm test && npm run lint && npm run build   # frontend: unit tests (vitest), lint, type-check and build
```

There is no CI pipeline yet, so run the suite before every release.

## Deploy

- **Container (Cloud Run):** `Dockerfile` and `deploy/deploy.sh`. One uvicorn process,
  capped at one instance on purpose, with Litestream replicating the SQLite store
  (`cache/mcp/grp.db`) to GCS. `deploy/entrypoint.sh` refuses to boot unless OAuth is
  configured (or `GRP_ALLOW_ANONYMOUS=1`) and receipts are replicated (or
  `GRP_ALLOW_EPHEMERAL_RECEIPTS=1`). Rasters are not baked into the image.
- **VM:** run the same app under uvicorn behind a TLS-terminating proxy; no deploy script
  ships for this yet. Receipts, packs and contribution records live in `cache/mcp/grp.db`,
  and approved contributions also write `conf/*.contrib.yml` and data under `cache/`, so
  back up all three.

## Access & authentication

Authentication is AuthKit (OAuth 2.1), switched on **per deployment**. With it
off (the default), the whole platform is open and anonymous — how local dev and
the test suite run. With it on, visitors sign in to the web app, and MCP
clients authenticate against the same AuthKit accounts.

### For end users

Access is granted by whoever runs the deployment: you are invited by email, and
only invited emails can create an account (Sign-up is off).

**The web platform.** Open the URL you were given.

1. The page shows a public **Sign in to continue** screen — click **Sign in**.
2. AuthKit opens in the same tab. The first time, set up your account with the
   invited email (via the invitation link); later, sign in with your password,
   Google, or your organization's SSO.
3. You land back on the platform, signed in. Your email shows in the bar
   top-right; that menu has **Sign out** and **Switch account**.

Receipt links, health checks, map tiles and public embeds need no sign-in. If
you cannot get in (expired invitation, forgotten password), ask the operator to
resend the invitation or reset the password.

**Claude Code (terminal).** One command, nothing to paste:

```bash
claude mcp add --transport http servirplatform https://<deployment-url>/mcp
```

The first tool call opens your browser for the same AuthKit sign-in; afterwards
the client authenticates itself. Useful commands:

```bash
claude mcp list                                          # servers + auth state
claude mcp logout servirplatform && claude mcp login servirplatform   # re-test login
claude mcp remove servirplatform                         # disconnect
```

**Claude Desktop.** Add the MCP server by URL — Settings → Developer → MCP
servers → **Add MCP server**, entering `https://<deployment-url>/mcp` — or add
it to `claude_desktop_config.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "servirplatform": {
      "type": "http", "url": "https://<deployment-url>/mcp"
    }
  }
}
```

The first tool call opens your browser for the same AuthKit sign-in. To sign in
as someone else, sign out of Claude Desktop (or remove and re-add the server)
and go through the login again.

**Signing out vs switching accounts.** In the web app, **Sign out** ends the
app's session and lands on the public page; AuthKit's own session survives, so a
fresh **Sign in** resumes in one click. **Switch account** forces the AuthKit
login screen instead. Sessions are per browser profile — two people on one
machine should use separate profiles.

### For operators: enabling and configuring a deployment

#### Environment variables

Set them in `apps/api/.env` for local runs (the app reads that file) or in the
deployed service's environment. All are listed in `apps/api/.env.example`.

| Variable | Meaning |
| --- | --- |
| `GRP_OAUTH_ENABLED=1` | Master switch. With it on, the MCP transport requires OAuth; with a client id also set, web login is mounted and the app is gated. Unset = open. |
| `GRP_PUBLIC_URL` | The origin clients reach. Load-bearing: it becomes the `/auth/callback` origin **and** the MCP token audience, so it must equal the origin registered in WorkOS or logins fail with a redirect/audience mismatch. |
| `GRP_AUTHKIT_DOMAIN` | The AuthKit tenant domain (e.g. `https://<tenant>.authkit.app`). |
| `GRP_AUTHKIT_CLIENT_ID` | The Connect OAuth application's client id — the web login client (PKCE public client, no secret). Empty = MCP-only: no login routes, app open. |
| `GRP_ALLOW_ANONYMOUS=1` | Deploy-only escape hatch: lets the container boot with auth off. Never set in normal operation. |
| `WORKOS_API_KEY` | Admin-only, **local** secret for the script used to add, revoke, or refresh email invitations; never set on the deployed service. |

#### The two WorkOS application objects

AuthKit settings are per environment (staging and production are separate), so
repeat this in each. Two different "application" objects exist in the dashboard
and are NOT linked — each governs a different flow:

| Object | Where | What it is for | Settings that live here |
| --- | --- | --- | --- |
| **Connect → OAuth application** | Connect → Applications | The OAuth client *your raw login flow* uses | **Client ID** → `GRP_AUTHKIT_CLIENT_ID`; **Redirect URI** `https://<public_url>/auth/callback` |
| **AuthKit → default application** | AuthKit → Applications | The environment's app object; hosted-UI flows that carry no OAuth redirect (invitation acceptance, password resets) | The default redirect, where the browser lands after those flows — without it, accepted invites end on AuthKit's broken `/default-redirect` page |

Environment checklist (per staging/prod):

1. **Authentication → Sign up**: off. Only invited emails can create accounts (the whitelist).
2. **Connect → OAuth application**: copy the client id into `GRP_AUTHKIT_CLIENT_ID`; set the redirect URI `https://<public_url>/auth/callback`.
3. **AuthKit → default application**: add `https://<public_url>/` and mark it **default** (hosted invite-accept and password-reset flows land there).
4. **Connect → Configuration**: the MCP **resource indicator** `https://<public_url>/mcp` and the DCR toggle, for the MCP transport.

The golden rule: `GRP_PUBLIC_URL` must equal the origin registered on both
objects and in the resource indicator.

#### Changing the deployment URL

Moving a deployment to a new origin touches the same value in five places.
Change `GRP_PUBLIC_URL` and re-deploy, then update the three WorkOS
registrations (same environment) to the new origin:

- Connect → Applications → the OAuth application → **Redirect URI**: `https://<new-url>/auth/callback` (its Client ID does not change).
- AuthKit → Applications → the default application → add `https://<new-url>/` and mark it **default**.
- Connect → Configuration → the MCP **resource indicator**: `https://<new-url>/mcp`.

A mismatch shows up as a login that bounces, or an MCP client refused with an
audience error.

#### Enabling and disabling auth

- **Enable:** set `GRP_OAUTH_ENABLED=1` and, for web login, `GRP_AUTHKIT_CLIENT_ID`
  (plus the domain and public URL). The deployment scripts
  (`deploy/entrypoint.sh`, `deploy/deploy.sh`) refuse to start or deploy
  otherwise.
- **Disable (roll back to open):** unset `GRP_OAUTH_ENABLED`; to drop web login
  alone, unset `GRP_AUTHKIT_CLIENT_ID`. The auth routes are not mounted,
  `/auth/me` answers 404, and the app serves open — the local and test default.
  The container's fail-closed check is bypassed only by the explicit
  `GRP_ALLOW_ANONYMOUS=1`.

#### Managing users (add / remove)

With Sign-up off, an **invitation is the whitelist**: only an invited email can
create an account. Accepting an invitation proves the inbox, so a first-time
sign-up with the invited email is auto-verified (no separate email OTP) while
the invitation is fresh; resending restarts that window.

To invite someone to your application rather than your organization: **Dashboard
→ Users** (in the sidebar) **→ Invites →** create an invite, leave the
organization unset, and set the role to **Member**.

A script can also add, revoke or refresh invitations, one at a time or in batches:

```bash
cd apps/api
uv run python scripts/manage_workos_users.py add analyst@example.org --file more.csv
uv run python scripts/manage_workos_users.py refresh analyst@example.org
uv run python scripts/manage_workos_users.py revoke former-member@example.org
uv run python scripts/manage_workos_users.py list
```

- `add` sends a new invitation;
- `refresh` resends a pending one (or adds one if none is pending);
- `revoke` withdraws a pending invitation;
- `list` shows pending/accepted/revoked. Emails come as arguments or a `--file` (one per line, or a CSV with an `email` column);
- `--dry-run` previews without calling WorkOS.

> The script needs `WORKOS_API_KEY=sk_...` in `apps/api/.env` — from **Developer
→ API keys** in the *same environment* whose users you manage (a staging key
manages staging users). The key is a local admin secret; never deploy it.
**Removing someone who already accepted** is done in the dashboard (Users),
not the script: `revoke` only withdraws a still-pending invitation.

### For developers: where the auth lives

The server plays two roles, each with its own small module:

1. **Resource server for `/mcp`** — `apps/api/src/app/mcp/auth.py`. It never
   issues tokens; it publishes discovery and verifies the JWTs clients bring.
   A `ResourceServer(AuthKitProvider)` subclass pins the audience to
   `<GRP_PUBLIC_URL>/mcp` and holds the AuthKit domain as a plain string (no
   trailing-slash normalization), so the token audience, the discovery document
   and the 401 challenge all agree. Keys are fetched from
   `<GRP_AUTHKIT_DOMAIN>/oauth2/jwks`.
2. **OAuth client for the standard app** — `apps/api/src/app/web_auth.py`. It
   sends people to AuthKit and keeps a browser session: an authorization-code +
   PKCE flow (public client, no secret) with an in-memory session store behind
   an HttpOnly `grp_session` cookie, refreshing the access token while alive.

**What is gated, and how.** With web login on, `SessionGate`
(`apps/api/src/app/main.py`) wraps the app. Always open: the public API set
(`/api/health`, `/api/resolve/*`, `/api/raster/*`,
`/api/food-security/rag/document/*`), the SPA's static code and host
(`/assets/*`, `/favicon.ico`, `/` — the shell and its signed-out landing page
carry no data), `/auth/*` and `/.well-known/*` (how a login starts), and `/mcp`
(which has its own transport-level OAuth). Gated: `/api` data answers
`401 {"detail":"login required"}`; other UI paths redirect to
`/auth/login?next=…`. The MCP transport independently refuses anonymous calls
with a `401` + `WWW-Authenticate` pointing at its discovery document.

**How the web UI decides.** On load the SPA probes `GET /auth/me`: `200` →
signed in; `401` → signed out (renders the landing page); `404` (routes not
mounted) → auth off (renders normally, no auth chrome). Auth state lives in a
`useAuth` hook (`apps/web/src/hooks/useAuth.ts`) over a single auth module
(`apps/web/src/lib/auth.ts`); `/api` calls send `credentials: 'include'`, and a
`401` from one starts a fresh login.

**Running without auth.** The commands in [Run locally](#run-locally) already
do: the app only gates when the env vars above are set. Local test commands:

```bash
uv run pytest              # backend tests (the suite turns OAuth off itself)
cd apps/web && npm test     # frontend unit tests
```

**Auth-on local development.** The login round-trip and the `grp_session`
cookie live on the backend origin — `GRP_PUBLIC_URL` (required once OAuth is on; set it
to `http://127.0.0.1:8001` locally) decides that host, not `VITE_API_BASE_URL`. Two shapes:

- **Same-origin (simplest).** Build the frontend and let the backend serve it:
  `cd apps/web && npm run build`, then run the backend with
  `GRP_WEB_DIST=apps/web/dist` and browse `http://127.0.0.1:8001`. Identical to
  production.
- **Vite dev (cross-origin).** Set `VITE_API_BASE_URL` in the frontend's `.env`
  (see [`apps/web/.env.example`](apps/web/.env.example)) to the backend URL, e.g.
  `VITE_API_BASE_URL=http://127.0.0.1:8001`. Both sides must be `127.0.0.1`
  (never mix in `localhost`): the cookie is host-scoped to 127.0.0.1, and CORS
  already allows the Vite origin.

Either shape needs the backend origin registered in WorkOS for local testing,
because the OAuth provider is a managed service: add
`http://127.0.0.1:8001/auth/callback` as a redirect URI and
`http://127.0.0.1:8001/` as the default redirect on the environment's
applications (see the operator section above).

**Design constraints to keep** — no WorkOS API key or client secret on the
server (the web login client is a PKCE public client; MCP clients self-register
via DCR). The frontend keeps no provider values either: `/auth/*` and the
session live on the backend only.

## Agent skills

Three Claude Code skills live in [`.claude/skills/`](.claude/skills/). The MCP server also
serves them to any client as `servirplatform://skill/<name>`. Start a new Claude Code
session in the repo to load them.

| Skill | What it does |
| --- | --- |
| [`contribute-logic`](.claude/skills/contribute-logic/SKILL.md) | Turns a formula, threshold or index into a platform method that the gate checks and every domain pack computes and cites. |
| [`trace-emit`](.claude/skills/trace-emit/SKILL.md) | Instruments a backend so every response can carry a structured trace: which steps ran, how long each took, which external services and caches were hit, and what each step decided (including LLM token and cost detail). |
| [`trace-visualize`](.claude/skills/trace-visualize/SKILL.md) | Turns a trace a backend already emits into something readable: a step list, timeline, flow diagram, CLI output or printable report. |

`trace-visualize` depends on `trace-emit`: a trace has to be produced before it can be
rendered, so run them in that order, never in the same pass.

## More

- [`apps/api/README.md`](apps/api/README.md) — the web app's original chat agent: its graph and module map.
- [`apps/api/API_EXAMPLES.md`](apps/api/API_EXAMPLES.md) — request and response examples for the web app's two endpoints, `POST /api/chat` and `POST /api/tiffs`.
- [`apps/api/DEMO.md`](apps/api/DEMO.md) — example queries for the chat agent.
- [`SOURCE_DATA_APPROACH.md`](SOURCE_DATA_APPROACH.md) — the four-layer risk model (precomputed risk, recomputed risk, then planned reclassing and upstream fetch) and its rationale.
- [`apps/web/README.md`](apps/web/README.md) — the frontend: dev server, and how it talks to the API.

## License

Licensed under the Eclipse Public License 2.0 (EPL-2.0). See [LICENSE](LICENSE.md) for the full text.
