"""The connect-time instructions as DATA: sections in priority order, each with a tier,
a title and its homes. Tier 1 fits budget.INSTRUCTIONS_CORE, tiers 1+2 fit
budget.INSTRUCTIONS, tier 3 is served whole at its homes and only pointed to here."""

from __future__ import annotations


from . import budget

HOW_TO_USE = "servirplatform://how-to-use"
GUIDE = f"resource:{HOW_TO_USE}"


# --------------------------------------------------------------------------- derived lines
def _packs():
    from . import packs
    return packs


def _display(pid: str, sp: dict) -> str:
    return str(sp.get("display_name") or pid).replace(" Platform", "")


def _shape(sp: dict) -> str:
    return "+".join(k for k in sp["target_keys"] if k not in sp.get("optional_keys", ()))


def _feeds_bound(pid: str) -> int:
    from . import registry
    return sum(1 for v in list(registry.FEEDS.values())
               if v.get("status") == "available" and v.get("pack") == pid)


def _methods_bound(pid: str) -> int:
    try:
        from ..contrib import methods
        return len(methods.for_pack(pid))
    except Exception:
        return 0


def pack_lines(detailed: bool = True) -> list[str]:
    """One clause per pack: name, COUNTS of what it covers (its `covers` hook plus the
    feeds and methods bound to it) and its target shape; `detailed=False`, name and shape
    only. Never a layer, stack, class or feed NAME: those are in platform_capabilities."""
    packs = _packs()
    out = []
    for pid, sp in packs.PACKS.items():
        covers = packs.text(sp, "covers")
        gap = None if covers else (f"described at servirplatform://pack/{pid}"
                                   if packs.text(sp, "summary") else "coverage not declared by this pack")
        if not detailed:                           # counts go; a declared gap never does
            out.append(f"{_display(pid, sp)}" + (f": {gap}" if gap else "") + f" ({_shape(sp)})")
            continue
        parts = [covers or gap]
        for n, word in ((_feeds_bound(pid), "feed"), (_methods_bound(pid), "method")):
            if n:
                parts.append(f"{n} {word}{'s' if n != 1 else ''}")
        out.append(f"{_display(pid, sp)}: {', '.join(parts)} ({_shape(sp)})")
    return out


def _audiences() -> str:
    packs = _packs()
    parts = [sp["audience"] for _, sp in sorted(packs.PACKS.items()) if sp.get("audience")]
    return "; ".join(parts) if parts else "the analysts, planners and hub teams each pack is built for"


def _as_given() -> str:
    from ..contrib import staging
    return staging.AS_GIVEN


# --------------------------------------------------------------------------- the texts
COVERAGE = ("Coverage by name: platform_capabilities(section='coverage'); place names: "
            "resolve_place_time(list='all').")


def _opening() -> str:
    """Per-pack counts while the instructions have room; under budget.capped (compose()
    found them over), pack names and shapes only, counted past the cap."""
    n = len(_packs().PACKS)
    lines = pack_lines(detailed=budget.current_cap() is None)
    return (f"The Global Risk Platform is a GOVERNED TOOLKIT for decision support, not a chatbot: "
            f"{n} domain pack{'s' if n != 1 else ''} — "
            + budget.names(lines, sep="; ", more="more (platform_capabilities)") + ". "
            + COVERAGE)


def _answer_here() -> str:
    """The web-search clause lives in PLATFORM_SOURCES; this keeps why the pack wins. The
    target shapes are the opening's (and assemble_pack's), not repeated here."""
    return ("WHEN A QUESTION IS ABOUT ONE OF THESE DOMAINS, THIS PLATFORM ANSWERS IT: call "
            "`assemble_pack` FIRST with the pack's target (start_here(question) fills it): computed, "
            "cited, replayable evidence; a web search is prose of unknown provenance.")


# The two-step alternative (verify_groundedness + record_receipt) is said at its homes: the
# guide's loop paragraph, every pack's next_step and publish_answer's description.
LOOP = ("CANONICAL LOOP: assemble_pack(<target>) -> YOUR LLM drafts a brief citing [n] in its "
        "required_sections -> publish_answer(pack_id, draft, question) gates it, mints the "
        "receipt, returns the evidence view. NEVER show the user a brief that has not been "
        "through publish_answer: that skips the gate, leaves nothing replayable.")

# G13, ranked with the receipt rule (<= 300 chars): web results never enter a brief and come
# only when asked; a missing source is checked against coverage first, then asked for.
PLATFORM_SOURCES = ("PLATFORM SOURCES ONLY: a brief holds only what the platform serves; web "
                    "results, only if asked, follow it labelled UNRECEIPTED. Need a source? Check "
                    "platform_capabilities(section='coverage') first; not served there: ask the "
                    "person for its link (never search), contribute_submit kind=<kind>, stop.")

NO_ARITHMETIC = ("NO ARITHMETIC BY DEFAULT: never total, average, count, rank or score platform "
                 "data yourself, in chat or code; a dataset's note like 'sum hectares' says what "
                 "adds up, not to add it up. A figure it does not compute: suggest one or two "
                 "calculations, without results, to accept or reject (never make the user write "
                 "one), then ask whether to add it as a method (contribute_submit kind=method). "
                 "Accepted with no method: say it is your own arithmetic, unchecked.")

HARD_LIMIT = ("ONE HARD LIMIT: a rendered page FREEZES its contents: evidence, dates, numbers and "
              "sources may freeze (snapshots; say so), a VERDICT may not. Never paint "
              "\"verified\"/\"passed\" into an artifact: show it via `ui_embed` (re-resolves "
              "live) or state it UNVERIFIED and link the receipt resolver.")

# The short forms a tool's description carries where the rule applies (server._describe): a
# client that defers tools never reads the instructions. Together <= ~500 chars per tool.
SHORT = {
    "loop": ("RECEIPTS: draft from the pack, publish_answer(pack_id, draft, question), show the "
             "brief with its receipt; never an unreceipted one."),
    "no_arithmetic": ("NO ARITHMETIC: quote figures as returned; never total, average, count, rank "
                      "or score them, even where a note says 'sum hectares'. A missing figure: "
                      "suggest a calculation (no result) to accept or reject, then offer a method "
                      "(contribute_submit kind=method)."),
    "platform_sources": ("PLATFORM SOURCES ONLY: nothing from the web in a brief; a source not "
                         "served: ask the person for its link, contribute_submit, stop."),
    "hard_limit": ("ONE HARD LIMIT: a page you build never paints a verdict ('verified', "
                   "'passed'): embed it (ui_embed) or state it UNVERIFIED and link the receipt."),
}


def _modes() -> str:
    return ("- BUILD-TIME: asked to BUILD a tool/app, write a REUSABLE artifact calling these "
            "tools as SOURCE FILES IN THE USER'S OWN PROJECT (run, version, deploy), not a "
            "one-off run, never a hosted preview (it FREEZES any verdict). A figure it does not "
            "compute (an amount per area, a score, a ranking): ASK BEFORE BUILDING, as above "
            "(kind=method: servirplatform://skill/contribute-logic); do NOT drop it or compute "
            "it unasked.\n"
            "- RUN-TIME: answer a question now via the canonical loop (corpus_search/"
            "corpus_document: provenance; context_get: crop calendar).\n"
            "- CONTRIBUTE-TIME: someone offers data or logic (a formula, rule or threshold is "
            f"kind=method): call `contribute_submit` (kind alone returns that kind's form: read it "
            f"before asking). {_as_given()} Declared fields (title, "
            "description, source, licence, vintage, countries) are the CONTRIBUTOR'S OWN WORDS: "
            "missing one, ASK and stop; never compose them or submit before they answer.\n"
            "- METHODS are contributed logic that packs compute and cite; "
            "platform_capabilities lists them first. Asked about methods or calculations, list "
            "THOSE, not these tools.")


def _who_for() -> str:
    return (f"KNOW WHO THIS IS FOR, AND SHOW THEM. The people using this are {_audiences()}. "
            "What helps them is SEEING the evidence chain — how old each source is, which are "
            "live pulls, what is missing — not reading it as prose. You do not need to be asked "
            "for a visual. Assume one helps, and produce it.")


RICHEST_OUTPUT = ("USE THE RICHEST OUTPUT YOUR HOST SUPPORTS. This server cannot see which client "
                  "you are running in, so it assumes a coding-agent host (files, HTML you can "
                  "open, markdown, tables) — write a real file the user can open rather than "
                  "describing one. If your host renders more (artifacts, canvases, inline images, "
                  "iframes), use that instead. If it renders less, degrade deliberately: a clean "
                  "table beats a paragraph. Never default to prose just because prose always works.")

OWN_INTERFACE = ("USE THE PLATFORM'S OWN INTERFACE, DO NOT INVENT ONE. This server ships its "
                 "design language and its components: `ui_design` for tokens (colour, type, the "
                 "trust conventions), `ui_catalog` for what exists, `ui_component` for ready-built "
                 "markup and brand assets, `ui_embed` for anything showing a verdict. Tool results "
                 "tell you what to reach for — `record_receipt` returns a `render_with` block "
                 "naming the component for each part. Reach for those before hand-rolling a chart, "
                 "a graph or a colour scheme; what you invent will not match, and a verdict you "
                 "draw yourself attests nothing.")

OBSERVABILITY = ("SHIP OBSERVABILITY WITH ANYTHING YOU BUILD. A product built on this platform "
                 "must emit an execution trace: every response it produces carries a structured "
                 "record of what ran — steps, timings, upstream calls, LLM usage and cost, and "
                 "what each step decided. Read `servirplatform://skill/trace-emit` BEFORE writing "
                 "the backend (it is a complete, portable guide with worked examples) and "
                 "`servirplatform://skill/trace-visualize` for rendering the trace. An answer "
                 "whose execution cannot be inspected is only half governed.")

IFRAME = ("NEVER PASTE IFRAME MARKUP INTO A CHAT REPLY. Chat hosts block external frames (CSP "
          "frame-src 'self'), so it renders as a blank white box. In hosts that render MCP Apps "
          "the evidence panel appears beside the tool result by itself; ui_embed's iframe is only "
          "for web pages you are writing.")

INTERACTIVE = ("INTERACTIVE SURFACES: this platform ships MCP Apps. If someone questions how risk "
               "is WEIGHTED, call `risk_weights` — in a host that renders MCP Apps it opens a "
               "picker with a slider per vulnerability layer, and proposing from it goes through "
               "the contribution gate. If someone asks to SEE the evidence behind an answer, "
               "`record_receipt` and `publish_answer` carry an evidence view the host renders. "
               "Measured: small models asked these very questions answered from memory instead "
               "of opening the tool, so reach for them by name.")


# --------------------------------------------------------------------------- the data
# Priority order. `home`: where a tier-3 section's FULL text is served besides the guide;
# `pointer`: the one line that stays in the instructions for it.
SECTIONS: tuple[dict, ...] = (
    {"key": "opening", "tier": 1, "title": "What this is", "text": _opening},
    {"key": "answer_here", "tier": 1, "title": "This platform answers its domains",
     "text": _answer_here},
    {"key": "loop", "tier": 1, "title": "The canonical loop", "text": LOOP},
    {"key": "platform_sources", "tier": 1, "title": "Platform sources only",
     "text": PLATFORM_SOURCES, "home": ("prompt:run_analysis", "prompt:build_a_tool")},
    {"key": "no_arithmetic", "tier": 1, "title": "No arithmetic by default", "text": NO_ARITHMETIC},
    {"key": "hard_limit", "tier": 1, "title": "One hard limit", "text": HARD_LIMIT},
    {"key": "modes", "tier": 2, "title": "Build-time, run-time, contribute-time", "text": _modes},
    {"key": "who_for", "tier": 3, "title": "Know who this is for, and show them", "text": _who_for,
     "pointer": "KNOW WHO THIS IS FOR (ui_catalog)", "triggers": ("KNOW WHO THIS IS FOR",),
     "home": ("tool:ui_catalog",)},
    {"key": "richest_output", "tier": 3, "title": "Use the richest output your host supports",
     "text": RICHEST_OUTPUT,
     "pointer": "RICHEST OUTPUT your host supports (ui_component)",
     "triggers": ("RICHEST OUTPUT",), "home": ("tool:ui_component",)},
    {"key": "own_interface", "tier": 3, "title": "Use the platform's own interface",
     "text": OWN_INTERFACE,
     "pointer": "PLATFORM'S OWN INTERFACE (ui_design)", "triggers": ("OWN INTERFACE",),
     "home": ("tool:ui_design",)},
    {"key": "interactive_surfaces", "tier": 3, "title": "Interactive surfaces", "text": INTERACTIVE,
     "pointer": ("risk WEIGHTED: risk_weights; to SEE the evidence: "
                 "publish_answer/record_receipt (MCP Apps)"),
     "triggers": ("WEIGHTED", "risk_weights", "SEE the evidence", "publish_answer", "record_receipt"),
     "home": ("tool:risk_weights", "tool:record_receipt")},
    {"key": "observability", "tier": 3, "title": "Ship observability with anything you build",
     "text": OBSERVABILITY,
     "pointer": "SHIP OBSERVABILITY: servirplatform://skill/trace-emit (build_a_tool)",
     "triggers": ("SHIP OBSERVABILITY", "trace-emit"),
     "home": ("prompt:build_a_tool", "skill:trace-emit")},
    {"key": "iframe", "tier": 3, "title": "Never paste iframe markup into a chat reply",
     "text": IFRAME,
     "pointer": "NEVER PASTE IFRAME MARKUP into chat (ui_embed)", "triggers": ("IFRAME",),
     "home": ("tool:ui_embed",)},
)

# The guide's own trigger, kept in the instructions: a question about the server itself.
ASKED_HOW = f"How to use this server: explain_platform or {HOW_TO_USE} (every rule in full)."


PACK_IS_EVIDENCE = ("A pack is evidence, NOT a finished answer. Draft from it, citing the numbered "
                    "items as [n] under its required_sections, then publish_answer(pack_id, draft, "
                    "question) gates the draft and mints the receipt (verify_groundedness then "
                    "record_receipt are the same two steps apart). An answer that skips the gate "
                    "is ungoverned and has nothing to replay; the receipt is also what returns the "
                    "evidence view, so skipping it means the user reads prose instead of seeing "
                    "the evidence chain. Each result names its own next call.")


def calling_a_pack() -> str:
    """Each pack's target and how to read what comes back; every other parameter (layers,
    vectors, within, geometry, ...) is described on assemble_pack's parameters page."""
    packs = _packs()
    targets = "\n".join(
        f"- `{pid}`: " + "; ".join(f"{k} — {v}" for k, v in (sp.get("target_doc") or {}).items())
        for pid, sp in sorted(packs.PACKS.items()))
    return (
        "### Targets, one pack at a time (bare country/crop stays food-security)\n"
        + targets + "\n\n"
        "### Beyond the target\n"
        "Every parameter (an area you hold, raster layer values, contributed vector layers "
        "measured inside the area, `within` another layer, the map's point layer, a calendar "
        "override, the reply's detail) is described where a client reads it, in "
        "assemble_pack's input schema, and whole at " + PARAMETERS.format(tool="assemble_pack")
        + ".\n\n"
        "### What comes back\n"
        "{status, pack_id, citations: [{n, ...}], gaps, queries, required_sections, stats, "
        "layers, vectors} — a declined call carries `note` (missing key, torn corpus, bad "
        "override). DECLARED GAPS are content: surface them; they are what is missing, said "
        "honestly. " + PACK_IS_EVIDENCE)


# --------------------------------------------------------------------------- composing
def _text(section: dict) -> str:
    t = section["text"]
    return t() if callable(t) else t


def section_text(key: str) -> str:
    return _text(next(s for s in SECTIONS if s["key"] == key))


def tier(n: int) -> str:
    return "\n\n".join(_text(s) for s in SECTIONS if s["tier"] == n)


def pointers() -> str:
    """Tier 3 in one paragraph: a line per section with its trigger and home, then the guide."""
    lines = [s["pointer"] for s in SECTIONS if s["tier"] == 3]
    return "ALSO, in full at their homes: " + "; ".join(lines) + ". " + ASKED_HOW


def _fitted() -> tuple[str, str]:
    """(tier 1, the whole text) with the most names that fit both budgets (budget.fitted); the
    tightest when nothing does (compose() logs it). Live state is read once (budget.snapshot)."""
    def render():
        t1 = tier(1)
        return t1, "\n\n".join(p for p in (t1, tier(2), pointers()) if p)
    with budget.snapshot():
        (t1, out), _ok = budget.fitted(render, lambda r: len(r[0]) <= budget.INSTRUCTIONS_CORE
                                       and len(r[1]) <= budget.INSTRUCTIONS)
    return t1, out


def core() -> str:
    """What a client keeping only budget.INSTRUCTIONS_CORE chars must still see."""
    return _fitted()[0]


def compose() -> str:
    """The instructions as sent at initialize, measured against both budgets."""
    t1, out = _fitted()
    budget.check("instructions tier 1", t1, budget.INSTRUCTIONS_CORE)
    budget.check("instructions", out, budget.INSTRUCTIONS)
    return out


def homes(section: dict) -> tuple[str, ...]:
    """Every surface a section's full text is served at: the instructions for tiers 1-2,
    the guide for all, and the declared tool/prompt/skill homes."""
    base = ("instructions",) if section["tier"] < 3 else ()
    return base + (GUIDE,) + tuple(section.get("home", ()))


def inventory() -> list[tuple[str, str]]:
    return [(s["key"], where) for s in SECTIONS for where in homes(s)]


def home_text(surface: str) -> str:
    """The full text of every section homed on `surface` ('tool:<name>', 'prompt:<name>',
    'skill:<name>'), for that surface to append."""
    return "\n\n".join(_text(s) for s in SECTIONS if surface in s.get("home", ()))


def with_home(surface: str, text: str) -> str:
    extra = home_text(surface)
    return f"{text}\n\n{extra}" if extra else text


def rules(keys) -> str:
    """The short form of each section `keys` names, in the sections' priority order."""
    return " ".join(SHORT[s["key"]] for s in SECTIONS if s["key"] in keys and s["key"] in SHORT)


def with_rules(keys, text: str) -> str:
    said = rules(keys)
    return f"{text}\n\n{said}" if said else text


def _coverage() -> str:
    try:
        from . import registry
        return registry.coverage_line()
    except Exception:
        return ""


PARAMETERS = HOW_TO_USE + "/parameters/{tool}"


def tool_params(name: str, said: dict) -> str:
    """One tool's parameter descriptions, whole: the page servirplatform://how-to-use/parameters/
    <tool> serves (its input schema says the same)."""
    lines = [f"- `{p}`: {' '.join(str(d).split())}" for p, d in (said or {}).items() if d]
    return (f"# `{name}`: how to call it\nEvery parameter as its input schema says it (its "
            "description says when to use it).\n\n" + "\n".join(lines) + "\n") if lines else ""


def params_pointer(tools) -> str:
    """'## Every tool's parameters': where each tool's are served whole — a page per tool, so the
    guide stays one reply however many layers, stacks and feeds the parameters name."""
    names = [n for n, said in tools or () if any((said or {}).values())]
    return ("\n## Every tool's parameters\nEach tool's input schema describes every parameter; the "
            f"same text, whole, one page per tool: {PARAMETERS} — "
            + ", ".join(f"`{n}`" for n in names) + ".\n") if names else ""


def how_to_use(prompts: list[tuple[str, str]] | None = None, tools=None) -> str:
    """The guide resource: how to use the platform, with every rule and every tool's parameters
    in full — derived from the pack registry and the server's own prompt and tool lists."""
    packs = _packs()
    menu = "\n".join(f"- `{name}`: {' '.join(str(doc or '').split())}" for name, doc in prompts or ())
    menu = f"\n## Prompts (the host's prompt menu)\n{menu}\n" if menu else ""
    names = ", ".join(_display(pid, sp) for pid, sp in packs.PACKS.items())
    examples = "\n".join(
        f"- {_display(pid, sp)}: `assemble_pack("
        + ", ".join(f"{k}={v!r}" for k, v in (sp.get("doctor_target") or {}).items()) + ")`"
        for pid, sp in packs.PACKS.items() if sp.get("doctor_target"))
    rules = "\n\n".join(f"### {s['title']}\n{_text(s)}" for s in SECTIONS)
    return f"""# How to use the Global Risk Platform

A **governed toolkit** for decision support across {len(packs.PACKS)} domain packs — {names} — \
not a chatbot. Every insight carries its evidence, declines say why, and a groundedness gate \
blocks ungrounded briefs. {_coverage()} What each pack covers today, by name: \
`platform_capabilities(section='coverage')` or `servirplatform://pack/<id>`; every place name: \
`resolve_place_time(list='all')`.

## Build-time — build your own tool on ours
Ask your AI client to *build* something. It produces a reusable app/script that calls these
tools, so the guardrails live in your tool, not in a one-off chat.
- Claude Code: run the `build_a_tool` prompt (`/` menu) or describe the tool you want.
- Claude Desktop: pick **build_a_tool** from the prompt (+) menu.

## Run-time — get one governed answer now
Ask a question. The assistant runs the loop and returns a grounded, cited brief; \
`start_here(question)` says which pack, which arguments and the rules, read from the question's \
words. One worked target per pack:
{examples}
{menu}
## The canonical loop (what a good answer does)
platform_capabilities (scope honestly) → assemble_pack(the pack's target: \
{packs.target_shapes_long()}) → your LLM drafts a brief from the pack citing [n] in its \
required_sections → publish_answer(pack_id, draft, question) gates the draft, mints the \
replayable receipt and returns the evidence view. Never show a brief that has not been through \
publish_answer. (verify_groundedness + record_receipt are the same two steps apart.)

## Calling and reading a pack
{calling_a_pack()}

## The rules, in full
The connect-time instructions carry the first ones whole and point here for the rest.

{rules}
{params_pointer(tools)}
## See what's actually live (never trust a stale doc)
Call **platform_capabilities** (or run the `explain_platform` prompt) for the current tools,
methods, packs, feeds and DECLARED gaps — that map is derived from the live server.
"""
