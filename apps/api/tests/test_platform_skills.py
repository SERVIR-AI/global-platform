"""The trace disciplines travel WITH the platform — served whole over MCP.

A builder connected over MCP cannot read this repository; if the skill does not
arrive complete (SKILL.md + references + assets), the guidance is a broken tool.
"""

import asyncio

from app.mcp import skills


def test_served_skills_bundle_whole(log):
    for name in skills.SERVED:
        b = skills.bundle(name)
        log("OUTPUT", f"{name}: {len(b)//1024} KB, {b.count('===== FILE:')} files")
        assert "SKILL.md" in b
        assert b.count("===== FILE:") >= 3      # front page + references at least
        assert "references/" in b               # the declared dependencies made it


def test_skill_resources_registered(log):
    from app.mcp.server import mcp
    uris = [str(r.uri) for r in asyncio.run(mcp.list_resources())]
    for name in skills.SERVED:
        assert f"servirplatform://skill/{name}" in uris
    log("CHECK", "one resource per served skill")


def test_absent_skill_declines_instead_of_serving_empty(monkeypatch, tmp_path, log):
    monkeypatch.setattr(skills, "_SKILLS_DIR", tmp_path)
    b = skills.bundle("trace-emit")
    log("OUTPUT", b[:70])
    assert "not installed on this deployment" in b
    assert skills.available() == []


def test_builds_are_told_to_instrument(log):
    """The user's directive, literally: ship tracing with anything they build."""
    from app.mcp.server import INSTRUCTIONS, build_a_tool
    assert "SHIP OBSERVABILITY" in INSTRUCTIONS
    assert "servirplatform://skill/trace-emit" in INSTRUCTIONS
    p = build_a_tool()
    assert "trace-emit" in p and "execution trace" in p
    assert "including one real trace" in p
    log("CHECK", "instructions and the build prompt both require tracing")


def test_the_instrument_rule_rides_the_capabilities_payload(log):
    """Measured in the tracing-blind build (2026-08-27): connect-time INSTRUCTIONS
    were ignored while payload-carried guidance (render_with) was honored to the
    letter. The observability requirement therefore rides platform_capabilities —
    the payload every cold builder reads FIRST — not only the instructions."""
    from app.mcp import registry
    caps = registry.capabilities(available_tools=["assemble_pack"])
    obs = caps["builder_requirements"]["observability"]
    log("OUTPUT", obs["rule"][:70])
    assert "trace_id" in obs["rule"] and "duration_ms" in obs["rule"]
    assert obs["read_first"] == "servirplatform://skill/trace-emit"
    assert "publish_answer" in obs["surface"]
    assert "real run" in obs["prove"]


def test_a_build_turns_an_implied_calculation_into_a_method(log):
    """A figure the platform does not compute is asked for and offered as a method,
    never written into the app, in the instructions and in the build prompt alike."""
    from app.mcp.server import INSTRUCTIONS, build_a_tool
    build_line = next(l for l in INSTRUCTIONS.split("\n- ") if l.startswith("BUILD-TIME"))
    assert "kind=method" in build_line and "do NOT" in build_line
    assert "servirplatform://skill/contribute-logic" in build_line
    contribute_line = next(l for l in INSTRUCTIONS.split("\n- ") if l.startswith("CONTRIBUTE-TIME"))
    assert "data or logic" in contribute_line
    p = build_a_tool()
    assert "IF THE TOOL NEEDS A CALCULATION" in p and "kind=method" in p
    log("CHECK", "build rule, contribute rule and build prompt all name the method route")


def test_the_build_prompt_names_only_published_tools(log):
    """It once told every builder to run ui_validate, a tool that was never published."""
    import re
    from app.mcp.server import build_a_tool, mcp
    published = {t.name for t in asyncio.run(mcp.list_tools())}
    not_tools = {"pack_id", "required_sections"}
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", build_a_tool())) - not_tools
    log("OUTPUT", sorted(named))
    assert named <= published, f"the build prompt names tools that are not published: {sorted(named - published)}"
