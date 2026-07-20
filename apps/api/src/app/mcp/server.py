"""FastMCP server — the bones as MCP tools. Run over stdio:
    uv run python -m app.mcp.server
Tools are thin carriers over the food-security functions; logic lives in the
called modules, not here (the tool-vs-prompt litmus, ARCHITECTURE §1).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import fetch, registry

mcp = FastMCP("global-risk-platform")


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
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
