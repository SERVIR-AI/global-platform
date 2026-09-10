"""The MCP surface is a CONTRACT, so it is asserted by name rather than by count.

A client's config, a hub's script and the runbook all name these strings. A library
upgrade that quietly renames or drops one of them is not a refactor, it is a broken
consumer, and nothing else in the suite would notice: the tools are tested through the
modules behind them, never through the server object that publishes them.

Written when the server moved off the MCP SDK's vendored FastMCP 1.0 onto fastmcp,
which is exactly the kind of change this guards.
"""
import asyncio

from app.mcp import packs
from app.mcp.app_ui import UI_URI
from app.mcp.server import mcp

TOOLS = {
    "platform_capabilities", "corpus_search", "corpus_document", "context_get",
    "resolve_place_time", "assemble_pack", "verify_groundedness", "record_receipt",
    "publish_answer", "compose_run", "feeds_query",
    "ui_design", "ui_catalog", "ui_component", "ui_embed",
}
PROMPTS = {"build_a_tool", "run_analysis", "explain_platform"}
PANEL_TOOLS = ("record_receipt", "publish_answer")


def test_every_tool_is_still_published_under_its_own_name(log):
    """The 15 tool names a consumer can call, asserted as a set."""
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    log("OUTPUT", str(sorted(names)))
    log("CHECK", "no tool renamed, dropped or silently added")
    assert names == TOOLS


def test_every_prompt_is_still_published(log):
    """The prompts a host shows in its slash or plus menu."""
    names = {p.name for p in asyncio.run(mcp.list_prompts())}
    log("OUTPUT", str(sorted(names)))
    assert names == PROMPTS


def test_resources_cover_the_app_the_guide_and_every_pack(log):
    """One manifest per PACKS row, plus the MCP App and the human guide."""
    uris = {str(r.uri) for r in asyncio.run(mcp.list_resources())}
    log("OUTPUT", str(sorted(uris)))
    expected = {UI_URI, "servirplatform://how-to-use",
                "servirplatform://skill/trace-emit",
                "servirplatform://skill/trace-visualize"} | {
        f"servirplatform://pack/{pid}" for pid in packs.available()}
    log("CHECK", "a new pack row would have to appear here too")
    assert uris == expected


def test_the_panel_tools_still_carry_their_ui_meta(log):
    """`meta.ui.resourceUri` is the ONLY thing that makes a host render the evidence
    panel beside the result. It is a dict a library upgrade can drop without error."""
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in PANEL_TOOLS:
        meta = getattr(tools[name], "meta", None) or {}
        log("OUTPUT", f"{name}: ui={meta.get('ui')}")
        assert meta.get("ui", {}).get("resourceUri") == UI_URI


def test_the_panel_tools_still_declare_an_output_schema(log):
    """The panel reads structuredContent, which a host only receives when the tool
    declares an output schema. It comes from the `-> dict[str, Any]` annotation now
    that `structured_output=True` is gone, so it is worth asserting it survived."""
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in PANEL_TOOLS:
        schema = getattr(tools[name], "output_schema", None)
        log("OUTPUT", f"{name}: output_schema={'present' if schema else 'ABSENT'}")
        assert schema, f"{name} would return no structuredContent for the panel"
