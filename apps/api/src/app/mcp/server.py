"""FastMCP server — the bones as MCP tools. Run over stdio:
    uv run python -m app.mcp.server
Tools are thin carriers over the food-security functions; logic lives in the
called modules, not here (the tool-vs-prompt litmus, ARCHITECTURE §1).
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import context, fetch, registry

# Host/port for the remote (streamable-http) transport. Remote is the faithful
# build surface: consumers connect by URL, so no filesystem path leaks into a
# client config for a coding agent to follow into our source (ARCHITECTURE §6).
mcp = FastMCP("global-risk-platform",
              host=os.environ.get("GRP_MCP_HOST", "127.0.0.1"),
              port=int(os.environ.get("GRP_MCP_PORT", "8000")),
              stateless_http=True)  # each tool call is self-contained; no session to terminate


@mcp.tool()
def platform_capabilities() -> dict:
    """The platform map: packs, sources with provenance, calendars, and DECLARED
    gaps. Call this first in a build session to scope honestly before writing code.
    """
    return registry.capabilities()


@mcp.tool()
def corpus_search(query: str, k: int = 5, country: str | None = None,
                  crop: str | None = None, temporal: str | None = None,
                  doc_type: str | None = None) -> dict:
    """Search the food-security library; passages carry a provenance passport
    (source, date, validation, archived copy).

    Returns: {status, query, corpus, min_relevance, hits}.
      status "ok"       -> hits: [{score, text, passport{...}}]
      status "empty"    -> hits: [], and `note` says WHY (empty library /
                           filters exclude / below the relevance floor — a
                           below-floor miss is a decline, not a weak match)
      status "declined" -> hits: [], and `note` says WHY (missing key / torn corpus)
    Whenever status != "ok", render `note` — it is the honest cause.
    """
    return fetch.search(query, k=k, country=country, crop=crop,
                        temporal=temporal, doc_type=doc_type)


@mcp.tool()
def context_get(country: str, crop: str, asked_month: int | None = None,
                override: list[dict] | None = None,
                override_country: str | None = None,
                override_crop: str | None = None) -> dict:
    """The hub-default crop calendar for a country/crop, and the phase the asked
    month falls in (asked_month defaults to the current month — "this season").

    A human `override` (list of {season, planting:[m,m], harvest:[m,m]}) is
    honored but LABELED: the returned calendar is marked adjusted=true and cited
    as "ADJUSTED by the requester". Pass override_country/override_crop to pin it
    to its target — if they mismatch the requested country/crop the override is
    DROPPED and the drop is declared in `gaps` (a swap never applies silently).

    Returns: {status, country, crop, asked_month, adjusted, calendar, gaps}.
      status "empty"    -> no calendar configured for this country/crop (`note`)
      status "declined" -> bad asked_month / malformed override (`note`)
    """
    return context.get(country, crop, asked_month=asked_month, override=override,
                       override_country=override_country, override_crop=override_crop)


@mcp.tool()
def corpus_document(doc_id: str | None = None) -> dict:
    """Trace a passage back to its source document.

    Returns: {status, ...}.
      No doc_id, status "ok" -> {documents, inventory: [{doc_id, chunks, passport}]}
      With a doc_id, status "ok" -> {doc_id, chunks, passport} (the trace-back terminus)
      status "declined" -> `note` says WHY (unreadable corpus / unknown doc_id)
    Whenever status != "ok", render `note`.
    """
    return fetch.document(doc_id)


def main() -> None:
    """Default stdio (local dev). `--http` serves streamable-http on
    GRP_MCP_HOST:GRP_MCP_PORT/mcp — the faithful remote build surface."""
    import sys
    transport = "streamable-http" if "--http" in sys.argv else "stdio"
    os.environ["GRP_MCP_TRANSPORT"] = transport  # so capabilities reports it honestly
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
