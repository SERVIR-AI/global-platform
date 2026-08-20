"""Minimal client for the GRP MCP server (streamable HTTP transport).

The server speaks JSON-RPC 2.0 over a single POST endpoint and replies with a
one-shot SSE stream (`event: message` / `data: {...}`). No session handshake
is required for tool calls, so we just POST each call independently.
"""
import itertools
import json

import requests

GRP_MCP_URL = "http://127.0.0.1:8002/mcp"
_ids = itertools.count(1)


class GrpError(RuntimeError):
    pass


def _post(payload):
    resp = requests.post(
        GRP_MCP_URL,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=60,
    )
    resp.raise_for_status()
    body = resp.text
    # streamable-http SSE: pull the JSON out of the last "data: ..." line
    data_line = None
    for line in body.splitlines():
        if line.startswith("data:"):
            data_line = line[len("data:"):].strip()
    if data_line is None:
        # plain JSON response (non-SSE)
        data_line = body
    return json.loads(data_line)


def call_tool(name, arguments):
    """Call an MCP tool and return its parsed JSON result payload."""
    message = _post(
        {
            "jsonrpc": "2.0",
            "id": next(_ids),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    if "error" in message:
        raise GrpError(message["error"].get("message", "MCP call failed"))
    result = message["result"]
    if result.get("isError"):
        raise GrpError(str(result.get("content")))
    content = result["content"][0]["text"]
    return json.loads(content)


def read_resource(uri):
    message = _post(
        {
            "jsonrpc": "2.0",
            "id": next(_ids),
            "method": "resources/read",
            "params": {"uri": uri},
        }
    )
    if "error" in message:
        raise GrpError(message["error"].get("message", "MCP resource read failed"))
    return message["result"]["contents"][0]["text"]
