"""Compositions: registry rows + runners, and the sections contract.

risk.brief exists because the accompanied path (no consumer LLM) was FS-only —
and compose.run hardcoded FS SECTIONS into every persisted pack, so a second
domain's receipts would have carried the wrong output contract.
"""

import pytest

from app.mcp import compose, registry, store
from app.risk import synthesis as risk_synthesis


def _fake_out(**over):
    d = {"declined": False, "brief": "## What the numbers show\n\nZero [1].",
         "citations": [{"n": 1, "kind": "measurement", "source": "s", "text": "Zero."}],
         "parsed": {"place": "battambang", "hazard": "flood"},
         "target": {"place": "battambang", "hazard": "flood"},
         "evidence": {"queries": None, "viz": {"bounds": [0, 0, 1, 1]}},
         "gaps": ["no risk corpus"], "grounded": {"passed": True},
         "provider": "openai", "model": "m", "trace": ["t"], "usage": [],
         "required_sections": list(risk_synthesis.SECTIONS), "pack": "risk"}
    d.update(over)
    return d


def test_both_compositions_have_row_and_runner(log):
    for name in ("foodsecurity.brief", "risk.brief"):
        assert name in registry.COMPOSITIONS
        assert name in compose._RUNNERS
    log("CHECK", "metadata row + runner present for both")


def test_unknown_composition_names_what_exists(log):
    out = compose.run(composition="nope", question="q")
    log("OUTPUT", str(out["available"]))
    assert out["status"] == "declined"
    assert "risk.brief" in out["available"]


def test_risk_brief_persists_the_RISK_contract(monkeypatch, log):
    """The compose.py hardcode fix: the persisted pack carries the runner's own
    sections, pack id and target — not food-security's."""
    monkeypatch.setattr(risk_synthesis, "synthesize",
                        lambda q, provider=None, model=None: _fake_out())
    out = compose.run(composition="risk.brief", question="flood in battambang?")
    log("OUTPUT", f"status={out['status']} receipt={out.get('receipt_id')}")
    assert out["status"] == "ok" and out["receipt_id"]
    pack = store.load_pack(out["pack_id"])
    assert pack["required_sections"] == list(risk_synthesis.SECTIONS)
    assert pack["pack"] == "risk"
    assert pack["target"] == {"place": "battambang", "hazard": "flood"}
    assert pack["viz"] == {"bounds": [0, 0, 1, 1]}      # embed-resolvable
    assert "viz" not in pack["stats"]


def test_risk_brief_decline_passes_through(monkeypatch, log):
    monkeypatch.setattr(risk_synthesis, "synthesize",
                        lambda q, provider=None, model=None: _fake_out(
                            declined=True, brief=None,
                            decline_reason="DECLINE: no such hazard"))
    out = compose.run(composition="risk.brief", question="quantum risk in atlantis?")
    log("OUTPUT", str(out["note"]))
    assert out["status"] == "declined"
    assert "no such hazard" in out["note"]


def test_risk_manifest_no_longer_denies_its_own_runner(log):
    """The manifest said 'runner not implemented' while COMPOSITIONS advertised it —
    the platform contradicting itself about the same feature."""
    m = registry.pack_manifest("risk")
    log("OUTPUT", m["compositions"]["risk.brief"][:60])
    assert "not implemented" not in m["compositions"]["risk.brief"]
    assert "compose_run" in m["compositions"]["risk.brief"]


class _FakeMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _FakeResp:
    def __init__(self, content=None, tool_calls=None, finish="stop"):
        c = type("C", (), {})()
        c.message = _FakeMsg(content, tool_calls)
        c.finish_reason = finish
        self.choices = [c]
        self.usage = None


class _FakeToolCall:
    def __init__(self, arguments):
        self.function = type("F", (), {"arguments": arguments, "name": "set_risk_target"})()
        self.id = "t1"


class _FakeClient:
    """Scripted LLM: first call answers the parse, later calls answer the draft."""
    def __init__(self, parse_resp, draft_resp):
        self._resps = [parse_resp, draft_resp, draft_resp]
        class _Completions:
            def __init__(s2, outer): s2._outer = outer
            def create(s2, **kw): return s2._outer._resps.pop(0)
        self.chat = type("Chat", (), {})()
        self.chat.completions = _Completions(self)


def _wire(monkeypatch, client, gather=None):
    import app.llm as llm
    monkeypatch.setattr(llm, "build_client", lambda provider: client)
    monkeypatch.setattr(llm, "default_model", lambda provider: "fake-model")
    if gather is not None:
        monkeypatch.setattr(risk_synthesis, "gather_risk_evidence", gather)


def test_risk_synthesize_happy_path_offline(monkeypatch, log):
    def gather(target, focus, trace, extras):
        trace.append("gather ok")
        return ([{"n": 1, "kind": "measurement", "source": "s",
                  "text": "Zero hospitals."}],
                ["no risk corpus"], {"queries": None})
    draft = "\n\n".join(f"{s}\n\nStated in the evidence [1]."
                        for s in risk_synthesis.SECTIONS)
    client = _FakeClient(_FakeResp(tool_calls=[_FakeToolCall('{"place":"x","hazard":"flood"}')]),
                         _FakeResp(content=draft))
    _wire(monkeypatch, client, gather)
    out = risk_synthesis.synthesize("flood in x?", provider="openai")
    log("OUTPUT", f"declined={out['declined']} sections={out['required_sections'][0]}")
    assert out["declined"] is False and out["grounded"]["passed"] is True
    assert out["required_sections"] == list(risk_synthesis.SECTIONS)
    assert out["citations"][-1]["kind"] == "gaps"        # citable gaps ride this path too
    assert out["pack"] == "risk" and out["target"]["place"] == "x"


def test_risk_synthesize_out_of_scope_declines(monkeypatch, log):
    client = _FakeClient(_FakeResp(content="That is not a hazard question."),
                         _FakeResp(content="unused"))
    _wire(monkeypatch, client)
    out = risk_synthesis.synthesize("write me a poem", provider="openai")
    log("OUTPUT", str(out["decline_reason"]))
    assert out["declined"] is True


def test_risk_synthesize_infra_failure_declines_governed(monkeypatch, log):
    """The review's high finding: Overpass/geocoder/raster failures escaped as raw
    tool errors while the identical gather declines governed via assemble_pack."""
    def gather(target, focus, trace, extras):
        raise RuntimeError("Overpass unavailable: all mirrors failed")
    client = _FakeClient(_FakeResp(tool_calls=[_FakeToolCall('{"place":"x","hazard":"flood"}')]),
                         _FakeResp(content="unused"))
    _wire(monkeypatch, client, gather)
    out = risk_synthesis.synthesize("flood in x?", provider="openai")
    log("OUTPUT", str(out["decline_reason"]))
    assert out["declined"] is True
    assert "upstream/infrastructure failure" in out["decline_reason"]
    assert "Overpass unavailable" in out["decline_reason"]
