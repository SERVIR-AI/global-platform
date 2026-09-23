"""A config pasted as YAML text is the developer's conf/ file, submitted over the MCP."""
from app.contrib import staging


def test_yaml_text_carries_its_own_kind():
    r = staging.submit("", """
kind: vector
layer: evacuation_centres
""")
    assert r["status"] == "declined" and r["kind"] == "vector"
    assert any("missing required field 'url'" in p for p in r["problems"])


def test_bad_yaml_is_named_not_swallowed():
    r = staging.submit("vector", "layer: [unclosed")
    assert r["status"] == "declined"
    assert any("not valid YAML" in p for p in r["problems"])


def test_json_text_works_too():
    r = staging.submit("raster", '{"layer": "population_x"}')
    assert r["status"] == "declined" and r["kind"] == "raster"
    assert not any("legend" in p for p in r["problems"])
