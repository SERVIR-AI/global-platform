"""Evidence lookup CLI for analysts: takes a topic, prints the top corpus
passages with their provenance passport (source, publication date, validation
level, archived-copy link). Thin client over the grp MCP server's
corpus_search tool — spawns it over stdio, same as scripts/mcp_call.py.

    uv run python scripts/evidence_lookup.py "maize failure early warning"
    uv run python scripts/evidence_lookup.py "drought Zambia" --k 5 --country zambia

Note on "archived-copy link": the passport's archived_copy is a
platform-relative path (e.g. /api/food-security/rag/document/<doc_id>), not
yet a live URL — the REST route that serves it is a declared Phase-2/3 gap in
this build. Treat it as a doc_id reference for now; resolve the full document
via corpus_document(doc_id) or the grp MCP tool of the same name.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import textwrap

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp.server"])


async def corpus_search(topic: str, k: int, country: str | None, crop: str | None,
                         temporal: str | None, doc_type: str | None) -> dict:
    args = {"query": topic, "k": k, "country": country, "crop": crop,
             "temporal": temporal, "doc_type": doc_type}
    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("corpus_search", args)
            text = result.content[0].text
            return json.loads(text)


def render(payload: dict) -> str:
    if payload.get("declined"):
        return f"Search declined: {payload['reason']}"

    hits = payload.get("hits", [])
    if not hits:
        return payload.get("note", "No results.")

    lines = [f'Top {len(hits)} passage(s) for "{payload["query"]}":', ""]
    for i, hit in enumerate(hits, 1):
        p = hit["passport"]
        snippet = " ".join(hit["text"].split())
        snippet = textwrap.shorten(snippet, width=280, placeholder="...")
        lines.append(f"[{i}] {p.get('title') or '(untitled)'}  (score {hit['score']:.3f})")
        lines.append(f"    Source:          {p.get('source')}")
        lines.append(f"    Published:       {p.get('pub_date')}")
        lines.append(f"    Validation:      {p.get('validation')}")
        lines.append(f"    Archived copy:   {p.get('archived_copy') or '(not archived)'}")
        lines.append(f"    Passage:         {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Look up evidence passages in the food-security corpus.")
    parser.add_argument("topic", help="Search topic, e.g. 'maize failure early warning'")
    parser.add_argument("--k", type=int, default=3, help="Number of passages to return (default: 3)")
    parser.add_argument("--country", default=None)
    parser.add_argument("--crop", default=None)
    parser.add_argument("--temporal", default=None)
    parser.add_argument("--doc-type", dest="doc_type", default=None)
    args = parser.parse_args()

    payload = asyncio.run(corpus_search(args.topic, args.k, args.country, args.crop,
                                         args.temporal, args.doc_type))
    print(render(payload))


if __name__ == "__main__":
    main()
