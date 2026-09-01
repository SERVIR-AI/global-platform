"""The PACKS registry — the seam that makes a second domain rows, not code.

The acceptance test is the last one: a pack registered as a pure registry row
(no imports changed, no tool code touched) flows the ENTIRE loop — assemble ->
verify -> publish -> receipt — under its own output contract. That is the
ARCHITECTURE §2/§3 promise, previously true for feeds and compositions and false
for the loop itself.
"""

import pytest

from app.mcp import assemble, packs, publish, record, store, verify


@pytest.fixture
def toy_pack(monkeypatch):
    """A domain pack that exists ONLY as a registry row."""
    def gather(target, focus, trace, extras):
        trace.append(f"toy[{target['place']}]")
        citations = [
            {"n": 1, "kind": "measurement", "retrieval": "computed-at-pack-time",
             "source": "toy sensor", "title": "site reading",
             "text": f"Reading at {target['place']}: 42 units."},
            {"n": 2, "kind": "reference", "retrieval": "archived-document",
             "source": "toy archive", "title": "site history",
             "pub_date": "2026-01", "text": "Historically 40 units."},
        ]
        return citations, ["no toy corpus exists"], {"queries": None, "sites": 1}
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy Pack", "version": "v0",
        "target_keys": ("place",), "target_doc": {"place": "site name"},
        "gather": gather,
        "sections": lambda: ["## Reading", "## Caveats"],
        "corpus": None,
        "default_focus": lambda t: t.get("place", ""),
    })
    return "toy"


def test_fs_still_rides_the_registry_unchanged(monkeypatch, log):
    """Back-compat is load-bearing: bare (country, crop) calls must stay
    food-security with the historic top-level keys — Desktop configs and the
    embed resolver read pack.country/pack.crop."""
    seen = {}
    def fake_gather(target, focus, trace, extras):
        seen.update(target=target, focus=focus, extras=extras)
        return [{"n": 1, "kind": "document", "retrieval": "archived-document",
                 "source": "s", "title": "t", "text": "x [1]"}], [], {"queries": {}}
    monkeypatch.setitem(packs.PACKS, "food-security",
                        {**packs.PACKS["food-security"], "gather": fake_gather})
    out = assemble.assemble(country="Kenya", crop="maize")
    log("OUTPUT", f"pack={out['pack']} country={out['country']}")
    assert out["status"] == "ok" and out["pack"] == "food-security"
    assert out["country"] == "Kenya" and out["crop"] == "maize"     # historic keys
    assert out["target"] == {"country": "Kenya", "crop": "maize"}
    assert out["required_sections"][0] == "## What history says"


def test_params_no_pack_claims_resolve_to_no_pack(log):
    """place/hazard are claimed by risk now, so the unclaimed case needs a param
    that never existed — infer must return None, and assemble turns None into a
    decline listing every pack (covered by the toy-pack test's decline path)."""
    assert packs.infer(None, frobnicate="x") is None
    assert packs.infer(None, place="battambang") == "risk"
    assert packs.infer(None, country="Kenya") == "food-security"
    assert packs.infer(None) == "food-security"          # bare call stays FS
    log("CHECK", "inference: claimed params route, unclaimed decline, bare = FS")


def test_a_row_only_pack_flows_the_entire_loop(toy_pack, log):
    """THE acceptance test: no tool code was edited for this pack to exist."""
    p = assemble.assemble(pack="toy")
    assert p["status"] == "declined"                    # place missing: honest
    p = assemble.assemble(pack="toy", place="siteA")
    assert p["status"] == "ok", p.get("note")
    log("OUTPUT", f"sections={p['required_sections']}")
    assert p["required_sections"] == ["## Reading", "## Caveats"]
    assert p["target"] == {"place": "siteA"}
    assert "country" not in p                            # FS keys only on FS packs

    draft = ("## Reading\n\nThe reading is 42 units [1].\n\n"
             "## Caveats\n\nHistorically 40 units [2].")
    out = publish.answer(p["pack_id"], draft, question="toy site check")
    assert out["status"] == "ok" and out["passed"] is True
    assert out["target"] == {"place": "siteA"}
    fresh = out["evidence_freshness"]
    log("OUTPUT", f"computed={fresh.get('computed_sources')}")
    assert fresh["computed_sources"] == [1]              # third retrieval grade
    # [3] is the auto-appended gaps citation (retrieval "config" -> archived bucket)
    assert fresh["archived_sources"] == [2, 3]
    assert fresh["pulled_sources"] == []
    src = {s["n"]: s for s in out["sources"]}
    assert src[1]["retrieval"] == "computed-at-pack-time"
    assert src[3]["retrieval"] == "config"


def test_verify_decline_no_longer_leaks_fs_sections(log):
    v = verify.groundedness("draft", "0000000000000000")
    log("OUTPUT", str(v.get("available_packs")))
    assert v["status"] == "declined"
    assert "required_sections" not in v
    assert "food-security" in v["available_packs"]


def test_legacy_packs_without_retrieval_still_classify(log):
    """Packs minted before 2026-08-27 have no retrieval field — the kind-tuple
    fallback must keep their receipts honest."""
    pid = store.save_pack({"pack": "food-security", "country": "Kenya",
                           "crop": "maize", "citations": [
                               {"n": 1, "kind": "index", "source": "NOAA"},
                               {"n": 2, "kind": "document", "source": "FEWS"}],
                           "gaps": [], "required_sections": ["## A"]})
    r = record.record(pack_id=pid, question="q")
    log("OUTPUT", str(r["evidence_freshness"]["pulled_sources"]))
    assert r["evidence_freshness"]["pulled_sources"] == [1]
    assert r["evidence_freshness"]["archived_sources"] == [2]


def test_every_pack_gets_a_manifest_resource(log):
    from app.mcp.server import mcp
    uris = [str(r.uri) for r in mcp._resource_manager.list_resources()]
    log("OUTPUT", str([u for u in uris if "/pack/" in u]))
    for pid in packs.available():
        assert f"servirplatform://pack/{pid}" in uris


def test_assemble_description_names_every_pack(log):
    from app.mcp import registry
    d = registry.describe_assemble()
    for pid, spec in packs.PACKS.items():
        assert pid in d, pid
        for k in spec["target_keys"]:
            assert k in d
    log("CHECK", "all packs and target params advertised")
