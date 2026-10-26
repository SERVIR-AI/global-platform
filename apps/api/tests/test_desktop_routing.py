"""G14: a client that defers tools reads no instructions. start_here routes a question to the pack,
stack, place and years with the rules; a lookup of a product a pack maps steers to it; and
GRP_SERVE_INSTRUCTIONS=0 serves none, as claude.ai and Claude Desktop see it."""
import asyncio
import json
import logging
from pathlib import Path

import pytest

from app.config import get_settings
from test_fast_connect import LIVE_STACKS, MORE_COUNTRIES, install, live_sized

SALADO = ("Using servirplatform, inside the Manglares El Salado reserve in Ecuador, has any mangrove "
          "become shrimp ponds since 2020, according to MapBiomas?")
FLOOD = "How many people would be exposed to a 100-year flood in Battambang, Cambodia?"
MAIZE = "Are there early signs of maize failure in Kenya's Trans-Nzoia this season?"
UNCOVERED = "Is coral reef bleaching getting worse around Fiji?"
POINTS = "mapbiomas_ec_lulc_points"
# The validation feed live serves: sample points of a product the land-cover pack maps.
POINTS_FEED = {
    "status": "available", "pack": "land-cover", "adapter": "generic_json", "contributed": True,
    "title": "LULC training data for Ecuador - ML", "source": "MapBiomas Ecuador - EcoCiencia, 2025",
    "description": "37 land-use / land-cover sample points used to train and validate the classifier",
    "validation": "single-agency", "residency": "external call-out",
    "fetch": {"url": "https://example.invalid/points.json", "records_path": "rows",
              "fields": {"class": "class", "year": "year", "lat": "lat", "lon": "lon"}}}


def _poly(name, x0, y0, w, h, **props):
    return {"type": "Feature", "properties": {"name": name, **props},
            "geometry": {"type": "Polygon", "coordinates": [[[x0, y0], [x0 + w, y0], [x0 + w, y0 + h],
                                                              [x0, y0 + h], [x0, y0]]]}}


def _boundaries(tmp_path, reg, layer, title, country, features) -> None:
    """A contributed named polygon layer (a gazetteer) in the registry `reg` installs."""
    p = tmp_path / f"{layer}.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
    reg["vectors"][layer] = {"title": title, "geometry": "polygons", "countries": [country],
                             "name_field": "name", "local_path": str(p), "features": len(features)}


def _registry(tmp_path, monkeypatch, squeezed=False) -> dict:
    """The live-like registry with Ecuador's protected areas as a boundary layer and the MapBiomas
    validation points feed; `squeezed` adds live's other countries (as in test_fast_connect)."""
    from app.graph.geo import ingest, vectors
    from app.mcp import feeds, registry
    reg = live_sized(tmp_path, LIVE_STACKS)
    _boundaries(tmp_path, reg, "ec_protected_areas", "Protected areas of Ecuador (MAATE SNAP)", "Ecuador", [
        _poly("Manglares El Salado", -79.98, -2.26, 0.05, 0.05, country="Ecuador"),
        _poly("Manglares Churute", -79.70, -2.50, 0.20, 0.20, country="Ecuador"),
        _poly("El Salado", -78.50, -0.50, 0.05, 0.05, country="Ecuador")])
    if squeezed:                         # live's other countries and its Taiwan earthquake hazard
        conf = reg["settings"]
        cat, schema = (json.loads(conf[k].read_text()) for k in ("tiffs_config_path", "raster_schema_path"))
        cat["hazard_earthquake_taiwan_rp475"] = {
            "title": "Earthquake hazard, 1-in-475 year PGA on rock, Taiwan", "countries": ["Taiwan"],
            "role": "hazard", "source": "a national seismic hazard model", "license": "CC-BY-4.0",
            "vintage": "2024", "local_path": "hazard_earthquake_taiwan_rp475.tif", "contributed": True}
        schema["layers"]["hazard_earthquake_taiwan_rp475"] = {"dtype": "float32", "valid_min": 0,
                                                              "valid_max": 2, "units": "g"}
        for k, v in (("tiffs_config_path", cat), ("raster_schema_path", schema)):
            conf[k].write_text(json.dumps(v))
        for i, c in enumerate(MORE_COUNTRIES):
            reg["vectors"][f"more_layer_{i:02d}"] = {
                "title": f"Protected areas ({c}, WDPA) set {i}", "geometry": "polygons", "countries": [c],
                "local_path": f"vectors/more_layer_{i:02d}.geojson"}
    install(reg, monkeypatch.setattr, monkeypatch.setitem)
    real = vectors.master_path
    monkeypatch.setattr(vectors, "master_path",
                        lambda e: Path(e["local_path"]) if Path(e["local_path"]).is_absolute() else real(e))
    monkeypatch.setitem(registry.FEEDS, POINTS, dict(POINTS_FEED))
    monkeypatch.setattr(feeds, "_checked_url", lambda url: [])          # offline: no DNS check
    ingest._GAZETTEER.clear()
    return reg


@pytest.fixture
def salado(tmp_path, monkeypatch):
    from app.graph.geo import ingest
    reg = _registry(tmp_path, monkeypatch)
    yield reg
    ingest._GAZETTEER.clear()


def _call(tool, args) -> tuple[str, dict]:
    """(the text a client reads, the structured reply) for one real tool call."""
    from fastmcp import Client
    from app.mcp.server import mcp

    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args)
            return r.content[0].text, r.structured_content
    return asyncio.run(go())


def _next(reply: dict, tool: str) -> dict:
    return next(c["args"] for c in reply["next_calls"] if c["tool"] == tool)


# --------------------------------------------------------------------------- start_here routes
def test_start_here_routes_the_el_salado_question_to_the_mapbiomas_stack(salado, log):
    """The question live Desktop answered from 37 sample points: start_here sends it to the
    land-cover pack's MapBiomas Ecuador stack over the reserve's own boundary, since 2020 to the
    stack's latest year, with Mangrove detailed; the points feed is named as sample points."""
    text, out = _call("start_here", {"question": SALADO})
    log("OUTPUT", f"{len(text):,} chars: {json.dumps(out, ensure_ascii=False)[:2500]}")
    from app.mcp import budget
    assert out["status"] == "ok" and out["route"][0]["pack"] == "land-cover"
    a = _next(out, "assemble_pack")
    assert a["pack"] == "land-cover" and a["place"] == "Manglares El Salado"
    assert a["stack"] in ("landcover_ec_mapbiomas", "landcover_ec_mapbiomas_cutoff")
    assert a["year"] == 2020 and a["year_to"] == 2025 and "Mangrove" in a["classes"]
    assert _next(out, "resolve_place_time") == {"place": "Manglares El Salado"}
    assert _next(out, "publish_answer")["question"] == SALADO
    assert "Protected areas of Ecuador" in out["filled_from"]["place"]
    assert "MapBiomas" in out["filled_from"]["stack"] and "MapBiomas" in out["route"][0]["matched"]
    lookup = next(r for r in out["related_lookups"] if r["args"].get("dataset") == POINTS)
    assert "sample points" in lookup["what"] and "not for" in lookup["what"]
    assert len(out["rules"]) == 4 and any("publish_answer" in r for r in out["rules"])
    assert len(text) <= budget.REPLY_CHARS and len(text) < 6000
    assert list(out)[-1] == "your_next_output" and "stack=" in out["your_next_output"]
    _t, es = _call("start_here", {"question": "¿En la reserva Manglares El Salado de Ecuador, el manglar "
                                              "se convirtió en camaroneras desde 2020, según MapBiomas?"})
    e = _next(es, "assemble_pack")
    assert (e["place"], e["year"], e["year_to"], e["stack"]) == ("Manglares El Salado", 2020, 2025, a["stack"])


def test_a_flood_question_routes_to_risk_and_a_maize_question_to_food_security(salado, log):
    """Two other packs on the same registry: a 100-year flood with people exposed goes to risk
    with that return period's map and the place the question names; maize in Kenya to food
    security with its crop and country, though Kenya also has land-cover stacks."""
    _t, flood = _call("start_here", {"question": FLOOD})
    _t, maize = _call("start_here", {"question": MAIZE})
    log("OUTPUT", f"flood: {_next(flood, 'assemble_pack')}; maize: {_next(maize, 'assemble_pack')}")
    f = _next(flood, "assemble_pack")
    assert flood["route"][0]["pack"] == "risk"
    assert f["hazard"] == "flood_rp100" and f["place"] == "Battambang, Cambodia"
    m = _next(maize, "assemble_pack")
    assert maize["route"][0]["pack"] == "food-security" and m["crop"] == "maize" and m["country"] == "Kenya"
    assert not any(c["tool"] == "resolve_place_time" for c in maize["next_calls"])


def test_an_uncovered_subject_is_declined_with_a_contribute_pointer(salado, log):
    """Nothing serves coral reefs: not covered, never a guess at a pack — ask the person for the
    source (the contribute pointer names the kinds) after checking the coverage section."""
    text, out = _call("start_here", {"question": UNCOVERED})
    log("OUTPUT", f"{len(text):,} chars: {out}")
    assert out["status"] == "not_covered" and "route" not in out
    assert "contribute_submit kind=" in out["not_served"] and "never search the web" in out["not_served"]
    assert out["next_calls"] == [{"tool": "platform_capabilities", "args": {"section": "coverage"},
                                  "why": out["next_calls"][0]["why"]}]
    assert len(out["rules"]) == 4


def test_a_named_product_that_does_not_cover_the_place_is_said_not_swapped(salado, log):
    """MapBiomas over a Kenyan place: no MapBiomas stack covers Kenya, so none is filled and the
    gap says where it does map, with the contribute pointer."""
    _t, out = _call("start_here", {"question": "How much mangrove does Lamu in Kenya have according to MapBiomas?"})
    log("OUTPUT", {k: out.get(k) for k in ("filled_from", "gaps")})
    a = _next(out, "assemble_pack")
    assert out["route"][0]["pack"] == "land-cover" and "stack" not in a
    assert any("MapBiomas" in g and "Ecuador" in g and "Kenya" in g for g in out["gaps"])
    assert "contribute_submit" in out["gaps"][-1]


def test_freshly_contributed_stack_feed_boundary_and_pack_route_with_no_code_change(salado, tmp_path, monkeypatch, log):
    """A stack lands in the gate's catalog file, a boundary layer and a points feed are approved,
    and an extension pack with its own choices is dropped in: each routes at once; a pack that
    declares no routing words is named, never skipped."""
    from app.graph.geo import ingest
    from app.mcp import packs, registry
    get_settings().tiffs_contrib_path.write_text(json.dumps({"landcover_bnt_bs": {
        "kind": "stack", "role": "landcover", "title": "Bahamas National Trust mangrove extent 2024",
        "countries": ["Bahamas"], "legend": {1: "Mangrove", 2: "Not mangrove"}, "years": [2018, 2024],
        "contributed": True, "source": "Bahamas National Trust", "license": "CC-BY-4.0",
        "extent": {"lon": [-78.5, -77.5], "lat": [24.0, 25.0]}}}))
    reg = {"vectors": {}}
    _boundaries(tmp_path, reg, "bs_parks", "Bahamas national parks (BNT)", "Bahamas",
                [_poly("Andros West Side National Park", -78.2, 24.3, 0.3, 0.3, country="Bahamas")])
    from app.graph.geo import vectors
    landed = vectors.landed()
    monkeypatch.setattr(vectors, "landed", lambda: {**landed, **reg["vectors"]})
    ingest._GAZETTEER.clear()
    monkeypatch.setitem(registry.FEEDS, "bnt_mangrove_plots", {
        "status": "available", "pack": "land-cover", "adapter": "generic_json",
        "title": "Bahamas National Trust mangrove survey plots", "source": "Bahamas National Trust",
        "fetch": {"url": "https://example.invalid/p.json", "fields": {"latitude": "a", "longitude": "b"}}})
    q = "How much mangrove was lost in Andros West Side National Park since 2018, according to the Bahamas National Trust map?"
    _t, out = _call("start_here", {"question": q})
    a = _next(out, "assemble_pack")
    log("OUTPUT", f"{a}; related {out.get('related_lookups')}")
    assert a["stack"] == "landcover_bnt_bs" and a["place"] == "Andros West Side National Park"
    assert a["year"] == 2018 and a["year_to"] == 2024
    assert any(r["args"].get("dataset") == "bnt_mangrove_plots" and "sample points" in r["what"]
               for r in out["related_lookups"])
    gather = (lambda *a, **k: ([], [], None))
    monkeypatch.setitem(packs.PACKS, "reef", {
        "display_name": "Reef Platform", "target_keys": ("place", "year"),
        "target_doc": {"place": "a reef site", "year": "a survey year"},
        "use_for": "coral cover and bleaching on surveyed reefs", "gather": gather, "sections": lambda *a: []})
    monkeypatch.setitem(packs.PACKS, "bare", {
        "display_name": "Bare Platform", "target_keys": ("hazard",), "target_doc": {"hazard": "x"},
        "gather": gather, "sections": lambda *a: []})
    _t, reef = _call("start_here", {"question": UNCOVERED})
    log("OUTPUT", f"reef: {reef['route']}; {reef.get('packs_without_routing_words')}")
    assert reef["status"] == "ok" and reef["route"][0]["pack"] == "reef"
    assert _next(reef, "assemble_pack")["pack"] == "reef"
    assert any(s.startswith("bare: declares no routing words") for s in reef["packs_without_routing_words"])


def test_a_passed_pack_and_years_win_and_a_bad_one_declines(salado, log):
    """The caller's pack, place and years are used as passed; an unknown pack declines with the
    list, and an empty question declines."""
    _t, out = _call("start_here", {"question": "mangrove change", "pack": "land-cover",
                                   "place": "Manglares Churute", "year": 2000, "year_to": 2020})
    a = _next(out, "assemble_pack")
    assert (a["pack"], a["place"], a["year"], a["year_to"]) == ("land-cover", "Manglares Churute", 2000, 2020)
    _t, bad = _call("start_here", {"question": SALADO, "pack": "no-such-pack"})
    assert bad["status"] == "declined" and "land-cover" in bad["note"]
    _t, empty = _call("start_here", {"question": "  "})
    log("OUTPUT", [bad["note"], empty["note"]])
    assert empty["status"] == "declined"


# --------------------------------------------------------------------------- the texts
@pytest.mark.parametrize("which", ["repo", "live_like", "squeezed"])
def test_every_description_fits_and_names_the_products_and_countries(tmp_path, monkeypatch, caplog, which, log):
    """On the repo, live-like and live-squeezed registries: every description and parameter fits with
    no overrun logged; start_here's and assemble_pack's routing paragraphs name every product family
    and country the registries hold; each says what it is not for, naming a sibling tool."""
    from app.mcp import budget, instructions, packs, registry, server
    if which != "repo":
        _registry(tmp_path, monkeypatch, squeezed=which == "squeezed")
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        _instr, descs, params = server.LIVE.build()
    log("OUTPUT", {n: len(d) for n, d in descs.items()})
    assert not caplog.messages, caplog.messages
    assert all(len(d) <= budget.TOOL_DESCRIPTION for d in descs.values())
    assert all(len(t) <= budget.PARAM_DESCRIPTION for s in params.values() for t in s.values())
    families, countries = packs._lc_maps()[0], registry.countries()
    for name in ("start_here", "assemble_pack"):
        first = descs[name].split("\n\n")[0]
        assert not [f for f in families if f not in first], name
        assert not [c for c in countries if c not in first], name
        if which != "repo":
            assert "MapBiomas" in first and "Ecuador" in first, name
    s = descs["start_here"]
    assert s.startswith("START HERE") and s.lower().index("not for") < 600
    assert "Call this first for any question about" in s and "which tool to call with which arguments" in s
    for name, d in descs.items():
        at = d[:1500].lower().find("not for")
        assert at >= 0 and any(o in d[at:at + 1500] for o in descs if o != name), name
    for name, keys in server.RIDES.items():
        assert all(instructions.SHORT[k] in descs[name] for k in keys), name
    assert set(server.RIDES["start_here"]) == {"loop", "no_arithmetic", "platform_sources"}


def test_parameters_carry_the_moved_detail(salado, log):
    """How to call lives in each parameter's own description: assemble_pack's stack names the
    MapBiomas Ecuador stacks and how 'since 2020' reads, its layers and vectors the LAYER VALUES and
    VECTOR LAYERS detail; feeds_query's params its filters; start_here's question how it reads."""
    from app.mcp import server
    _instr, _descs, params = server.LIVE.build()
    a, f, s = params["assemble_pack"], params["feeds_query"], params["start_here"]
    log("OUTPUT", {"stack": a["stack"][:300], "start_here": s})
    assert "landcover_ec_mapbiomas (MapBiomas" in a["stack"] and "'according to ...'" in a["stack"]
    assert "'since 2020': year=2020" in a["year_to"]
    assert "LAYER VALUES" in a["layers"] and "VECTOR LAYERS" in a["vectors"] and "ec_protected_areas" in a["vectors"]
    assert "`bbox`" in f["params"] and "`limit` passes everywhere" in f["params"]
    assert set(s) == {"question", "place", "year", "year_to", "pack"} and "'since 2020'" in s["question"]
    assert all("`" + pid + "`" in s["pack"] for pid in ("food-security", "risk", "land-cover"))


# --------------------------------------------------------------------------- lookups steer
def test_a_points_feed_of_a_product_the_pack_maps_steers_to_it(salado, monkeypatch, log):
    """feeds_query on the MapBiomas Ecuador validation points: one line inside NOTE_CHARS names the
    land-cover pack, the call with the stack, and says these are sample points with no receipt; a
    feed of no mapped product carries none, and a decline is left as it was."""
    from app.mcp import budget, feeds
    monkeypatch.setitem(feeds.ADAPTERS, "generic_json", lambda params, spec: {
        "as_of": "2025", "count": 2, "summary": "2 points", "query_receipt": "stub",
        "url": "https://example.invalid/points.json", "stale_data": None,
        "records": [{"class": "Mangrove", "year": 2020, "lat": -2.2, "lon": -79.9},
                    {"class": "Aquaculture", "year": 2020, "lat": -2.3, "lon": -79.8}]})
    text, out = _call("feeds_query", {"dataset": POINTS})
    hint = out["for_a_governed_answer"]
    line = hint["measured_by_pack"]
    log("OUTPUT", f"{len(line)} chars: {line}")
    assert list(hint)[0] == "measured_by_pack" and len(line) <= budget.NOTE_CHARS
    assert line.startswith("MapBiomas maps for Ecuador are measured by the land-cover pack")
    assert "assemble_pack(pack='land-cover'" in line and "stack='landcover_ec_mapbiomas'" in line
    assert "sample points" in line and "no receipt" in line and "call assemble_pack(" in line
    assert "first" in line and "a sample showing none is not the maps' answer" in line
    assert list(out).index("for_a_governed_answer") < list(out).index("records")
    _t, other = _call("feeds_query", {"dataset": "hub_feed_00"})
    assert "measured_by_pack" not in (other.get("for_a_governed_answer") or {})
    _t, declined = _call("feeds_query", {"dataset": "no_such_feed"})
    assert declined["status"] == "declined" and "for_a_governed_answer" not in declined


def test_document_passages_naming_a_mapped_product_steer_too(salado, log):
    """A search whose passages come from a MapBiomas Ecuador report carries the same line, saying
    they are document passages; one about ENSO carries none."""
    from app.mcp import loop
    hits = {"status": "ok", "query": "mangrove loss Ecuador", "corpus": "land-cover", "hits": [
        {"score": 0.8, "text": "...", "passport": {"title": "MapBiomas Ecuador Collection 4 methods",
                                                    "source": "MapBiomas Ecuador - EcoCiencia"}}]}
    line = loop.with_entry_hint(hits)["for_a_governed_answer"].get("measured_by_pack")
    log("OUTPUT", line)
    assert line and "document passages" in line and "land-cover" in line
    enso = {"status": "ok", "query": "El Niño outlook", "hits": [
        {"score": 0.8, "text": "...", "passport": {"title": "CPC ENSO Diagnostic Discussion", "source": "NOAA CPC"}}]}
    assert "measured_by_pack" not in loop.with_entry_hint(enso)["for_a_governed_answer"]


# --------------------------------------------------------------------------- the switch
@pytest.mark.parametrize("mode", ["legacy", None])
def test_the_instructions_switch_serves_none_as_a_deferring_client_sees_it(monkeypatch, mode, log):
    """GRP_SERVE_INSTRUCTIONS=0: initialize carries no instructions (legacy handshake and the
    modern one), the tools and their descriptions are unchanged; back on, the composed text."""
    from fastmcp import Client
    from app.mcp import instructions
    from app.mcp.server import mcp

    def connect():
        async def go():
            async with (Client(mcp, mode=mode) if mode else Client(mcp)) as c:
                tools = {t.name: t.description for t in await c.list_tools()}
                return c.instructions, tools
        return asyncio.run(go())
    on_instr, on_tools = connect()
    monkeypatch.setattr(get_settings(), "grp_serve_instructions", False)
    off_instr, off_tools = connect()
    log("OUTPUT", f"on: {len(on_instr or ''):,} chars; off: {off_instr!r}; {len(off_tools)} tools")
    assert on_instr == instructions.compose() and not off_instr
    assert off_tools == on_tools and "start_here" in off_tools
    assert mcp.instructions is None
    monkeypatch.setattr(get_settings(), "grp_serve_instructions", True)
    again, _tools = connect()
    assert again == instructions.compose()


# --------------------------------------------------------------------------- positive evidence only
GENERIC = ["what was the average rent for an apartment in Kathmandu, Nepal last year?",
           "How has the GDP of Kenya changed since 2015?",
           "which areas of Jakarta have the highest crime levels",
           "PM2.5 air pollution level in Delhi",
           "Show me the years with the most tourists in Bali",
           "What is the average monthly income of fishers in Fiji?",
           "wind speed in the Gulf of Nicoya",
           "Is global coral bleaching getting worse?"]


@pytest.mark.parametrize("which", ["repo", "live_like"])
def test_generic_words_never_route_an_uncovered_question(tmp_path, monkeypatch, which, log):
    """A word a pack's texts or titles happen to hold ('year', 'level', 'changed', 'areas',
    'monthly', 'Gulf of Nicoya') is no route: each uncovered question is not_covered with no
    assemble_pack call; what it shares with a pack is listed as a weak match, never routed on."""
    from app.mcp import start
    if which != "repo":
        _registry(tmp_path, monkeypatch)
    out = {q: start.route(q) for q in GENERIC}
    log("OUTPUT", {q[:40]: (r["status"], r.get("weak_matches")) for q, r in out.items()})
    for q, r in out.items():
        assert r["status"] == "not_covered" and "route" not in r, q
        assert not any(c["tool"] == "assemble_pack" for c in r["next_calls"]), q
    assert "GDP is not served here" in out[GENERIC[1]]["gaps"]


def test_a_place_no_map_reaches_is_not_covered(salado, log):
    """Covered subjects asked of a place the pack never reaches: the gap first, the contribute
    pointer, no assemble_pack call — a declared country decides, else where the country lies; a
    place nothing places is said to be not known, never claimed, and asks for nothing."""
    from app.mcp import start
    lc = start.route("How much mangrove did Cambodia lose since 2000?")
    fs = start.route("Is maize failing in Brazil this season?")
    log("OUTPUT", [lc["gaps"], fs["gaps"]])
    for r, pack in ((lc, "land-cover"), (fs, "food-security")):
        assert r["status"] == "not_covered" and r["route"][0]["pack"] == pack
        assert r["gaps"][0].startswith(f"nothing the {pack} pack holds covers")
        assert "contribute_submit kind=" in r["not_served"]
        assert not any(c["tool"] == "assemble_pack" for c in r["next_calls"])
    unknown = start.route("How much mangrove was lost since 2000?", place="Atlantis")
    log("OUTPUT", unknown["gaps"])
    assert unknown["status"] == "ok" and unknown["gaps"][0].startswith("whether the land-cover pack covers Atlantis")
    assert len(unknown["gaps"]) == 1 and "covering the place" not in json.dumps(unknown["filled_from"])
    if not start._tz_points():
        pytest.skip("no time-zone tables on this system: where a country lies is not known")
    for q in ("How many people would a 100-year flood expose in Kenya?",
              "How many people would be exposed to a 100-year flood in Nairobi, Kenya?",
              "How many people would be exposed to a 100-year flood in Suva, Fiji?"):
        r = start.route(q)
        assert r["status"] == "not_covered" and r["gaps"][0].startswith("nothing the risk pack holds covers"), q
    assert start.route(FLOOD)["status"] == "ok"


def test_a_product_is_named_by_what_sets_it_apart(salado, log):
    """ESA CCI is not ESA WorldCover: no stack, and 'ESA CCI is not served here'; a descriptive
    product ('Mangrove extent') is no name, so Kenya is not told it maps only Indonesia; GMW names
    Global Mangrove Watch by its initials; JAXA, which nothing holds, is said first."""
    from app.mcp import start
    cci = start.route("How much cropland in Costa Rica since 2000 according to ESA CCI land cover?")
    lamu = start.route("What is the mangrove extent in Lamu, Kenya?")
    gmw = start.route("Mangrove extent in Lamu, Kenya, since 2000 according to GMW")
    jaxa = start.route("How much forest did Ecuador lose since 2000 according to JAXA?")
    log("OUTPUT", {"cci": (_next(cci, "assemble_pack"), cci["gaps"][0]), "lamu": lamu.get("gaps"),
                   "gmw": _next(gmw, "assemble_pack"), "jaxa": jaxa["gaps"][0]})
    assert "stack" not in _next(cci, "assemble_pack") and cci["gaps"][0].startswith("ESA CCI is not served here")
    assert "say so first" in cci["note"]
    assert lamu["status"] == "ok" and not lamu.get("gaps")
    assert _next(gmw, "assemble_pack")["stack"] == "landcover_ke_gmw_v3_2class_ke_coast"
    assert "GMW" in gmw["route"][0]["matched"]
    assert jaxa["gaps"][0].startswith("JAXA is not served here")


def test_a_product_family_tie_goes_to_the_map_with_the_question_s_classes(tmp_path, monkeypatch, log):
    """MapBiomas Agua registered before MapBiomas land cover: a question naming only the family
    reads the land-cover map (its legend and most classes), never the first registered."""
    import test_desktop_routing as me
    from app.graph.geo import ingest
    from app.mcp import start
    agua = next(s for s in LIVE_STACKS if s[0] == "landcover_ec_mbwaterbg")
    monkeypatch.setattr(me, "LIVE_STACKS", [agua] + [s for s in LIVE_STACKS if s is not agua])
    _registry(tmp_path, monkeypatch)
    try:
        r = start.route("What changed in Manglares El Salado since 2020 according to MapBiomas?")
        log("OUTPUT", _next(r, "assemble_pack"))
        assert _next(r, "assemble_pack")["stack"] == "landcover_ec_mapbiomas"
    finally:
        ingest._GAZETTEER.clear()


def test_class_and_subject_words_are_never_places(salado, tmp_path, monkeypatch, log):
    """A capitalised class or subject after a preposition is not a place, a possessive country
    is read before the possessive's word, a second boundary named is said, and a boundary in
    another country than the question names is not the place."""
    from app.graph.geo import ingest, vectors
    from app.mcp import start

    def place(q):
        return _next(start.route(q), "assemble_pack")["place"]
    assert place("What share of Mangrove in Ecuador became Aquaculture since 2020 according to MapBiomas?") == "Ecuador"
    assert place("How much of the Forest in Ecuador was lost since 2000?") == "Ecuador"
    assert place("Flood exposure for People in Battambang") == "Battambang"
    assert place("According to MapBiomas, how much of Ecuador's Mangrove was lost since 2020?") == "Ecuador"
    assert place("How many people are exposed to floods in Cambodia's Battambang province?") == "Battambang, Cambodia"
    two = start.route("Compare mangrove loss since 2000 in Manglares Churute and Manglares El Salado according to MapBiomas")
    log("OUTPUT", two["also"])
    assert "Manglares Churute" in two["also"][0]
    reg = {"vectors": {}}
    _boundaries(tmp_path, reg, "gt_forest", "Forest reserves of Guatemala", "Guatemala",
                [_poly("Mangrove Forest", -91.0, 13.9, 0.1, 0.1, country="Guatemala")])
    landed = vectors.landed()
    monkeypatch.setattr(vectors, "landed", lambda: {**landed, **reg["vectors"]})
    ingest._GAZETTEER.clear()
    assert place("How much mangrove forest did Ecuador lose since 2000 according to MapBiomas?") == "Ecuador"


def test_the_default_year_and_its_edges(salado, log):
    """No year asked: the latest year of the map whose legend holds the class asked (2020, not the
    2026 of a landform map); a year_to past the maps snaps with a gap; 'the 2010s' is a decade; a
    passed year beats 'the past 5 years'; a year_to alone gets an earlier year."""
    from app.mcp import start
    ind = _next(start.route("How much mangrove is there in Indonesia?"), "assemble_pack")
    log("OUTPUT", ind)
    assert ind["year"] == 2020 and ind["classes"] == ["Mangrove"]
    far = start.route("How did mangrove in Manglares El Salado change between 2000 and 2030 according to MapBiomas?")
    assert _next(far, "assemble_pack")["year_to"] == 2025 and far["gaps"][0].startswith("year_to=2030")
    dec = _next(start.route("How did mangrove in Manglares El Salado change in the 2010s according to MapBiomas?"),
                "assemble_pack")
    assert (dec["year"], dec["year_to"]) == (2010, 2019)
    past = start.route("How has mangrove changed over the past 5 years in Manglares El Salado according to "
                       "MapBiomas?", year=2010)
    assert _next(past, "assemble_pack")["year"] == 2010 and past["filled_from"]["year"] == "as passed"
    alone = _next(start.route("mangrove in Ecuador according to MapBiomas", year_to=2010), "assemble_pack")
    assert alone["year"] < alone["year_to"] == 2010


def test_a_product_without_a_place_names_no_stack_and_no_foreign_feed(salado, log):
    """MapBiomas maps two countries: with no place or country named, no stack is filled (the
    choices are listed); a Brazil question never lists the Ecuador points feed."""
    from app.mcp import start
    r = start.route("MapBiomas mangrove change since 2020")
    log("OUTPUT", r.get("other_choices"))
    assert "stack" not in _next(r, "assemble_pack") and r["other_choices"]["stack"]
    br = start.route("How much mangrove did Brazil lose since 2018 according to MapBiomas?")
    assert _next(br, "assemble_pack")["stack"].startswith("landcover_br_")
    assert not any(x["args"].get("dataset") == POINTS for x in br.get("related_lookups") or [])


def test_steering_needs_the_rows_country_and_reads_only_the_passages(salado, monkeypatch, log):
    """A MapBiomas feed whose country is unknown names no stack; a Kenya feed is never pointed at a
    map of Southeast Asia; a search is steered by its passages, never by the caller's query."""
    from app.mcp import registry, start
    monkeypatch.setitem(registry.FEEDS, "mb_points", {
        "status": "available", "pack": "land-cover", "title": "MapBiomas LULC validation points",
        "source": "MapBiomas", "fetch": {"fields": {"lat": "lat", "lon": "lon"}}})
    monkeypatch.setitem(registry.FEEDS, "glofas_ke", {
        "status": "available", "pack": "risk", "title": "River discharge exceeding 1-in-100 thresholds, Kenya",
        "source": "GloFAS", "countries": ["Kenya"]})
    mb = start.steer({"status": "ok", "dataset": "mb_points", "records": [{"lat": 1, "lon": 2}]})
    log("OUTPUT", mb)
    assert mb and "stack=<the MapBiomas stack for the place>" in mb and "Brazil, Ecuador" in mb
    assert start.steer({"status": "ok", "dataset": "glofas_ke", "records": [{"v": 1}]}) is None
    assert start.steer({"status": "ok", "query": "MapBiomas mangrove Ecuador", "hits": [
        {"passport": {"title": "CPC ENSO Diagnostic Discussion", "source": "NOAA CPC"}}]}) is None


def test_rows_are_points_unless_the_dataset_says_it_samples_and_methods_come_first(salado, monkeypatch, log):
    """Coordinates alone are point records; a dataset that says it is a sample (training,
    validation, survey plots) is sample points; an approved method that computes from a related
    feed is listed before the feed."""
    from app.mcp import loop, start
    assert start._rows_are({"title": "Station rainfall", "fetch": {"fields": {"lat": 1, "lon": 2}}}) \
        == "point records (lat/lon)"
    assert start._rows_are(POINTS_FEED) == "sample points (lat/lon)"
    monkeypatch.setattr(loop, "methods_reading", lambda ds: [{
        "method": "mangrove_share", "title": "Mangrove share", "computes": {"share": "%"},
        "get_its_result": [{"tool": "assemble_pack", "args": {"pack": "land-cover"}}]}] if ds == POINTS else [])
    r = start.route(SALADO)
    log("OUTPUT", r["related_lookups"][:2])
    assert r["related_lookups"][0]["method"] == "mangrove_share"
    assert "use it before reading the rows" in r["related_lookups"][0]["what"]


def test_a_scaffold_shaped_pack_routes_and_its_unknown_keys_are_said(salado, monkeypatch, log):
    """A pack shaped as the scaffold writes it (a two-word display name and usage notes, no
    keywords) routes on them; a target key assemble_pack has no parameter for is said in
    `missing`, never put in the call."""
    from app.mcp import packs, start
    monkeypatch.setitem(packs.PACKS, "water-security", {
        "display_name": "Water Security", "target_keys": ("basin", "date"),
        "target_doc": {"basin": "TODO: describe basin", "date": "TODO: describe date"},
        "usage_notes": "river discharge and reservoir storage for a basin",
        "gather": lambda *a, **k: ([], [], None), "sections": lambda *a: []})
    r = start.route("What is the water security outlook for the Mekong basin next week?")
    log("OUTPUT", (r["route"], r.get("missing")))
    assert r["route"][0]["pack"] == "water-security"
    a = _next(r, "assemble_pack")
    assert "basin" not in a and "date" not in a
    assert sum("no such parameter" in m for m in r["missing"]) == 2


def test_a_long_question_stays_inside_the_reply_budget(salado, log):
    """A 60,000-char question: the reply quotes it once, clipped, and every next call points to
    it instead of repeating it — inside budget.REPLY_CHARS."""
    from app.mcp import budget
    q = SALADO + " " + " ".join(["mangrove"] * 7000)
    text, out = _call("start_here", {"question": q})
    log("OUTPUT", f"{len(q):,}-char question: {len(text):,}-char reply")
    assert len(text) <= budget.REPLY_CHARS and len(text) < 8000
    assert _next(out, "publish_answer")["question"].startswith("<the person's question")


def test_the_texts_lead_with_when_not_to_however_much_is_contributed(salado, monkeypatch, log):
    """Twenty contributed boundary layers and six extension packs: every description says what it
    is not for within its first 600 chars, naming a sibling, before any derived list grows."""
    from app.mcp import packs, server
    for i in range(20):
        salado["vectors"][f"zz_parks_{i:02d}"] = {
            "title": f"Protected areas and community forests of district {i} (national agency, 2024)",
            "geometry": "polygons", "countries": ["Ecuador"], "name_field": "name",
            "local_path": f"vectors/zz_parks_{i:02d}.geojson"}
    for i in range(6):
        monkeypatch.setitem(packs.PACKS, f"ext{i}", {
            "display_name": f"Extension {i} Platform", "target_keys": ("place",), "target_doc": {"place": "x"},
            "keywords": f"subject {i}: a long detail of what extension {i} measures in many places",
            "use_for": f"what extension pack {i} answers for a place, said at some length",
            "gather": lambda *a, **k: ([], [], None), "sections": lambda *a: []})
    _instr, descs, _params = server.LIVE.build()
    for name, d in descs.items():
        at = d.lower().find("not for")
        assert 0 <= at < 600 and any(o in d[at:at + 600] for o in descs if o != name), (name, at)
    assert "zz_parks" not in descs["resolve_place_time"][:descs["resolve_place_time"].lower().find("not for")]


@pytest.mark.parametrize("squeezed", [False, True])
def test_the_guide_and_each_parameters_page_fit_one_reply(tmp_path, monkeypatch, caplog, squeezed, log):
    """On the live-sized registries the guide, read through a client, fits budget.REPLY_CHARS with
    nothing logged and names each tool's parameters page; each page fits too."""
    from fastmcp import Client
    from app.mcp import budget, instructions
    from app.mcp.server import mcp
    _registry(tmp_path, monkeypatch, squeezed=squeezed)

    async def read():
        async with Client(mcp) as c:
            guide = (await c.read_resource(instructions.HOW_TO_USE))[0].text
            pages = {t.name: (await c.read_resource(instructions.PARAMETERS.format(tool=t.name)))[0].text
                     for t in await c.list_tools()}
            return guide, pages
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        guide, pages = asyncio.run(read())
    log("OUTPUT", f"guide {len(guide):,} chars; pages {max(len(p) for p in pages.values()):,} at most")
    assert len(guide) <= budget.REPLY_CHARS and not caplog.messages
    assert all(len(p) <= budget.REPLY_CHARS for p in pages.values())
    assert "landcover_ec_mapbiomas" in pages["assemble_pack"]


def test_a_contributed_stack_is_named_in_the_stack_parameter_on_a_squeezed_registry(tmp_path, monkeypatch, log):
    """Each parameter is fitted on its own: a stack contributed to a live-squeezed registry is
    named in `stack` however long `vectors` is."""
    from app.mcp import server
    _registry(tmp_path, monkeypatch, squeezed=True)
    get_settings().tiffs_contrib_path.write_text(json.dumps({"landcover_bnt_bs": {
        "kind": "stack", "role": "landcover", "title": "Bahamas National Trust mangrove extent 2024",
        "countries": ["Bahamas"], "legend": {1: "Mangrove", 2: "Not mangrove"}, "years": [2024],
        "contributed": True, "source": "Bahamas National Trust", "license": "CC-BY-4.0"}}))
    _instr, _descs, params = server.LIVE.build()
    assert "landcover_bnt_bs" in params["assemble_pack"]["stack"]


def test_food_security_routing_names_the_calendar_with_an_empty_library(monkeypatch, log):
    """With no document in the library, the food-security keywords still name the calendar's crops
    and countries — the list start_here routes on."""
    from app.mcp import packs, registry
    real = registry.coverage
    monkeypatch.setattr(registry, "coverage", lambda: {**real(), "crops": [], "countries": []})
    kw = packs._fs_keywords()
    log("OUTPUT", kw)
    assert "maize" in kw and "Kenya" in kw


def test_the_scaffold_asks_for_routing_words_and_the_doctor_warns(tmp_path, monkeypatch, log):
    """The pack scaffold carries keywords and use_for; the doctor warns when a pack declares no
    routing words, naming what to declare."""
    from app.contrib import packdev
    from app.mcp import packs
    monkeypatch.setattr(get_settings(), "cache_dir", tmp_path / "cache")   # the dry-run's pack and receipt
    packdev.new_pack("fresh", dest_dir=tmp_path)
    ns: dict = {}
    exec((tmp_path / "fresh.py").read_text(), ns)
    assert "keywords" in ns["SPEC"] and "use_for" in ns["SPEC"]
    monkeypatch.setitem(packs.PACKS, "fresh", ns["SPEC"])
    out = packdev.doctor("fresh")
    log("OUTPUT", out.get("warnings"))
    assert out["status"] == "passed" and "SPEC['keywords']" in out["warnings"][0]
