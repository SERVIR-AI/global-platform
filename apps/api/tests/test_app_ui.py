"""The MCP Apps evidence surface.

Registered as a `ui://` resource with `text/html;profile=mcp-app`, so a host that
advertises `io.modelcontextprotocol/ui` renders it in a sandboxed iframe beside the
tool result. That is the point: it does not depend on the model choosing to draw
something, which is what failed when we only asked in the instructions.
"""

import json

from app.mcp import app_ui


def _receipt(**over):
    d = {"question": "El Nino in Kenya", "receipt_id": "abc123",
         "public_resolver": "http://host/api/resolve/receipt/abc123",
         "passed": True, "evidence_tier": "platform-registered",
         "sources": [{"n": 1, "source": "NOAA CPC", "title": "ONI", "pub_date": "MJJ 2026",
                      "validation": "single-agency"},
                     {"n": 2, "source": "FEWS NET", "title": "outlook", "pub_date": "2015-10",
                      "validation": "single-agency"}],
         "evidence_freshness": {"pulled_sources": [1]},
         "gaps": ["no 2026 Kenya outlook"]}
    d.update(over)
    return d


def test_registered_with_the_mcp_app_mime_type(log):
    """The mime type IS the contract: without `;profile=mcp-app` a host treats it as
    a plain HTML resource and never renders it as an app."""
    log("OUTPUT", f"{app_ui.UI_URI} -> {app_ui.UI_MIME}")
    assert app_ui.UI_MIME == "text/html;profile=mcp-app"
    assert app_ui.UI_URI.startswith("ui://")


def test_the_resource_is_actually_on_the_server(log):
    from app.mcp.server import mcp
    uris = [str(r.uri) for r in mcp._resource_manager.list_resources()]
    log("OUTPUT", str(uris))
    assert app_ui.UI_URI in uris


def test_it_never_renders_a_verdict(log):
    """A rendered surface freezes what is in it, and a frozen verdict attests
    nothing (rule 5). Evidence is safe to freeze; the verdict stays a live link."""
    html = app_ui.standalone(_receipt())
    body = html.split("</style>", 1)[1].lower()
    # The one legitimate mention of these names is the guard that DELETES them
    # (`const VERDICT_FIELDS = [...]`), added when we found the live MCP Apps path
    # was unguarded. Excise the declaration, then hold the original line: nowhere
    # else may a verdict field appear.
    guard = body.split("const verdict_fields = ", 1)[1].split(";", 1)[0]
    body = body.replace(guard, "")
    log("CHECK", "no verdict in the markup OR the payload, guard aside")
    # Not just undrawn: the verdict must not be in the payload either, or it is one
    # line of markup away from being frozen into the surface.
    assert "passed" not in body and "verified_text" not in body
    assert "evidence_tier" not in body and "draft_sha256" not in body
    assert "attests nothing" in body          # it says WHY it is absent
    assert "/api/resolve/receipt/abc123" in html   # and links the live resolver


def test_it_shows_pulled_versus_archived_and_the_gaps(log):
    """The three things prose buries: how old each source is, which were pulled
    live, and what is missing."""
    html = app_ui.standalone(_receipt())
    payload = json.loads(html.split('id="payload">', 1)[1].split("</script>", 1)[0])
    log("OUTPUT", f"pulled={payload['evidence_freshness']['pulled_sources']}")
    assert payload["evidence_freshness"]["pulled_sources"] == [1]
    assert payload["gaps"] == ["no 2026 Kenya outlook"]
    assert "pulled live" in html and "archived" in html and "What is missing" in html


def test_it_inherits_the_platform_palette(log):
    """Styled from our own tokens, so an app cannot drift from the design language
    a builder is simultaneously told to use."""
    html = app_ui.template()
    log("CHECK", "grp custom properties present")
    assert "--grp-" in html


def test_a_broken_theme_does_not_break_the_app(monkeypatch, log):
    """ui_design can fall back to config; this must survive worse than that."""
    monkeypatch.setattr(app_ui.ui, "css_vars",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no theme")))
    html = app_ui.template()
    log("CHECK", "still renders with a minimal palette")
    assert "--grp-base-100" in html and len(html) > 500


def test_tool_descriptions_name_their_subject_without_hardcoding_it(log):
    """A model picks a tool from its first line, so that line must name the subject
    in a user's words. But a hand-kept keyword list goes stale the moment a feed or
    a pack is added — which is the drift this registry prevents everywhere else.
    So the subject line is GENERATED from live state.
    """
    from app.mcp import registry
    line = registry.coverage_line()
    log("OUTPUT", line[:110])
    # names what we actually have, read from the registry and the corpus
    assert "Kenya" in line and "maize" in line
    assert any(t in line for t in ("Oceanic Nino Index", "Dipole Mode Index"))


def test_a_new_feed_appears_in_the_description_with_no_code_change(monkeypatch, log):
    """The point of generating it: add a capability, and the tool that should be
    chosen for it starts saying so by itself."""
    from app.mcp import registry
    before = registry.describe_assemble()
    monkeypatch.setitem(registry.FEEDS, "chirps_rainfall",
                        {**registry.FEEDS["chirps_rainfall"], "status": "available",
                         "title": "CHIRPS rainfall", "source": "Climate Hazards Center"})
    after = registry.describe_assemble()
    log("OUTPUT", "CHIRPS now advertised: " + str("CHIRPS rainfall" in after))
    assert "CHIRPS rainfall" not in before and "CHIRPS rainfall" in after


def test_assemble_pack_tells_a_model_to_prefer_it_over_web_search(log):
    """The failure this fixes: asked about El Nino with the server connected, Claude
    ran a web search, because no description mentioned the subject at all."""
    from app.mcp import registry
    d = registry.describe_assemble()
    log("CHECK", d.splitlines()[0][:70])
    assert "START HERE" in d and "INSTEAD OF A WEB SEARCH" in d
    assert "what is MISSING" in d          # says what a web search cannot give


def test_the_view_opens_the_handshake_with_a_ui_initialize_REQUEST(log):
    """The bug that made Claude Desktop render a blank panel. Spec 2026-01-26:
    "The Host MUST NOT send any request or notification to the View before it
    receives an `initialized` notification" — and `initialized` is only valid after
    a `ui/initialize` REQUEST completes. We posted the notification unilaterally and
    never opened the request, so the host correctly sent nothing and the app sat on
    "loading..." forever."""
    html = app_ui.template()
    log("CHECK", "ui/initialize is sent as a request, with a JSON-RPC envelope")
    assert 'request("ui/initialize"' in html
    assert 'jsonrpc: "2.0", id, method, params' in html     # a REQUEST: it carries an id
    assert '"2026-01-26"' in html                           # the protocol version
    # the notification is now a RESPONSE to the host, not an opening move
    assert 'function ready()' in html and 'ui/notifications/initialized' in html


def test_it_declares_app_capabilities_both_ways(log):
    """The spec's normative text names `appCapabilities`; its own sample code uses
    MCP-style `capabilities`/`clientInfo`. A missing field hangs the handshake; an
    extra one is ignored — so send both rather than bet on the host."""
    html = app_ui.template()
    log("CHECK", "appCapabilities AND capabilities/clientInfo present")
    assert "appCapabilities" in html and "availableDisplayModes" in html
    assert "clientInfo" in html and "capabilities: {}" in html


def test_it_reads_the_tool_result_where_the_spec_puts_it(log):
    """`ui/notifications/tool-result` carries a raw CallToolResult as `params`, so
    the payload is `params.structuredContent`."""
    html = app_ui.template()
    log("CHECK", "params.structuredContent read from the notification")
    assert 'm.method === "ui/notifications/tool-result"' in html
    assert "m.params?.structuredContent" in html


def test_sources_carry_a_platform_absolute_archived_link(monkeypatch, log):
    """A relative archived path resolves against whatever origin is RENDERING —
    an MCP App iframe or a consumer's page, neither of which is us. So it 404s
    exactly where the trace-back matters most.

    Built from a hand-made pack, not assemble(): retrieval needs the embeddings API,
    and a suite that reaches the network fails on someone else's timeout.
    """
    from app.mcp import record, store
    monkeypatch.setenv("GRP_PUBLIC_BASE", "http://10.1.30.110:8080")
    pack_id = store.save_pack({
        "country": "Kenya", "crop": "maize", "citations": [
            {"n": 1, "kind": "document", "source": "FEWS NET", "title": "outlook",
             "archived_copy": "/api/food-security/rag/document/abc123",
             "url": "https://fews.net/x.pdf"},
            {"n": 2, "kind": "index", "source": "NOAA CPC", "title": "ONI",
             "url": "https://cpc.example/oni.txt"}],
        "gaps": [], "required_sections": ["## A"]})
    r = record.record(pack_id=pack_id, question="q")
    docs = [s for s in r["sources"] if s.get("archived_copy")]
    log("OUTPUT", docs[0]["archived_url"])
    assert docs, "expected the document source to carry an archived copy"
    assert docs[0]["archived_url"] == (
        "http://10.1.30.110:8080/api/food-security/rag/document/abc123")
    # a live pull has no archived document, and must not invent one
    assert "archived_url" not in [s for s in r["sources"] if s["n"] == 2][0]


def test_links_go_through_the_host_not_the_iframe(log):
    """A sandboxed iframe cannot navigate the parent, so `<a target="_blank">` does
    nothing — the receipt link and every source were dead on Desktop. The spec's
    `ui/open-link` is the only route, gated by hostCapabilities.openLinks."""
    html = app_ui.template()
    log("CHECK", "ui/open-link used, capability-gated, with a standalone fallback")
    assert 'request("ui/open-link", { url: url })' in html
    assert "_caps.openLinks" in html
    assert "window.open(url" in html          # standalone browser view still works
    assert "wireLinks()" in html


def test_every_source_card_offers_its_document(log):
    """Provenance you cannot open is a claim, not a trace."""
    html = app_ui.template()
    log("CHECK", "archived copy + upstream source on each card")
    assert "archived copy" in html and ">source</a>" in html
    assert 'data-url="${esc(s.archived_url)}"' in html


def test_a_link_response_does_not_wipe_host_capabilities(log):
    """The "works once or twice then stops" bug. `_pending` was a bare Set of ids, so
    EVERY response was handled as the initialize response — and `ui/open-link` acks
    with an empty result, so `_caps = m.result.hostCapabilities || {}` erased
    openLinks after the first click. Links then fell through to window.open, which a
    sandboxed iframe blocks. request-display-mode broke it the same way."""
    html = app_ui.template()
    log("CHECK", "responses are dispatched by the METHOD that was requested")
    assert "const _pending = new Map()" in html          # id -> method, not a bare Set
    assert 'const method = _pending.get(m.id)' in html
    # capabilities may only be adopted from the handshake response
    body = html.split('const method = _pending.get(m.id)')[1]
    caps_line = body.index("_caps = m.result.hostCapabilities")
    init_guard = body.index('method === "ui/initialize"')
    assert init_guard < caps_line, "capabilities must be set inside the init branch"


def test_a_blocked_link_says_so_instead_of_looking_dead(log):
    """A host may deny ui/open-link. Silence is indistinguishable from a broken app."""
    html = app_ui.template()
    log("CHECK", "denied and unsupported links both surface a note with the URL")
    assert 'method === "ui/open-link" && m.error' in html
    assert "Link blocked by the host" in html
    assert "will not open links from the panel" in html


def test_the_charts_are_interactive(log):
    """User: the graphs should be apps with clickers and pickers, not pictures."""
    html = app_ui.template()
    log("CHECK", "range picker, point picker, threshold toggle, live readout")
    # chips are BUILT by chip(i, act, ...) at runtime; the source carries the acts
    for act in ('"range-12"', '"range-60"', '"table"', '"bands"', '"src-live"'):
        assert f"chip(i, {act}" in html, act
    assert "data-act=" in html and "svg.cv" in html   # unified dispatch + svg click-to-read
    assert "redrawChart" in html and "wireCharts()" in html
    assert 'class="readout"' in html
    # a redraw must re-report height or the host keeps the old box
    assert "reportSize();" in html.split("function redrawChart")[1][:400]


def test_esc_closes_the_attribute_injection_hole(log):
    """CONFIRMED by adversarial review: esc() covered only <>& while its output
    lands inside double-quoted attributes (href, data-url, aria-label) — and titles
    and URLs come from external feeds and corpus documents. A title of
    x" onpointerover="... closed the attribute and injected a handler; script in
    the panel can reach tools/call. Quotes are now escaped."""
    html = app_ui.template()
    log("CHECK", "quote characters in the esc character class")
    esc_block = html.split("const esc")[1][:300]   # entities contain ';', so no split on it
    assert "&quot;" in esc_block and "&#39;" in esc_block


def test_the_view_answers_ping_and_swallows_no_host_request(log):
    """CONFIRMED by review: messages were classified by id alone, so a host request
    whose id collided with ours was consumed as if it answered us — and no host
    request was ever answered, so JSON-RPC `ping` liveness read the View as dead."""
    html = app_ui.template()
    log("CHECK", "structural classification + ping response")
    assert 'm.method === "ping"' in html
    assert '"not implemented: " + m.method' in html
    assert "!m.method && m.id && _calls.has(m.id)" in html


def test_svg_click_survives_letterboxing(log):
    """CONFIRMED by review: width-proportional click math is only correct at or
    under 560px; Desktop's 736px box letterboxes the chart and the readout named
    an earlier month than the one clicked. getScreenCTM maps exactly."""
    html = app_ui.template()
    log("CHECK", "getScreenCTM used with a proportional fallback")
    assert "getScreenCTM" in html and "matrixTransform" in html
    assert "getBoundingClientRect" in html


def test_standalone_payload_cannot_break_out_of_its_script_block(log):
    """CONFIRMED by review: json.dumps leaves "</" intact, so a title containing
    "</script>" terminated the payload block at HTML-parse time and executed what
    followed. "<\\/" is legal JSON and identical after parsing."""
    import json as _json
    h = app_ui.standalone({"question": "q",
                           "sources": [{"n": 1, "title": "</script><script>evil()</script>"}]})
    seg = h.split('id="payload">')[1]
    payload = seg[:seg.index("</script>")]
    d = _json.loads(payload)
    log("OUTPUT", d["sources"][0]["title"])
    assert d["sources"][0]["title"] == "</script><script>evil()</script>"


def test_a_blocked_publish_renders_the_refusal_not_loading_forever(log):
    """Desktop mounts the panel for EVERY publish_answer call. A blocked result has
    no sources and no question, so the render guard never fired and the panel sat
    on 'loading…' — seen live on Desktop 2026-08-27. A refusal is a first-class
    display state: the gate refusing is the platform working."""
    html = app_ui.template()
    log("CHECK", "blocked/declined route to renderRefusal before the sources guard")
    assert "renderRefusal" in html
    assert 'd.status === "blocked" || d.status === "declined"' in html
    # the refusal is deliberately QUIET now (user: mid-retry blocks are workflow
    # noise) — one line, details collapsed, but still shown, never suppressed
    assert "Draft blocked by the groundedness gate" in html
    assert "what failed" in html
