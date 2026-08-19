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
    "GOVERNED and replayable. Now SHOW it — call ui_embed for the evidence chain "
    "rather than describing it in prose.")


# Placed at the TAIL of the pack as well as the head. The first attempt put the
# forward edge at the top only, reasoning that a model "stops reading once it has
# enough" — that is human intuition and it measured as wrong: gpt-4o read past it
# and wrote the brief into its reply anyway. The whole payload is in context, and
# the text closest to the generation point is the END, not the start. So the
# instruction is repeated where it is read last.
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
    """The last seam, and the reason the visual never appeared: the evidence view
    hangs off the receipt, so a loop that ends here ends in prose."""
    return {
        # Not required — a host that cannot render markup should not be told to
        # emit it. But the default must be to show, not to describe.
        "required": False,
        "unless": ("your host cannot render markup — then present the sources as a "
                   "TABLE with publisher, date and validation. Never a paragraph."),
        "step": "3 of 3 — show the evidence chain",
        "call": {"tool": "ui_embed",
                 "args": {"component": "provenance_graph", "receipt_id": receipt_id}},
        "why": ("The reader is deciding whether to act months ahead. What helps them "
                "is SEEING which sources are live pulls, how old each one is, and "
                "what is missing. `render_with` names the component for each part."),
        "if_you_stop_here": (
            "The answer is governed but reads like any other prose answer — the user "
            "cannot see the evidence chain they are being asked to trust."),
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
