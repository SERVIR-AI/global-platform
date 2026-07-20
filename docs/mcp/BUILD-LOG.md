# MCP server — build log

Progressive Phase-1 build of `docs/mcp/ARCHITECTURE.md`. Each step is small and
manually testable. Tool names use underscores (MCP/LLM identifiers can't contain
dots): architecture `platform.capabilities` → tool `platform_capabilities`.

**Layout:** `apps/api/src/app/mcp/` — `registry.py` (pure logic), `server.py`
(FastMCP carriers). Manual-test client: `apps/api/scripts/mcp_call.py`.

**Run the server (stdio):** `uv run python -m app.mcp.server`
**Connect from Claude Code:** `claude mcp add grp -- uv run --directory <repo>/apps/api python -m app.mcp.server`

---

## Step 1 — server skeleton + `platform_capabilities` (2026-07-20)

**Built:** the FastMCP server and the map tool. `platform_capabilities` reads real
state (corpus doc/chunk counts + El Niño event windows via `Corpus`; hub calendars
via `calendar.load()`) and **declares every gap** (climate feeds, analytics,
widgets, skills, the acreage deferral) rather than omitting it — the honesty ethos
applied to the server itself. No LLM, no network. Added `mcp>=1.2`; restored
`pytest` as a dev-group dependency (it had been floating, and `uv sync` pruned it).

**Verified:** pure function returns 62 docs / 1758 chunks / 3 events / kenya+zambia
maize calendars; full MCP round-trip over stdio (initialize → list_tools →
call_tool) returns the map; suite 138 passed / 18 skipped.

**Test:**
```
cd apps/api
uv run python scripts/mcp_call.py                    # lists: platform_capabilities
uv run python scripts/mcp_call.py platform_capabilities   # the map, gaps declared
uv run python -c "from app.mcp.registry import capabilities, CONTRACT; print(len(CONTRACT), 'rules')"
uv run pytest -q                                     # 138 passed / 18 skipped
```

## Step 2 — `fetch` bone: `corpus_search` + `corpus_document` (2026-07-20)

**Built:** the fetch bone (`mcp/fetch.py`, thin over `Corpus`). `corpus_search`
returns top-k passages each with a **provenance passport** (source, date, validation
level, event, residency, source URL, archived-copy link) and declines below the
relevance floor with its named cause (empty/filtered/below-floor). `corpus_document`
lists the inventory or traces one doc to its archived original. Real OpenAI embeddings.

**Verified (real MCP round-trip):** "El Nino maize Kenya" → 2 hits, scores 0.67/0.66,
validation `multi-agency-consensus`, archived-copy links resolve; "quantum
chromodynamics" → honest below-floor decline; inventory 62 docs; suite 138/18.

**Test:**
```
uv run python scripts/mcp_call.py corpus_search '{"query":"El Nino impact on maize in Kenya","k":2}'
uv run python scripts/mcp_call.py corpus_search '{"query":"quantum chromodynamics"}'   # honest decline
uv run python scripts/mcp_call.py corpus_document '{"doc_id":"7405f3e274287b10"}'       # trace-back
uv run python scripts/mcp_call.py corpus_document                                       # inventory (62)
```

## Step 2.1 — fetch contract hardening (2026-07-20)

**Why:** a Claude Desktop build of an evidence-lookup CLI (faithful, contract-only)
guessed the decline field as `reason`/`decline_reason` and would have **swallowed the
honest cause**, because the empty-results reason lived in an unadvertised `note` field
it never observed (it only saw the happy path). Contract-discoverability gap — and tool
descriptions ARE the product surface.

**Built:** uniform, self-describing return contract on both fetch tools —
`status` ("ok"|"empty"|"declined") + `note` (the cause whenever there are no
hits). Spelled the full return shape into the tool descriptions so a consumer
renders declines correctly WITHOUT triggering one. Rule 2 ("declines say why") now
holds at the surface the consumer actually reads.

**Verified:** ok / empty-below-floor / empty-filtered / declined-unknown-doc all
carry a discoverable cause; suite 138/18.

**Test:**
```
uv run python scripts/mcp_call.py corpus_search '{"query":"quantum chromodynamics"}'   # status:empty + note
uv run python scripts/mcp_call.py corpus_search '{"query":"maize","country":"atlantis"}' # status:empty (filtered)
uv run python scripts/mcp_call.py corpus_document '{"doc_id":"deadbeef"}'                # status:declined + note
```
