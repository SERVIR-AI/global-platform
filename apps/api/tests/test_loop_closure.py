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


def test_the_receipt_guidance_depends_on_the_host(pack, log):
    """This USED to say "call ui_embed" unconditionally — and on Claude Desktop the
    model obeyed, pasting an <iframe> into chat, which the host CSP
    (frame-src 'self' blob: data:) blocked into a giant blank white box. In an
    MCP-Apps host the panel has ALREADY rendered; the iframe is for pages a model
    is writing. So the guidance now branches on the host."""
    r = record.record(pack_id=pack["pack_id"], question="El Nino in Kenya")
    nxt = r["next_step"]
    log("OUTPUT", nxt["step"])
    assert nxt["required"] is False
    assert "Do NOT paste iframe markup" in nxt["if_host_renders_mcp_apps"]
    web = nxt["if_building_a_web_page"]
    assert web["call"] == {"tool": "ui_embed",
                           "args": {"component": "provenance_graph",
                                    "receipt_id": r["receipt_id"]}}
    assert "TABLE" in nxt["if_neither"]
    assert "blank box" in loop.RECEIPT_NEEDS_SHOWING


def test_every_stage_of_the_loop_hands_on_to_the_next(pack, log):
    """The regression that matters: no stage may go quiet. If a later refactor
    drops the forward edge from any seam, this fails."""
    v = verify.groundedness(_passing_draft(pack), pack["pack_id"])
    r = record.record(pack_id=pack["pack_id"], report_id=v["report_id"], question="q")
    chain = [pack["next_step"]["call"]["tool"], v["next_step"]["call"]["tool"]]
    log("OUTPUT", " -> ".join(["assemble_pack"] + chain) + " -> host-aware display")
    assert chain == ["publish_answer", "record_receipt"]
    # the last seam no longer commands a call — it branches on the host, and the
    # web-page branch still names ui_embed with the real receipt id
    assert r["next_step"]["if_building_a_web_page"]["call"]["tool"] == "ui_embed"


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


def test_publish_carries_the_platform_execution_trace(pack, log):
    """A published answer says HOW the loop ran, not only what it produced: the
    gather is timed on the pack, the gate and mint are timed at publish, and the
    consumer's drafting step is DECLARED as outside the platform rather than
    silently missing — the attestation boundary is stated, not implied."""
    out = publish.answer(pack["pack_id"], _passing_draft(pack), question="q")
    t = out["trace"]
    names = [st["step"] for st in t["steps"]]
    log("OUTPUT", f"steps={names}")
    assert names == ["assemble", "draft", "verify", "record"]
    by = {st["step"]: st for st in t["steps"]}
    assert by["assemble"]["duration_ms"] is not None          # timed at gather
    assert by["assemble"]["detail"]                           # assembly trace strings
    assert by["draft"]["outside_platform"] is True
    assert by["draft"]["duration_ms"] is None                 # declared, not measured
    assert by["verify"]["duration_ms"] >= 0
    assert by["record"]["duration_ms"] >= 0
    assert out["receipt_id"] in by["record"]["summary"]


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


def test_trust_chrome_can_be_pointed_at_the_deployed_platform(monkeypatch, log):
    """The committed theme holds LOCALHOST values, which are right for dev and wrong
    the moment the image is shared: `COPY conf ./conf` baked them in, so the
    deployed server handed every consumer a resolver URL pointing at THEIR OWN
    machine. Dead resolve links, dead embeds, no provenance graph — rule 5 says the
    verdict resolves against the PLATFORM, so the platform must know its address."""
    from app.mcp import ui
    monkeypatch.setenv("GRP_PUBLIC_BASE", "http://10.1.30.110:8080/")
    t = ui.tokens()
    log("OUTPUT", t["product"]["resolver"]["base"])
    assert t["product"]["resolver"]["base"] == "http://10.1.30.110:8080"   # trailing / trimmed
    assert t["product"]["embed_base"]["url"] == "http://10.1.30.110:8080"
    assert ui.embed("provenance_graph", receipt_id="x")["src"].startswith(
        "http://10.1.30.110:8080/?embed=")


def test_localhost_stays_the_default_for_development(log):
    """Unset means dev: the override must not become a required variable."""
    from app.mcp import ui
    log("CHECK", "no GRP_PUBLIC_BASE -> committed theme values")
    assert ui.tokens()["product"]["resolver"]["base"].startswith("http://localhost")


def test_series_id_is_the_feeds_query_dataset_key(log):
    """CROSS-BOUNDARY CONTRACT the analytics panel depends on: `insight.series[].id`
    must be usable verbatim as feeds_query's `dataset` argument — the app's
    "full history" chip calls feeds_query(series.id). Rename a feed key without
    this test and the button breaks silently, only at click time, only in a host
    that proxies tool calls."""
    from app.food_security import synthesis
    from app.mcp import registry
    sr = synthesis._series("enso_oni", [
        {"season": "MJJ", "year": 2026, "value": 1.39, "classification": "moderate El Nino"},
        {"season": "AMJ", "year": 2026, "value": 0.95, "classification": "weak El Nino"}])
    log("OUTPUT", f"series.id = {sr['id']}")
    assert sr["id"] == "enso_oni"
    assert sr["id"] in registry.FEEDS and registry.FEEDS[sr["id"]]["status"] == "available"
    assert "limit" in (registry.FEEDS[sr["id"]].get("params") or {})


def test_the_panel_is_capability_gated_analytics(log):
    """User: a proper analytics tool. The unlock is tools/call through the host —
    and it must be GATED on hostCapabilities.serverTools, with the pack snapshot
    still fully usable when the host does not proxy."""
    from app.mcp import app_ui
    html = app_ui.template()
    log("CHECK", "bridge, gating, live-vs-pack honesty, analogues, overlay, stats")
    assert 'method: "tools/call"' in html
    assert "serverToolsOK" in html and "_caps && _caps.serverTools" in html
    # live pulls are labelled as OUTSIDE the receipt — the trust distinction
    assert "not attested by it" in html
    assert "enso_event_history" in html          # analogue events
    assert "overlayCard" in html and "read co-movement" in html
    assert "mean" in html and "vs prev" in html  # stats strip
    # degradation is stated, not silent
    assert "host does not proxy tool calls" in html


@pytest.fixture(scope="module")
def risk_pack():
    """The risk pack always DECLARES gaps (no corpus, no vintages...) — the pack
    where the citable-gaps contract is always exercised."""
    return assemble.assemble(pack="risk", place="battambang", hazard="flood")


def test_declared_gaps_are_a_citable_pack_entry(risk_pack, pack, log):
    """Models kept writing honest what's-missing paragraphs the gate then blocked
    as uncited: gaps were content with no citable identity. They are now the pack's
    LAST citation, and the draft rules name it. A pack with NO gaps (FS kenya/maize
    today) gets no such entry — an empty gaps citation would be noise."""
    last = risk_pack["citations"][-1]
    log("OUTPUT", f"[{last['n']}] kind={last['kind']} retrieval={last['retrieval']}")
    assert risk_pack["gaps"], "fixture must declare gaps"
    assert last["kind"] == "gaps"
    assert last["retrieval"] == "config"
    for g in risk_pack["gaps"]:
        assert g in last["text"]                       # gaps verbatim -> number-scan
    ns = [c["n"] for c in risk_pack["citations"]]
    assert len(ns) == len(set(ns)) and last["n"] == max(ns)
    rules = " ".join(risk_pack["next_step"]["draft_rules"])
    assert f"[{last['n']}]" in rules                   # the drafter is told
    assert not pack["gaps"] and pack["citations"][-1]["kind"] != "gaps"


def test_a_gaps_only_missing_section_passes_the_gate(risk_pack, log):
    """The acceptance case for citable gaps: a draft whose what's-missing paragraph
    cites ONLY the gaps entry must pass — this exact draft was blocked before."""
    gaps_n = risk_pack["citations"][-1]["n"]
    parts = []
    for s in risk_pack["required_sections"]:
        if "missing" in s.lower():
            parts.append(f"{s}\n\nThe pack itself declares what is absent [{gaps_n}].")
        else:
            parts.append(f"{s}\n\nEvidence for this section is cited here [1].")
    v = verify.groundedness("\n\n".join(parts), risk_pack["pack_id"])
    log("OUTPUT", f"passed={v['passed']} failures={v['failures']}")
    assert v["passed"] is True
