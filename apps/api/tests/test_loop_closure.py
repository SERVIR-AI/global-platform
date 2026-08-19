"""The canonical loop must CLOSE — assemble -> verify -> record -> show.

These exist because it twice did not. On two different clients a model called
assemble_pack, wrote a good brief from the pack and stopped: no gate, no receipt,
no evidence view. The connect-time instructions said to continue; by the time a
pack of seventeen citations came back, that briefing was far away and the pack
looked like a finished answer. So each stage now carries its own next call, and
these tests hold that in place — including WHERE it sits in the payload, which is
the part that actually decides whether it gets read.
"""

import pytest

from app.mcp import assemble, feeds, loop, publish, record, store, verify


@pytest.fixture(scope="module")
def pack():
    return assemble.assemble(country="kenya", crop="maize")


def _passing_draft(p):
    """A minimal draft that satisfies the gate: every required section present,
    every paragraph cited, no self-written Sources."""
    return "\n\n".join(f"{s}\n\nEvidence for this section is cited here [1]."
                       for s in p["required_sections"])


def test_the_pack_says_it_is_not_an_answer(pack, log):
    """The failure mode was not disobedience, it was a pack that read as complete.
    The payload has to say, in the payload, that it is not."""
    log("OUTPUT", pack["answer_status"])
    assert "not an answer" in pack["answer_status"].lower()
    assert "publish_answer" in pack["answer_status"]


def test_the_next_call_sits_above_the_citations(pack, log):
    """Placement IS the fix. A model with everything it needs to write stops
    reading, and this pack carries 17 citations — guidance underneath them is
    guidance nobody sees. Assert the ORDER, not just the presence."""
    keys = list(pack)
    log("OUTPUT", " -> ".join(keys[:6]))
    assert keys.index("next_step") < keys.index("citations")
    assert keys.index("answer_status") < keys.index("citations")


def test_the_pack_names_the_next_call_with_real_arguments(pack, log):
    """'Then verify it' is advice. A tool name plus the actual pack_id is an
    instruction that can be followed without thinking."""
    nxt = pack["next_step"]
    log("OUTPUT", f"{nxt['call']['tool']}(pack_id={nxt['call']['args']['pack_id']})")
    assert nxt["required"] is True
    # ONE call, not two. Measured: with two steps left, no model tested completed
    # the loop; with one, gpt-4o and gpt-4o-mini both did.
    assert nxt["call"]["tool"] == "publish_answer"
    assert nxt["call"]["args"]["pack_id"] == pack["pack_id"]
    assert "then" not in nxt
    assert "verify_groundedness" in nxt["granular_alternative"]


def test_the_pack_states_what_skipping_costs(pack, log):
    """A step read as ceremony gets skipped. Name the loss instead."""
    cost = pack["next_step"]["if_you_stop_here"]
    log("OUTPUT", cost)
    assert "receipt" in cost and "replay" in cost


def test_guidance_is_never_persisted_as_evidence(pack, log):
    """next_step tells THIS caller what to do next; it is not part of the evidence
    and must not turn up inside a receipt's sources later."""
    stored = store.load_pack(pack["pack_id"])
    log("CHECK", f"stored pack keys: {sorted(stored)}")
    assert "next_step" not in stored and "answer_status" not in stored


def test_a_passing_verdict_points_at_the_receipt(pack, log):
    """Second seam. A verdict is a waypoint; on its own it ties to nothing."""
    v = verify.groundedness(_passing_draft(pack), pack["pack_id"])
    log("OUTPUT", f"passed={v['passed']} -> {v['next_step']['call']['tool']}")
    assert v["passed"] is True, v["failures"]
    assert v["next_step"]["call"]["tool"] == "record_receipt"
    assert v["next_step"]["call"]["args"]["report_id"] == v["report_id"]
    assert list(v).index("next_step") < list(v).index("failures")


def test_a_blocked_draft_is_never_sent_on_to_the_receipt(pack, log):
    """The forward edge must not push a failing draft onward — that would turn a
    nudge into a way to launder an ungrounded brief past the gate."""
    v = verify.groundedness("Maize will fail this season.", pack["pack_id"])
    log("OUTPUT", f"passed={v['passed']} -> {v['next_step']['step']}")
    assert v["passed"] is False
    assert v["answer_status"].startswith("BLOCKED")
    assert v["next_step"]["call"]["tool"] == "publish_answer"   # retry, not the receipt
    assert v["next_step"]["fix_then_retry"] == v["failures"]


def test_the_receipt_asks_to_be_SHOWN_not_described(pack, log):
    """Third seam, and the reason no visual ever appeared: the evidence view hangs
    off the receipt, so a loop that ends at the receipt ends in prose."""
    r = record.record(pack_id=pack["pack_id"], question="El Nino in Kenya")
    nxt = r["next_step"]
    log("OUTPUT", f"{nxt['call']['tool']}({nxt['call']['args']['component']})")
    assert nxt["call"] == {"tool": "ui_embed",
                           "args": {"component": "provenance_graph",
                                    "receipt_id": r["receipt_id"]}}
    # required=False is deliberate: a host that cannot render markup should not be
    # told to emit it — but it is told what to do instead, never "prose is fine".
    assert nxt["required"] is False
    assert "TABLE" in nxt["unless"] and "Never a paragraph" in nxt["unless"]


def test_every_stage_of_the_loop_hands_on_to_the_next(pack, log):
    """The regression that matters: no stage may go quiet. If a later refactor
    drops the forward edge from any seam, this fails."""
    v = verify.groundedness(_passing_draft(pack), pack["pack_id"])
    r = record.record(pack_id=pack["pack_id"], report_id=v["report_id"], question="q")
    chain = [pack["next_step"]["call"]["tool"], v["next_step"]["call"]["tool"],
             r["next_step"]["call"]["tool"]]
    log("OUTPUT", " -> ".join(["assemble_pack"] + chain))
    assert chain == ["publish_answer", "record_receipt", "ui_embed"]


def test_the_reminder_is_repeated_at_the_very_END_of_the_pack(pack, log):
    """Measured, not assumed. Head placement alone failed: gpt-4o read the whole
    payload and wrote the brief into its reply anyway. The text nearest the point
    of generation is the LAST thing in the result, so it is repeated there."""
    keys = list(pack)
    log("OUTPUT", f"last key: {keys[-1]}")
    assert keys[-1] == "your_next_output"
    assert "publish_answer" in pack["your_next_output"]
    assert pack["pack_id"] in pack["your_next_output"]


def test_publish_gates_and_receipts_in_one_call(pack, log):
    """The whole point of the composite: one call cannot end with a receipt for an
    ungated draft, and cannot end without one for a gated draft."""
    out = publish.answer(pack["pack_id"], _passing_draft(pack), question="q")
    log("OUTPUT", f"passed={out['passed']} receipt={out['receipt_id'][:12]}")
    assert out["status"] == "ok" and out["passed"] is True
    assert out["receipt_id"] and out["report_id"]
    assert out["render_with"]["provenance"]["tool"] == "ui_embed"


def test_publish_mints_NO_receipt_for_a_blocked_draft(pack, log):
    """The composite must not become a way around the gate. A real model hit this
    in UAT: its first draft was blocked, it fixed it, the second passed."""
    out = publish.answer(pack["pack_id"], "Maize will fail this season.", question="q")
    log("OUTPUT", f"status={out['status']} failures={out['failures']}")
    assert out["status"] == "blocked"
    assert "receipt_id" not in out
    assert out["failures"]
    assert out["next_step"]["call"]["tool"] == "publish_answer"   # fix and retry


def test_a_lookup_offers_the_way_into_the_loop(log):
    """gpt-4o-mini entered through corpus_search and answered straight from the
    passages — no pack, no gate, no receipt. A lookup is legitimate, so this is a
    route in rather than a refusal."""
    hinted = loop.with_entry_hint({"status": "ok", "hits": []})
    log("OUTPUT", hinted["for_a_governed_answer"]["call"]["tool"])
    assert hinted["for_a_governed_answer"]["call"]["tool"] == "assemble_pack"
    assert "not a governed answer" in hinted["for_a_governed_answer"]["note"]


def test_a_declined_lookup_is_not_hinted(log):
    """A decline's `note` is the honest cause and must not be crowded out by
    guidance about a tool that would decline for the same reason."""
    declined = {"status": "declined", "note": "unknown dataset"}
    log("CHECK", "declined result passes through untouched")
    assert loop.with_entry_hint(declined) == declined


def test_internal_callers_never_see_the_hint(monkeypatch, log):
    """The hint is attached at the TOOL boundary. synthesis calls feeds.query to
    build a pack; guidance mixed into that would end up inside the evidence.

    Stubbed at the adapter seam — this suite stays offline, and a test that reaches
    the real upstream is how live network calls crept in here once before."""
    monkeypatch.setitem(feeds.ADAPTERS, "climate_index", lambda params, spec: {
        "as_of": "MJJ 2026", "count": 1, "summary": "stub", "records": [{"v": 1}],
        "query_receipt": "stub", "url": "https://example.invalid", "stale_data": None})
    out = feeds.query("enso_oni")
    log("CHECK", f"feeds.query keys: {sorted(out)}")
    assert out["status"] == "ok"                 # the hint would apply if it were added
    assert "for_a_governed_answer" not in out


def test_the_app_strips_the_verdict_before_rendering(pack, log):
    """Rule 5 at the surface. `structuredContent` is ONE payload with two readers:
    the model needs `passed`, the iframe must never receive it, because a rendered
    surface freezes what is in it. Previously `evidence_payload` guarded only the
    standalone browser path, so on the real MCP Apps path the verdict rode straight
    into the renderer."""
    from app.mcp import app_ui
    out = publish.answer(pack["pack_id"], _passing_draft(pack), question="q")
    tmpl = app_ui.template()
    log("OUTPUT", f"tool result carries: {[k for k in app_ui._VERDICT_FIELDS if k in out]}")
    # the MODEL still gets the verdict — removing it would be lying to the caller
    assert out["passed"] is True and out["report_id"]
    # ...and the renderer removes every one of them before drawing
    for field in app_ui._VERDICT_FIELDS:
        assert f'"{field}"' in tmpl.split("const VERDICT_FIELDS = ")[1].split(";")[0]
    assert "render(strip(d))" in tmpl


def test_the_apps_field_list_is_generated_not_retyped(log):
    """A hand-copied list in the JS would drift from the Python tuple the moment a
    field is added, and the drift would be invisible until a verdict rendered."""
    import json as _json
    from app.mcp import app_ui
    injected = app_ui.template().split("const VERDICT_FIELDS = ")[1].split(";")[0]
    log("OUTPUT", injected)
    assert _json.loads(injected) == list(app_ui._VERDICT_FIELDS)
