"""The canonical loop's FORWARD EDGE, carried in tool RESULTS — not only in the
connect-time instructions.

Instructions arrive once, at initialize, and are a long way away by the time a
tool returns seventeen citations. Twice, on two different clients, a model called
assemble_pack, drafted a good brief from the pack, and stopped: no gate, no
receipt, nothing replayable — and no rendered evidence either, because the
evidence app hangs off record_receipt, so skipping the receipt silently skips the
visual too. A pack LOOKS like a finished answer and nothing at the point of
decision said it was not one.

So every stage names its own next call, with real argument values, and says what
is lost by stopping there. Consumers run whatever model they have — a hub's own
LLM may be a small one — so the loop must not depend on the model still
remembering a briefing it read at connect.
"""

from __future__ import annotations

PACK_IS_NOT_AN_ANSWER = (
    "INCOMPLETE — this pack is EVIDENCE, not an answer. Draft the brief from it, "
    "then finish with ONE call to publish_answer(pack_id, draft, question). Do not "
    "show the user a brief that has not been through it.")

VERDICT_IS_NOT_A_RECEIPT = (
    "INCOMPLETE — the draft passed the gate, but the answer is not yet replayable. "
    "One required call remains: record_receipt.")

BLOCKED = (
    "BLOCKED — this draft did NOT pass the gate. Do not publish it. Fix the "
    "failures below and call verify_groundedness again.")

# What is actually lost by stopping at each seam. Stated as consequence, not as
# etiquette: a model skips a step it reads as ceremony.
_LOST_AT_PACK = (
    "The brief would be ungoverned: nothing has checked it for citations that do "
    "not exist in the pack, there is no receipt, and neither you nor the user can "
    "replay or share the answer. record_receipt is also what returns the evidence "
    "view — skip it and the user gets prose instead of the evidence chain.")

_LOST_AT_VERDICT = (
    "The verdict exists but is tied to nothing: no receipt links the question, the "
    "evidence and the verdict, so the answer cannot be re-resolved later — and the "
    "evidence view, which record_receipt returns, is never shown.")


# G13: the one build policy, here where every builder's publish_answer reply carries it and in
# build_a_tool: a built page or app shows no web content at all, labelled or not.
BUILD_SOURCES = ("Bake no web content into a page or app you build, not even in a labelled "
                 "section: every figure and link it shows comes from a platform response (a pack, "
                 "a receipt, a feed). A source it lacks (not in platform_capabilities(section="
                 "'coverage')): ask the person for its link and contribute it (contribute_submit); "
                 "never search the web for one.")


RECEIPT_NEEDS_SHOWING = (
    "GOVERNED and replayable. If your host renders MCP Apps (Claude Desktop, "
    "claude.ai), the evidence panel is ALREADY displayed beside this result — do "
    "not add a visual. NEVER paste iframe markup into a chat reply: chat hosts "
    "forbid external frames (CSP frame-src 'self'), so it renders as a blank box.")


# Placed at the TAIL of the pack as well as the head — an LLM holds the whole
# payload, and the text closest to the generation point is the END, not the start.
YOUR_NEXT_OUTPUT = (
    "STOP — do not write the brief as your reply yet. Write it, then send it as your "
    "next output: publish_answer(pack_id='{pack_id}', draft=<the full brief>, "
    "question=<the user's question>). ONE call finishes the loop — it gates the "
    "draft, mints the receipt and returns the evidence view. A brief typed straight "
    "into the conversation is ungoverned, unreceipted and unshowable, which is the "
    "one failure this platform exists to prevent.")


def _draft_rules(required_sections, gaps_citation_n=None) -> list[str]:
    """The gate's blocking rules, stated where the draft is actually written."""
    rules = [
        "Use these section headers EXACTLY: " + " / ".join(required_sections),
        "Cite pack items as [n]. Every paragraph needs at least one citation.",
        "Only cite numbers that appear in the pack — a [n] that is not in the pack "
        "is a phantom citation and blocks the gate.",
        "Do NOT write your own '## Sources' section; the receipt carries the "
        "sources with their provenance.",
        "Platform sources only: a figure the pack does not hold (in digits, words or "
        "a magnitude like '3.2 million'; write '7.4m' in full) or a link or web domain "
        "it does not carry blocks the gate.",
    ]
    if gaps_citation_n is not None:
        rules.insert(2, f"The declared gaps are themselves citable evidence: entry "
                        f"[{gaps_citation_n}]. Your what's-missing paragraphs should "
                        f"cite [{gaps_citation_n}] — an honest gap statement no "
                        "longer fails the gate as uncited.")
    return rules


def after_assemble(pack_id: str, required_sections, gaps_citation_n=None) -> dict:
    """The hop a model has twice failed to take: pack -> gate -> receipt."""
    return {
        "required": True,
        "step": "FINISH — one call gates the draft, mints the receipt, returns the "
                "evidence view",
        "call": {"tool": "publish_answer",
                 "args": {"pack_id": pack_id,
                          "draft": "<your full drafted brief, verbatim>",
                          "question": "<the user's question, verbatim>"}},
        # The draft is passed whole and hashed, so the receipt can answer "is the
        # circulating copy the text you verified?" — say so, or it reads as
        # pointless duplication and gets skipped.
        "pass_the_whole_draft": (
            "Send the complete draft text, not a summary: the gate hashes exactly "
            "what it checked so the receipt proves which text passed."),
        "draft_rules": _draft_rules(required_sections, gaps_citation_n),
        "granular_alternative": (
            "verify_groundedness(draft, pack_id) then record_receipt(pack_id, "
            "report_id) do the same two things separately — same gate, same receipt."),
        "if_you_stop_here": _LOST_AT_PACK,
    }


def after_verify(pack_id: str, report_id: str, passed: bool,
                 failures=None) -> dict:
    """Passed -> mint the receipt. Failed -> fix and re-gate, never publish."""
    if not passed:
        return {
            "required": True,
            "step": "the gate BLOCKED this draft — do not show it to the user",
            "fix_then_retry": list(failures or []),
            "call": {"tool": "publish_answer",
                     "args": {"pack_id": pack_id,
                              "draft": "<corrected draft, verbatim>",
                              "question": "<the user's question, verbatim>"}},
            "if_you_stop_here": (
                "Publishing a blocked draft is the one thing this platform exists "
                "to prevent."),
        }
    return {
        "required": True,
        "step": "2 of 2 — mint the receipt",
        "call": {"tool": "record_receipt",
                 "args": {"pack_id": pack_id, "report_id": report_id,
                          "question": "<the user's question, verbatim>"}},
        "if_you_stop_here": _LOST_AT_VERDICT,
    }


def after_record(receipt_id: str, verdict: dict) -> dict:
    """The last seam, by host: an MCP-Apps host already shows the panel (an iframe pasted into
    chat renders as a blank box: Claude Desktop CSP, 2026-08-25); a page you write shows the
    verdict as the receipt's render_with.verdict does, its embed or the resolver, UNVERIFIED."""
    if verdict.get("tool") == "ui_embed":
        page = {"note": ("ui_embed's iframe is for pages YOU are writing (a dashboard, a report "
                         "file the user opens in a browser) — there it re-resolves live and is "
                         "the only way to show a verdict."),
                "platform_sources": BUILD_SOURCES,
                "call": {"tool": "ui_embed", "args": {"component": verdict["component"],
                                                      "receipt_id": receipt_id}}}
    else:
        page = {"note": ("no component embeds this pack's verdict yet: in a page you write, state "
                         "it UNVERIFIED and link the resolver, which re-resolves it live; never "
                         "paint \"passed\" into the page."),
                "platform_sources": BUILD_SOURCES, "link": verdict.get("url")}
    return {
        "required": False,
        "step": "presentation — depends on your host",
        "if_host_renders_mcp_apps": (
            "Claude Desktop / claude.ai: the evidence panel is ALREADY shown beside "
            "this tool result. Add nothing visual. Do NOT paste iframe markup into "
            "the chat — external frames are blocked by the host's CSP and render as "
            "a blank box."),
        "if_building_a_web_page": page,
        "if_neither": (
            "Text-only host: present the sources as a TABLE with publisher, date "
            "and validation. Never a paragraph."),
    }


# --- the way IN --------------------------------------------------------------
# A model can enter through a lookup instead of the loop. gpt-4o-mini did exactly
# that on a plain question: corpus_search, then a confident answer written straight
# from the passages, with no pack, no gate and no receipt. The lookup tools are
# legitimate on their own, so this is a route in, not a demand.
from . import packs as _packs   # the target shapes below are generated from the pack rows

# The one contextual line a reply with rows or figures carries (<= budget.NOTE_CHARS); the
# rule in full is in the instructions and the assemble_pack / feeds_query descriptions.
NO_ARITHMETIC = ("Quote figures as returned; do not add up, average, count, rank or score them "
                 "yourself, in chat or code. A dataset's note like 'sum hectares' says what adds "
                 "up, not to add it up. For a figure no pack computes, suggest one or two "
                 "calculations, without their results, to accept or reject, then offer a method "
                 "(contribute_submit kind=method); done without one, say so: your own "
                 "arithmetic, no receipt.")

# G13: the one line a decline or a declared-gaps list carries (<= budget.NOTE_CHARS), saying
# what PLATFORM SOURCES ONLY says in the instructions. not_served() names the kinds.
_WEB = ("Web results, only if the person asks, follow the answer labelled UNRECEIPTED, never "
        "inside it.")
NOT_SERVED = ("Not served here, and nothing in platform_capabilities(section='coverage') fits? Ask "
              "the person to contribute the source (contribute_submit kind={kinds}; kind alone "
              "returns the form: read it before asking) and stop; never search the web for one. "
              + _WEB)
# A caller's error or an outage: the data may well be served, so no contribution is asked for.
NOT_A_GAP = "Not a coverage gap: fix the call as the note says, or retry later. " + _WEB


def not_served(said=(), gap: bool = True) -> str:
    """NOT_SERVED naming the kinds `said` (a decline note, a gap list) names by their own
    words; naming none, every kind whose form takes a source for a gap list (`gap`), and
    NOT_A_GAP for a decline — derived, never typed."""
    from . import budget
    from ..contrib import staging
    text = " ".join(str(s) for s in (said if isinstance(said, (list, tuple)) else [said]) if s)
    kinds = staging.kinds_named(text)
    if not kinds and not gap:
        return NOT_A_GAP
    return budget.fit("the not-served line", lambda: NOT_SERVED.format(kinds=budget.names(
        kinds or staging.source_kinds(), last=" or ")), budget.NOTE_CHARS)


ENTRY_HINT = {
    "no_arithmetic": NO_ARITHMETIC,
    # <= budget.NOTE_CHARS whatever the number of packs: the target shapes ride `call`.
    "note": ("These are raw lookup results — evidence, not a governed answer. Answering a "
             "user's question with them? Go through the loop: assemble_pack (the call below) "
             "gathers the pack's evidence with its declared gaps and mints the pack_id "
             "publish_answer needs to gate and receipt your brief."),
    "call": {"tool": "assemble_pack",
             "args": {"<the pack's target params, e.g.>": _packs.target_shapes(),
                      "focus": "<what the user actually asked>"}},
}


METHOD_FIRST = ("An approved method already computes figures from this dataset "
                "(methods_reading). For a figure it computes, do not count or add up these "
                "rows: call assemble_pack for the place asked, quote the method's cited "
                "result, and publish_answer receipts it. The pack keeps only the rows inside "
                "the place's own boundary; a bbox is a rectangle and takes in neighbouring areas.")


def methods_reading(dataset) -> list[dict]:
    """The approved methods, and the caller's staged ones, that read `dataset`, each with
    the assemble_pack call that returns its cited result. Read from the method registry
    and the pack registry, so a method contributed today is named with no code change."""
    if not dataset:
        return []
    try:
        from ..contrib import methods
        rows = {**methods.staged_visible(), **methods.landed()}
    except Exception:                              # a lookup never fails on its pointer
        return []
    out = []
    for name, row in sorted(rows.items()):
        reads = {str(next(iter(r.values()))) for r in (row.get("inputs") or {}).values()
                 if isinstance(r, dict) and r}
        if str(dataset) not in reads:
            continue
        calls = []
        for pid in row.get("packs") or []:
            sp = _packs.PACKS.get(pid)
            if sp:
                req = [k for k in sp["target_keys"] if k not in (sp.get("optional_keys") or ())]
                calls.append({"tool": "assemble_pack",
                              "args": {"pack": pid, **{k: f"<the {k} asked about>" for k in req}}})
        out.append({"method": name, "title": row.get("title") or name,
                    "computes": {o: (spec or {}).get("unit")
                                 for o, spec in (row.get("outputs") or {}).items()},
                    "get_its_result": calls})
    return out


def with_entry_hint(result: dict) -> dict:
    """Attach the route into the loop to a lookup result. A decline keeps its honest cause
    first and gains only the method pointer.

    Applied at the TOOL boundary (shape.py then orders it ahead of the rows), never inside
    the fetch/feeds functions: the platform's own internal callers (synthesis assembling a
    pack) must not find guidance mixed into their evidence.
    """
    if not isinstance(result, dict):
        return result
    reading = methods_reading(result.get("dataset"))
    pointer = {"method_first": METHOD_FIRST, "methods_reading": reading} if reading else {}
    if result.get("status") != "ok":
        return {**result, **pointer} if pointer else result
    return {"status": "ok", "for_a_governed_answer": {**pointer, **ENTRY_HINT}, **result}
