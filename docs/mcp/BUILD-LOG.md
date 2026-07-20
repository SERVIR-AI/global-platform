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
