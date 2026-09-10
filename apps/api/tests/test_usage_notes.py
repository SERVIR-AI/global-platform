"""usage_notes: contributor guidance that travels with the resource, capped at
500 chars, surfaced to the agent at the moment of use."""

import pytest

from app.contrib import feedspecs, notes, rasters, sources, tables
from app.food_security import synthesis as fs
from app.mcp import assemble, packs


LONG = "x" * 501
NOTE = "Station under-reads in monsoon months; compare with CHIRPS before citing."


def test_the_limit_names_itself_and_the_alternative(log):
    fails = notes.validate(LONG)
    log("OUTPUT", fails[0][:110])
    assert "501 characters" in fails[0] and "500" in fails[0]
    assert "document source" in fails[0]          # points at the right home
    assert notes.validate(None) == [] and notes.validate(NOTE) == []


def test_every_gate_enforces_the_limit(log):
    doc = {"pack": "food-security", "url": "https://x", "source": "s", "title": "t",
           "pub_date": "2026-09", "temporal": "forecast",
           "validation": "unvalidated", "usage_notes": LONG}
    feed = {"dataset": "d", "title": "t", "description": "d", "source": "s",
            "validation": "single-agency", "residency": "external call-out",
            "cadence": "monthly", "adapter": "generic_table",
            "fetch": {"url": "u", "index_name": "I", "units": "u"},
            "usage_notes": LONG}
    table = {"dataset": "d", "file": "/nope.csv", "title": "t", "description": "d",
             "source": "s", "validation": "unvalidated", "license": "l",
             "vintage": "v", "cadence": "monthly", "columns": {"a": "A"},
             "units": "u", "usage_notes": LONG}
    raster = {"layer": "hazard_x", "file": "/nope.tif", "title": "t",
              "description": "d", "source": "s", "license": "l", "vintage": "v",
              "legend": {1: "a"}, "declared": {"dtype": "int8", "valid_min": 0,
                                               "valid_max": 5},
              "usage_notes": LONG}
    for name, fails in [("doc", sources.validate_entry(doc)),
                        ("feed", feedspecs.validate_spec(feed)),
                        ("table", tables.validate_manifest(table)),
                        ("raster", rasters.validate_manifest(raster))]:
        assert any("501 characters" in f for f in fails), name
    log("CHECK", "all four gates refuse over-limit guidance")


def test_guidance_reaches_the_drafting_model(log):
    """The FS evidence block shows the guidance WITH the evidence text."""
    c = {"kind": "document", "source": "s", "title": "t", "text": "Rain fell.",
         "temporal": "forecast", "usage_notes": NOTE}
    block = fs._render_pack([c])
    log("OUTPUT", block[:120])
    assert "contributor guidance: Station under-reads" in block


def test_pack_level_guidance_rides_the_assemble_response(monkeypatch, log):
    def gather(target, focus, trace, extras):
        return ([{"n": 1, "kind": "m", "source": "s", "title": "t",
                  "text": "42 units.", "validation": "unvalidated",
                  "retrieval": "computed-at-pack-time"}], [], {"queries": None})
    monkeypatch.setitem(packs.PACKS, "noted", {
        "display_name": "N", "version": "v0", "target_keys": ("place",),
        "target_doc": {"place": "p"}, "gather": gather,
        "sections": lambda: ["## A"], "corpus": None,
        "default_focus": lambda t: "", "usage_notes": NOTE})
    out = assemble.assemble(pack="noted", place="x")
    log("OUTPUT", out.get("usage_notes", "")[:60])
    assert out["status"] == "ok" and out["usage_notes"] == NOTE


def test_doctor_flags_overlong_pack_guidance(monkeypatch, log):
    from app.contrib import packdev
    monkeypatch.setitem(packs.PACKS, "gassy", {
        "display_name": "G", "version": "v0", "target_keys": ("place",),
        "target_doc": {"place": "p"},
        "gather": lambda t, f, tr, e: ([], [], {}),
        "sections": lambda: ["## A"], "corpus": None,
        "default_focus": lambda t: "", "usage_notes": LONG,
        "doctor_target": {"place": "x"}})
    out = packdev.doctor("gassy")
    log("OUTPUT", [f for f in out["failures"] if "usage_notes" in f][0][:80])
    assert any("usage_notes" in f for f in out["failures"])


def test_feed_guidance_reaches_the_query_response(monkeypatch, log):
    """The adapter note (usage_notes rides it) must surface on the OK path —
    guidance that only appears on failure paths guides nothing."""
    from app.mcp import feeds, registry
    spec = {"status": "available", "adapter": "generic_csv", "source": "s",
            "validation": "unvalidated", "cadence": "monthly",
            "usage_notes": NOTE, "title": "t",
            "fetch": {"path": "IGNORED", "columns": {"a": "A"}, "units": "u"}}
    monkeypatch.setitem(registry.FEEDS, "noted_feed", spec)
    monkeypatch.setitem(feeds.ADAPTERS, "generic_csv",
                        lambda params, sp: {"as_of": "2026-08", "count": 1,
                                            "records": [{"a": 1}], "summary": "s",
                                            "query_receipt": "q", "url": None,
                                            "stale_data": {"served_stale": False},
                                            "note": sp.get("usage_notes")})
    out = feeds.query("noted_feed")
    log("OUTPUT", out["note"][:70])
    assert out["status"] == "ok" and out["note"] == NOTE
