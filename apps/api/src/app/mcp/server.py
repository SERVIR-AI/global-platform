"""FastMCP server — the bones as MCP tools. Run over stdio:
    uv run python -m app.mcp.server
Tools are thin carriers over the food-security functions; logic lives in the
called modules, not here (the tool-vs-prompt litmus, ARCHITECTURE §1).
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from . import assemble, compose, context, feeds, fetch, record, registry, resolve, ui, verify

# Orientation shown to a connecting LLM at initialize — so it isn't a headless
# chicken. DESCRIBE the two consumption patterns; don't enforce (no mode switch).
# Kept terse: every connection pays its token cost.
INSTRUCTIONS = """The Global Risk Platform — a GOVERNED TOOLKIT for food-security \
decision support, not a chatbot. Every insight carries its evidence; declines say \
why; answers are replayable; a groundedness gate blocks ungrounded briefs.

Two ways to use it:
- BUILD-TIME: the user asks you to BUILD a tool/app. Produce a REUSABLE artifact \
(a script/app) that calls these tools — do NOT just run the pipeline once.
- RUN-TIME: the user asks a question. Answer it now via the canonical loop.

Canonical loop: platform_capabilities (scope honestly; surface declared gaps) -> \
assemble_pack(country, crop) -> YOUR LLM drafts a brief from the pack, citing [n] \
in the pack's required_sections -> verify_groundedness(draft, pack_id) -> publish \
ONLY if it passes -> record_receipt(pack_id, report_id) mints a replayable receipt. \
Use corpus_search/corpus_document for evidence with provenance, context_get for the \
(adjustable) crop calendar. Start with platform_capabilities.

If the user asks how to use this server, read the `grp://how-to-use` resource (a \
human-readable guide) — or run the `explain_platform` prompt — and answer from it."""

# Host/port for the remote (streamable-http) transport. Remote is the faithful
# build surface: consumers connect by URL, so no filesystem path leaks into a
# client config for a coding agent to follow into our source (ARCHITECTURE §6).
mcp = FastMCP("global-risk-platform",
              instructions=INSTRUCTIONS,
              host=os.environ.get("GRP_MCP_HOST", "127.0.0.1"),
              port=int(os.environ.get("GRP_MCP_PORT", "8000")),
              stateless_http=True)  # each tool call is self-contained; no session to terminate


@mcp.tool()
def platform_capabilities() -> dict:
    """The platform map: which tools/prompts are live, the bones and their status,
    packs, sources with provenance, calendars, and DECLARED gaps. Bone status is
    derived from the live registry, so it never drifts. Call this first in a build
    session to scope honestly before writing code.
    """
    return registry.capabilities(
        available_tools=[t.name for t in mcp._tool_manager.list_tools()],
        available_prompts=[p.name for p in mcp._prompt_manager.list_prompts()],
        available_resources=[str(r.uri) for r in mcp._resource_manager.list_resources()])


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
def assemble_pack(country: str, crop: str, focus: str | None = None,
                  override: list[dict] | None = None,
                  override_country: str | None = None,
                  override_crop: str | None = None) -> dict:
    """Assemble a deterministic, citable EVIDENCE PACK for a country/crop and mint
    a `pack_id`. This is the hand-off seam of the platform: YOUR LLM writes the
    brief from this pack (cite the numbered items as [n]), then calls
    verify_groundedness(draft, pack_id) to gate it. No LLM runs inside this tool.

    The pack gathers: corpus evidence (a forecast slice + a retrospective slice,
    each item with source/date/validation/archived-copy), the live GEOGLAM
    conditions feed, and the crop calendar. A calendar `override` is target-pinned
    (mismatch dropped + declared). DECLARED GAPS are returned as content — surface
    them; they are what's missing, said honestly.

    Returns: {status, pack_id, country, crop, citations:[{n,...}], gaps, queries,
    required_sections, stats}. `required_sections` is what verify_groundedness
    will demand — draft those exact section headers so the gate passes in one shot.
    status "declined" -> `note` (missing key / torn corpus / bad override).
    """
    return assemble.assemble(country, crop, focus=focus, override=override,
                             override_country=override_country, override_crop=override_crop)


@mcp.tool()
def verify_groundedness(draft: str, pack_id: str) -> dict:
    """Gate a draft brief against the evidence pack it was written from. The
    verdict is computed SERVER-SIDE (you cannot pass a "passed" flag) and a
    resolvable report_id is persisted.

    The draft must be built from a pack minted by assemble_pack, cite its items
    as [n], and contain the required sections (returned as `required_sections` —
    conform to them or the gate fails "missing sections"). Blocking failures:
    missing sections, a model-written "## Sources", no citations, phantom
    citations (numbers not in the pack), uncited paragraphs. Recorded but not
    blocking: numbers in the draft absent from the evidence.

    Returns: {status, report_id, passed, evidence_tier, required_sections,
    failures, cited, phantom_citations, missing_sections, uncited_paragraphs,
    numbers_unverified_recorded}. status "declined" -> unknown pack_id (`note`).
    """
    return verify.groundedness(draft, pack_id)


@mcp.tool()
def resolve_place_time(country: str, crop: str, region: str | None = None,
                       when: str | None = None) -> dict:
    """Turn human place-and-time into machine place-and-time: country/crop + a time
    expression (`when`: a month name, 1-12, or "this season" — the default) → the
    season windows and which phase that month falls in.

    Returns: {status, country, crop, region, asked_month, month_name, seasons,
    active_seasons, region_resolution}. `region` is currently passed through
    UNRESOLVED — a sub-national gazetteer/admin geometries are a declared Phase-2
    gap, stated in `region_resolution`. status "empty" -> no calendar for that
    country/crop; "declined" -> unparseable `when` (`note`).
    """
    return resolve.place_time(country, crop, region=region, when=when)


@mcp.tool()
def compose_run(composition: str = "foodsecurity.brief", question: str = "",
                override: list[dict] | None = None,
                override_country: str | None = None,
                override_crop: str | None = None,
                provider: str | None = None, model: str | None = None) -> dict:
    """Run a pre-wired COMPOSITION end-to-end — for consumers with NO LLM of their
    own (dashboards, cron jobs, automated bulletins).

    NOTE this runs the PLATFORM's LLM server-side, on the platform's key and bill.
    `provider`/`model` pick among the platform's CONFIGURED providers — they do not
    accept your credentials. If you have your own model, prefer the loop —
    assemble_pack → you draft → verify_groundedness — which uses YOUR model for the
    same evidence, the same gate and the same receipt. Use this tool when you have
    no model credentials or nowhere to run code.

    Compositions are registry rows (see `compositions` in platform_capabilities), so
    new ones add no tools. Returns: {status, brief, citations, gaps, grounded,
    pack_id, report_id, receipt_id, draft_sha256, provider, model}. status
    "declined" -> `note` (unknown composition / no question / ungroundable).
    """
    return compose.run(composition=composition, question=question, override=override,
                       override_country=override_country, override_crop=override_crop,
                       provider=provider, model=model)


@mcp.tool()
def record_receipt(pack_id: str | None = None, report_id: str | None = None,
                   receipt_id: str | None = None, question: str | None = None) -> dict:
    """Mint or resolve a replayable RECEIPT — the shareable proof of an answer.

    Mint (pass pack_id, and report_id from verify_groundedness): ties the question,
    the evidence pack, and the verdict into one persisted receipt_id. Resolve (pass
    receipt_id): returns that receipt. The receipt is claim-scoped — it attests that
    every cited claim traces to the pack, NOT the truth of the underlying sources.

    Returns: {status, receipt_id, question, pack_id, report_id, passed,
    evidence_tier, sources, claim_scope, minted_at, resolve_with}. A hosted,
    copy-paste-survivable resolver URL is a declared Phase-2 gap. status "declined"
    -> `note` (no pack for mint / unknown receipt_id).
    """
    return record.record(pack_id=pack_id, report_id=report_id,
                         receipt_id=receipt_id, question=question)


@mcp.tool(description=feeds.describe())
def feeds_query(dataset: str, params: dict | None = None) -> dict:
    return feeds.query(dataset=dataset, params=params)


@mcp.tool(description=ui.describe_design())
def ui_design(format: str = "all") -> dict:
    return ui.design(fmt=format)


@mcp.tool(description=ui.describe_catalog())
def ui_catalog() -> dict:
    return ui.catalog()


@mcp.tool(description=ui.describe_component())
def ui_component(name: str) -> dict:
    return ui.component(name)


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


@mcp.resource("grp://pack/food-security", mime_type="application/json")
def food_security_pack() -> dict:
    """The Food-Security domain pack manifest (v0): what ships, what it produces,
    and every declared gap — plus a real worked-example receipt to resolve."""
    return registry.pack_manifest()


@mcp.resource("grp://how-to-use", mime_type="text/markdown")
def how_to_use() -> str:
    """How to use the Global Risk Platform (human-readable) — attach/open in the host."""
    return """# How to use the Global Risk Platform

A **governed toolkit** for food-security decision support — not a chatbot. Every
insight carries its evidence, declines say why, and a groundedness gate blocks
ungrounded briefs. You use it in one of two ways.

## Build-time — build your own tool on ours
Ask your AI client to *build* something. It produces a reusable app/script that
calls these tools, so the guardrails live in your tool, not in a one-off chat.
- Claude Code: run the `build_a_tool` prompt (`/` menu) or just say
  "build me a bulletin tool for a country and crop".
- Claude Desktop: pick **build_a_tool** from the prompt (+) menu.

## Run-time — get one governed answer now
Ask a question. The assistant runs the loop and returns a grounded, cited brief.
- Use the `run_analysis` prompt, or ask e.g. "what should I expect for maize in
  Kenya this season?"

## The canonical loop (what a good answer does)
platform_capabilities (scope honestly) → assemble_pack(country, crop) → your LLM
drafts a brief from the pack citing [n] in its required_sections →
verify_groundedness(draft, pack_id) → publish only if it passes →
record_receipt mints a replayable receipt.

## See what's actually live (never trust a stale doc)
Call **platform_capabilities** (or run the `explain_platform` prompt) for the
current tools, bones, and DECLARED gaps — that map is derived from the live
server, so it's always accurate.
"""


@mcp.prompt()
def build_a_tool(goal: str = "a food-security bulletin generator") -> str:
    """BUILD-TIME: scaffold a reusable app/tool on the grp platform (not a one-off run)."""
    return (f"Build a REUSABLE tool that: {goal}.\n\n"
            "Use the grp MCP server. Produce a standalone artifact (a script/app) that "
            "a user can re-run — do NOT just run the pipeline once in chat. Follow the "
            "canonical loop: call platform_capabilities first (scope honestly, surface "
            "declared gaps), assemble_pack(country, crop), have the tool's own LLM draft "
            "a brief from the pack citing [n] in the pack's required_sections, then "
            "verify_groundedness(draft, pack_id) and publish ONLY if it passes. Show me "
            "the finished artifact and prove it runs.")


@mcp.prompt()
def run_analysis(country: str = "Kenya", crop: str = "maize") -> str:
    """RUN-TIME: answer one food-security question now via the canonical loop."""
    return (f"Using the grp MCP server, produce a grounded brief for {crop} in {country} "
            "now: assemble_pack, draft from the pack citing [n] in the required_sections, "
            "verify_groundedness, and give me the brief plus the report_id — or the "
            "decline reason if it can't be grounded. Do not fabricate beyond the pack.")


@mcp.prompt()
def explain_platform() -> str:
    """What the grp platform is and how to use it (build-time vs run-time)."""
    return ("Call platform_capabilities, then explain in plain terms what the grp "
            "platform is, what it can and can't do right now (its declared gaps), and "
            "the two ways to use it: BUILD-TIME (build a reusable tool on its bones) vs "
            "RUN-TIME (answer one question now).")


def main() -> None:
    """Default stdio (local dev). `--http` serves streamable-http on
    GRP_MCP_HOST:GRP_MCP_PORT/mcp — the faithful remote build surface."""
    import sys
    transport = "streamable-http" if "--http" in sys.argv else "stdio"
    os.environ["GRP_MCP_TRANSPORT"] = transport  # so capabilities reports it honestly
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
