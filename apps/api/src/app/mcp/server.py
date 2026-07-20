"""FastMCP server — the bones as MCP tools. Run over stdio:
    uv run python -m app.mcp.server
Tools are thin carriers over the food-security functions; logic lives in the
called modules, not here (the tool-vs-prompt litmus, ARCHITECTURE §1).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import registry

mcp = FastMCP("global-risk-platform")


@mcp.tool()
def platform_capabilities() -> dict:
    """The platform map: packs, sources with provenance, calendars, and DECLARED
    gaps. Call this first in a build session to scope honestly before writing code.
    """
    return registry.capabilities()


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
