"""The trace disciplines travel WITH the platform — served whole over MCP.

A builder connected over MCP cannot read this repository; if the skill does not
arrive complete (SKILL.md + references + assets), the guidance is a broken tool.
"""

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
    uris = [str(r.uri) for r in mcp._resource_manager.list_resources()]
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
