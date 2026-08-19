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
