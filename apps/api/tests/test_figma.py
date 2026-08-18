"""Live design tokens from Figma. Offline: the HTTP layer is stubbed.

The file is read through the document tree because Figma's Variables API is
Enterprise-only and this plan does not offer the scope. That inference is lossy,
so these tests pin what the reader must NOT do: guess, or hide what it could not
resolve.
"""

import pytest

from app.mcp import figma


@pytest.fixture(autouse=True)
def _no_cache():
    """The Figma cache is module-level, so one test's stubbed file would answer the
    next test's call. Same footgun the climate-index tests guard against."""
    figma._CACHE.clear()
    yield
    figma._CACHE.clear()


def _doc(children, styles=None):
    return {"name": "SERVIR Design System", "version": "123", "lastModified": "2026-08-17T15:01:07Z",
            "styles": styles or {},
            "document": {"type": "DOCUMENT", "children": children}}


def _text(chars, x=0, y=0):
    return {"type": "TEXT", "characters": chars,
            "absoluteBoundingBox": {"x": x, "y": y, "width": 57, "height": 30}}


def _rect(hexv, x=0, y=0):
    r, g, b = (int(hexv[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return {"type": "RECTANGLE", "fills": [{"color": {"r": r, "g": g, "b": b}}],
            "absoluteBoundingBox": {"x": x, "y": y, "width": 56, "height": 56}}


def _stub(monkeypatch, doc):
    monkeypatch.setattr(figma, "_get", lambda path, token: doc)
    monkeypatch.setattr(figma, "get_settings",
                        lambda: type("S", (), {"figma_token": "t", "figma_file_key": "k"})())


def test_label_text_is_the_declaration_not_the_swatch_geometry(monkeypatch, log):
    """Swatches sit BETWEEN labels in this file, so one label's swatch is to its
    right and the next one's is to its left. Pairing by nearest-rectangle looked
    obvious and produced Servir_Gray=#3DB4F2, which is the light blue. The label
    carries name AND hex, so it is the thing to trust."""
    _stub(monkeypatch, _doc([{"type": "CANVAS", "name": "Style", "children": [
        _text("Servir_Gray\n#6D6E71", x=1951), _rect("#6D6E71", x=2015),
        _rect("#3B3A3A", x=2071), _text("Dark_gray\n#3B3A3A", x=2141)]}]))
    out = figma.read_tokens()
    log("OUTPUT", str(out["colors"]))
    assert out["colors"] == {"Servir_Gray": "#6D6E71", "Dark_gray": "#3B3A3A"}


def test_a_label_naming_a_colour_nothing_renders_is_reported(monkeypatch, log):
    """Cross-check against the page rather than a guessed pairing: a hex that
    appears on no swatch is drift between what was written and what is shown."""
    _stub(monkeypatch, _doc([{"type": "CANVAS", "name": "Style", "children": [
        _text("Ghost\n#ABCDEF"), _rect("#6D6E71")]}]))
    out = figma.read_tokens()
    kinds = [c["kind"] for c in out["conflicts"]]
    log("OUTPUT", str(out["conflicts"]))
    assert "value_not_rendered" in kinds
    assert out["colors"]["Ghost"] == "#ABCDEF"      # still returned, just flagged


def test_duplicate_and_malformed_are_surfaced_not_resolved(monkeypatch, log):
    """Both are really in the file: two names on #FAFAFA, and one hex typed with a
    doubled hash. The platform declares what it cannot resolve."""
    _stub(monkeypatch, _doc([{"type": "CANVAS", "name": "Style", "children": [
        _text("Light_gray\n#FAFAFA"), _text("White\n#FAFAFA"),
        _text("Servir-Light_blue\n##3DB4F2"),
        _rect("#FAFAFA"), _rect("#3DB4F2")]}]))
    out = figma.read_tokens()
    kinds = {c["kind"] for c in out["conflicts"]}
    log("OUTPUT", str(sorted(kinds)))
    assert {"duplicate_value", "malformed_label"} <= kinds
    # both names survive; nothing is silently dropped
    assert out["colors"]["Light_gray"] == out["colors"]["White"] == "#FAFAFA"
    assert out["colors"]["Servir-Light_blue"] == "#3DB4F2"


def test_provenance_states_the_method_and_its_limits(monkeypatch, log):
    """A consumer must be able to see that these were inferred from a rendered
    page, not read from declared variables."""
    _stub(monkeypatch, _doc([{"type": "CANVAS", "name": "Style", "children": []}]))
    p = figma.read_tokens()["provenance"]
    log("OUTPUT", p["method"])
    assert p["method"] == "document-tree inference"
    assert "Enterprise-only" in p["caveat"]
    assert p["last_modified"] and p["file_key"] and p["fetched_at"]


def test_an_upstream_failure_raises_rather_than_returning_empty(monkeypatch, log):
    """ui_design must never silently serve an empty palette because Figma was down.
    Callers fall back to config on this exception."""
    def boom(path, token):
        raise figma.FigmaUnavailable("figma returned HTTP 500 for files/k")
    monkeypatch.setattr(figma, "_get", boom)
    monkeypatch.setattr(figma, "get_settings",
                        lambda: type("S", (), {"figma_token": "t", "figma_file_key": "k"})())
    with pytest.raises(figma.FigmaUnavailable):
        figma.read_tokens()
    log("CHECK", "raises, so the caller can fall back")


def test_no_token_configured_declines_clearly(monkeypatch, log):
    monkeypatch.setattr(figma, "get_settings",
                        lambda: type("S", (), {"figma_token": None, "figma_file_key": "k"})())
    with pytest.raises(figma.FigmaUnavailable, match="no FIGMA_TOKEN"):
        figma.read_tokens()
    log("CHECK", "names the missing setting")


def test_design_falls_back_to_config_when_figma_is_down(monkeypatch, log):
    """ui_design must never fail and never serve an empty palette. A consumer
    mid-build gets the committed theme instead, and is TOLD that is what happened."""
    from app.mcp import ui
    monkeypatch.setattr(figma, "read_tokens",
                        lambda *a, **k: (_ for _ in ()).throw(figma.FigmaUnavailable("down")))
    out = ui.design("tokens")
    log("OUTPUT", str(out["design_source"]))
    assert out["status"] == "ok"
    assert out["design_source"]["source"] == "config"
    assert "not the live design file" in out["design_source"]["note"]
    assert out["tokens"]["palette"]                     # never empty
    assert "brand_colors" not in out["tokens"]          # no stale live data pretending


def test_design_says_which_source_it_served(monkeypatch, log):
    """The fallback is silent in BEHAVIOUR and loud in the PAYLOAD. A builder must
    never have to guess whether they got the live file or a copy."""
    from app.mcp import ui
    monkeypatch.setattr(figma, "read_tokens", lambda *a, **k: {
        "colors": {"Servir_Blue": "#2380B0"}, "declared_styles": [],
        "conflicts": [{"kind": "duplicate_value", "name": "White", "detail": "x"}],
        "provenance": {"file": "SERVIR Design System", "last_modified": "2026-08-17T15:01:07Z"}})
    out = ui.design("tokens")
    src = out["design_source"]
    log("OUTPUT", str(src))
    assert src["source"] == "figma-live" and src["file"] == "SERVIR Design System"
    assert len(src["conflicts"]) == 1               # conflicts travel to the consumer
    assert out["tokens"]["brand_colors"] == {"Servir_Blue": "#2380B0"}


def test_live_colours_do_not_overwrite_semantic_roles(monkeypatch, log):
    """`palette` is the role map every component is built on. Letting a colour page
    rename roles would restyle the whole UI the moment a designer edits a swatch."""
    from app.mcp import ui
    monkeypatch.setattr(figma, "read_tokens", lambda *a, **k: {
        "colors": {"primary": "#FF0000"}, "declared_styles": [], "conflicts": [],
        "provenance": {"file": "f", "last_modified": "x"}})
    out = ui.design("tokens")
    log("OUTPUT", f"palette primary stays {out['tokens']['palette']['primary']}")
    assert out["tokens"]["palette"]["primary"] != "#FF0000"
    assert out["tokens"]["brand_colors"]["primary"] == "#FF0000"   # available, separate


def _comp(name, node_id):
    return {"type": "COMPONENT", "name": name, "id": node_id}


def test_variant_properties_are_parsed_not_string_matched(monkeypatch, log):
    """Figma encodes variants in the NAME: "Treatment=Black, Layout=Stacked". A
    builder must be able to ask for a treatment, not match a string."""
    _stub(monkeypatch, _doc([{"type": "CANVAS", "name": "Style", "id": "0:1", "children": [
        {"type": "COMPONENT_SET", "name": "SERVIR/Logo/Lockup", "id": "1:1", "children": [
            _comp("Treatment=Primary, Layout=Horizontal", "1:2"),
            _comp("Treatment=Reversed Mono, Layout=Stacked", "1:3")]}]}]))
    sets = figma.read_components()["sets"]
    log("OUTPUT", str(sets[0]["variants"]))
    assert sets[0]["name"] == "SERVIR/Logo/Lockup"
    assert {"treatment": "Primary", "layout": "Horizontal"}.items() <= sets[0]["variants"][0].items()


def test_ambiguous_variant_request_declines_rather_than_picking(monkeypatch, log):
    """Two variants match a partial request. Returning either would hand a builder a
    logo they did not ask for, on a ground it may not suit."""
    from app.mcp import ui
    monkeypatch.setattr(figma, "read_components", lambda *a, **k: {"sets": [
        {"name": "L", "node_id": "1:1", "variants": [
            {"node_id": "1:2", "treatment": "Primary", "layout": "Horizontal"},
            {"node_id": "1:3", "treatment": "Primary", "layout": "Stacked"}]}]})
    out = ui.component("L", treatment="Primary")
    log("OUTPUT", out["note"])
    assert out["status"] == "declined" and "narrow it" in out["note"]
    assert len(out["available_variants"]) == 2


def test_a_rendered_asset_states_that_its_url_expires(monkeypatch, log):
    """Figma image URLs expire. A page that hotlinks one breaks silently weeks later,
    so the expiry ships with the asset."""
    from app.mcp import ui
    monkeypatch.setattr(figma, "read_components", lambda *a, **k: {"sets": [
        {"name": "L", "node_id": "1:1",
         "variants": [{"node_id": "1:2", "treatment": "Black"}]}]})
    monkeypatch.setattr(figma, "render", lambda ids, fmt="svg", **k: {
        "images": {"1:2": "https://figma/x.svg"},
        "caveat": "These URLs are generated by Figma and EXPIRE (~30 days)."})
    out = ui.component("L", treatment="Black")
    log("OUTPUT", out["caveat"][:60])
    assert out["status"] == "ok" and "EXPIRE" in out["caveat"]
    assert out["trust_class"] == "presentational"   # carries no verdict


def test_catalog_still_lists_our_recipes_when_figma_is_down(monkeypatch, log):
    """Our own components are unaffected by a third party being unreachable."""
    from app.mcp import ui
    monkeypatch.setattr(figma, "read_components",
                        lambda *a, **k: (_ for _ in ()).throw(figma.FigmaUnavailable("down")))
    out = ui.catalog()
    log("OUTPUT", out["design_file_components"]["note"][:70])
    assert out["status"] == "ok" and out["components"]
    assert out["design_file_components"]["source"] == "unavailable"


def test_cache_serves_last_good_when_figma_breaks(monkeypatch, log):
    """A ten-minute-old design file beats dropping the caller to a config copy of
    unknown age — but it must say it is stale."""
    calls = {"n": 0}
    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise figma.FigmaUnavailable("figma returned HTTP 500")
        return {"colors": {"a": "#000000"}}
    first = figma.cached("k", flaky)
    second = figma.cached("k", flaky, ttl=0)      # force a refetch, which fails
    log("OUTPUT", str(second["cache"]))
    assert first["cache"]["served_stale"] is False
    assert second["cache"]["served_stale"] is True and second["colors"] == {"a": "#000000"}
    assert "unreachable" in second["cache"]["reason"]


def test_live_type_scale_reaches_the_consumer(monkeypatch, log):
    """Caught by running the real thing, not by a test: the reader extracted 10 type
    styles and ui.design surfaced the config's 3, because the live scale was never
    wired through. Both are present now, and separate."""
    from app.mcp import ui
    monkeypatch.setattr(figma, "read_tokens", lambda *a, **k: {
        "colors": {}, "declared_styles": [], "conflicts": [],
        "typography": [{"family": "Roboto Condensed", "weight": 300, "size_px": 36}],
        "provenance": {"file": "f", "last_modified": "x"}})
    out = ui.design("tokens")["tokens"]
    log("OUTPUT", f"roles={len(out['typography'])} scale={len(out['design_file_type_scale'])}")
    assert out["design_file_type_scale"][0]["size_px"] == 36
    assert out["typography"] and out["typography"] is not out["design_file_type_scale"]
