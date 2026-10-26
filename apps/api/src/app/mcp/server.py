"""FastMCP server — the bones as MCP tools. Run over stdio:
    uv run python -m app.mcp.server
Tools are thin carriers over the domain packs' functions; logic lives in the
called modules, not here (the tool-vs-prompt litmus, ARCHITECTURE §1).
"""

from __future__ import annotations

from typing import Any

import base64
import contextvars
import builtins
import logging
import os
import threading
import time
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from mcp.types import Icon

from . import (app_ui, assemble, auth, budget, compose, context, feeds, fetch, instructions,
               loop, weights_ui,
               publish, record, registry, resolve, shape, start, store, tooltext, ui, verify)
from ..config import get_settings

log = logging.getLogger(__name__)


def _transport_security() -> dict:
    """DNS-rebinding protection defends a LOCALHOST server from a browser being
    tricked into reaching it; behind a hosted proxy the Host is the public domain
    and the default rejects every request (421). `*` disables it for that case.

    Returned as http_app()/run() KWARGS: the setting moved off the constructor when
    the server moved onto fastmcp, so the caller that builds the ASGI app applies it."""
    hosts = os.environ.get("GRP_MCP_ALLOWED_HOSTS", "").strip()
    if not hosts:
        # "auto" and NOT the library default: fastmcp defaults host_origin_protection
        # to False, which installs no guard at all, where the SDK this server used to
        # run on defaulted to protection ON. "auto" restores that exactly — it checks
        # Host and Origin when the server is bound to a loopback address (the case the
        # protection is for) and stays out of the way otherwise (the hosted-proxy case).
        return {"host_origin_protection": "auto"}
    if hosts == "*":
        return {"host_origin_protection": False}
    allowed = [h.strip() for h in hosts.split(",") if h.strip()]
    return {"host_origin_protection": True, "allowed_hosts": allowed,
            "allowed_origins": [f"https://{h}" for h in allowed]}


def _http_transport() -> dict:
    """The kwargs to build an HTTP transport with, plus the precondition that
    must hold before one is built (the OAuth provider is attached here).

    Two entry points build their own transport — `uvicorn app.main:app` via
    http_app(), and `python -m app.mcp.server --http` via run() — and none of
    this is a constructor argument both would inherit. One shared builder keeps
    the Host-header rule and the OAuth provider identical on both surfaces.

    stateless_http: each tool call is self-contained, so there is no session to
    terminate and nothing for a proxy to have to pin to one instance.
    """
    mcp.auth = auth.provider()
    return {"stateless_http": True, **_transport_security()}

# Orientation shown to a connecting LLM at initialize: composed from mcp/instructions.py
# (sections as data, budgeted to what the strictest client keeps; derived from the packs).
def _target_shapes(sep: str = " | ") -> str:
    from . import packs as _packs
    return _packs.target_shapes(sep)


def _instructions() -> str:
    return instructions.compose()


def _serving() -> bool:
    """GRP_SERVE_INSTRUCTIONS, read at call time: off, initialize carries no instructions."""
    return bool(getattr(get_settings(), "grp_serve_instructions", True))


_STARTED = time.perf_counter()
INSTRUCTIONS = _instructions()

# Host, port, transport security and statelessness are no longer constructor
# arguments: they belong to whoever builds the ASGI app or runs the transport, so
# they come from _http_transport() at that point.
# Remote is still the faithful build surface: consumers connect by URL, so no filesystem path leaks into
# a client config for a coding agent to follow into our source (ARCHITECTURE §6).
def _brand_icons() -> list[Icon]:
    """The SERVIR Symbol as the server's icon: Primary for light grounds, Reversed
    Color for dark, as the design file defines them (SERVIR/Logo/Symbol, nodes
    3:2720 and 3:3027). Committed renders, not a live pull: a Figma render URL
    expires, and the icon must not depend on Figma being up at start. Data URIs, so
    a host has nothing to fetch and nothing to be gated from. Without one, hosts fall
    back to the domain's favicon — the parent company's, not SERVIR's."""
    out = []
    for treatment, theme in (("primary", "light"), ("reversed-color", "dark")):
        png = ui.theme_path().parent / "brand" / f"servir-symbol-{treatment}-64.png"
        if png.exists():
            out.append(Icon(src="data:image/png;base64,"
                                + base64.b64encode(png.read_bytes()).decode(),
                            mimeType="image/png", sizes=["64x64"], theme=theme))
    return out


mcp = FastMCP("servirplatform", instructions=INSTRUCTIONS if _serving() else None, icons=_brand_icons(),
              website_url=get_settings().grp_public_url.strip() or None)

# tool name -> the function that writes its description (DESCRIBERS) or its parameters'
# (PARAMS) from live state. One map feeds the decorator AND _LiveText: nothing frozen at import.
DESCRIBERS: dict[str, Any] = {}
PARAMS: dict[str, Any] = {}
RIDES: dict[str, tuple] = {}            # tool -> the rules whose short form its description carries
_BUILT: dict[str, str] = {}             # the text each tool registered with at import
_PARAMS_BUILT: dict[str, dict] = {}


def _describe(name: str, fn, rules: tuple = (), params=None) -> str:
    """Register the tool's live describer: `fn`, the short form of each of `rules` and every section
    homed on it, fitted to budget.TOOL_DESCRIPTION (lists counted by rank, an overrun logged); and
    `params`, its parameters' descriptions, fitted to budget.PARAM_DESCRIPTION. Returns the text."""
    def live() -> str:
        return budget.fit(f"tool description {name}",
                          lambda: instructions.with_home(f"tool:{name}",
                                                         instructions.with_rules(rules, fn())),
                          budget.TOOL_DESCRIPTION)
    DESCRIBERS[name], RIDES[name] = live, tuple(rules)
    if params is not None:
        PARAMS[name] = lambda: budget.fit_each(f"parameter of {name}:", params,
                                               budget.PARAM_DESCRIPTION)
        _PARAMS_BUILT[name] = PARAMS[name]()
    _BUILT[name] = live()
    return _BUILT[name]


def _with_params(schema: dict, said: dict | None) -> dict:
    """The input schema with each parameter's served description; the same object when none changes."""
    props = (schema or {}).get("properties") or {}
    said = {k: v for k, v in (said or {}).items() if k in props and v and props[k].get("description") != v}
    if not said:
        return schema
    return {**schema, "properties": {k: ({**p, "description": said[k]} if k in said else p)
                                     for k, p in props.items()}}


def _signature() -> tuple:
    """(path, mtime, size) of where contributions land: every conf file or directory the
    settings name, the vector registry and each pack's document library, and the feed rows'
    generation. A change makes the cached text stale before its TTL (which catches the rest)."""
    from ..graph.geo import vectors
    from ..rag.store import corpus_dir
    from . import packs
    s = get_settings()
    cache = str(s.cache_dir)
    paths = [p for p in vars(s).values() if isinstance(p, Path) and not str(p).startswith(cache)]
    paths += [Path(cache) / vectors.REGISTRY] + [corpus_dir(str(sp["corpus"]))
                                                 for sp in packs.PACKS.values() if sp.get("corpus")]
    out = []
    for p in paths:
        try:
            st = os.stat(p)
            out.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((str(p), None, None))
    return (*out, ("feeds generation", registry.FEEDS_GEN))


def _sync_feeds() -> None:
    try:
        registry.sync_declarative_feeds()
    except Exception:
        log.exception("feeds: conf/feeds could not be re-read; serving the rows already held")


class _LiveText(Middleware):
    """Connect-time text served from memory (a client drops a server slow to connect): built at
    startup and served at once on every handshake and tools/list; stale (TTL, or a contribution
    store changed), ONE background thread rebuilds it. Invalidated: rebuilt in line."""
    TTL = 300.0                         # a backstop: _signature() catches contributions at once

    def __init__(self):
        self._at = 0.0                  # monotonic time of the last build; 0.0: none to serve
        self._instr = None
        self._descs: dict = {}
        self._params: dict = {}         # tool -> {parameter: description}
        self._sig = None
        self._gen = 0                   # bumped by invalidate(): an older build is dropped
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def build(self) -> tuple[str | None, dict, dict]:
        """(instructions, {tool: description}, {tool: {parameter: description}}) from live
        state, each source read once."""
        descs, params = {}, {}
        with budget.snapshot():
            for out, kept, makers, what in ((descs, self._descs, DESCRIBERS, "description"),
                                            (params, self._params, PARAMS, "parameters")):
                for name, fn in makers.items():
                    try:
                        out[name] = fn()
                    except Exception:     # keep the last text served rather than fail a listing
                        log.exception("connect-time text: the %s %s failed to build", name, what)
                        if kept.get(name):
                            out[name] = kept[name]
            try:
                instr = _instructions()
            except Exception:
                log.exception("connect-time text: the instructions failed to build")
                instr = None
        return instr, descs, params

    def rebuild(self, why: str = "asked") -> None:
        """Build in a context of its own (no caller, no caps), swap it in, log it once. A feed
        another worker landed is read in first (registry.sync_declarative_feeds)."""
        _sync_feeds()
        gen, sig, t0 = self._gen, _signature(), time.perf_counter()
        instr, descs, params = contextvars.Context().run(self.build)
        ms = (time.perf_counter() - t0) * 1000
        if gen != self._gen:
            return                        # invalidated while building: the next request rebuilds
        self._descs, self._params = descs, params
        if instr:
            self._instr = instr
            mcp.instructions = instr if _serving() else None    # sessions created from now on
        self._sig, self._at = sig, time.monotonic()
        (log.warning if ms > budget.REBUILD_MS else log.info)(
            "connect-time text rebuilt in %.0f ms (%s): %d descriptions, instructions %s chars",
            ms, why, len(descs), f"{len(instr or ''):,}")

    def seed(self, instr: str, descs: dict, ms: float, params: dict | None = None) -> None:
        """What import built is served from the first request: no rebuild needed."""
        self._instr, self._descs, self._params = instr, dict(descs), dict(params or {})
        self._sig, self._at = _signature(), time.monotonic()
        log.info("connect-time text built at startup in %.0f ms: %d descriptions, instructions "
                 "%s chars", ms, len(descs), f"{len(instr):,}")

    def invalidate(self) -> None:
        """Drop the cached text: the next request builds it in line."""
        self._gen += 1
        self._at = 0.0

    def wait(self, timeout: float | None = None) -> None:
        """Until a background rebuild in flight has finished."""
        t = self._thread
        if t is not None:
            t.join(timeout)

    def _background(self, why: str) -> None:
        try:
            self.rebuild(why)
        except Exception:
            log.exception("connect-time text: background rebuild failed; serving the last text")

    def _fresh(self) -> None:
        """Serve now; rebuild in line only when nothing was ever built."""
        if self._at == 0.0:
            self.rebuild("nothing built yet")
            return
        why = ("older than its TTL" if time.monotonic() - self._at >= self.TTL else
               "a contribution store changed" if _signature() != self._sig else None)
        if why is None:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._background, args=(why,),
                                            name="connect-text-rebuild", daemon=True)
            self._thread.start()

    def _handshake(self, result):
        """The served instructions in a handshake reply (this session's options were built before
        the request reached us); none with GRP_SERVE_INSTRUCTIONS off."""
        instr = self._instr if _serving() else None
        if result is None or (not instr and _serving()):
            return result
        if isinstance(result, dict):
            out = {k: v for k, v in result.items() if k != "instructions"}
            return {**out, "instructions": instr} if instr else out
        try:
            return result.model_copy(update={"instructions": instr}) \
                if hasattr(result, "instructions") else result
        except Exception:
            return result

    def _for_sessions(self) -> None:
        """The served instructions (none with GRP_SERVE_INSTRUCTIONS off) for sessions created
        from now on."""
        if self._instr or not _serving():
            mcp.instructions = self._instr if _serving() else None

    async def on_initialize(self, context, call_next):
        self._fresh()
        self._for_sessions()
        return self._handshake(await call_next(context))

    async def on_discover(self, context, call_next):
        """The modern handshake (server/discover) carries the same served text."""
        self._fresh()
        self._for_sessions()
        return self._handshake(await call_next(context))

    async def on_call_tool(self, context, call_next):
        """A feed another worker landed or retired is this worker's too before a tool reads it."""
        _sync_feeds()
        return await call_next(context)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        self._fresh()
        descs, params = self._descs, self._params
        out = []
        for t in tools:
            name = getattr(t, "name", None)
            d = descs.get(name)
            upd = {"description": d} if d and d != t.description else {}
            schema = _with_params(getattr(t, "parameters", None), params.get(name))
            if schema is not getattr(t, "parameters", None):
                upd["parameters"] = schema
            out.append(t.model_copy(update=upd) if upd else t)
        return out


class _ErrorBudget(Middleware):
    """An error a tool raises is measured against budget.ERROR_CHARS (a client drops the
    middle of a longer one) and logged when over; then raised unchanged."""

    async def on_call_tool(self, context, call_next):
        try:
            return await call_next(context)
        except Exception as exc:
            name = getattr(getattr(context, "message", None), "name", "?")
            budget.check(f"tool error {name}", str(exc), budget.ERROR_CHARS)
            raise


def _braced(text: str) -> str:
    """An argument as it goes into a next-call template ({next} is the only placeholder)."""
    return text.replace("{", "{{").replace("}", "}}")


# async because the surface accessors are coroutines: there is no sync way to ask
# the server what it is serving, and asking IT rather than a list is the whole point.
async def _capabilities() -> dict:
    return registry.capabilities(
        available_tools=[t.name for t in await mcp.list_tools()],
        available_prompts=[p.name for p in await mcp.list_prompts()],
        available_resources=[str(r.uri) for r in await mcp.list_resources()])


# The rules a tool's description carries in short (instructions.SHORT), where they apply: tools
# whose results feed an answer, and tools that build or show a page.
_ANSWER = ("loop", "no_arithmetic", "platform_sources")
_PAGE = ("hard_limit",)


@mcp.tool(description=_describe("start_here", start.describe, _ANSWER, start.describe_params))
def start_here(question: str, place: str | None = None, year: int | str | None = None,
               year_to: int | str | None = None, pack: str | None = None) -> dict:
    """Route a question (start.describe is the contract a model reads)."""
    return shape.shape(start.route(question, place=place, year=year, year_to=year_to, pack=pack), "start")


@mcp.tool(description=_describe("platform_capabilities", tooltext.capabilities,
                                ("platform_sources",), tooltext.capabilities_params))
async def platform_capabilities(section: str | None = None, detail: str = "concise") -> dict:
    """The platform map (tooltext.capabilities is the contract a model reads)."""
    if detail not in shape.DETAIL:
        return shape.bad_detail(detail)
    caps = await _capabilities()
    if section:
        if section not in caps:
            return {"status": "declined", "section": section,
                    "note": "unknown section — one of: " + ", ".join(caps)}
        return {section: caps[section]}
    if detail == "full":
        return caps
    return shape.shape(shape.concise_capabilities(caps), "capabilities")


@mcp.tool(description=_describe("corpus_search", tooltext.search, _ANSWER, tooltext.search_params))
def corpus_search(query: str, k: int = 5, country: str | None = None,
                  crop: str | None = None, temporal: str | None = None,
                  doc_type: str | None = None, pack: str | None = None,
                  offset: int = 0) -> dict:
    """Search a pack's document library (tooltext.search is the contract a model reads)."""
    offset = max(0, int(offset or 0))
    named = {"query": query, "k": k, "country": country, "crop": crop, "temporal": temporal,
             "doc_type": doc_type, "pack": pack}
    again = ("corpus_search(" + ", ".join(_braced(f"{a}={v!r}") for a, v in named.items()
                                          if v is not None) + ", offset={next})")
    return shape.shape(loop.with_entry_hint(
        fetch.search(query, k=k + offset, country=country, crop=crop,
                     temporal=temporal, doc_type=doc_type, pack=pack)), "search",
        offset=offset, again=again)


@mcp.tool(description=_describe("context_get", tooltext.calendar, ("loop",),
                                tooltext.calendar_params))
def context_get(country: str, crop: str, asked_month: int | None = None,
                override: list[dict] | None = None,
                override_country: str | None = None,
                override_crop: str | None = None) -> dict:
    """The hub-default crop calendar (tooltext.calendar is the contract a model reads)."""
    return context.get(country, crop, asked_month=asked_month, override=override,
                       override_country=override_country, override_crop=override_crop)


@mcp.tool(description=_describe("assemble_pack", registry.describe_assemble, _ANSWER,
                                registry.assemble_params))
def assemble_pack(country: str | None = None, crop: str | None = None,
                  focus: str | None = None, pack: str | None = None,
                  place: str | None = None, hazard: str | None = None,
                  min_severity: int | None = None,
                  override: list[dict] | None = None,
                  override_country: str | None = None,
                  override_crop: str | None = None,
                  geometry: dict | list | None = None,
                  map_layer: str | None = None,
                  year: int | str | None = None, year_to: int | str | None = None,
                  classes: list[str] | None = None,
                  stack: str | None = None,
                  layers: list[str] | None = None,
                  vectors: list[str] | None = None,
                  within: str | None = None, detail: str = "concise") -> dict:
    """Thin carrier: registry.describe_assemble() is the contract a model reads and
    registry.assemble_params() each parameter's. detail='full' skips the reply shaping
    (mcp/shape.py) and, like any call, gathers a new pack."""
    if detail not in shape.DETAIL:
        return assemble.declined(shape.bad_detail(detail)["note"], coverage=False)
    out = assemble.assemble(geometry=geometry, map_layer=map_layer, stack=stack, country=country, crop=crop, focus=focus, pack=pack,
                            place=place, hazard=hazard, min_severity=min_severity,
                            override=override, override_country=override_country,
                            override_crop=override_crop,
                            year=year, year_to=year_to, classes=classes,
                            layers=layers, vectors=vectors, within=within)
    return out if detail == "full" else shape.shape(out, "pack")


@mcp.tool(description=_describe("verify_groundedness", tooltext.verify,
                                ("loop", "platform_sources"), tooltext.verify_params))
def verify_groundedness(draft: str, pack_id: str) -> dict:
    """Gate a draft (tooltext.verify is the contract a model reads)."""
    return verify.groundedness(draft, pack_id)


@mcp.tool(description=_describe("resolve_place_time", tooltext.places, (),
                                tooltext.places_params))
def resolve_place_time(country: str | None = None, crop: str | None = None,
                       region: str | None = None, when: str | None = None,
                       place: str | None = None, list: str | None = None,
                       detail: str = "concise") -> dict:
    """Place and season resolution (tooltext.places is the contract a model reads)."""
    import sys
    if detail not in shape.DETAIL:
        return shape.bad_detail(detail)
    out = resolve.place_time(country=country, crop=crop, region=region,
                             when=when, place=place, list=list,
                             names_cap=sys.maxsize if detail == "full" else None)
    if detail == "full":
        return shape.complete_places(out)
    if isinstance(out, dict) and isinstance(out.get("layers"), builtins.list):
        return shape.shape(shape.concise_places(out), "places")
    return shape.shape(out)


@mcp.tool(description=_describe("compose_run", tooltext.compose, (), tooltext.compose_params))
def compose_run(composition: str = "foodsecurity.brief", question: str = "",
                override: list[dict] | None = None,
                override_country: str | None = None,
                override_crop: str | None = None,
                provider: str | None = None, model: str | None = None) -> dict:
    """A composition run server-side (tooltext.compose is the contract a model reads)."""
    return shape.shape(compose.run(composition=composition, question=question, override=override,
                                   override_country=override_country,
                                   override_crop=override_crop, provider=provider, model=model),
                       "pack")


# `meta.ui.resourceUri` is how MCP Apps links a result to a renderable surface:
# a host that advertises io.modelcontextprotocol/ui fetches ui://servirplatform/evidence and
# renders it in a sandboxed iframe beside the text. Hosts that do not understand
# _meta ignore it, so the text answer is unchanged for everyone else.
@mcp.tool(description=_describe("record_receipt", tooltext.receipt, _PAGE, tooltext.receipt_params),
          meta={"ui": {"resourceUri": app_ui.UI_URI}})
def record_receipt(pack_id: str | None = None, report_id: str | None = None,
                   receipt_id: str | None = None,
                   question: str | None = None, detail: str = "concise") -> dict[str, Any]:
    """Mint or resolve a replayable receipt (the description above is the contract)."""
    if detail not in shape.DETAIL:
        return shape.bad_detail(detail)
    out = record.record(pack_id=pack_id, report_id=report_id, receipt_id=receipt_id,
                        question=question, with_insight=detail == "full")
    return out if detail == "full" else shape.shape(out, "receipt")


# The MCP App rides on this tool as well as record_receipt: this is where a
# run-time answer actually ENDS, so it is where a host that can render should get
# the evidence view without the model having to ask for it.
@mcp.tool(description=_describe("publish_answer", publish.describe,
                                ("platform_sources", "hard_limit"), publish.describe_params),
          meta={"ui": {"resourceUri": app_ui.UI_URI}})
def publish_answer(pack_id: str, draft: str,
                   question: str | None = None, detail: str = "concise") -> dict[str, Any]:
    if detail not in shape.DETAIL:
        return shape.bad_detail(detail)
    out = publish.answer(pack_id=pack_id, draft=draft, question=question)
    return out if detail == "full" else shape.shape(out, "receipt")


@mcp.tool(description=_describe("feeds_query", feeds.describe, _ANSWER, feeds.describe_params))
def feeds_query(dataset: str, params: dict | None = None) -> dict:
    return shape.shape(loop.with_entry_hint(feeds.query(dataset=dataset, params=params)), "feed")


@mcp.tool(description=_describe("ui_design", ui.describe_design, _PAGE, ui.describe_design_params))
def ui_design(format: str = "all") -> dict:
    return ui.design(fmt=format)


@mcp.tool(description=_describe("ui_catalog", ui.describe_catalog, _PAGE))
def ui_catalog() -> dict:
    return ui.catalog()


@mcp.tool(description=_describe("ui_component", ui.describe_component, _PAGE,
                                ui.describe_component_params))
def ui_component(name: str, treatment: str | None = None,
                 layout: str | None = None) -> dict:
    return ui.component(name, treatment=treatment, layout=layout)


@mcp.tool(description=_describe("ui_embed", ui.describe_embed, _PAGE, ui.describe_embed_params))
def ui_embed(component: str, receipt_id: str | None = None, place: str | None = None) -> dict:
    return ui.embed(component, receipt_id=receipt_id, place=place)


# --- the contribute bone: staged, previewable, reviewed -----------------------
from ..contrib import staging as _staging  # noqa: E402  (after mcp exists)


@mcp.tool(description=_describe("contribute_submit", _staging.describe_submit,
                                ("platform_sources",), _staging.describe_params))
def contribute_submit(kind: str = "", manifest: dict | str | None = None) -> dict:
    from ..contrib import jobs
    return jobs.submit(kind, manifest)


@mcp.tool(description=_describe("risk_weights", weights_ui.describe, (), weights_ui.describe_params),
          meta={"ui": {"resourceUri": weights_ui.UI_URI}})
def risk_weights(hazard: str | None = None) -> dict:
    return {"status": "ok", "weights_picker": weights_ui.payload(hazard)}


@mcp.tool(description=_describe("contribute_status", tooltext.status, (), tooltext.status_params))
def contribute_status(contribution_id: str | None = None, action: str = "show",
                      offset: int = 0) -> dict:
    """Your contributions (tooltext.status is the contract a model reads)."""
    if action == "withdraw":
        if not contribution_id:
            return {"status": "declined", "note": "withdraw needs a contribution_id"}
        return _staging.withdraw(contribution_id)
    if action == "audit":
        return _staging.reconcile()
    if action != "show":
        return {"status": "declined",
                "note": f"unknown action {action!r} — show, withdraw or audit"}
    return _staging.status(contribution_id, offset=offset)


@mcp.tool(description=_describe("contribute_review", tooltext.review, (), tooltext.review_params))
def contribute_review(action: str = "list", contribution_id: str | None = None,
                      note: str | None = None, status_filter: str = "pending",
                      offset: int = 0) -> dict:
    """The review queue (tooltext.review is the contract a model reads)."""
    if action == "list":
        return shape.shape(_staging.review_list(status_filter=status_filter), "review",
                           offset=offset, again=("contribute_review(action='list', status_filter="
                                                 + _braced(repr(status_filter)) + ", offset={next})"))
    if action not in ("approve", "reject", "remove"):
        return {"status": "declined",
                "note": f"unknown action {action!r} — list, approve, reject or remove"}
    if not contribution_id:
        return {"status": "declined", "note": f"{action} needs a contribution_id"}
    if action == "approve":
        return _staging.approve(contribution_id, note=note)
    return _staging.reject(contribution_id, note or "")


@mcp.tool(description=_describe("corpus_document", tooltext.document,
                                ("loop", "platform_sources"), tooltext.document_params))
def corpus_document(doc_id: str | None = None, pack: str | None = None,
                    offset: int = 0) -> dict:
    """A passage's source document (tooltext.document is the contract a model reads)."""
    args = [_braced(a) for a in
            ([f"doc_id={doc_id!r}"] if doc_id else []) + ([f"pack={pack!r}"] if pack else [])]
    return shape.shape(fetch.document(doc_id, pack=pack), "document", offset=offset,
                       again="corpus_document(" + ", ".join(args + ["offset={next}"]) + ")")


def _register_pack_manifests() -> None:
    """One servirplatform://pack/<id> resource per PACKS row — a new pack's
    manifest appears with no server edit (the count discipline, applied to
    resources)."""
    from . import packs as _packs
    for pid in _packs.available():
        def _mk(pid: str = pid):
            def manifest() -> dict:
                return registry.pack_manifest(pid)
            manifest.__name__ = "pack_" + pid.replace("-", "_")
            manifest.__doc__ = (f"The {pid} domain pack manifest: what ships, what "
                                "it produces, and every declared gap.")
            return manifest
        mcp.resource(f"servirplatform://pack/{pid}",
                     mime_type="application/json")(_mk())


_register_pack_manifests()


def _register_skills() -> None:
    """servirplatform://skill/<name> — the build disciplines a consumer's agent
    reads before writing code. Registered even when the files are absent so the
    resource DECLINES with a reason instead of vanishing (rule 2, for guidance)."""
    from . import skills as _skills
    for name in _skills.SERVED:
        def _mk(name: str = name):
            def skill() -> str:
                return _skills.bundle(name)
            skill.__name__ = "skill_" + name.replace("-", "_")
            skill.__doc__ = f"The {name} skill, bundled whole (SKILL.md + references + assets)."
            return skill
        mcp.resource(f"servirplatform://skill/{name}",
                     mime_type="text/markdown")(_mk())


_register_skills()


# MCP Apps (Jan 2026): a host advertising `io.modelcontextprotocol/ui` renders this
# in a sandboxed iframe beside the tool result. Claude Desktop advertises exactly
# that in its initialize handshake, so this does NOT depend on the model deciding to
# draw something — which is what went wrong when we only asked in the instructions.
@mcp.resource(weights_ui.UI_URI, mime_type=weights_ui.UI_MIME)
def weights_picker_template() -> str:
    """The weights picker's HTML, rendered by a host that supports MCP Apps."""
    return weights_ui.html()


@mcp.resource(app_ui.UI_URI, mime_type=app_ui.UI_MIME)
def evidence_app() -> str:
    """Interactive evidence view: sources with age and provenance, and what is
    MISSING. Deliberately shows no verdict — a rendered surface freezes what is in
    it, so the verdict stays a live link to the resolver."""
    return app_ui.template()


@mcp.resource("servirplatform://capabilities/{section}", mime_type="application/json")
async def capabilities_section(section: str) -> dict:
    """One section of platform_capabilities, whole — feeds (briefs, params, declared gaps),
    methods, domain_packs, bones, ... An unknown section declines with the list."""
    caps = await _capabilities()
    if section not in caps:
        return {"status": "declined", "section": section,
                "note": "unknown section — one of: " + ", ".join(caps)}
    return {section: caps[section]}


# The resources a shaped reply spills to (mcp/shape.py): one section of a stored pack or
# receipt, whole, and the full place list — served from the store, never re-gathered.
@mcp.resource("servirplatform://pack/{pack_id}/{section}", mime_type="application/json")
def pack_section(pack_id: str, section: str) -> dict:
    """One section of a stored evidence pack, whole — citations, layers, vectors, methods,
    stats, trace, queries, gaps, exec: what assemble_pack returns, never the map payload
    it keeps out of a reply (the hazard_map embed draws that). Unknown ones decline."""
    pack = store.load_pack(pack_id)
    if pack is None:
        return {"status": "declined", "note": f"no pack with id {pack_id!r}"}
    served = [k for k in pack if k not in assemble.NOT_RETURNED]
    if section not in served:
        return {"status": "declined", "section": section,
                "note": "unknown section — one of: " + ", ".join(served)
                        + ("; the map is drawn by ui_embed(component='hazard_map', receipt_id=...)"
                           if "viz" in pack else "")}
    return {section: pack[section]}


@mcp.resource("servirplatform://receipt/{receipt_id}/{section}", mime_type="application/json")
def receipt_section(receipt_id: str, section: str) -> dict:
    """One section of a receipt, whole — sources, evidence_freshness, gaps, claim_scope,
    render_with, or a part of the published insight (map, layers, series, ...)."""
    return record.section(receipt_id, section)


@mcp.resource("servirplatform://places", mime_type="application/json")
def places() -> dict:
    """Every contributed boundary name with its km² and size class — the full list that
    resolve_place_time(list='all') groups and abridges."""
    import sys
    from ..graph.geo import ingest
    return ingest.list_boundaries(names_cap=sys.maxsize)


async def _served_params() -> list[tuple[str, dict]]:
    """[(tool, {parameter: description})] as tools/list serves them."""
    return [(t.name, {k: (v or {}).get("description") for k, v in
                      ((t.parameters or {}).get("properties") or {}).items()})
            for t in await mcp.list_tools()]


@mcp.resource("servirplatform://how-to-use", mime_type="text/markdown")
async def how_to_use() -> str:
    """How to use the Global Risk Platform (human-readable) — attach/open in the host. Each
    tool's parameters are a page of their own (tool_parameters), named here."""
    guide = instructions.how_to_use([(p.name, p.description) for p in await mcp.list_prompts()],
                                    await _served_params())
    budget.check("the how-to-use guide", guide, budget.REPLY_CHARS)
    return guide


@mcp.resource(instructions.PARAMETERS, mime_type="text/markdown")
async def tool_parameters(tool: str) -> str:
    """One tool's parameters, whole, as its input schema serves them (how to call it)."""
    tools = dict(await _served_params())
    if tool not in tools:
        return f"No tool named {tool!r}. Tools: {', '.join(sorted(tools))}."
    page = instructions.tool_params(tool, tools[tool]) or f"`{tool}` takes no parameters."
    budget.check(f"the {tool} parameters page", page, budget.REPLY_CHARS)
    return page


@mcp.prompt()
def build_a_tool(goal: str = "a bulletin generator for one of the platform's domains") -> str:
    """BUILD-TIME: scaffold a reusable app/tool on the servirplatform platform (not a one-off run)."""
    return (f"Build a REUSABLE tool that: {goal}.\n\n"
            "Use the servirplatform MCP server. Produce a standalone artifact (a script/app) written "
            "as SOURCE FILES IN MY PROJECT that I can run, version and deploy — not a "
            "hosted preview. Do NOT just run the pipeline once in chat. Follow the "
            "canonical loop: call platform_capabilities first (scope honestly, surface "
            "declared gaps), assemble_pack with the pack's target (" + _target_shapes(", ")
            + "), have the tool's own LLM draft "
            "a brief from the pack citing [n] in the pack's required_sections, then "
            "publish_answer(pack_id, draft, question), which gates the draft and mints the "
            "receipt: the tool shows a brief ONLY once it passes, a blocked one never. "
            "INSTRUMENT IT: read servirplatform://skill/trace-emit first and make every "
            "response the tool produces carry an execution trace (steps, timings, "
            "upstream calls, LLM usage); surface the trace per "
            "servirplatform://skill/trace-visualize. Show me the finished artifact and "
            "prove it runs — including one real trace.\n\n"
            "IF THE TOOL NEEDS A CALCULATION the platform does not provide (an amount per "
            "area, a score, a ranking, an index, an equation), ASK ME BEFORE YOU BUILD: do "
            "NOT write it into the code unasked, and do NOT leave out the figure I asked for "
            "to avoid calculating it. Suggest one or two concrete calculations for me to accept or reject "
            "(do not make me write one), or take mine. Then ask me whether to add it to the "
            "platform as a method "
            "(contribute_submit kind=method, following servirplatform://skill/contribute-logic) "
            "so the tool reads the platform's computed result. If a contributed method already "
            "covers it, read that method's result from a pack; never copy its steps into the "
            "code. If I approve it but want no "
            "method, the tool may compute it, labelled on screen as its own calculation, "
            "not a platform figure.\n\n"
            "WHAT YOU BUILD RESTS ON PLATFORM SOURCES ONLY: bake no web data into a built page "
            "or app, not even in a labelled section (no scraped figures, no web links, no web "
            "search in its own LLM). If a source it needs is not served (check "
            "platform_capabilities(section='coverage') first), STOP and ask me for its link to "
            "contribute (contribute_submit; kind alone returns the form); never search the web "
            "for one yourself.\n\n"
            "BEFORE you tell me it is ready, check every file you wrote and fix what "
            "fails: every figure it shows comes from a platform response it fetches when "
            "it runs (a receipt, a pack or a feed), never typed into the code or computed "
            "by the app itself; every publisher it names is one a receipt cites; every link "
            "it shows is one a pack or receipt carries; every "
            "verdict comes from ui_embed; and it calls the platform at all. Six builds in "
            "a row failed those checks "
            "and every one of them looked finished: invented numbers under invented "
            "publishers, and in one case exposure figures generated by Math.random() "
            "that rerolled on each render. A build nobody gated is a build nobody "
            "checked.\n\n" + instructions.home_text("prompt:build_a_tool"))


@mcp.prompt()
def run_analysis(country: str = "Kenya", crop: str = "maize") -> str:
    """RUN-TIME: answer one food-security question now via the canonical loop."""
    return (f"Using the servirplatform MCP server, produce a grounded brief for {crop} in {country} "
            f"now, through the canonical loop: assemble_pack(country='{country}', crop='{crop}'); "
            "draft the brief from the pack, citing [n] under its required_sections; then "
            "publish_answer(pack_id, draft, question), which gates the draft and mints the "
            "receipt. Show me the brief only once it passes, with its receipt_id and "
            "public_resolver; blocked, fix the failures and publish again; declined, give me "
            "the reason. Do not fabricate beyond the pack.\n\n"
            "RENDER IT, do not just print prose. Give me an artifact with the brief and "
            "its numbered sources side by side, each source showing who published it, "
            "when, and how it was validated; any index or series as a small chart; and "
            "the declared gaps shown as their own panel rather than a footnote — what "
            "the pack could NOT find is part of the answer.\n"
            "Show the groundedness result as UNVERIFIED with a link to the receipt "
            "resolver, or embed it with ui_embed so it re-resolves live. Do not paint a "
            "verdict into the page.\n\n" + instructions.home_text("prompt:run_analysis"))


@mcp.prompt()
def contribute_logic(logic: str = "") -> str:
    """Contribute a calculation (an equation, a threshold rule, an index) as a platform
    METHOD that every answer of its pack computes and cites."""
    from . import skills as _skills
    ask = (f"The person's logic, in their words: {logic}\n\n" if logic.strip() else "")
    return (ask + "Follow this skill step by step. Ask the person for everything it says "
            "is theirs; never invent it.\n\n" + _skills.bundle("contribute-logic"))


@mcp.prompt()
def explain_platform() -> str:
    """What the servirplatform platform is and how to use it (build-time vs run-time)."""
    return ("Call platform_capabilities, and platform_capabilities(section='domain_packs') for "
            "each pack's declared gaps in full, then explain in plain terms what the servirplatform "
            "platform is, what it can and can't do right now (its declared gaps), and "
            "the two ways to use it: BUILD-TIME (build a reusable tool on its bones) vs "
            "RUN-TIME (answer one question now).")


def main() -> None:
    """Default stdio (local dev). `--http` serves streamable HTTP on
    GRP_MCP_HOST:GRP_MCP_PORT/mcp — the faithful remote build surface."""
    import sys
    transport = "streamable-http" if "--http" in sys.argv else "stdio"
    os.environ["GRP_MCP_TRANSPORT"] = transport  # so capabilities reports it honestly
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    # The transport is named "http" here; the env var keeps the protocol's own name.
    mcp.run(transport="http",
            host=os.environ.get("GRP_MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("GRP_MCP_PORT", "8000")),
            **_http_transport())


# The connect-time text import built is what the first request is served; it refreshes
# off the request path from then on (_LiveText).
LIVE = _LiveText()
LIVE.seed(INSTRUCTIONS, _BUILT, (time.perf_counter() - _STARTED) * 1000, params=_PARAMS_BUILT)
mcp.add_middleware(LIVE)
mcp.add_middleware(_ErrorBudget())


if __name__ == "__main__":
    main()
