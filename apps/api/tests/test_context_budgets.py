"""Context budgets (G12): what the strictest known client keeps of the instructions and the
descriptions, measured against mcp/budget.py — on the repo registry, on a contributed
stack, on a pack with no coverage hook, and through the real handshake."""
import asyncio
import json
import logging
import re

from app.contrib import staging
from app.mcp import assemble, budget, feeds, instructions, loop, packs, publish, record, registry, resolve, shape, skills
from app.mcp.server import INSTRUCTIONS, _capabilities, _instructions, build_a_tool, mcp
from test_contrib_methods import BOX, _threshold, _units, env  # noqa: F401  (a contributed table + method)
from test_landcover_pack import lc_env  # noqa: F401  (a contributed, synthetic stack)
from test_loop_closure import pack  # noqa: F401  (the live food-security pack)
from test_resolve_list_boundaries import sites  # noqa: F401  (a contributed boundary layer)
from test_risk_pack import offline_aoi  # noqa: F401  (the offline risk pack)

CORE_RULES = ("NEVER show the user a brief that has not been through publish_answer",
              "NO ARITHMETIC BY DEFAULT", "accept or reject", "as a method", "own arithmetic",
              "without results", "ONE HARD LIMIT")


def _tools():
    return {t.name: (t.description or "") for t in asyncio.run(mcp.list_tools())}


def _params() -> dict:
    """{tool: {parameter: description}} as a client reads them, from tools/list."""
    async def read():
        from fastmcp import Client
        async with Client(mcp) as c:
            return {t.name: {k: v.get("description") or "" for k, v in
                             (t.input_schema.get("properties") or {}).items()}
                    for t in await c.list_tools()}
    return asyncio.run(read())


def _pages() -> dict:
    """{tool: its parameters page} from servirplatform://how-to-use/parameters/<tool>, read as a
    client reads a resource."""
    async def read():
        from fastmcp import Client
        async with Client(mcp) as c:
            return {t.name: (await c.read_resource(instructions.PARAMETERS.format(tool=t.name)))[0].text
                    for t in await c.list_tools()}
    return asyncio.run(read())


def _served() -> dict:
    """Every surface a section may be homed on, read as a client would (every published
    prompt rendered with its defaults); a tool serves its description and its parameters'."""
    async def read():
        from fastmcp import Client
        async with Client(mcp) as c:
            guide = (await c.read_resource(instructions.HOW_TO_USE))[0].text
            prompts = {f"prompt:{p.name}": "\n".join(m.content.text for m in
                                                     (await c.get_prompt(p.name)).messages)
                       for p in await c.list_prompts()}
            return guide, prompts
    guide, prompts = asyncio.run(read())
    out = {"instructions": instructions.compose(), instructions.GUIDE: guide, **prompts}
    params = _params()
    out.update({f"tool:{k}": "\n\n".join([v, *[d for d in params.get(k, {}).values() if d]])
                for k, v in _tools().items()})
    out.update({f"skill:{n}": skills.bundle(n) for n in skills.SERVED})
    return out


# --------------------------------------------------------------------------- the budgets
def test_measure_is_the_text_a_client_reads(log):
    """budget.measure counts the serialised text fastmcp hands the client, not a json.dumps
    of our own that over-counts by the separators."""
    from fastmcp import Client

    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool("ui_catalog", {})
            return r.content[0].text, r.structured_content
    text, structured = asyncio.run(go())
    log("OUTPUT", f"text {len(text):,} chars; measure {budget.measure(structured):,}")
    assert budget.measure(structured) == len(text)
    assert budget.measure(text) == len(text) and budget.fits(text, len(text))


def test_an_overrun_is_logged_never_silent(caplog, log):
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        n = budget.check("a test surface", "x" * 12, 10)
    log("OUTPUT", caplog.messages)
    assert n == 12 and any("a test surface" in m and "12" in m for m in caplog.messages)


def test_instructions_fit_the_client_budgets(caplog, log):
    """Tier 1 inside the 2,048 a pre-v2.1.296 client kept; everything inside the 4,096 kept
    now — and composing them logs no overrun on this registry."""
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        full, core = instructions.compose(), instructions.core()
    log("OUTPUT", f"instructions {len(full):,} chars; core {len(core):,}")
    assert full == _instructions()        # both built here: the import-time text saw other methods
    assert len(core) <= budget.INSTRUCTIONS_CORE and len(full) <= budget.INSTRUCTIONS
    assert full.startswith(core)
    assert not caplog.messages


def test_the_core_rules_sit_inside_the_first_2048(log):
    """The receipt rule, no arithmetic and the one hard limit are what a client that keeps
    only the core must still see — and the tiers come in their declared order."""
    kept = instructions.compose()[:budget.INSTRUCTIONS_CORE]
    for rule in CORE_RULES:
        assert rule in kept, rule
    full = instructions.compose()
    at = [full.index(instructions.section_text(s["key"])) for s in instructions.SECTIONS
          if s["tier"] < 3]
    assert at == sorted(at), "tier-1/2 sections out of their declared order"
    assert full.index(instructions.pointers()) > max(at)
    log("CHECK", f"{len(CORE_RULES)} rules inside {budget.INSTRUCTIONS_CORE} chars; order kept")


def test_the_handshake_hands_out_the_composed_instructions(log):
    """What initialize actually returns (through _LiveText), measured as a client sees it."""
    from fastmcp import Client

    async def go():
        async with Client(mcp, mode="legacy") as c:
            return c.initialize_result.instructions
    said = asyncio.run(go())
    log("OUTPUT", f"{len(said):,} chars")
    assert said == instructions.compose() and len(said) <= budget.INSTRUCTIONS


# --------------------------------------------------------------------------- derived, not typed
def _catalog_names() -> set[str]:
    """Every NAME the registries hold that belongs in capabilities, never in the opening."""
    from app.land_cover.synthesis import landcover_stacks
    from app.graph.geo import vectors
    names = set()
    for row in landcover_stacks().values():
        names.add(str(row.get("title")))
        names.update(str(v) for v in (row.get("legend") or {}).values())
    names.update(str(c["title"]) for c in packs.risk_coverage().values())
    names.update(str(v.get("title")) for v in registry.FEEDS.values() if v.get("title"))
    names.update(str(e.get("title")) for e in vectors.visible().values() if e.get("title"))
    return {n for n in names if len(n) > 3}


def test_the_opening_names_counts_not_layers(lc_env, log):  # noqa: F811
    """On a contributed catalog the opening carries the pack names and counts; the stack,
    class, hazard and feed names stay in platform_capabilities and assemble_pack."""
    core = instructions.core()
    names = _catalog_names()
    leaked = sorted(n for n in names if n in core)
    log("OUTPUT", f"{len(names)} catalog names checked; opening: {core[:200]}...")
    assert not leaked, leaked
    for pid, sp in packs.PACKS.items():
        assert instructions._display(pid, sp) in core and instructions._shape(sp) in core
    assert "1 stack" in core and "Test land cover" not in core


def test_a_contributed_stack_moves_the_count_and_names_itself_at_its_home(lc_env, monkeypatch, log):  # noqa: F811
    """A second stack lands: the opening's count follows it, its name reaches assemble_pack's
    description (keywords) and its title the guide (coverage by name), and every text stays
    inside its budget."""
    from app.land_cover import synthesis
    rows = dict(synthesis.landcover_stacks())
    first = next(iter(rows.values()))
    rows["landcover_testland"] = {**first, "title": "Testland mangrove extent",
                                  "countries": ["Testland"],
                                  "legend": {0: "Not mangrove", 1: "Mangrove"}}
    monkeypatch.setattr(synthesis, "landcover_stacks", lambda: rows)
    core, desc = instructions.core(), registry.describe_assemble()
    log("OUTPUT", core[:260])
    assert "2 stacks" in core and "Testland" not in core
    assert "Testland" in desc and len(desc) <= budget.TOOL_DESCRIPTION
    assert "landcover_testland (mangrove extent; Testland" in registry.assemble_params()["stack"]
    assert "Testland mangrove extent" in instructions.how_to_use()
    assert len(core) <= budget.INSTRUCTIONS_CORE and len(instructions.compose()) <= budget.INSTRUCTIONS


def test_a_pack_without_a_covers_hook_says_so(monkeypatch, log):
    """A fourth, row-only pack: named with its target shape, declared as not covering,
    never skipped — and the core still fits."""
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy Platform", "target_keys": ("site",), "target_doc": {"site": "x"},
        "gather": lambda *a, **k: ([], [], None), "sections": lambda *a: []})
    core = instructions.core()
    log("OUTPUT", f"{len(core):,} chars: ...{core[core.index('Toy'):core.index('Toy') + 60]}")
    assert "4 domain packs" in core
    assert "Toy: coverage not declared by this pack (site)" in core
    assert len(core) <= budget.INSTRUCTIONS_CORE


# --------------------------------------------------------------------------- nothing is lost
def test_nothing_is_lost(log):
    """Every declared section's FULL text is served at every home its inventory names, and
    every tier-3 section keeps a pointer inside the instructions."""
    served = _served()
    full = instructions.compose()
    for key, where in instructions.inventory():
        assert where in served, f"{key}: unknown home {where}"
        assert instructions.section_text(key) in served[where], f"{key} missing at {where}"
    for s in instructions.SECTIONS:
        if s["tier"] == 3:
            assert s["pointer"] in full and s.get("home"), s["key"]
            assert s["text"] if not callable(s["text"]) else s["text"]()
            assert s.get("triggers"), s["key"]
            for word in s["triggers"]:
                assert word in s["pointer"] and word in full[:budget.INSTRUCTIONS], (s["key"], word)
    assert instructions.ASKED_HOW in full and "explain_platform" in full
    log("CHECK", f"{len(instructions.inventory())} (section, home) pairs all served")


def test_homes_are_published_surfaces(log):
    """A home is a tool, prompt or skill the server actually publishes."""
    tools = set(_tools())
    prompts = {p.name for p in asyncio.run(mcp.list_prompts())}
    for key, where in instructions.inventory():
        kind, _, name = where.partition(":")
        ok = {"instructions": True, "resource": name == instructions.HOW_TO_USE,
              "tool": name in tools, "prompt": name in prompts,
              "skill": name in skills.SERVED}[kind]
        assert ok, f"{key}: {where} is not published"
    log("CHECK", "every home resolves to a published surface")


def test_the_guide_is_derived_and_teaches_the_three_step_loop(log):
    """The how-to-use resource names every pack from the registry, a worked target per
    pack, and publish_answer as the finish — not the retired five-step loop."""
    guide = _served()[instructions.GUIDE]
    for pid, sp in packs.PACKS.items():
        assert instructions._display(pid, sp) in guide
        if sp.get("doctor_target"):
            assert all(f"{k}=" in guide for k in sp["doctor_target"])
    assert "publish_answer(pack_id, draft, question)" in guide
    assert "publish only if it passes" not in guide
    log("OUTPUT", f"guide {len(guide):,} chars")


# --------------------------------------------------------------------------- descriptions
def test_home_descriptions_stay_within_budget(log):
    """Appending a rule to a tool's description must not push it past what tool search
    matches."""
    tools = _tools()
    homes = {w.partition(":")[2] for _, w in instructions.inventory() if w.startswith("tool:")}
    for name in sorted(homes):
        log("OUTPUT", f"{name}: {len(tools[name]):,} chars")
        assert len(tools[name]) <= budget.TOOL_DESCRIPTION, name


def test_every_tool_description_fits_upfront_loading(monkeypatch, log):
    """Derived from the server's own tool list, never a typed set — and the generated ones
    under both review switches (contribute_submit changes text with grp_auto_approve)."""
    from app.config import get_settings
    from app.mcp.server import DESCRIBERS
    over = {n: len(d) for n, d in _tools().items() if len(d) > budget.TOOL_DESCRIPTION}
    assert not over, over
    for on in (True, False):
        monkeypatch.setattr(get_settings(), "grp_auto_approve", on)
        sizes = {name: len(fn()) for name, fn in DESCRIBERS.items()}
        log("OUTPUT", f"auto_approve={on}: {sizes}")
        assert all(n <= budget.TOOL_DESCRIPTION for n in sizes.values()), sizes


def test_search_words_lead_the_reshaped_descriptions(log):
    """A model reads the first line and tool search the first 4,096 chars: the three
    descriptions that were cut open with the words a question is routed on, and still name
    every pack and every kind (derived)."""
    tools = _tools()
    heads = {"assemble_pack": ("COMPUTED, CITED EVIDENCE", "INSTEAD OF A WEB SEARCH"),
             "feeds_query": ("live from the source", "INSTEAD OF A WEB SEARCH"),
             "contribute_submit": ("Contribute a source or logic", "gate")}
    for name, words in heads.items():
        opening = tools[name].split("\n\n")[0]
        assert len(opening) <= 1500, (name, len(opening))
        for w in words:
            assert w in opening, (name, w)
    for pid in packs.PACKS:
        assert f"`{pid}`" in tools["assemble_pack"]
    for kind in staging.KINDS:
        assert f"{kind} (" in tools["contribute_submit"]
    assert staging.AS_GIVEN in tools["contribute_submit"] and staging.FORM_CALL in tools["contribute_submit"]
    log("CHECK", "first lines carry the routing words; packs and kinds all named")


def test_every_description_says_what_it_answers_and_when_not_to_use_it(log):
    """G14: a client that defers tools reads no instructions, only names and descriptions. Every
    tool's opening (first 1,500 chars) says when NOT to use it and names the sibling tool."""
    tools = _tools()
    for name, desc in tools.items():
        head = desc[:1500]
        at = head.lower().find("not for")
        assert at >= 0, name
        assert any(other in head[at:] for other in tools if other != name), name
    log("CHECK", f"{len(tools)} descriptions say when not to use them, naming the sibling")


def test_the_rules_ride_where_they_apply(log):
    """Every tool is described through the one seam, and the short form of each rule it carries
    is in its served description; together <= ~500 chars a tool."""
    from app.mcp.server import RIDES
    tools = _tools()
    assert set(RIDES) == set(tools)
    for name, keys in RIDES.items():
        assert set(keys) <= set(instructions.SHORT), name
        for k in keys:
            assert instructions.SHORT[k] in tools[name], (name, k)
        assert len(instructions.rules(keys)) <= 520, name
    for name in ("assemble_pack", "feeds_query", "corpus_search"):
        assert set(RIDES[name]) == {"loop", "no_arithmetic", "platform_sources"}, name
    for name in ("ui_design", "ui_catalog", "ui_component", "ui_embed", "publish_answer", "record_receipt"):
        assert "hard_limit" in RIDES[name], name
    assert "platform_sources" in RIDES["contribute_submit"]
    log("OUTPUT", {n: len(instructions.rules(k)) for n, k in RIDES.items()})


def test_parameters_carry_the_how_to_call_detail_and_the_guide_holds_it_whole(log):
    """How to call moved out of the descriptions into each parameter's own description, every
    one of them described, each inside budget.PARAM_DESCRIPTION and served whole on its tool's
    parameters page, which the guide names."""
    tools, params, pages = _tools(), _params(), _pages()
    guide = _served()[instructions.GUIDE]
    assert instructions.PARAMETERS in guide and guide.index("## The rules, in full") < guide.index(
        "## Every tool's parameters")
    for name, said in params.items():
        assert not said or f"`{name}`" in guide.split("## Every tool's parameters")[1], name
        for p, d in said.items():
            assert d, (name, p)
            assert len(d) <= budget.PARAM_DESCRIPTION, (name, p, len(d))
            assert " ".join(d.split()) in pages[name], (name, p)
    a = params["assemble_pack"]
    assert "LAYER VALUES" in a["layers"] and "What a layer's values state" in a["layers"]
    assert "VECTOR LAYERS" in a["vectors"] and "overlaps count once" in a["within"]
    assert "ADJUSTED by the requester" in a["override"] and "EPSG:4326" in a["geometry"]
    assert "stats.assets" in a["map_layer"] and "servirplatform://pack/" in a["detail"]
    for pid, sp in packs.PACKS.items():
        assert f"`{pid}`" in a["pack"], pid
        for k, doc in (sp.get("target_doc") or {}).items():
            assert k not in a or doc in a[k], (pid, k)
    for gone in ("LAYER VALUES", "VECTOR LAYERS", "detail='full'"):
        assert gone not in tools["assemble_pack"], gone
    f = params["feeds_query"]["params"]
    assert "`limit` passes everywhere" in f and "declined with the accepted list" in f
    m = params["contribute_submit"]["manifest"]
    assert "PREPARATION" in m and "preview: true" in m and "CONTRIBUTOR'S OWN WORDS" in m
    assert "PREPARATION" not in tools["contribute_submit"]
    assert "VERBATIM" in params["publish_answer"]["draft"] and "daisyui" in params["ui_design"]["format"]
    log("OUTPUT", {n: sum(len(d) for d in s.values()) for n, s in params.items()})


# Clauses the descriptions said before how-to-call moved into each parameter (G14): each is still
# served where a client reads that tool — its description, a parameter, or the guide.
MOVED = {
    "compose_run": ("Use this tool when you have no model credentials or nowhere to run code",),
    "resolve_place_time": ("which are small enough to answer quickly",
                           "Pass whichever half your question has"),
    "feeds_query": ("so the platform computes and cites it for everyone",
                    "a param a feed does not take is declined with the accepted list"),
    "publish_answer": ("VERBATIM and in full", "do not write your own '## Sources'"),
    "corpus_search": ("searches the wrong shelf",),
    "corpus_document": ("A doc_id from another pack's citation needs that pack",),
    "assemble_pack": ("LAYER VALUES", "VECTOR LAYERS", "overlaps count once", "ADJUSTED by the requester"),
    "contribute_status": ("removal stops future use, it does not rewrite history",),
}


def test_the_clauses_moved_out_of_the_descriptions_are_still_served(log):
    """Every clause pinned in MOVED is served at its tool (description or a parameter) or in the
    guide: a later rewrite cannot drop one silently."""
    served = _served()
    for name, clauses in MOVED.items():
        where = (served[f"tool:{name}"] + "\n" + served[instructions.GUIDE]).casefold()
        for c in clauses:
            assert c.casefold() in " ".join(where.split()), (name, c)
    log("CHECK", f"{sum(map(len, MOVED.values()))} moved clauses still served")


def test_the_texts_agree_on_the_first_call_and_the_lookup_rows(log):
    """The instructions name start_here as what fills assemble_pack's target (one first call,
    said one way); feeds_query leads with the subjects people ask; a lookup's hint says its rows
    stay out of a receipted brief."""
    tools = _tools()
    assert "start_here(question) fills it" in instructions.core()
    assert tools["feeds_query"].startswith("CURRENT CLIMATE, CROP AND HAZARD DATA")
    assert "keep it out of a receipted brief" in loop.ENTRY_HINT["note"]
    assert len(loop.ENTRY_HINT["note"]) <= budget.NOTE_CHARS


def test_an_extension_pack_routes_through_the_same_seam(monkeypatch, log):
    """A row-only pack: its `use_for` joins assemble_pack's routing lead and its target docs the
    parameters; one declaring no `use_for` is still named with its targets, never skipped."""
    gather = (lambda *a, **k: ([], [], None))
    monkeypatch.setitem(packs.PACKS, "reef", {
        "display_name": "Reef Platform", "target_keys": ("place", "year"),
        "target_doc": {"place": "a reef site name", "year": "a survey year"},
        "use_for": "coral cover and bleaching on surveyed reefs", "gather": gather,
        "sections": lambda *a: []})
    monkeypatch.setitem(packs.PACKS, "bare", {
        "display_name": "Bare Platform", "target_keys": ("hazard",), "target_doc": {"hazard": "a bare hazard"},
        "gather": gather, "sections": lambda *a: []})
    desc, params = registry.describe_assemble(), registry.assemble_params()
    log("OUTPUT", desc[:400])
    assert "coral cover and bleaching on surveyed reefs" in desc.split("\n\n")[0]
    assert "reef: a reef site name" in params["place"] and "reef: a survey year" in params["year"]
    assert "`bare` (hazard)" in desc and "`bare`" in params["pack"]
    assert "bare: a bare hazard" in params["hazard"]


def test_a_contributed_boundary_layer_reaches_the_texts_with_no_code_change(sites, log):  # noqa: F811
    """A contributed named polygon layer: resolve_place_time's description names it among the
    places, and assemble_pack's `vectors` parameter among the layers it measures."""
    tools, params = _tools(), _params()
    log("OUTPUT", tools["resolve_place_time"][:400])
    assert "Sprint sites (test)" in tools["resolve_place_time"]
    assert "sprint_sites" in params["assemble_pack"]["vectors"]


def test_a_form_request_returns_the_kinds_required_fields(monkeypatch, log):
    """contribute_submit(kind=X) with no manifest, through the tool: that kind's form from
    the row the gate validates against, for every kind the gate knows; no job row, nothing
    stored; an unknown kind declines with the list."""
    from fastmcp import Client
    from app.contrib import jobs

    def no_row(*a, **k):
        raise AssertionError("a form request wrote a job row")
    monkeypatch.setattr(jobs, "_write", no_row)

    async def go(kind):
        async with Client(mcp) as c:
            return (await c.call_tool("contribute_submit", {"kind": kind})).structured_content
    for kind in staging.KINDS:
        out = asyncio.run(go(kind))
        fields = staging._KINDS[kind]["fields"]
        assert out["status"] == "form" and out["kind"] == kind, out
        assert list(out["fields"]["required"]) == list(fields["required"])
        assert list(out["fields"]["optional"]) == list(fields["optional"])
        assert ("how" in out) == bool(fields.get("guide"))
        assert staging.AS_GIVEN in out["note"] and budget.measure(out) <= budget.REPLY_CHARS
        log("OUTPUT", f"{kind}: {len(fields['required'])} required fields, {budget.measure(out):,} chars")
    bad = asyncio.run(go("recipe"))
    assert bad["status"] == "declined" and any("unknown kind" in p for p in bad["problems"])
    assert bad["forms"] == staging.FORM_CALL


def test_feed_names_in_the_description_briefs_and_gaps_in_capabilities(log):
    """feeds_query's description names every feed (derived); each feed's brief, its params
    as docs and the reason behind a declared gap live in platform_capabilities: nothing lost."""
    desc = feeds.describe()
    caps = registry.capabilities(available_tools=["platform_capabilities"])
    assert "servirplatform://capabilities/feeds" in desc and "DECLARED GAP" not in desc
    for name, row in registry.FEEDS.items():
        assert f"`{name}`" in desc, name
        brief = caps["feeds"][name]
        if row.get("description"):
            assert brief["about"] == str(row["description"]), name
        if row.get("status") == "available":
            assert (row.get("title") or name) in desc
        else:
            assert brief["declared_gap"] == row["reason"]
        doc = feeds.params_doc(row)
        if doc:
            assert brief["params"] == {k: str(v) for k, v in doc.items()}
    log("OUTPUT", f"{len(registry.FEEDS)} feeds named in {len(desc):,} chars")


def test_a_capabilities_section_is_one_resource_away(log):
    """servirplatform://capabilities/<section> serves that section whole, as the descriptions
    point to it; an unknown section declines with the list."""
    from fastmcp import Client

    async def read(section):
        async with Client(mcp) as c:
            got = await c.read_resource(f"servirplatform://capabilities/{section}")
            return json.loads(got[0].text)
    caps = registry.capabilities(available_tools=["platform_capabilities"])
    got = asyncio.run(read("feeds"))["feeds"]
    name = next(iter(caps["feeds"]))
    assert set(got) == set(caps["feeds"]) and got[name]["params"] == caps["feeds"][name]["params"]
    bad = asyncio.run(read("nope"))
    assert bad["status"] == "declined" and "feeds" in bad["note"]
    log("OUTPUT", f"feeds section: {budget.measure(got):,} chars")


def test_the_build_prompt_carries_the_observability_rule_whole(log):
    """The rule moved out of the instructions; its home is the prompt every builder runs."""
    p = build_a_tool()
    assert instructions.section_text("observability") in p
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", p)) - {"pack_id", "required_sections"}
    assert named <= set(_tools())
    log("CHECK", "rule present; prompt still names only published tools")


# --------------------------------------------------------------------------- replies (G12.4)
def _call(tool, args):
    """(the text a client reads, the structured content) for one real tool call."""
    from fastmcp import Client

    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args)
            return r.content[0].text, r.structured_content
    return asyncio.run(go())


def _read(uri):
    from fastmcp import Client

    async def go():
        async with Client(mcp) as c:
            return json.loads((await c.read_resource(uri))[0].text)
    return asyncio.run(go())


def _ranks(reply) -> list[int]:
    rank = {name: i for i, (name, _) in enumerate(shape.PRIORITY)}
    rank.update({"bulk": len(rank), "tail": len(rank) + 1})
    return [rank[g] for g in shape.groups(reply)]


NEVER_SPILLED = ("pack_id", "answer_status", "next_step", "no_arithmetic", "required_sections",
                 "citations", "your_next_output")


def _same_or_cut(shaped_cits, raw_cits):
    """Every citation keeps its n and its text — whole, or its head marked as cut (the whole
    text one resource away)."""
    assert [c["n"] for c in shaped_cits] == [c["n"] for c in raw_cits]
    cut = 0
    for c, r in zip(shaped_cits, raw_cits):
        if c["text"] != r["text"]:
            assert c["text"].endswith(shape.CUT_MARK) and r["text"].startswith(c["text"][:-len(shape.CUT_MARK)])
            cut += 1
    return cut


def _pack_contract(raw, shaped, log, label):
    """What every shaped pack holds at the default budget: fits, declared order, one short
    contextual line, and whatever had to spill (a live pack can be over budget) is one
    resource away, whole — the drafting essentials of every citation never leave."""
    keys = list(shaped)
    spilled = {k: v for k, v in shaped.items() if shape.is_pointer(v) and k != "citations_full"}
    log("OUTPUT", f"{label}: {budget.measure(raw):,} -> {budget.measure(shaped):,} chars; "
                  f"{keys[:7]}...; spilled {sorted(spilled) or 'nothing'}"
                  f"{' + citations compacted' if 'citations_full' in shaped else ''}")
    assert budget.measure(shaped) <= budget.REPLY_CHARS
    assert keys[:2] == ["status", "pack_id"] and keys[-1] == "your_next_output"
    order = ["answer_status", "next_step", "no_arithmetic", "required_sections", "stats", "citations"]
    at = [keys.index(k) for k in order]
    assert at == sorted(at), keys
    assert all(keys.index(k) > keys.index("citations") for k in ("trace", "exec", "gaps", "queries"))
    assert _ranks(shaped) == sorted(_ranks(shaped)), shape.groups(shaped)
    assert set(shaped) - {"citations_full"} == set(raw)
    for k in NEVER_SPILLED:
        assert k in shaped and not shape.is_pointer(shaped[k]), k
    for k, v in spilled.items():
        assert _read(v["spilled_to"]) == {k: raw[k]}, k
    if "citations_full" in shaped:
        assert all(set(c) <= set(shape.CITATION_ESSENTIALS) for c in shaped["citations"])
        for c, r in zip(shaped["citations"], raw["citations"]):
            assert all(c.get(k) == r.get(k) for k in shape.CITATION_ESSENTIALS if k != "text")
        log("OUTPUT", f"{label}: {_same_or_cut(shaped['citations'], raw['citations'])} citation text(s) cut")
        assert _read(shaped["citations_full"]["spilled_to"]) == {"citations": raw["citations"]}
    else:
        assert shaped["citations"] == raw["citations"]
    assert len(shaped["no_arithmetic"]) <= budget.NOTE_CHARS
    assert "accept or reject" in shaped["no_arithmetic"]


def test_a_land_cover_pack_fits_and_follows_the_priority_through_the_tool(lc_env, log):  # noqa: F811
    args = dict(pack="land-cover", place="Test District", year=2000, year_to=2022)
    raw = assemble.assemble(**args)
    _pack_contract(raw, shape.shape(raw, "pack"), log, "land-cover change pack")
    text, out = _call("assemble_pack", args)
    assert len(text) <= budget.REPLY_CHARS and len(text) == budget.measure(out)
    assert list(out) == list(shape.shape(raw, "pack"))
    _, full = _call("assemble_pack", {**args, "detail": "full"})
    assert list(full) == list(raw), "detail='full' is the module's own reply, unshaped"
    _, bad = _call("assemble_pack", {**args, "detail": "terse"})
    assert bad["status"] == "declined" and "concise" in bad["note"]


def test_a_risk_pack_fits_and_follows_the_priority(offline_aoi, log):  # noqa: F811
    raw = assemble.assemble(place="Testville", hazard="flood")
    assert raw["status"] == "ok", raw.get("note")
    _pack_contract(raw, shape.shape(raw, "pack"), log, "risk pack")


def test_a_food_security_pack_fits_and_follows_the_priority(pack, log):  # noqa: F811
    assert pack["status"] == "ok", pack.get("note")
    _pack_contract(pack, shape.shape(pack, "pack"), log, "food-security pack (live)")


def test_a_contributed_table_and_method_fit_and_follow_the_priority(env, monkeypatch, log):  # noqa: F811
    """A freshly contributed table read through feeds_query, and a pack computing the method
    contributed over it: both shaped at the boundary, the method named before the rows."""
    _units(env)
    staging.submit("method", _threshold())
    text, hinted = _call("feeds_query", {"dataset": "test_restoration_units", "params": {"limit": 5}})
    keys = list(hinted)
    log("OUTPUT", f"contributed table lookup: {len(text):,} chars; {keys}")
    assert len(text) <= budget.REPLY_CHARS and keys[:2] == ["status", "dataset"]
    assert keys.index("for_a_governed_answer") < keys.index("records")
    hint = hinted["for_a_governed_answer"]
    assert hint["methods_reading"][0]["method"] == "restorable_above_threshold"
    assert len(hint["no_arithmetic"]) <= budget.NOTE_CHARS and "shown" not in hinted

    def gather(target, focus, trace, extras):
        return ([{"n": 1, "kind": "area", "retrieval": "computed-at-pack-time", "source": "test",
                  "title": "the area", "text": "Test Province"}], [],
                {"queries": None, "_area": {"name": "Test Province", "geometry": BOX}})
    monkeypatch.setitem(packs.PACKS["land-cover"], "gather", gather)
    raw = assemble.assemble(pack="land-cover", place="Test Province", year="2020")
    assert raw["status"] == "ok", raw.get("note")
    shaped = shape.shape(raw, "pack")
    _pack_contract(raw, shaped, log, "pack with a contributed method")
    assert shaped["methods"]["restorable_above_threshold"]["results"]["restorable_ha"]["value"] == 1871


def test_over_budget_spills_the_lowest_priority_bulk_to_resources_that_resolve(lc_env, monkeypatch, caplog, log):  # noqa: F811
    """Past the budget the bulk goes first, lowest priority first, each section replaced by
    a pointer whose resource serves it whole; ids, the next call, the contextual line, the
    required sections and the citations never leave. When even that is not enough the
    citations compact to their drafting essentials; an impossible budget is logged."""
    raw = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022)
    pid, full = raw["pack_id"], budget.measure(raw)
    monkeypatch.setattr(budget, "REPLY_CHARS", full - 3000)
    shaped = shape.shape(raw, "pack")
    spilled = {k: v for k, v in shaped.items() if shape.is_pointer(v)}
    log("OUTPUT", f"{full:,} -> {budget.measure(shaped):,} chars; spilled {sorted(spilled)}")
    assert budget.measure(shaped) <= budget.REPLY_CHARS and "layers" in spilled
    for k in ("pack_id", "answer_status", "next_step", "no_arithmetic", "required_sections",
              "citations", "your_next_output"):
        assert k in shaped and not shape.is_pointer(shaped[k]), k
    assert shaped["citations"] == raw["citations"] and list(shaped)[-1] == "your_next_output"
    for k, v in spilled.items():
        assert v["spilled_to"] == f"servirplatform://pack/{pid}/{k}" and v["chars"] == budget.measure(raw[k])
        assert "detail='full'" in v["how"]
        assert _read(v["spilled_to"]) == {k: raw[k]}, k
    # tighter: citations compact; the full citations are one resource away, right after them
    monkeypatch.setattr(budget, "REPLY_CHARS", 12_000)
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        tight = shape.shape(raw, "pack")
    assert all(set(c) <= set(shape.CITATION_ESSENTIALS) for c in tight["citations"])
    _same_or_cut(tight["citations"], raw["citations"])
    assert list(tight).index("citations_full") == list(tight).index("citations") + 1
    assert _read(tight["citations_full"]["spilled_to"]) == {"citations": raw["citations"]}
    log("OUTPUT", f"at 12,000: {budget.measure(tight):,} chars; warnings {caplog.messages}")
    monkeypatch.setattr(budget, "REPLY_CHARS", 3_000)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        shape.shape(raw, "pack")
    assert any("pack reply" in m for m in caplog.messages), "an overrun must be logged"
    assert _read("servirplatform://pack/0000000000000000/layers")["status"] == "declined"
    assert "citations" in _read(f"servirplatform://pack/{pid}/nope")["note"]


def _passing_draft(p):
    return "\n\n".join(f"{s}\n\nEvidence for this section is cited here [1]."
                       for s in p["required_sections"])


def test_a_published_answer_leads_with_the_receipt_and_keeps_the_brief(lc_env, monkeypatch, log):  # noqa: F811
    """The receipt inside the first 2,000 chars, the brief never spilled; over budget the
    panel's bulk goes to servirplatform://receipt/<id>/<section>, which serves it whole —
    and a receipt RESOLVED later leads with its id too (it used to come last)."""
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022)
    draft = _passing_draft(p)
    text, out = _call("publish_answer", {"pack_id": p["pack_id"], "draft": draft, "question": "forest?"})
    assert out["status"] == "ok", out
    rid = out["receipt_id"]
    log("OUTPUT", f"publish {len(text):,} chars; keys {list(out)[:6]}")
    assert len(text) <= budget.REPLY_CHARS and list(out)[:2] == ["status", "receipt_id"]
    assert rid in text[:2000] and '"public_resolver"' in text[:2000]
    assert out["insight"]["brief"] == draft and out["trace"]["steps"]
    raw = publish.answer(p["pack_id"], draft, question="forest?")
    monkeypatch.setattr(budget, "REPLY_CHARS", 9_000)
    tight = shape.shape(raw, "receipt")
    rid = raw["receipt_id"]
    assert budget.measure(tight) <= 9_000
    assert tight["insight"]["brief"] == draft and tight["render_with"] == raw["render_with"]
    assert tight["next_step"] and tight["answer_status"] and tight["public_resolver"]
    spilled = {**{k: v for k, v in tight.items() if shape.is_pointer(v)},
               **{k: v for k, v in tight["insight"].items() if shape.is_pointer(v)}}
    log("OUTPUT", f"at 9,000: {budget.measure(tight):,} chars; spilled {sorted(spilled)}")
    assert spilled and "sources" in spilled
    for k, v in spilled.items():
        assert v["spilled_to"] == f"servirplatform://receipt/{rid}/{k}"
        assert f"record_receipt(receipt_id='{rid}', detail='full')" in v["how"] and "mints nothing" in v["how"]
        source = raw["insight"] if k in raw["insight"] and k not in raw else raw
        assert _read(v["spilled_to"]) == {k: source[k]}, k
    assert _read(f"servirplatform://receipt/{rid}/insight")["insight"]["brief"] == draft
    assert _read(f"servirplatform://receipt/{rid}/render_with") == {"render_with": raw["render_with"]}
    assert _read(f"servirplatform://receipt/{rid}/nope")["status"] == "declined"
    monkeypatch.setattr(budget, "REPLY_CHARS", 40_000)
    res = shape.shape(record.record(receipt_id=rid), "receipt")
    assert list(res)[:2] == ["status", "receipt_id"]
    assert '"public_resolver"' in budget.serialise(res)[:2000]
    _, full = _call("record_receipt", {"receipt_id": rid, "detail": "full"})
    assert list(full) == list(record.record(receipt_id=rid, with_insight=True))
    assert full["insight"]["brief"] == draft and full["insight"]["layers"] == raw["insight"]["layers"]
    assert "insight" not in record.record(receipt_id=rid)        # concise resolve stays as it was


def test_a_long_feed_is_capped_to_the_budget_with_a_steering_line(monkeypatch, log):
    """Feeds return their rows oldest first: a capped reply keeps the NEWEST ones (the as_of
    row among them), counts what it holds, and says how many of how many."""
    rows = [{"season": "MJJ", "year": 1950 + i // 12, "value": round(i * 0.01, 2),
             "classification": "neutral"} for i in range(6000)]
    monkeypatch.setitem(feeds.ADAPTERS, "climate_index", lambda params, spec: {
        "as_of": "MJJ 2449", "count": len(rows), "summary": "6000 records", "records": rows,
        "query_receipt": "stub", "url": "https://example.invalid", "stale_data": None})
    text, out = _call("feeds_query", {"dataset": "enso_oni"})
    shown = len(out["records"])
    log("OUTPUT", f"{len(text):,} chars; {out['shown']}")
    assert len(text) <= budget.REPLY_CHARS and 0 < shown < 6000
    assert out["shown"].startswith(f"the most recent {shown:,} of 6,000 rows shown") and "limit" in out["shown"]
    assert out["count"] == shown and out["records"] == rows[-shown:]
    assert out["records"][-1] == rows[-1] and out["records"][-1]["year"] == 2449
    keys = list(out)
    assert keys[:2] == ["status", "dataset"] and keys[-1] == "records"
    assert keys.index("for_a_governed_answer") < keys.index("shown") < keys.index("passport")
    assert "do not add up" in text[:2000]
    assert len(out["for_a_governed_answer"]["no_arithmetic"]) <= budget.NOTE_CHARS
    small = shape.shape(loop.with_entry_hint({"status": "ok", "dataset": "d", "records": [1, 2]}), "feed")
    assert "shown" not in small and small["records"] == [1, 2]


def test_search_and_document_replies_page_to_the_budget(log):
    """Hits past the budget are one `offset` away; a long document comes in parts, each inside
    the budget in SERIALISED chars (escapes counted), and the parts rebuild it exactly."""
    hits = [{"score": 0.9, "text": f"{i:03d}" + "x" * 1500, "passport": {"source": "s" * 900}}
            for i in range(60)]
    out = shape.shape(loop.with_entry_hint({"status": "ok", "query": "q", "corpus": "c",
                                            "hits": hits}), "search")
    n = len(out["hits"])
    assert budget.measure(out) <= budget.REPLY_CHARS and 0 < n < 60
    assert out["shown"].startswith(f"{n} of 60 hits shown") and f"offset={n}" in out["shown"]
    more = shape.shape({"status": "ok", "query": "q", "corpus": "c", "hits": hits}, "search", offset=n)
    assert more["hits"][0] == hits[n] and f"(from {n})" in more["shown"]
    for text in ("y" * 120_000, "12\t34\t56\n" * 20_000):
        parts, at = [], 0
        while True:
            again = "corpus_document(doc_id='d', pack='risk', offset={next})"
            doc = shape.shape({"status": "ok", "doc_id": "d", "corpus": "c", "chunks": 3,
                               "extracted_text": text}, "document", offset=at, again=again)
            assert budget.measure(doc) <= budget.REPLY_CHARS
            parts.append(doc["extracted_text"])
            at += len(doc["extracted_text"])
            if at >= len(text):
                assert "that is the end" in doc["shown"]
                break
            assert f"corpus_document(doc_id='d', pack='risk', offset={at})" in doc["shown"]
        assert "".join(parts) == text and len(parts) > 1
        log("OUTPUT", f"{len(text):,} chars of text in {len(parts)} parts")


def test_capabilities_concise_section_and_full_through_the_tool(log):
    """The map at a glance: methods first, packs as counts with their manifest resource,
    feeds as names with a pointer; a section whole on request; detail='full' the whole map."""
    text, out = _call("platform_capabilities", {})
    caps = asyncio.run(_capabilities())
    log("OUTPUT", f"concise {len(text):,} chars vs full {budget.measure(caps):,}; keys {list(out)}")
    assert len(text) <= budget.REPLY_CHARS and len(text) < budget.measure(caps)
    keys = list(out)
    assert keys[:6] == ["server", "contract", "usage", "methods_note", "methods", "tools_available"]
    assert keys.index("domain_packs") < keys.index("feeds")
    assert out["builder_requirements"] == caps["builder_requirements"]
    assert out["coverage"] == caps["coverage"] and set(out["coverage"]["packs"]) == set(packs.PACKS)
    assert out["feeds"]["available"] == sorted(k for k, v in caps["feeds"].items() if v.get("status") == "available")
    assert "servirplatform://capabilities/feeds" in out["feeds"]["briefs"]
    assert [p["id"] for p in out["domain_packs"]] == packs.available()
    assert all(p["manifest"] == f"servirplatform://pack/{p['id']}" for p in out["domain_packs"])
    assert all(k in out["sections"] for k in caps)
    _, sec = _call("platform_capabilities", {"section": "feeds"})
    assert sec == {"feeds": caps["feeds"]}
    _, full = _call("platform_capabilities", {"detail": "full"})
    assert list(full) == list(caps) and full["feeds"] == caps["feeds"]
    _, bad = _call("platform_capabilities", {"section": "nope"})
    assert bad["status"] == "declined" and "feeds" in bad["note"]


def test_the_place_list_is_grouped_by_size_and_whole_at_its_resource(sites, monkeypatch, log):  # noqa: F811
    text, out = _call("resolve_place_time", {"list": "all"})
    (layer,) = out["layers"]
    log("OUTPUT", f"{len(text):,} chars; {layer['names_by_size']}")
    assert "names" not in layer and layer["areas"] == 3
    assert layer["names_by_size"]["small"] == ["Estero (Costa Rica)", "Gulf box (Ecuador)"]
    assert layer["names_by_size"]["large"] == ["Gulf box (Costa Rica)"]
    assert "servirplatform://places" in out["how_to_use"]
    assert list(out).index("how_to_use") < list(out).index("layers")
    full = _read("servirplatform://places")
    names = full["layers"][0]["names"]
    assert len(names) == 3 and all("km2" in r and "size" in r for r in names)
    _, raw = _call("resolve_place_time", {"list": "all", "detail": "full"})
    plain = resolve.place_time(list="all")
    assert raw["layers"] == plain["layers"]
    assert raw["how_to_use"].startswith(plain["how_to_use"].rstrip())
    assert "Complete: every name in all 1 boundary layer(s), 3 names" in raw["how_to_use"]
    monkeypatch.setattr(budget, "REPLY_CHARS", 300)
    monkeypatch.setattr(shape, "_MIN_SPILL", 0)
    tight = shape.shape(shape.concise_places(resolve.place_time(list="all")), "places")
    assert tight["layers"]["spilled_to"] == shape.PLACES and tight["how_to_use"]


def test_the_rule_lives_upstream_and_replies_carry_one_line(log):
    """The long no-arithmetic rule is in the instructions' core, its short form in every
    description a model reads before rows or figures; every reply carries one line under NOTE_CHARS."""
    tools = _tools()
    for name in ("assemble_pack", "feeds_query", "corpus_search"):
        assert instructions.SHORT["no_arithmetic"] in tools[name], name
        assert "NO ARITHMETIC" in tools[name] and "accept or reject" in tools[name], name
    assert "NO ARITHMETIC BY DEFAULT" in instructions.core()
    assert len(loop.NO_ARITHMETIC) <= budget.NOTE_CHARS
    assert loop.ENTRY_HINT["no_arithmetic"] == loop.NO_ARITHMETIC
    # MGV-529: a contributed feed's usage note ("sum hectares") rides every feeds_query reply;
    # the counter-rule is where that note is read, in the core, and in the reply line
    assert "'sum hectares'" in tools["feeds_query"] and "contribute_submit kind=method" in tools["feeds_query"]
    assert "computes and cites it for everyone" in tools["feeds_query"]
    assert "'sum hectares'" in instructions.core() and "'sum hectares'" in loop.NO_ARITHMETIC
    for key, line in loop.ENTRY_HINT.items():
        if isinstance(line, str):
            assert len(line) <= budget.NOTE_CHARS, key
    log("CHECK", f"line {len(loop.NO_ARITHMETIC)} chars; rule in core + 2 descriptions")


def test_the_priority_table_is_the_reply_order_for_every_shaped_kind(log):
    """Declared order, not typed per tool: a verify verdict and a blocked publish follow the
    same table — guidance before the failure lists, bulk after."""
    v = shape.shape({"status": "ok", "report_id": "r", "passed": True, "answer_status": "a",
                     "next_step": {}, "evidence_tier": "t", "draft_sha256": "h",
                     "required_sections": [], "failures": [], "cited": [1], "warnings": []})
    assert list(v) == ["status", "report_id", "passed", "answer_status", "evidence_tier",
                       "draft_sha256", "next_step", "required_sections", "failures", "cited", "warnings"]
    blocked = shape.shape({"status": "blocked", "answer_status": "B", "note": "n", "report_id": "r",
                           "failures": ["f"], "required_sections": ["s"], "next_step": {}}, "receipt")
    assert list(blocked) == ["status", "report_id", "answer_status", "note", "next_step",
                             "required_sections", "failures"]
    assert shape.shape("not a dict") == "not a dict"
    log("CHECK", "one table orders verify, blocked publish and packs alike")


# --------------------------------------------------------------------------- growth (fix pass)
def _grow(monkeypatch, n_stacks=6, n_feeds=8, n_points=3, n_polys=3):
    """Contributions as a busy hub would land them: stacks over new countries, live feeds,
    point layers inside the risk region and polygon layers — names long, as real ones are."""
    from app.graph.geo import registry as geo_registry, vectors
    from app.land_cover import synthesis
    rows = dict(synthesis.landcover_stacks())
    first = next(iter(rows.values()))
    for i in range(n_stacks):
        rows[f"landcover_hub{i}"] = {**first, "title": f"Hub {i} national mangrove and wetland extent",
                                     "countries": [f"Countryland{i}", f"Otherland{i}"],
                                     "legend": {0: "Not mangrove", 1: "Mangrove", 2: f"Salt flat {i}"}}
    monkeypatch.setattr(synthesis, "landcover_stacks", lambda: rows)
    for i in range(n_feeds):
        monkeypatch.setitem(registry.FEEDS, f"hub_feed_{i}", {
            "status": "available", "pack": "land-cover", "adapter": "generic_table",
            "title": f"Hub {i} monthly mangrove carbon and restoration survey table",
            "source": f"Hub {i} national forestry agency", "description": "x"})
    layers = {f"th_points_{i}": {"title": f"Evacuation shelters and towers, set {i} (Thailand, DDPM)",
                                 "bbox": [100.2, 13.5, 100.8, 14.0], "countries": ["Thailand"]}
              for i in range(n_points)}
    layers.update({f"cr_polys_{i}": {"title": f"Protected wetlands {i} (Costa Rica, SINAC)",
                                     "bbox": [-85.3, 9.9, -84.7, 10.3], "countries": ["Costa Rica"]}
                   for i in range(n_polys)})
    fam = {k: ("points" if k.startswith("th_") else "polygons") for k in layers}
    monkeypatch.setattr(vectors, "visible", lambda: layers)
    monkeypatch.setattr(vectors, "family", lambda e: fam.get(next((k for k, v in layers.items() if v is e), ""), "points"))
    monkeypatch.setattr(geo_registry, "countable", lambda: ("hospitals", "schools", "buildings",
                                                            *[k for k in layers if k.startswith("th_")]))
    monkeypatch.setattr(packs, "risk_coverage", lambda: {"hazard_flood": {
        "title": "Flood hazard (Southeast Asia)", "bounds": [92.0, -11.1, 141.2, 28.7], "countries": []}})
    return rows, layers


def test_every_text_stays_inside_its_budget_as_contributions_grow(lc_env, monkeypatch, caplog, log):  # noqa: F811
    """Six stacks, eight feeds, three point and three polygon layers land: every tool
    description and the instructions still fit (derived lists counted past what fits, not
    cut by the client), nothing is logged as over, and every name is in the coverage section."""
    from app.mcp.server import DESCRIBERS
    rows, layers = _grow(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        sizes = {name: len(fn()) for name, fn in DESCRIBERS.items()}
        full, core = instructions.compose(), instructions.core()
    log("OUTPUT", f"{sizes}; instructions {len(full):,}, core {len(core):,}")
    assert all(n <= budget.TOOL_DESCRIPTION for n in sizes.values()), sizes
    assert len(full) <= budget.INSTRUCTIONS and len(core) <= budget.INSTRUCTIONS_CORE
    assert not caplog.messages, caplog.messages
    desc = DESCRIBERS["assemble_pack"]()
    assert "platform_capabilities(section='coverage')" in desc and "COMPUTED, CITED EVIDENCE" in desc
    cov = registry.capabilities(available_tools=["platform_capabilities"])["coverage"]
    blob = json.dumps(cov, ensure_ascii=False)
    for row in rows.values():
        assert str(row["title"]) in blob and all(c in blob for c in row.get("countries") or [])
    for name, row in registry.FEEDS.items():
        if row.get("status") == "available" and row.get("title"):
            assert row["title"] in cov["live_feeds"], name
    assert all(any(k in v for v in cov["contributed_vector_layers"]) for k in layers)
    # an uncapped list still names everything; a capped one counts what it left out
    with budget.capped(36):
        assert budget.names(["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"]) \
            == "alpha, beta, gamma, delta, epsilon and 2 more"
    assert budget.names(["a", "b", "c"], last=" and ") == "a, b and c"


def test_instructions_fit_with_more_packs_and_a_summary_only_pack(monkeypatch, log):
    """Two extra packs: the opening drops to names and shapes rather than overrun either
    budget, every pack still named; a pack with a summary and no `covers` hook points to its
    manifest instead of claiming it declared nothing."""
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy Platform", "target_keys": ("site",), "target_doc": {"site": "x"},
        "summary": "TOY — a summary-only extension pack", "gather": lambda *a, **k: ([], [], None),
        "sections": lambda *a: []})
    core = instructions.core()
    assert "Toy: described at servirplatform://pack/toy (site)" in core
    monkeypatch.setitem(packs.PACKS, "toy2", {
        "display_name": "Second Toy Platform", "target_keys": ("site", "day"),
        "target_doc": {"site": "x", "day": "y"}, "gather": lambda *a, **k: ([], [], None),
        "sections": lambda *a: []})
    full, core = instructions.compose(), instructions.core()
    log("OUTPUT", f"{len(full):,} / {len(core):,}: {core[:300]}")
    assert len(full) <= budget.INSTRUCTIONS and len(core) <= budget.INSTRUCTIONS_CORE
    assert "5 domain packs" in core and full.startswith(core)
    for pid, sp in packs.PACKS.items():
        assert instructions._display(pid, sp) in core and instructions._shape(sp) in core
    for rule in CORE_RULES:
        assert rule in core, rule


def _mangrove_change_pack() -> dict:
    """The shape of a deployed mangrove-change pack (GMW, 1996->2020; recorded at 105-119 KB):
    long layer-values and land-cover citations, document passages with passports, a 24 KB
    layers block — the drafting essentials alone are over the budget."""
    cits, n = [], 0

    def cit(kind, text_len, **extra):
        nonlocal n
        n += 1
        cits.append({"n": n, "kind": kind, "title": f"{kind} citation {n} for Chomes, Gulf of Nicoya",
                     "source": "Global Mangrove Watch v3", "pub_date": "2022-06-01",
                     "validation": "peer-reviewed", "retrieval": "computed-at-pack-time",
                     "review_status": "auto-approved: no human reviewed it",
                     "text": (f"[{n}] " + "Mangrove extent 1996 412.3 ha; 2020 398.1 ha; loss 14.2 ha. ") * (text_len // 60),
                     **extra})
    for _ in range(5):
        cit("class_area", 560, series={"points": [{"t": y, "v": 400.0} for y in range(1996, 2021)]})
    for _ in range(4):
        cit("document", 1600, doc_id="d" * 16, chunk_id="c1", archived_copy="/archive/x.pdf",
            url="https://example.invalid/report.pdf", temporal="2019", score=0.71)
    for _ in range(3):
        cit("feed", 900, dataset="hub_carbon", usage_notes="Sum hectares and carbon; area-weight the score",
            stale_data={"cadence": "monthly", "retrieved_at": "2026-10-20"}, rows_total=120)
    for _ in range(2):
        cit("class_factor", 2300, values_in="Mg C/ha", usage_notes="per class")
    for _ in range(2):
        cit("landcover_layer", 2900, prepared=["reprojected to EPSG:4326"] * 6,
            fetched={"steps": [{"op": "fetch", "url": "https://x.test/" + "p" * 300}] * 3})
    cit("method", 1200)
    for _ in range(5):
        cit("layer_values", 3600, layer="measure_agb", values_in="Mg/ha",
            prepared=["clipped to the boundary"] * 8,
            fetched={"steps": [{"op": "fetch", "url": "https://x.test/" + "q" * 400}] * 3})
    cit("gaps", 2200)
    return {"pack": "land-cover", "target": {"place": "Chomes", "year": "1996", "year_to": "2020"},
            "focus": "mangrove change", "citations": cits,
            "gaps": ["a declared gap, said in full " + "g" * 200] * 9,
            "queries": {"stack": "gmw_v3"}, "required_sections": ["## Extent", "## Change", "## Gaps"],
            "stats": {"by_year": {str(y): {"Mangrove": {"ha": 400.0 + y % 7, "share_pct": 12.5}}
                                  for y in range(1996, 2021)}},
            "trace": {"steps": [{"name": "gather", "ms": 1200, "detail": "t" * 200}] * 10},
            "exec": {"gather_ms": 1200.5},
            "layers": {"measure_agb": {"grid": [[1.5] * 120] * 30, "unit": "Mg/ha"},
                       "measure_soc": {"grid": [[2.5] * 120] * 30, "unit": "t C/ha"}}}


def _as_reply(pid: str, body: dict) -> dict:
    return {"status": "ok", "pack_id": pid, "answer_status": loop.PACK_IS_NOT_AN_ANSWER,
            "next_step": loop.after_assemble(pid, body["required_sections"]),
            "no_arithmetic": loop.NO_ARITHMETIC, **body,
            "your_next_output": loop.YOUR_NEXT_OUTPUT.format(pack_id=pid)}


def test_a_mangrove_change_pack_fits_with_its_figures_kept(lc_env, monkeypatch, caplog, log):  # noqa: F811
    """Past compaction the long citation texts share what room is left (a short one stays
    whole, each cut one marked, the whole list at citations_full) — and `stats`, the figures,
    stays: it spills only when that is what makes the reply fit."""
    from app.mcp import store
    body = _mangrove_change_pack()
    pid = store.save_pack(body)
    raw = _as_reply(pid, body)
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        out = shape.shape(raw, "pack")
    cut = _same_or_cut(out["citations"], raw["citations"])
    log("OUTPUT", f"{budget.measure(raw):,} -> {budget.measure(out):,} chars; {cut} texts cut; "
                  f"spilled {sorted(k for k, v in out.items() if shape.is_pointer(v))}")
    assert budget.measure(raw) > 100_000 and budget.measure(out) <= budget.REPLY_CHARS
    assert not caplog.messages, caplog.messages
    assert out["stats"] == raw["stats"] and 0 < cut < len(raw["citations"])
    short = [c for c in raw["citations"] if c["kind"] == "class_area"]
    assert all(next(x for x in out["citations"] if x["n"] == c["n"])["text"] == c["text"] for c in short)
    for c in out["citations"]:
        assert c["pub_date"] == "2022-06-01" and c["validation"] == "peer-reviewed"
    assert _read(out["citations_full"]["spilled_to"]) == {"citations": raw["citations"]}
    # a huge stats block: it spills, because that is what lets the reply fit
    big = {**body, "stats": {"rows": [{"year": y, "ha": 1.0, "note": "s" * 300} for y in range(120)]}}
    pid2 = store.save_pack(big)
    out2 = shape.shape(_as_reply(pid2, big), "pack")
    assert shape.is_pointer(out2["stats"]) and budget.measure(out2) <= budget.REPLY_CHARS
    assert _read(out2["stats"]["spilled_to"]) == {"stats": big["stats"]}
    # an impossible budget: spilling the figures would gain nothing, so they stay, logged
    monkeypatch.setattr(budget, "REPLY_CHARS", 6_000)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        out3 = shape.shape(raw, "pack")
    assert out3["stats"] == raw["stats"] and any("pack reply" in m for m in caplog.messages)


def test_compose_run_is_shaped_like_a_pack(lc_env, monkeypatch, log):  # noqa: F811
    """compose_run returns the pack's citations and the brief: at the tool boundary it fits,
    the brief leads the figures and is never spilled, the citations are one resource away."""
    from app.mcp import compose, store
    body = _mangrove_change_pack()
    pid = store.save_pack(body)
    brief = "## Extent\n\nMangrove covered 398.1 ha in 2020 [1].\n\n" * 20
    monkeypatch.setattr(compose, "run", lambda **kw: {
        "status": "ok", "composition": "landcover.brief", "brief": brief,
        "citations": body["citations"], "gaps": body["gaps"], "grounded": {"passed": True},
        "pack_id": pid, "report_id": "r" * 16, "receipt_id": "e" * 16, "draft_sha256": "h" * 64,
        "trace": body["trace"], "usage": {"tokens": 1}, "provider": "p", "model": "m",
        "note": "this composition ran the PLATFORM's LLM server-side (metered)"})
    text, out = _call("compose_run", {"composition": "landcover.brief", "question": "mangrove?"})
    keys = list(out)
    log("OUTPUT", f"{len(text):,} chars; {keys}")
    assert len(text) <= budget.REPLY_CHARS and out["brief"] == brief
    assert keys[:4] == ["status", "receipt_id", "report_id", "pack_id"]
    assert keys.index("brief") < keys.index("citations")
    assert _read(out["citations_full"]["spilled_to"]) == {"citations": body["citations"]}


def test_the_review_list_pages_inside_the_budget(monkeypatch, log):
    """41 contributions with their previews ran 172 KB: the list now pages inside the budget,
    each page names the next call, and the pages hold every contribution once."""
    recs = [{"contribution_id": f"{i:016x}", "kind": "raster", "status": "approved",
             "title": f"Contribution {i}", "preview": {"observed": {"bulk": "y" * 4000}}}
            for i in range(41)]
    monkeypatch.setattr(staging, "review_list", lambda status_filter="pending": {
        "status": "ok", "filter": status_filter, "contributions": recs})
    seen, offset, pages = [], 0, 0
    while True:
        text, out = _call("contribute_review", {"action": "list", "status_filter": "all",
                                                "offset": offset})
        assert len(text) <= budget.REPLY_CHARS
        pages += 1
        seen += [r["contribution_id"] for r in out["contributions"]]
        offset = len(seen)
        if offset >= len(recs):
            break
        assert f"contribute_review(action='list', status_filter='all', offset={offset})" in out["shown"]
        assert "contribute_status(contribution_id=<id>)" in out["shown"]
    log("OUTPUT", f"{len(recs)} contributions in {pages} pages")
    assert seen == [r["contribution_id"] for r in recs] and pages > 1


def test_a_publish_keeps_the_panels_map_and_spills_its_bulk_first(lc_env, log):  # noqa: F811
    """A 50 KB publish (a 33 KB panel map, 10 KB of sources): the bulk the model and the panel
    can spare goes first — the map the MCP App draws stays in the reply."""
    p = assemble.assemble(pack="land-cover", place="Test District", year=2000, year_to=2022)
    raw = publish.answer(p["pack_id"], _passing_draft(p), question="forest?")
    raw = {**raw, "insight": {**raw["insight"], "map": {"aoi": {"coords": [[1.0, 2.0]] * 2400}}},
           "sources": [{**raw["sources"][0], "title": "t" * 400} for _ in range(25)]}
    out = shape.shape(raw, "receipt")
    spilled = sorted([k for k, v in out.items() if shape.is_pointer(v)]
                     + [f"insight.{k}" for k, v in out["insight"].items() if shape.is_pointer(v)])
    log("OUTPUT", f"{budget.measure(raw):,} -> {budget.measure(out):,}; spilled {spilled}")
    assert budget.measure(raw) > budget.REPLY_CHARS >= budget.measure(out)
    assert not shape.is_pointer(out["insight"]["map"]) and out["insight"]["brief"] == raw["insight"]["brief"]
    assert "sources" in spilled


def test_a_pack_resource_serves_what_the_reply_carries_never_the_map_payload(offline_aoi, log):  # noqa: F811
    raw = assemble.assemble(place="Testville", hazard="flood")
    pid = raw["pack_id"]
    for key in assemble.NOT_RETURNED:
        got = _read(f"servirplatform://pack/{pid}/{key}")
        assert got["status"] == "declined" and key not in got["note"].split("—")[1].split(";")[0], key
    assert _read(f"servirplatform://pack/{pid}/citations") == {"citations": raw["citations"]}


def test_a_long_decline_or_error_is_logged_against_the_error_budget(caplog, log):
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        shape.shape({"status": "declined", "note": "n" * (budget.ERROR_CHARS + 1)})
    log("OUTPUT", caplog.messages)
    assert any("declined reply" in m and f"{budget.ERROR_CHARS:,}-char" in m for m in caplog.messages)


def test_a_raised_tool_error_is_measured_against_the_error_budget(caplog, log):
    """An exception a tool raises never passes through shape: the server's middleware measures
    it against ERROR_CHARS on the way out and re-raises it unchanged."""
    from fastmcp import Client, FastMCP
    from app.mcp.server import _ErrorBudget
    mini = FastMCP("mini")

    @mini.tool()
    def boom() -> dict:
        raise ValueError("e" * (budget.ERROR_CHARS + 5))
    mini.add_middleware(_ErrorBudget())

    async def go():
        async with Client(mini) as c:
            return await c.call_tool("boom", {}, raise_on_error=False)
    with caplog.at_level(logging.WARNING, logger="app.mcp.budget"):
        res = asyncio.run(go())
    log("OUTPUT", caplog.messages)
    assert res.is_error and any("tool error boom" in m for m in caplog.messages)


def test_a_contributed_tables_caveat_reaches_the_map_whole(monkeypatch, log):
    """A table's `description` is a gate field (say SYNTHETIC if it is): its caveat sits at the
    end, so the feeds section serves it whole — it was cut at 200 chars."""
    about = ("Restoration units per province with hectares and carbon, compiled by the hub from "
             "field surveys 2019-2023 and regridded to the national grid for this table; "
             "CAVEAT: hectares in rows marked est=1 are SYNTHETIC placeholders, not observed data.")
    monkeypatch.setitem(registry.FEEDS, "hub_restoration_units", {
        "status": "available", "pack": "land-cover", "adapter": "generic_table", "contributed": True,
        "title": "Hub restoration units", "source": "Hub", "description": about})
    caps = registry.capabilities(available_tools=["platform_capabilities"])
    log("OUTPUT", f"{len(about)} chars: {caps['feeds']['hub_restoration_units']['about'][-60:]}")
    assert len(about) > 200 and caps["feeds"]["hub_restoration_units"]["about"] == about
    assert _read("servirplatform://capabilities/feeds")["feeds"]["hub_restoration_units"]["about"] == about


def test_the_form_call_and_the_full_place_list_say_how_and_how_much(log):
    """Seen in acceptance: with tool search on, a model never opened contribute_submit and
    asked for generic fields; given every place name, it hedged that more might exist."""
    assert "kind alone returns that kind's form" in instructions.compose()[:budget.INSTRUCTIONS]
    out = shape.complete_places({"layers": [{"layer": "x", "names": [{"name": "A"}, {"name": "B"}]}],
                                 "how_to_use": "Pick one."})
    log("OUTPUT", out["how_to_use"])
    assert "Complete: every name in all 1 boundary layer(s), 2 names" in out["how_to_use"]
    assert shape.PLACES in out["how_to_use"]
