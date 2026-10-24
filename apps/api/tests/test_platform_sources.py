"""G13 — platform sources only: the rule, one not-served line on every decline and gap list,
and the shared gate on every pack (a contributed table + method included). Offline; everything
it mints or caches lands in a tmp dir."""
import asyncio

import pytest

from app.config import get_settings
from app.contrib import staging
from app.food_security import cropmonitor, synthesis as fs
from app.mcp import assemble, budget, compose, feeds, instructions, loop, packs, publish, record
from app.mcp import shape, store, verify
from app.mcp.server import mcp
from app.rag.embed import ProviderEmbedder
from app.rag.store import Corpus
from test_contrib_methods import BOX, _threshold, _units, env  # noqa: F401  (a contributed table + method)
from test_landcover_pack import lc_env  # noqa: F401  (a synthetic land-cover stack)
from test_risk_pack import offline_aoi  # noqa: F401  (the offline risk pack)
from test_synthesis import CROPS, LAYERS, ROWS, HashEmbedder

WEB = "https://www.example-weather.org/outlook/2026"          # a page no pack carries
DOMAIN = "reliefweb.int"


def _prompt(name: str, **args) -> str:
    """A published prompt as a client renders it."""
    from fastmcp import Client

    async def go():
        async with Client(mcp) as c:
            r = await c.get_prompt(name, args or None)
            return "\n".join(m.content.text for m in r.messages)
    return asyncio.run(go())


def _line(kinds) -> str:
    return loop.NOT_SERVED.format(kinds=budget.names(kinds, last=" or "))


@pytest.fixture(scope="module", autouse=True)
def _offline(tmp_path_factory):
    """The store, the cache (corpora, feed and Crop Monitor caches) in a tmp dir for the whole
    module, and hashed embeddings, as test_synthesis does: no row in cache/mcp, no network."""
    tmp, s = tmp_path_factory.mktemp("platform_sources"), get_settings()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(s, "cache_dir", tmp / "cache")
        mp.setattr(store, "_db_path", lambda: tmp / "grp.db")
        mp.setattr(s, "embedding_provider", "openai")
        mp.setattr(s, "embedding_model", "text-embedding-3-small")
        mp.setattr(ProviderEmbedder, "embed", lambda self, texts: HashEmbedder().embed(texts))
        yield tmp


@pytest.fixture(scope="module")
def pack(_offline):
    """The food-security pack built offline: canned Crop Monitor rows and driver feeds and a
    two-document library (test_synthesis's brief_env), kept in this module's store."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cropmonitor, "_fetch_json", lambda url, params: LAYERS if url.endswith(
            "FeatureServer") else CROPS if params.get("returnDistinctValues") == "true" else ROWS)
        mp.setattr(feeds, "query", lambda dataset, params=None: {
            "status": "ok", "dataset": dataset, "as_of": "MJJ 2026", "count": 1,
            "summary": f"canned {dataset}", "records": [{"season": "MJJ", "year": 2026, "value": 1.39}],
            "passport": {"source": "stub", "url": f"https://example.invalid/{dataset}",
                         "query": dataset, "stale_data": {"served_stale": False}}})
        corpus = Corpus(fs.CORPUS, embedder=ProviderEmbedder())
        corpus.ingest("El Nino seasonal rainfall forecast outlook Kenya maize: 1,250,000 people "
                      "may need assistance", {"source": "ICPAC GHACOF", "title": "GHACOF statement",
                                              "pub_date": "2026-05", "temporal": "forecast",
                                              "validation": "multi-agency-consensus",
                                              "url": "https://icpac.net/g.pdf"}, raw=b"pdf", filename="g.pdf")
        corpus.ingest("impact of past El Nino events on maize production and food security in "
                      "Kenya", {"source": "FAO GIEWS", "title": "El Nino impacts 2016",
                                "pub_date": "2016-08", "temporal": "retrospective",
                                "validation": "single-agency", "url": "https://fao.org/i.pdf"})
        p = assemble.assemble(country="kenya", crop="maize")
    assert p["status"] == "ok", p.get("note")
    return p


@pytest.fixture(autouse=True)
def _fresh_connect_text():
    """A client call under a fixture's stacks primes the server's 30 s connect-text cache;
    expire it after each test so no later test reads that state."""
    yield
    from app.mcp import server
    for mw in server.mcp.middleware:
        if type(mw).__name__ == "_LiveText":
            mw._at = 0.0


def _call(tool: str, args: dict) -> dict:
    """One real tool call; its structured reply as a client reads it."""
    from fastmcp import Client

    async def go():
        async with Client(mcp) as c:
            return (await c.call_tool(tool, args)).structured_content
    return asyncio.run(go())


# --------------------------------------------------------------------------- 1. the rule
def test_the_rule_sits_in_the_core_next_to_the_receipt_rule(log):
    """Tier 1, right after the canonical loop, inside the 2,048 chars an older client keeps;
    the web-search clause the answer-here section carried is merged into it, not repeated."""
    rule = instructions.PLATFORM_SOURCES
    full, core = instructions.compose(), instructions.core()
    at = full.index(rule)
    log("OUTPUT", f"rule {len(rule)} chars at {at}-{at + len(rule)}; core {len(core):,}; "
                  f"total {len(full):,}")
    assert len(rule) <= 300 and at + len(rule) <= budget.INSTRUCTIONS_CORE and rule in core
    assert full.index(instructions.LOOP) + len(instructions.LOOP) + 2 == at
    assert len(core) <= budget.INSTRUCTIONS_CORE and len(full) <= budget.INSTRUCTIONS
    # the check comes first and is explicit; only a source not served there is asked for
    for words in ("a brief holds only what the platform serves", "web results, only if asked, "
                  "follow it labelled UNRECEIPTED", "Check platform_capabilities(section='coverage') "
                  "first; not served there: ask the person for its link (never search), "
                  "contribute_submit kind=<kind>, stop."):
        assert words in rule, words
    assert "Never web-search" not in full and full.count("a brief holds only what") == 1
    assert "web search" in instructions.section_text("answer_here")       # the why stays


def test_the_run_and_build_prompts_carry_the_rule_and_the_publish_loop(log):
    """run_analysis taught verify_groundedness + report_id; both prompts now finish with
    publish_answer, carry the rule whole, and the build prompt bakes no web data in."""
    run, build = _prompt("run_analysis"), _prompt("build_a_tool")
    for name, text in (("run_analysis", run), ("build_a_tool", build)):
        log("OUTPUT", f"{name}: {len(text):,} chars")
        assert instructions.PLATFORM_SOURCES in text, name
        assert "publish_answer(pack_id, draft, question)" in text, name
        assert "verify_groundedness" not in text and "report_id" not in text, name
    assert "assemble_pack(country='Zambia', crop='maize')" in _prompt("run_analysis", country="Zambia")
    assert "bake no web data into a built page or app, not even in a labelled section" in build
    assert "never search the web for one" in build
    assert "every link it shows is one a pack or receipt carries" in build


# --------------------------------------------------------------------------- 2. the line
def test_the_kind_is_derived_from_each_kinds_own_words(monkeypatch, log):
    """No typed list: each kind row declares the words it is named by; a kind added later
    names itself, and with nothing named every kind whose form takes a source is offered."""
    assert _line(["document", "feed"]) == loop.not_served(
        ["no document in the library describes the season", "enso_outlook: feed unavailable"])
    every = loop.not_served("override must be a list of seasons")
    assert every == _line(staging.source_kinds()) and len(every) <= budget.NOTE_CHARS
    # a decline naming no kind is a caller's error or an outage: no contribution asked for
    assert loop.not_served("override must be a list of seasons", gap=False) == loop.NOT_A_GAP
    assert len(loop.NOT_A_GAP) <= budget.NOTE_CHARS and "contribute" not in loop.NOT_A_GAP
    # both lines say what tier 1 says of the web: only when asked, after the answer, labelled
    web = "only if the person asks, follow the answer labelled UNRECEIPTED, never inside it"
    assert web in every and web in loop.NOT_A_GAP and "fix the call" in loop.NOT_A_GAP
    assert "never search the web for one" in every and "Do not web-search" not in every
    assert set(staging.source_kinds()) == {k for k in staging.KINDS
                                          if "source" in staging._KINDS[k]["fields"]["required"]}
    monkeypatch.setattr(staging, "KINDS", (*staging.KINDS, "survey"))
    monkeypatch.setitem(staging._KINDS, "survey", {
        "what": "a household survey", "said_as": ("survey",),
        "fields": {"required": {"source": "who ran it"}, "optional": {}}})
    said = loop.not_served("no household survey covers this district")
    log("OUTPUT", said)
    assert "kind=survey;" in said and "survey" in staging.source_kinds()


def _declines_with(reply: dict, kinds, log, label):
    """`kinds` None: a caller's error or an outage, which says it is no coverage gap."""
    shaped = shape.shape(reply, "pack")
    log("OUTPUT", f"{label}: {reply['note'][:90]}... -> {reply['not_served'][:150]}")
    want = loop.NOT_A_GAP if kinds is None else _line(kinds)
    assert reply["status"] == "declined" and reply["not_served"] == want, label
    assert len(reply["not_served"]) <= budget.NOTE_CHARS
    assert list(shaped)[:3] == ["status", "note", "not_served"], list(shaped)


def test_every_decline_carries_the_line_with_the_kind_it_lacks(lc_env, monkeypatch, log):  # noqa: F811
    """At the seam, whichever way assemble_pack declines: an unknown raster layer, an unknown
    contributed vector layer, a place with no boundary ask for the source; an unknown pack, a
    bad override, an ambiguous place and an outage say they are no coverage gap."""
    base = dict(pack="land-cover", place="Test District", year=2000)
    _declines_with(assemble.assemble(**base, layers=["no_such_layer"]), ["raster"], log, "layer")
    _declines_with(assemble.assemble(**base, vectors=["no_such_rivers"]), ["vector"], log, "vector")
    _declines_with(assemble.assemble(pack="land-cover", place="Nepal", year=2000), ["vector"], log,
                   "no boundary")
    _declines_with(assemble.assemble(pack="no-such-pack"), None, log, "unknown pack")
    _declines_with(assemble.assemble(country="kenya", crop="maize", override=[{"x": 1}]), None,
                   log, "bad override")
    # through the tool, shaped as a client reads it, its own parameter check included
    _declines_with(_call("assemble_pack", {**base, "layers": ["no_such_layer"]}), ["raster"], log,
                   "tool: layer")
    _declines_with(_call("assemble_pack", {**base, "detail": "terse"}), None, log, "tool: detail")
    for exc, label in ((ValueError("'Palo Verde' names 2 different contributed areas. Pass one "
                                   "of them exactly as written here"), "ambiguous place"),
                       (OSError("geocoder unreachable"), "outage")):
        def gather(*_a, _exc=exc, **_k):
            raise _exc
        monkeypatch.setitem(packs.PACKS["land-cover"], "gather", gather)
        _declines_with(assemble.assemble(**base), None, log, label)


def test_a_risk_decline_names_the_hazard_map_it_lacks(offline_aoi, log):  # noqa: F811
    _declines_with(assemble.assemble(place="Testville", hazard="meteor"), ["raster"], log, "hazard")


def _carries_the_line(p: dict, log, label):
    """An ok pack: its gap list and gaps citation, then the one line after no_arithmetic;
    guidance, never stored with the evidence."""
    keys = list(shape.shape(p, "pack"))
    log("OUTPUT", f"{label}: {len(p['gaps'])} gap(s) -> {p['not_served'][:160]}")
    assert p["citations"][-1]["kind"] == "gaps"
    assert p["not_served"] == loop.not_served(p["gaps"]) and len(p["not_served"]) <= budget.NOTE_CHARS
    assert keys.index("no_arithmetic") < keys.index("not_served") < keys.index("required_sections")
    assert keys.index("not_served") < keys.index("citations")
    assert "not_served" not in (store.load_pack(p["pack_id"]) or {})


def test_every_gap_list_carries_the_line_through_to_the_receipt(lc_env, log):  # noqa: F811
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022)
    _carries_the_line(p, log, "land-cover")
    out = publish.answer(p["pack_id"], _draft(p, "Forest is mapped here [1]."), question="forest?")
    assert out["status"] == "ok" and out["not_served"] == p["not_served"]
    assert record.record(receipt_id=out["receipt_id"])["not_served"] == p["not_served"]
    assert "not_served" not in store.load_receipt(out["receipt_id"])
    # the one build policy rides the reply every builder gets back
    assert out["next_step"]["if_building_a_web_page"]["platform_sources"] == loop.BUILD_SOURCES


def test_the_risk_and_food_security_gap_lists_carry_it_too(offline_aoi, pack, log):  # noqa: F811
    _carries_the_line(assemble.assemble(place="Testville", hazard="flood"), log, "risk")
    _carries_the_line(pack, log, "food-security (live)")


# --------------------------------------------------------------------------- 3. the gate
CITS = [{"n": 1, "kind": "exposure", "source": "platform", "title": "people in the flood zone",
         "text": "3,215,433 people live in the flood zone; 12,040 ha of cropland lies inside it.",
         "url": "https://data.hub.example.org/flood/2024"},
        {"n": 2, "kind": "gaps", "source": "platform pack assembly", "title": "declared evidence gaps",
         "text": "Declared gaps in this evidence pack: (1) no document in the library."}]


def _gate(body: str, cits=CITS) -> dict:
    return fs.check_grounded("## Answer\n\n" + body, cits, sections=["## Answer"])


def test_the_gate_blocks_web_links_and_passes_the_packs_own(log):
    blocked = {"a web URL": f"See {WEB} for the outlook [1].",
               "a www link": "See www.example-weather.org/outlook for more [1].",
               "a bare domain": f"ReliefWeb ({DOMAIN}) reports more [1].",
               "a markdown link": "The [outlook](https://climate.example.net/x) says so [1].",
               "a page the pack does not carry": "See https://data.hub.example.org/flood/2024/x [1].",
               "a parent domain of the pack's host": "See hub.example.org or https://example.org [1].",
               "a country-code domain": "IPCC (ipcc.ch/report/ar6) and news.elmundo.cr report it [1].",
               "a country code outside the old regional list": "See minam.gob.pe/data and ine.gob.bo [1].",
               "a link shortener": "See bit.ly/3xYzAbc and t.co/AbC123 [1].",
               "another scheme": "Data at ftp://ftp.cpc.ncep.noaa.gov/precip/data [1].",
               "a protocol-relative link": "See //worldbank.org/en [1].",
               "an all-caps host": "See WORLDBANK.ORG [1].",
               "an obfuscated host": "See worldbank[.]org [1]."}
    for name, body in blocked.items():
        r = _gate(body)
        log("OUTPUT", f"{name}: passed={r['passed']} links={r['links_not_served']}")
        assert not r["passed"] and r["links_not_served"], name
        assert any("remove the link, or contribute the source so the platform serves it" in f
                   for f in r["failures"]), name
    own = "https://data.hub.example.org/flood/2024"
    served = {"the pack's own URL": f"It is published at {own} [1].",
              "the pack's host": "Its publisher, data.hub.example.org, states it [1].",
              "the receipt resolver": f"Receipt: {record._resolver_url('abcd1234abcd1234')} [1].",
              # the pack's own link however it is punctuated, fragmented or tagged
              "an em dash after it": f"Source: {own}—the pack's own [1].",
              "curly quotes": f"Source: “{own}” [1].", "guillemets": f"Source: «{own}» [1].",
              "an ellipsis": f"Source: {own}… [1].", "full-width punctuation": f"Source: {own}，more [1].",
              "a fragment": f"Source: {own}#page=2 [1].", "a query string": f"Source: {own}?utm_source=x [1].",
              # the server's own resources, by the scheme its resource addresses use
              "the server's resources": "Read servirplatform://places and servirplatform://pack/ab/gaps [1].",
              # not links at all
              "an app, a field, a missing space": "Open it in QGIS.app; stats.info is empty; 2020.net loss [1].",
              "code and file names": "The feature.id field, readme.md and era5.nc [1]."}
    for name, body in served.items():
        r = _gate(body)
        log("OUTPUT", f"{name}: passed={r['passed']}")
        assert r["passed"] and not r["links_not_served"], (name, r["failures"])
    assert not fs._links("chirps-v2.0.2020.nc, v3.0 and 2020.net")       # digit labels: no host
    with_link = [{**CITS[0], "text": CITS[0]["text"] + f" Outlook: {WEB}; see {DOMAIN}."}, CITS[1]]
    for name in ("a web URL", "a bare domain"):
        assert _gate(blocked[name], with_link)["passed"], name       # served once the pack carries it


def test_the_gate_reads_magnitudes_and_numbers_in_words(log):
    """'3.2 million' was read as 3.2, a small fraction under the >= 10 rule, and never checked.
    Each figure is blocked while the pack does not hold it and passes once it does, with the
    rounding of the unit it is written in."""
    assert fs._spelled("3.2 million")[0] == "3200000" and "3.2" not in fs._number_set("3200000")
    cases = {"7.4 million people": "7,400,000 people", "1.5 billion dollars": "US$1,500,000,000",
             "fifteen thousand hectares": "15,000 ha", "45k households": "45,000 households",
             "two hundred schools": "200 schools", "US$9.8M in damage": "9,800,000 dollars",
             # abbreviations: read as the magnitude they are, a currency code before or not
             "7.4m people": "7,400,000 people", "USD 7.4m in losses": "7,400,000 dollars",
             "K7.4m": "7,400,000 kwacha", "a 7.4b loss": "7,400,000,000 dollars",
             "7.4 millions": "7,400,000", "7.4 Mha of cropland": "7,400,000 ha",
             "2.8 Mt of maize": "2,800,000 tonnes", "half a million people": "500,000 people",
             # a count that looks like a year is a count
             "two thousand people": "2,000 people", "2 thousand people": "2,000 people",
             "2,000 people": "2,000 people", "nineteen hundred people": "1,900 people"}
    for figure, held in cases.items():
        body = f"About {figure} [1]."
        out, now = _gate(body), _gate(body, [{**CITS[0], "text": CITS[0]["text"] + f" {held}."}, CITS[1]])
        log("OUTPUT", f"{figure}: absent -> {out['numbers_unverified']}; held -> passed={now['passed']}")
        assert not out["passed"] and out["numbers_unverified"], figure
        assert now["passed"], (figure, now["failures"])
    rounded = _gate("About 3.2 million people and twelve thousand hectares [1].")
    log("OUTPUT", f"rounded from the pack: {rounded['numbers_derived']}")
    assert rounded["passed"] and {"3200000", "12000"} <= set(rounded["numbers_derived"])
    # a year stays a year, a span of time is not a figure, 'two point five million' is refused
    assert _gate("In 2020 people moved; over the past twenty years and the next twelve months [1].")["passed"]
    unread = _gate("Two point five million people [1].")
    assert not unread["passed"] and unread["numbers_unreadable"] == ["Two point five million"]
    # a bare '7.4m' may be metres: as the pack writes it, it passes; else write it in full
    assert _gate("The barrier stands at 7.4m [1].", [{**CITS[0], "text": "a barrier 7.4 m high."}, CITS[1]])["passed"]
    bare = _gate("Losses reached 7.4m [1].")
    assert not bare["passed"] and "write the figure in full" in bare["failures"][0]
    assert _gate("A 30m resolution map; 2.5m above sea level; Figure 2b; Sentinel-2B; winds of 65 kt [1].",
                 [{**CITS[0], "text": "30 m resolution; winds of 65 knots"}, CITS[1]])["passed"]


def test_only_a_quantity_vouches_by_rounding_or_conversion(log):
    """Review case (e): legend codes, pixel counts, a DOI and table cells vouched for web figures.
    A conversion now needs units named on both sides and 3+ significant digits, the magnitude
    window is half-open, and only a quantity vouches."""
    cits = [{"n": 1, "kind": "class_area", "source": "ESA WorldCover; doi:10.5281/zenodo.7254221",
             "title": "land cover by class", "text": (
                 "282.5 km² (28,250 ha) classified of 282.5 km² inside the boundary. By class: Tree "
                 "cover 137.7 km² (48.8%); Grassland 85.5 km² (30.3%). Pixels: 3,357,982 classified "
                 "of 3,357,982 inside the boundary. Grid: 36000×36000 px; EPSG:4326.")},
            {"n": 2, "kind": "document", "source": "IPCC", "title": "Wetlands supplement", "text": (
                "swamp Costa Rica Continuous 2930 Chamber Annual\nmarsh Costa Rica Intermittent "
                "3500 Chamber Annual")},
            {"n": 3, "kind": "layer_values", "source": "ESA WorldCover", "title": "values", "text": (
                "209,984 of 209,984 cells carry a value; equal values come in 5764 patch(es); "
                "10 (Tree cover) 137.7 km² (48.7%); 80 (Permanent water bodies) 21.95 km² (7.8%); "
                "min 10, max 95. Legend: 10=Tree cover; 20=Shrubland; 40=Cropland.")},
            {"n": 4, "kind": "gaps", "source": "platform pack assembly", "title": "gaps",
             "text": "Declared gaps in this evidence pack: (1) none."}]
    fire = ("A lightning fire that began on 28 May 2026 burned about 4,000 hectares of the park, "
            "and more than 1,000 hectares of wetland were rehabilitated over five years [4].")
    out = _gate(fire, cits)
    log("OUTPUT", f"(e) fire: {out['numbers_unverified']}")
    assert not out["passed"] and {"4000", "1000"} <= set(out["numbers_unverified"])
    for web in ("About 3 million hectares burned [1].", "About 4 thousand hectares [1].",
                "About 4k hectares [1].", "About 3k hectares [1].", "About 7 million birds [1].",
                "About 1,200 people were displaced [1].", "About 4.3 thousand hectares [1]."):
        r = _gate(web, cits)
        log("OUTPUT", f"{web} -> {r['numbers_unverified']}")
        assert not r["passed"], web
    for own in ("Tree cover is 13,770 ha [1].", "About 28 thousand ha [1].",
                "About 3.4 million pixels [1].", "About 2.2 thousand ha of water [3]."):
        r = _gate(own, cits)
        log("OUTPUT", f"{own} -> {r['numbers_derived']}")
        assert r["passed"], (own, r["failures"])


# --------------------------------------------------------------------------- 4. every pack
def _draft(p: dict, first: str) -> str:
    return "\n\n".join(f"{s}\n\n" + (first if i == 0 else "Evidence for this section is cited here [1].")
                       for i, s in enumerate(p["required_sections"]))


def _largest_figure(p: dict):
    """(citation, value) for the largest whole quantity a citation states: never a code, an
    id or a pixel count, which vouch for nothing by rounding."""
    best = None
    for c in p["citations"]:
        if c.get("kind") == "gaps":
            continue
        for v, tok, _unit, _sig, written, count_of, cell, _kind in fs._vouchers(str(c.get("text") or "")):
            if written and not count_of and not cell and fs._load_bearing(tok) and v == int(v) \
                    and (best is None or v > best[1]):
                best = (c, int(v))
    return best


def _magnitude(v: float) -> tuple[str, float]:
    scale, word = (10 ** 6, "million") if v >= 10 ** 6 else (10 ** 3, "thousand")
    return f"{v / scale:.1f} {word}", scale


def _still_passes_and_leaks_block(p: dict, log, label):
    """The pack's own figure (as digits and as a magnitude) and its own link pass the gate
    through publish_answer; a web link, a bare domain and an inflated magnitude are blocked."""
    assert p["status"] == "ok", p.get("note")
    hit = _largest_figure(p)
    link = next((c["url"] for c in p["citations"] if str(c.get("url") or "").startswith("http")), None)
    first = (f"The pack states {hit[1]:,} [{hit[0]['n']}]." if hit else "The pack is cited here [1].")
    if hit and hit[1] >= 1000:
        first += f" That is about {_magnitude(hit[1])[0]} [{hit[0]['n']}]."
    if link:
        first += f" Source: {link} [1]."
    ok = publish.answer(p["pack_id"], _draft(p, first), question="q")
    log("OUTPUT", f"{label}: figure {hit[1] if hit else None}, link {link} -> {ok['status']}")
    assert ok["status"] == "ok", ok.get("failures")
    for leak in (f" More at {WEB}.", f" See also {DOMAIN}."):
        out = publish.answer(p["pack_id"], _draft(p, first + leak), question="q")
        assert out["status"] == "blocked", (label, leak)
        assert any("contribute the source so the platform serves it" in f for f in out["failures"])
    if hit and hit[1] >= 1000:
        evid = fs._figures(" ".join(str(c.get("text") or "") for c in p["citations"]))
        v = next(k * hit[1] for k in (7, 11, 13, 17) if all(
            abs(float(e) - k * hit[1]) > _magnitude(k * hit[1])[1] for e in evid))
        out = publish.answer(p["pack_id"], _draft(p, f"About {_magnitude(v)[0]} [{hit[0]['n']}]."), "q")
        log("OUTPUT", f"{label}: inflated '{_magnitude(v)[0]}' -> {out['status']}")
        assert out["status"] == "blocked" and "numbers that appear in no citation" in out["failures"][0]
    # an invented round count in digits: no x100 'conversion' from an unrelated figure
    evid = fs._figures(" ".join(str(c.get("text") or "") for c in p["citations"]) + str(p.get("stats")))
    for fig in [f for f in range(1100, 9900, 100) if str(f) not in evid][:6]:
        out = publish.answer(p["pack_id"], _draft(p, f"An estimated {fig:,} people were displaced [1]."), "q")
        assert out["status"] == "blocked", (label, fig, out.get("numbers_derived"))


def test_a_land_cover_pack(lc_env, log):  # noqa: F811
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022)
    _still_passes_and_leaks_block(p, log, "land-cover")


def test_a_rounding_keeps_its_kind_its_unit_and_its_precision(log):
    """A rounding never vouches across what a figure counts or the unit it names; a magnitude
    naming its kind needs a pack figure of that kind; a 1-significant-figure magnitude is too
    coarse to convert; a record id after a proper noun vouches for nothing."""
    cits = [{**CITS[0], "text": CITS[0]["text"] + " A carbon stock of 4,806,646; Forest 30.2 km²; "
                                                  "see Zenodo 6894273 description."}, CITS[1]]
    passes = ("About 3 million people [1].", "About 12 thousand ha [1].", "About 5 million [1].",
              "About 3.0 thousand ha of forest [1].")
    blocks = ("About 3 million hectares [1].", "About 12 thousand people [1].",
              "About 5 million people [1].", "About 3 thousand ha of forest [1].",
              "About 7 million people [1].", "About 1.2 thousand km² of cropland [1].")
    for body in passes:
        r = _gate(body, cits)
        log("OUTPUT", f"{body} -> {r['numbers_derived']}")
        assert r["passed"], (body, r["failures"])
    for body in blocks:
        r = _gate(body, cits)
        log("OUTPUT", f"{body} -> {r['numbers_unverified']}")
        assert not r["passed"], (body, r["numbers_derived"])


def test_the_gate_notes_ride_the_reply_and_the_receipt(lc_env, log):  # noqa: F811
    """Which pack figure vouched for each figure, and every warning, on publish_answer's reply,
    the stored receipt and a later record_receipt, not only in the report."""
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022)
    evid = fs._figures(" ".join(str(c.get("text") or "") for c in p["citations"]))
    c, ha = next((c, f"{float(tok) * 100:g}") for c in p["citations"] if c.get("kind") != "gaps"
                 for _v, tok, unit, sig, written, _n, _cell, _kind in fs._vouchers(str(c.get("text") or ""))
                 if unit == "km2" and sig >= 3 and written and f"{float(tok) * 100:g}" not in evid)
    out = publish.answer(p["pack_id"], _draft(p, f"That class covers {ha} ha [{c['n']}]."), "q")
    log("OUTPUT", f"{ha} ha: {out.get('numbers_derived')}")
    assert out["status"] == "ok" and out["numbers_derived"], out.get("failures")
    again = record.record(receipt_id=out["receipt_id"])
    stored = store.load_receipt(out["receipt_id"])
    for key in record.GATE_NOTES:
        assert key in out and key in again and key in stored, key
    assert again["numbers_derived"] == stored["numbers_derived"] == out["numbers_derived"]


def test_a_risk_pack(offline_aoi, log):  # noqa: F811
    _still_passes_and_leaks_block(assemble.assemble(place="Testville", hazard="flood"), log, "risk")


def test_the_food_security_pack(pack, log):  # noqa: F811
    _still_passes_and_leaks_block(pack, log, "food-security (live)")


def test_a_contributed_table_and_method_pack(env, monkeypatch, log):  # noqa: F811
    """A freshly contributed table and a method over it, whose declared source is a link: the
    method's figure and its own link pass; a page beside it does not."""
    _units(env)
    staging.submit("method", _threshold(source="Hub restoration survey, "
                                               "https://hub.example.org/restoration-units"))

    def gather(target, focus, trace, extras):
        return ([{"n": 1, "kind": "area", "retrieval": "computed-at-pack-time", "source": "test",
                  "title": "the area", "text": "Test Province"}], [],
                {"queries": None, "_area": {"name": "Test Province", "geometry": BOX}})
    monkeypatch.setitem(packs.PACKS["land-cover"], "gather", gather)
    p = assemble.assemble(pack="land-cover", place="Test Province", year="2020")
    _carries_the_line(p, log, "contributed table + method")
    _still_passes_and_leaks_block(p, log, "contributed table + method")
    n = next(c["n"] for c in p["citations"] if "restorable" in str(c.get("text")))
    own = publish.answer(p["pack_id"], _draft(p, "The hub's survey, https://hub.example.org/"
                                                 f"restoration-units, gives 1,871 ha [{n}]."), "q")
    beside = publish.answer(p["pack_id"], _draft(p, "See https://hub.example.org/other-page for "
                                                    f"1,871 ha [{n}]."), "q")
    log("OUTPUT", f"own link -> {own['status']}; a page beside it -> {beside['status']}")
    assert own["status"] == "ok", own.get("failures")
    assert beside["status"] == "blocked"


# --------------------------------------------------------------------------- 5. the final pass
def test_a_span_of_time_is_exempt_in_words_and_checked_in_digits(log):
    """In words a span of time is no figure; in digits ('15 years') it is checked as at HEAD,
    so an invented span blocks until the pack states it."""
    words = _gate("Over the past twenty years, for ten to fifteen months and twenty-five years "
                  "ago, mangroves held [1].")
    assert words["passed"], words["failures"]
    held_cits = [{**CITS[0], "text": CITS[0]["text"] + " Over 15 years, 20 years and 12 months."},
                 CITS[1]]
    for digits in ("Over the past 15 years mangroves held [1].", "For the last 20 years it held [1].",
                   "Within 12 months it recovered [1]."):
        out, held = _gate(digits), _gate(digits, held_cits)
        log("OUTPUT", f"{digits} -> {out['numbers_unverified']}; held -> passed={held['passed']}")
        assert not out["passed"] and out["numbers_unverified"], digits
        assert held["passed"], (digits, held["failures"])


def test_a_year_shaped_count_reads_the_same_in_a_draft_and_its_pack(log):
    """'2000 people' is a count on both sides: invented it blocks, the pack's own vouches, a year
    the pack states never does; 'In 2020 farmers planted' stays a year on both sides."""
    count = "About 2000 people were displaced [1]."
    with_text = lambda extra: [{**CITS[0], "text": CITS[0]["text"] + extra}, CITS[1]]  # noqa: E731
    out = _gate(count)
    assert not out["passed"] and "2000" in out["numbers_unverified"]
    assert _gate(count, with_text(" 2000 people moved."))["passed"]
    assert not _gate(count, with_text(" The floods of 2000 were severe."))["passed"]
    assert _gate("In 2020 farmers planted less maize [1].")["passed"]
    pack_side = fs._merged([fs._evidence("In 2020 farmers planted less; 2000 people moved.")])
    log("OUTPUT", f"years the pack states: {sorted(pack_side.years)}")
    assert "2020" in pack_side.years and "2000" not in pack_side.years


def test_the_code_lists_are_data_and_every_code(log):
    """Country domains and currencies come from conf/gate_codes.yml (every ISO 3166 and ISO 4217
    code) and Unicode's own currency symbols; a currency the lists lack still says money from
    what follows it, so a rounding never crosses kinds."""
    import yaml
    codes = yaml.safe_load(get_settings().gate_codes_path.read_text())
    iso = set(codes["iso_3166_alpha2"].lower().split())
    assert len(iso) >= 249 and fs._CC == iso - set(codes["english_word_codes"].lower().split())
    assert {"pe", "bo", "ec", "id"} & fs._CC == {"pe", "bo", "ec"}       # 'id' is an English word
    for mark in ("₡", "₮", "₭", "៛", "؋", "MNT ", "CRC ", "KSh ", "K"):
        assert fs._currency().search(f"cost {mark}"), mark
    for money in ("₡3 million", "MNT 3 million", "3 million kip", "3 million colones",
                  "Le 3 million in damage"):
        r = _gate(f"Losses reached {money} [1].")              # the pack holds 3,215,433 PEOPLE
        log("OUTPUT", f"{money}: passed={r['passed']} derived={r['numbers_derived']}")
        assert not r["passed"], (money, r["numbers_derived"])
    assert _gate("About 3 million people live there [1].")["passed"]


def _verdict_links_the_resolver(p: dict, log, label):
    """A pack whose row offers no receipt-bound embed: the verdict is the resolver link, stated
    UNVERIFIED, and the web-page step says the same (it never suggests provenance_graph)."""
    out = publish.answer(p["pack_id"], _draft(p, "The pack is cited here [1]."), "q")
    assert out["status"] == "ok", out.get("failures")
    rw, page = out["render_with"], out["next_step"]["if_building_a_web_page"]
    log("OUTPUT", f"{label}: verdict {rw['verdict']}; page {page.get('link')}")
    assert rw["verdict"]["url"] == out["public_resolver"] and "UNVERIFIED" in rw["verdict"]["note"]
    assert "provenance" not in rw and "provenance_graph" not in str(page)
    assert page["link"] == out["public_resolver"] and "UNVERIFIED" in page["note"]
    assert record.record(receipt_id=out["receipt_id"])["render_with"]["verdict"] == rw["verdict"]


def test_a_land_cover_receipt_links_the_resolver_unverified(lc_env, monkeypatch, log):  # noqa: F811
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000)
    _verdict_links_the_resolver(p, log, "land-cover")
    # decided from the pack row, not the pack's name: a row that offers the graph gets it
    monkeypatch.setitem(packs.PACKS["land-cover"], "components", ("provenance_graph",))
    out = publish.answer(p["pack_id"], _draft(p, "The pack is cited here [1]."), "q")
    assert out["render_with"]["verdict"]["component"] == "provenance_graph"
    assert out["next_step"]["if_building_a_web_page"]["call"]["args"]["component"] == "provenance_graph"


def test_a_risk_receipt_links_the_resolver_unverified(offline_aoi, log):  # noqa: F811
    _verdict_links_the_resolver(assemble.assemble(place="Testville", hazard="flood"), log, "risk")


def test_the_food_security_receipt_embeds_what_its_row_offers(pack, log):
    out = publish.answer(pack["pack_id"], _draft(pack, "The pack is cited here [1]."), "q")
    rw, page = out["render_with"], out["next_step"]["if_building_a_web_page"]
    log("OUTPUT", f"verdict {rw['verdict']}")
    assert rw["verdict"]["component"] == page["call"]["args"]["component"] == "provenance_graph"
    assert rw["provenance"]["component"] == "provenance_graph"


def test_compose_run_declines_carry_the_line(monkeypatch, log):
    """compose_run declines at the same seam: a caller's error says it is no coverage gap; the
    runner's own decline names the kind it lacks."""
    for reply in (compose.run(composition="no.such", question="q"), compose.run(question=" "),
                  compose.run(question="q", override=[{"x": 1}])):
        assert reply["status"] == "declined" and reply["not_served"] == loop.NOT_A_GAP, reply

    def runner(*_a):
        return {"declined": True, "decline_reason": "no hazard map of meteor strikes covers it",
                "citations": [], "grounded": None, "trace": []}
    monkeypatch.setitem(compose._RUNNERS, "risk.brief", runner)
    out = compose.run(composition="risk.brief", question="meteor risk?")
    log("OUTPUT", out["not_served"][:120])
    assert out["status"] == "declined" and out["not_served"] == _line(["raster"])
    keys = list(shape.order(out))
    assert keys.index("note") + 1 == keys.index("not_served") < keys.index("citations")


def test_verify_and_publish_say_what_the_gate_cannot_read(lc_env, log):  # noqa: F811
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000)
    draft = _draft(p, "Two point five million hectares burned [1].")
    v = verify.groundedness(draft, p["pack_id"])
    keys = list(shape.order(v))
    log("OUTPUT", f"unreadable {v['numbers_unreadable']}")
    assert not v["passed"] and v["numbers_unreadable"] == ["Two point five million"]
    assert keys.index("numbers_unreadable") == keys.index("links_not_served") + 1
    b = publish.answer(p["pack_id"], draft, "q")
    assert b["status"] == "blocked" and b["numbers_unreadable"] == v["numbers_unreadable"]
    assert b["links_not_served"] == []


def test_a_table_not_landed_here_is_named_by_its_file_never_a_host_path(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path)
    spec = {"title": "t", "fetch": {"path": "/srv/staging-host/landed/rain.csv", "sha256": "x",
                                    "columns": {"v": "v"}}}
    with pytest.raises(feeds.FeedDecline) as err:
        feeds._adapt_generic_csv({}, spec)
    msg = str(err.value)
    assert "'rain.csv' is not landed on this host" in msg
    assert "/srv/staging-host" not in msg and str(tmp_path) not in msg


def test_the_gate_reads_the_evidence_and_the_draft_once(monkeypatch):
    """Each citation's text and fields, and the platform's own figures, are read once per check;
    each paragraph once (the attribution check reuses both)."""
    real, calls = fs._spelled, []
    monkeypatch.setattr(fs, "_spelled", lambda text, ambiguous=True: calls.append(ambiguous)
                        or real(text, ambiguous))
    cits = [CITS[0], {**CITS[0], "n": 2, "text": "4,000 ha of forest."}, {**CITS[1], "n": 3}]
    fs.check_grounded("## Answer\n\nA 12,040 ha figure [1].\n\nAnd 4,000 ha [2].", cits,
                      sections=["## Answer"], extra_evidence={"area_km2": 120.4})
    assert calls.count(False) == 2 * len(cits) + 1 and calls.count(True) == 3


def test_a_missing_code_list_never_stops_the_gate(tmp_path, log):
    """Deployed without conf/gate_codes.yml, the gate still imports and runs, and says which
    checks are off: a missing data file must never take publishing down."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src"
    env = {**os.environ, "GATE_CODES_PATH": str(tmp_path / "absent.yml"),
           "CACHE_DIR": str(tmp_path / "cache"), "PYTHONPATH": str(src)}
    run = subprocess.run([sys.executable, "-c", "from app.food_security import synthesis as s; "
                          "print(len(s._CC), callable(s.check_grounded))"],
                         env=env, capture_output=True, text=True, timeout=120)
    log("OUTPUT", f"rc={run.returncode} out={run.stdout.strip()} err={run.stderr.strip()[-160:]}")
    assert run.returncode == 0, run.stderr
    assert run.stdout.split() == ["0", "True"]
    assert "gate code lists unreadable" in run.stderr
