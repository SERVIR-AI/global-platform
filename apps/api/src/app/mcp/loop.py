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


def _draft_rules(required_sections) -> list[str]:
    """The gate's blocking rules, stated where the draft is actually written."""
    return [
        "Use these section headers EXACTLY: " + " / ".join(required_sections),
        "Cite pack items as [n]. Every paragraph needs at least one citation.",
        "Only cite numbers that appear in the pack — a [n] that is not in the pack "
        "is a phantom citation and blocks the gate.",
        "Do NOT write your own '## Sources' section; the receipt carries the "
        "sources with their provenance.",
    ]


def after_assemble(pack_id: str, required_sections) -> dict:
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
        "draft_rules": _draft_rules(required_sections),
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


def after_record(receipt_id: str) -> dict:
    """The last seam. This USED to say "call ui_embed" unconditionally — written for
    coding hosts before the MCP App existed. In a chat host the panel has already
    rendered beside the result, and a model that obeyed anyway pasted an <iframe>
    into its reply, which the host's CSP (frame-src 'self' blob: data:) blocked into
    a giant blank white box. Measured on Claude Desktop, 2026-08-25. So the guidance
    is now conditional on what kind of host is reading it."""
    return {
        "required": False,
        "step": "presentation — depends on your host",
        "if_host_renders_mcp_apps": (
            "Claude Desktop / claude.ai: the evidence panel is ALREADY shown beside "
            "this tool result. Add nothing visual. Do NOT paste iframe markup into "
            "the chat — external frames are blocked by the host's CSP and render as "
            "a blank box."),
        "if_building_a_web_page": {
            "note": ("ui_embed's iframe is for pages YOU are writing (a dashboard, "
                     "a report file the user opens in a browser) — there it "
                     "re-resolves live and is the only way to show a verdict."),
            "call": {"tool": "ui_embed",
                     "args": {"component": "provenance_graph",
                              "receipt_id": receipt_id}}},
        "if_neither": (
            "Text-only host: present the sources as a TABLE with publisher, date "
            "and validation. Never a paragraph."),
    }


# --- the way IN --------------------------------------------------------------
# A model can enter through a lookup instead of the loop. gpt-4o-mini did exactly
# that on a plain question: corpus_search, then a confident answer written straight
# from the passages, with no pack, no gate and no receipt. The lookup tools are
# legitimate on their own, so this is a route in, not a demand.
ENTRY_HINT = {
    "note": ("These are raw lookup results — evidence, not a governed answer. If you "
             "are answering a user's question with them, go through the loop instead: "
             "assemble_pack gathers the same evidence WITH the forecast/retrospective "
             "split, the crop calendar, the live conditions feed and the declared "
             "gaps, and it mints the pack_id that publish_answer needs to gate and "
             "receipt your brief."),
    "call": {"tool": "assemble_pack",
             "args": {"country": "<country>", "crop": "<crop>",
                      "focus": "<what the user actually asked>"}},
}


def with_entry_hint(result: dict) -> dict:
    """Attach the route into the loop to a successful lookup result.

    Applied at the TOOL boundary, not inside the fetch/feeds functions: the hint is
    for an LLM deciding what to do next, and the platform's own internal callers
    (synthesis assembling a pack) must not find guidance mixed into their evidence.
    """
    if not isinstance(result, dict) or result.get("status") != "ok":
        return result
    return {**result, "for_a_governed_answer": ENTRY_HINT}
