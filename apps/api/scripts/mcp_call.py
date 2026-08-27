"""Manual-test client for the MCP server: spawns it over stdio, lists or calls a
tool, prints the result. No live Claude needed.

    uv run python scripts/mcp_call.py                      # list tools
    uv run python scripts/mcp_call.py <tool> '<json args>' # call a tool

Examples:
    uv run python scripts/mcp_call.py
    uv run python scripts/mcp_call.py platform_capabilities
"""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp.server"])


async def run(tool: str | None, args: dict) -> None:
    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if tool is None:
                tools = await session.list_tools()
                for t in tools.tools:
                    print(f"{t.name}\n    {(t.description or '').strip().splitlines()[0]}")
                return
            result = await session.call_tool(tool, args)
            for block in result.content:
                text = getattr(block, "text", None)
                if text is None:
                    print(block)
                    continue
                try:
                    print(json.dumps(json.loads(text), indent=2))
                except (ValueError, TypeError):
                    print(text)


def main() -> None:
    tool = sys.argv[1] if len(sys.argv) > 1 else None
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    asyncio.run(run(tool, args))


if __name__ == "__main__":
    main()
