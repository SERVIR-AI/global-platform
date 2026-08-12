"""A6 — synthesis engine guardrails. No network: LLM scripted (SynthStub),
embeddings hashed, conditions feed canned. Declines are asserted as strictly as
answers. Live round trip: test_synthesis_live.py (SYNTHESIS_LIVE=1)."""

import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.food_security import cropmonitor, synthesis
from app.mcp import feeds, registry
from app.main import app
from app.rag.embed import ProviderEmbedder
from app.rag.store import Corpus

LAYERS = {"layers": [{"id": 3, "name": "Global_Synthesis_202604"}]}
CROPS = {"features": [{"attributes": {"Crop": c}} for c in ("Maize 1", "Maize 2")]}
ROWS = {"features": [
    {"attributes": {"Country": "Kenya", "Region": "Rift Valley", "Crop": "Maize 1",
                    "Conditions": "Favourable", "Drivers": "Dry"}},
    {"attributes": {"Country": "Kenya", "Region": "Coast", "Crop": "Maize 2",
                    "Conditions": " ", "Drivers": " "}},
]}


class HashEmbedder:
    def embed(self, texts):
        out = []
        for t in texts:
            seed = int.from_bytes(hashlib.sha1(t.encode()).digest()[:8], "big")
            v = np.random.default_rng(seed).standard_normal(64).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out)


PARSED = {"crop": "maize", "country": "Kenya", "focus": "El Nino outlook for maize"}

# One forecast doc + one retro doc + conditions -> citations [1][2][3].
GOOD_BRIEF = """## What history says
FAO reported reduced maize output in past El Nino events [2].

## The current signal
ICPAC's outlook projects above-normal rains [1]. The GEOGLAM Crop Monitor rates most regions Favourable [3].

## What's missing and how to weigh it
County-level ground data is not in the library; seasonal forecasts carry known error [1].

## Season timing caveat
The projections target the coming season, not the current harvest [1]."""


class SynthStub:
    """Parse call (has tools) -> plan_brief tool call / out-of-scope text / raw
    argument string; synthesis calls (no tools) -> scripted drafts, in order.
    Records every messages list it is called with."""

    def __init__(self, briefs, parse=("tool", PARSED)):
        self.briefs, self.parse = list(briefs), parse
        self.synth_calls, self.seen = 0, []
        self.chat = SimpleNamespace(completions=self)

    def create(self, model, messages, **kwargs):
        self.seen.append(messages)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)
        if "tools" in kwargs:
            kind, payload = self.parse
            if kind == "text":
                msg = SimpleNamespace(content=payload, tool_calls=None)
            else:
                args = json.dumps(payload) if kind == "tool" else payload
                tc = SimpleNamespace(id="c1", function=SimpleNamespace(
                    name="plan_brief", arguments=args))
                msg = SimpleNamespace(content=None, tool_calls=[tc])
        else:
            self.synth_calls += 1
            msg = SimpleNamespace(content=self.briefs.pop(0), tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=usage)


@pytest.fixture
def brief_env(monkeypatch, tmp_path):
    """Isolated cache + hashed embeddings + canned conditions + a 2-doc corpus
    (one forecast, one retrospective). Returns a stub installer."""
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "rag_min_relevance", 0.5)
    # Pin the embedder: get_embedder() picks a BACKEND from settings, so without
    # this the suite silently inherits the developer's .env — a vertex .env made
    # production code build a real VertexEmbedder that the stub below never touched.
    monkeypatch.setattr(get_settings(), "embedding_provider", "openai")
    monkeypatch.setattr(get_settings(), "embedding_model", "text-embedding-3-small")
    monkeypatch.setattr(ProviderEmbedder, "embed",
                        lambda self, texts: HashEmbedder().embed(texts))
    monkeypatch.setattr(cropmonitor, "_fetch_json",
                        lambda url, params: LAYERS if url.endswith("FeatureServer")
                        else CROPS if params.get("returnDistinctValues") == "true"
                        else ROWS)
    # Pillar-1 driver feeds. Stubbed at the feed seam, NOT at _driver_citations, so
    # registry iteration / citation shape / staleness handling still run — while the
    # suite stays offline. Without this the brief tests silently reached NOAA and IRI
    # over the network, which is how an offline suite rots into a flaky one.
    monkeypatch.setattr(feeds, "query", lambda dataset, params=None: {
        "status": "ok", "dataset": dataset, "as_of": "MJJ 2026", "count": 1,
        "summary": f"canned {dataset}",
        "records": [{"season": "MJJ", "year": 2026, "value": 1.39}],
        "passport": {"source": "stub", "url": f"https://example.invalid/{dataset}",
                     "query": dataset, "stale_data": {"served_stale": False}}})
    # ProviderEmbedder (embed stubbed) so the corpus identity matches synthesize()'s.
    corpus = Corpus(synthesis.CORPUS, embedder=ProviderEmbedder())
    corpus.ingest("El Nino seasonal rainfall forecast outlook Kenya maize "
                  "El Nino outlook for maize",
                  {"source": "ICPAC GHACOF", "title": "GHACOF statement",
                   "pub_date": "2026-05", "temporal": "forecast",
                   "validation": "multi-agency-consensus", "url": "https://icpac.net/g.pdf"},
                  raw=b"pdf", filename="g.pdf")
    corpus.ingest("impact of past El Nino events on maize production and "
                  "food security in Kenya",
                  {"source": "FAO GIEWS", "title": "El Nino impacts 2016",
                   "pub_date": "2016-08", "temporal": "retrospective",
                   "validation": "single-agency", "url": "https://fao.org/i.pdf"})

    def install(briefs, parse=("tool", PARSED)):
        stub = SynthStub(briefs, parse)
        monkeypatch.setattr(synthesis, "build_client", lambda provider: stub)
        return stub
    return install


def test_grounded_brief_ships_with_sources_and_receipts(brief_env):
    """The happy path: sections, per-paragraph citations, engine-appended Sources
    with URLs/archived copies/receipts, groundedness PASS."""
    stub = brief_env([GOOD_BRIEF])
    out = synthesis.synthesize("What should I expect for maize in Kenya under El Nino?")
    assert out["declined"] is False
    for section in synthesis.SECTIONS + ("## Sources",):
        assert section in out["brief"]
    assert out["grounded"]["passed"] and out["grounded"]["attempts"] == 1
    # Contiguous from 1, not a magic count: a new registry driver row must not
    # break unrelated tests (it did, twice).
    assert [c["n"] for c in out["citations"]] == list(range(1, len(out["citations"]) + 1))
    assert out["citations"][0]["temporal"] == "forecast"
    kinds = [c["kind"] for c in out["citations"]]
    assert kinds.count("conditions") == 1 and kinds.count("calendar") == 1
    n_drivers = sum(1 for s in registry.FEEDS.values()
                    if s.get("status") == "available" and s.get("brief_role") == "driver")
    assert kinds.count("index") == n_drivers   # every registry-tagged driver rides along
    assert "source: https://icpac.net/g.pdf" in out["brief"]
    assert "archived: /api/food-security/rag/document/" in out["brief"]
    assert "query: Crop IN" in out["brief"]            # the conditions receipt
    ev = out["evidence"]
    assert (ev["forecast_hits"], ev["retrospective_hits"]) == (1, 1)
    assert ev["conditions"] is True and ev["calendar"] == "default"
    assert ev["queries"]["forecast"].startswith("El Nino")   # retrieval provenance
    assert out["parsed"] == PARSED                           # the model's parse, exposed
    assert out["citations"][0]["chunk_id"].endswith(":0")    # chunk-level trace
    # the model was shown exactly the numbered pack and the gaps line
    synth_prompt = stub.seen[-1][-1]["content"]
    assert "[1] ICPAC GHACOF" in synth_prompt and "[3] GEOGLAM Crop Monitor" in synth_prompt
    assert "Known gaps" in synth_prompt


def test_render_pack_numbering_matches_citations(brief_env):
    brief_env([])
    citations, _, _ = synthesis.gather_evidence(PARSED, trace=[])
    pack = synthesis._render_pack(citations)
    for c in citations:
        assert f"[{c['n']}] {c['source']}" in pack


def test_phantom_citation_fails_retries_then_declines(brief_env):
    """A citation number not in the pack can never ship — including inside a
    cluster marker like [1, 9]: one retry, then an honest decline."""
    cluster = GOOD_BRIEF.replace("[2]", "[2, 99]")
    stub = brief_env([cluster, cluster])
    out = synthesis.synthesize("maize in Kenya?")
    assert out["declined"] is True and stub.synth_calls == 2
    assert out["grounded"]["phantom_citations"] == [99]
    assert "refusing to ship" in out["decline_reason"]
    assert "not in the evidence list: [99]" in stub.seen[-1][-1]["content"]  # retry feedback


def test_cluster_and_range_citations_are_understood(brief_env):
    """Valid clusters [1, 2] and ranges [1-3] count as citations, not failures;
    a bracketed year [2016] is not treated as a citation."""
    brief = GOOD_BRIEF.replace("[2]", "[1, 2]").replace("rains [1]", "rains [1-3]") \
        + "\n\nIn [2016] the rains failed across the region [2]."
    brief_env([brief])
    out = synthesis.synthesize("maize in Kenya?")
    assert out["declined"] is False
    assert out["grounded"]["phantom_citations"] == []
    assert 2016 not in out["grounded"]["cited"]


def test_uncited_paragraph_fails_then_passes_on_retry(brief_env):
    brief_env([GOOD_BRIEF.replace(" [2]", ""), GOOD_BRIEF])
    out = synthesis.synthesize("maize in Kenya?")
    assert out["declined"] is False
    assert out["grounded"]["attempts"] == 2


def test_missing_sections_and_model_written_sources_block(brief_env):
    """Structure is enforced, not requested: a draft missing a section, or writing
    its own Sources, fails the check."""
    no_caveat = GOOD_BRIEF.replace("## Season timing caveat", "## Timing")
    fake_sources = GOOD_BRIEF + "\n\n## Sources\n[1] A source I made up [1]"
    brief_env([no_caveat, fake_sources])
    out = synthesis.synthesize("maize in Kenya?")
    assert out["declined"] is True
    assert any("missing required sections" in f for f in
               out["grounded"]["failures"]) or any(
        "Sources section" in f for f in out["grounded"]["failures"])


def test_zero_citation_draft_fails(brief_env):
    empty = "\n\n".join(s + "\nSome uncited prose." for s in synthesis.SECTIONS)
    brief_env([empty, empty])
    out = synthesis.synthesize("maize in Kenya?")
    assert out["declined"] is True
    assert any("no citations at all" in f or "without citations" in f
               for f in out["grounded"]["failures"])


def test_out_of_scope_question_declines_at_parse(brief_env):
    stub = brief_env([], parse=("text", "I brief on food security, not football."))
    out = synthesis.synthesize("Who wins the World Cup final?")
    assert out["declined"] is True and stub.synth_calls == 0
    assert "football" in out["decline_reason"]


def test_malformed_parse_arguments_decline_not_500(brief_env):
    """A truncated tool-arguments string is an honest decline, never a stack trace."""
    brief_env([], parse=("rawargs", '{"crop": "maize", "country": "Ken'))
    r = TestClient(app).post("/api/food-security/chat", json={"question": "maize?"})
    assert r.status_code == 200
    assert r.json()["declined"] is True
    assert "rephrasing" in r.json()["decline_reason"]


def test_no_evidence_declines_without_a_synthesis_call(brief_env, monkeypatch):
    """Empty library + conditions outage -> deterministic decline naming every gap."""
    import shutil
    shutil.rmtree(get_settings().cache_dir / "rag", ignore_errors=True)

    def down(url, params):
        raise cropmonitor.ServiceUnreachable("network down")
    monkeypatch.setattr(cropmonitor, "_fetch_json", down)
    stub = brief_env([])
    out = synthesis.synthesize("maize in Kenya?")
    assert out["declined"] is True and stub.synth_calls == 0
    assert "no forecast/outlook document" in out["decline_reason"]
    assert "no analog-year/retrospective document" in out["decline_reason"]
    assert "conditions unavailable" in out["decline_reason"]


def test_model_decline_passes_through(brief_env):
    brief_env(["DECLINE: the evidence covers Kenya but the question asks about Peru."])
    out = synthesis.synthesize("maize in Peru?")
    assert out["declined"] is True and "Peru" in out["decline_reason"]


def test_unverified_numbers_are_recorded_not_blocking(brief_env):
    """v1 contract: citation/structure checks block; a number absent from the
    evidence (whole-token compare) is RECORDED (upgrade path to blocking)."""
    brief_env([GOOD_BRIEF.replace("above-normal rains", "a 9999 tonne surplus")])
    out = synthesis.synthesize("maize in Kenya?")
    assert out["declined"] is False
    assert "9999" in out["grounded"]["numbers_unverified"]


def test_conditions_truncation_is_visible(brief_env, monkeypatch):
    """More regions than the pack shows -> the evidence says so explicitly."""
    big = {"features": [dict(r) for r in ROWS["features"]] * 8}   # 16 rows
    monkeypatch.setattr(cropmonitor, "_fetch_json",
                        lambda url, params: LAYERS if url.endswith("FeatureServer")
                        else CROPS if params.get("returnDistinctValues") == "true"
                        else big)
    citation, gap = synthesis._conditions_citation("maize", "Kenya", trace=[])
    assert gap is None and "(first 12 of 16 rows)" in citation["text"]


def test_calendar_phase_math_handles_wrapping_windows():
    """Deterministic season phases, incl. windows that wrap the year end (A7.1:
    the asked-in month changes the framing)."""
    from app.food_security import calendar as cal
    zambia = {"season": "Main season", "planting": [11, 12], "harvest": [4, 6]}
    assert cal._phase(11, zambia) == "planting window"
    assert cal._phase(2, zambia) == "growing season"          # wraps the year end
    assert cal._phase(5, zambia) == "harvest window"
    assert cal._phase(8, zambia) == "off-season"
    kenya_short = {"season": "Short rains", "planting": [10, 11], "harvest": [1, 2]}
    assert cal._phase(1, kenya_short) == "harvest window"     # wrapped harvest


def test_calendar_citation_default_vs_adjusted(brief_env):
    """A7.2 AC: changing season start/end changes the brief's evidence — and the
    adjustment is visibly marked, never silently absorbed."""
    from app.food_security import calendar as cal
    default = cal.citation("Kenya", "maize", asked_month=7)
    assert default and default["adjusted"] is False
    assert "hub default" in default["text"] and "Long rains" in default["text"]
    assert default["url"]                                  # traceable baseline

    adjusted = cal.citation("Kenya", "maize", asked_month=7, override=[
        {"season": "Long rains (late onset)", "planting": [4, 6], "harvest": [9, 11]}])
    assert adjusted["adjusted"] is True
    assert "ADJUSTED by the requester" in adjusted["text"]
    assert "Long rains (late onset)" in adjusted["text"]
    assert adjusted["text"] != default["text"]


def test_calendar_flows_into_the_evidence_pack_and_prompt(brief_env):
    """The adjusted calendar becomes a numbered citation the model must use for
    the Season timing caveat."""
    good = GOOD_BRIEF + "\n\nAdjusted calendar applies [4]."
    stub = brief_env([good])
    out = synthesis.synthesize(
        "maize in Kenya?",
        calendar=[{"season": "Long rains", "planting": [4, 6], "harvest": [9, 11]}])
    assert out["declined"] is False
    cal_cite = next(c for c in out["citations"] if c["kind"] == "calendar")
    assert cal_cite["adjusted"] is True and cal_cite["n"] == len(out["citations"])
    assert out["evidence"]["calendar"] == "adjusted"
    assert "ADJUSTED by the requester" in stub.seen[-1][-1]["content"]


def test_calendar_override_for_another_country_is_dropped_and_declared(brief_env):
    """An adjustment made for Zambia must never be cited as Kenya's ADJUSTED
    calendar — it is dropped, the hub default used, and the drop declared."""
    stub = brief_env([GOOD_BRIEF])
    out = synthesis.synthesize(
        "maize in Kenya?",
        calendar=[{"season": "Main season", "planting": [12, 1], "harvest": [5, 7]}],
        calendar_target=("Zambia", "maize"))
    assert out["declined"] is False
    cal = next(c for c in out["citations"] if c["kind"] == "calendar")
    assert cal["adjusted"] is False                     # hub default used
    assert "NOT applied" in stub.seen[-1][-1]["content"]  # gap reached the prompt


def test_calendar_endpoint_and_request_validation(brief_env):
    client = TestClient(app)
    r = client.get("/api/food-security/calendar")
    assert r.status_code == 200 and "kenya" in r.json()["calendar"]

    r = client.post("/api/food-security/chat", json={
        "question": "maize?",
        "calendar": [{"season": "x", "planting": [13, 2], "harvest": [4, 6]}]})
    assert r.status_code == 422                            # months must be 1-12


def test_chat_endpoint_round_trip_and_trace(brief_env):
    brief_env([GOOD_BRIEF])
    client = TestClient(app)
    r = client.post("/api/food-security/chat",
                    json={"question": "maize in Kenya under El Nino?", "verbose": True})
    body = r.json()
    assert r.status_code == 200 and body["declined"] is False
    assert any("groundedness attempt 1 -> PASS" in t for t in body["trace"])

    brief_env([GOOD_BRIEF])
    r = client.post("/api/food-security/chat",
                    json={"messages": [{"role": "user", "content": "maize in Kenya?"}]})
    assert r.status_code == 200 and "trace" not in r.json()

    assert client.post("/api/food-security/chat", json={}).status_code == 400
    r = client.post("/api/food-security/chat",   # null/block content -> 400, not 500
                    json={"messages": [{"role": "user", "content": None}]})
    assert r.status_code == 400


def test_driver_feeds_reach_the_pack_and_are_marked_as_pulls(brief_env):
    """A3.1: the Pillar-1 drivers must actually land in the evidence pack, carry
    the no-local-inference constraint in their own text (so it survives into the
    receipt, not just the prompt), and be typed as live pulls."""
    from app.mcp import record
    brief_env([GOOD_BRIEF])
    citations, gaps, stats = synthesis.gather_evidence(PARSED, trace=[])
    drivers = [c for c in citations if c["kind"] == "index"]
    assert stats["drivers"] == len(drivers) > 0
    for d in drivers:
        assert "DRIVER SIGNAL ONLY" in d["text"]
        assert record._is_pulled(d), f"{d['source']} would mint as an archived document"


def test_drivers_alone_cannot_carry_a_brief(brief_env, monkeypatch, tmp_path):
    """The drivers are GLOBAL. On a question about one country they establish the
    ocean state and nothing about that place, so they must not clear the
    evidence bar — that would be the local inference Phase 1 forbids."""
    import shutil
    shutil.rmtree(get_settings().cache_dir / "rag", ignore_errors=True)

    def down(url, params):
        raise cropmonitor.ServiceUnreachable("network down")
    monkeypatch.setattr(cropmonitor, "_fetch_json", down)
    stub = brief_env([])                       # no drafts: a synthesis call would raise
    out = synthesis.synthesize("El Nino in Kenya?")
    assert out["declined"] is True and stub.synth_calls == 0
    assert "no forecast/outlook document" in out["decline_reason"]


def test_a_healthy_feed_is_never_labelled_cache_served(brief_env):
    """Crying wolf is its own dishonesty: the climate adapters always attach a
    stale_data dict, so testing its PRESENCE flagged every healthy feed as
    cache-served and put a false staleness warning into the evidence."""
    brief_env([GOOD_BRIEF])
    citations, _, _ = synthesis.gather_evidence(PARSED, trace=[])
    for c in (c for c in citations if c["kind"] == "index"):
        assert "LAST-GOOD CACHE" not in c["text"], c["source"]
