"""FastMCP server — the bones as MCP tools. Run over stdio:
    uv run python -m app.mcp.server
Tools are thin carriers over the domain packs' functions; logic lives in the
called modules, not here (the tool-vs-prompt litmus, ARCHITECTURE §1).
"""

from __future__ import annotations

from typing import Any

import base64
import inspect
import builtins
import os

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from mcp.types import Icon

from . import (app_ui, assemble, auth, budget, compose, context, feeds, fetch, instructions,
               loop, weights_ui,
               publish, record, registry, resolve, shape, store, ui, verify)
from ..config import get_settings


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


mcp = FastMCP("servirplatform", instructions=INSTRUCTIONS, icons=_brand_icons(),
              website_url=get_settings().grp_public_url.strip() or None)

# tool name -> the function that writes its description from live state. One map feeds the
# decorator AND _LiveText, so a generated description is never frozen at import.
DESCRIBERS: dict[str, Any] = {}


def _describe(name: str, fn) -> str:
    """Register `fn` as the tool's live describer (plus every instruction section homed on
    the tool), fitted to budget.TOOL_DESCRIPTION — derived name lists counted past what fits,
    an overrun logged — and return today's text."""
    def live() -> str:
        return budget.fit(f"tool description {name}",
                          lambda: instructions.with_home(f"tool:{name}", fn()),
                          budget.TOOL_DESCRIPTION)
    DESCRIBERS[name] = live
    return live()


class _LiveText(Middleware):
    """Connect-time text that follows the platform's state. Every description built
    from live state (packs, stacks, contributed layers, feeds) is regenerated for each
    new connection rather than frozen at process start; a 30-second cache keeps a
    burst of reconnects cheap."""
    TTL = 30.0

    def __init__(self):
        self._at = 0.0
        self._instr = None
        self._descs: dict = {}

    def _fresh(self) -> None:
        import time
        if self._instr is not None and time.monotonic() - self._at < self.TTL:
            return
        descs = {}
        for name, fn in DESCRIBERS.items():
            try:
                descs[name] = fn()
            except Exception:                 # keep the registered text rather than fail a listing
                pass
        try:
            self._instr = _instructions()
        except Exception:
            self._instr = None
        self._descs = descs
        self._at = time.monotonic()

    async def on_initialize(self, context, call_next):
        self._fresh()
        if self._instr:
            mcp.instructions = self._instr          # sessions created from now on
        result = await call_next(context)
        # This session's options were built before the request reached us: put the
        # fresh text into the reply itself.
        if self._instr and result is not None and hasattr(result, "instructions"):
            try:
                result = result.model_copy(update={"instructions": self._instr})
            except Exception:
                pass
        return result

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        self._fresh()
        out = []
        for t in tools:
            d = self._descs.get(getattr(t, "name", None))
            out.append(t.model_copy(update={"description": d}) if d and d != t.description else t)
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


@mcp.tool()
async def platform_capabilities(section: str | None = None, detail: str = "concise") -> dict:
    """The platform map: contributed METHODS first (logic people added, which packs
    compute and cite), then which tools/prompts are live, the bones and their status,
    packs, sources with provenance, calendars, and DECLARED gaps. Bone status is
    derived from the live registry, so it never drifts. Call this first in a build
    session to scope honestly before writing code.

    The default reply is the map at a glance (packs as counts, feeds and compositions
    as names). `section` returns one section whole (feeds, methods, domain_packs,
    compositions, bones, ...); detail='full' returns the whole map.
    """
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


@mcp.tool()
def corpus_search(query: str, k: int = 5, country: str | None = None,
                  crop: str | None = None, temporal: str | None = None,
                  doc_type: str | None = None, pack: str | None = None,
                  offset: int = 0) -> dict:
    """Search a domain pack's authoritative document library. Prefer this over a web
    search for the subjects it covers (see `platform_capabilities` for the live
    list): every passage carries a provenance passport
    (source, date, validation, archived copy).

    `pack` picks the library — each domain pack has its own. Omit it and you search
    food-security. Ask a hazard question without it and you will search the wrong
    shelf and be told, truthfully but uselessly, that nothing is relevant.

    Returns: {status, query, corpus, min_relevance, hits}.
      status "ok"       -> hits: [{score, text, passport{...}}]
      status "empty"    -> hits: [], and `note` says WHY (empty library /
                           filters exclude / below the relevance floor — a
                           below-floor miss is a decline, not a weak match)
      status "declined" -> hits: [], and `note` says WHY (missing key / torn corpus)
    Whenever status != "ok", render `note` — it is the honest cause. Hits past the reply
    budget are named in `shown`; `offset` skips that many ranked hits to read on (top 50).
    """
    offset = max(0, int(offset or 0))
    named = {"query": query, "k": k, "country": country, "crop": crop, "temporal": temporal,
             "doc_type": doc_type, "pack": pack}
    again = ("corpus_search(" + ", ".join(_braced(f"{a}={v!r}") for a, v in named.items()
                                          if v is not None) + ", offset={next})")
    return shape.shape(loop.with_entry_hint(
        fetch.search(query, k=k + offset, country=country, crop=crop,
                     temporal=temporal, doc_type=doc_type, pack=pack)), "search",
        offset=offset, again=again)


@mcp.tool()
def context_get(country: str, crop: str, asked_month: int | None = None,
                override: list[dict] | None = None,
                override_country: str | None = None,
                override_crop: str | None = None) -> dict:
    """The hub-default crop calendar for a country/crop, and the phase the asked
    month falls in (asked_month defaults to the current month — "this season").

    A human `override` (list of {season, planting:[m,m], harvest:[m,m]}) is
    honored but LABELED: the returned calendar is marked adjusted=true and cited
    as "ADJUSTED by the requester". Pass override_country/override_crop to pin it
    to its target — if they mismatch the requested country/crop the override is
    DROPPED and the drop is declared in `gaps` (a swap never applies silently).

    Returns: {status, country, crop, asked_month, adjusted, calendar, gaps}.
      status "empty"    -> no calendar configured for this country/crop (`note`)
      status "declined" -> bad asked_month / malformed override (`note`)
    """
    return context.get(country, crop, asked_month=asked_month, override=override,
                       override_country=override_country, override_crop=override_crop)


@mcp.tool(description=_describe("assemble_pack", registry.describe_assemble))
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
    """Thin carrier: registry.describe_assemble() is the contract a model reads; every
    parameter is explained in servirplatform://how-to-use. detail='full' skips the
    reply shaping (mcp/shape.py) and, like any call, gathers a new pack."""
    if detail not in shape.DETAIL:
        return shape.bad_detail(detail)
    out = assemble.assemble(geometry=geometry, map_layer=map_layer, stack=stack, country=country, crop=crop, focus=focus, pack=pack,
                            place=place, hazard=hazard, min_severity=min_severity,
                            override=override, override_country=override_country,
                            override_crop=override_crop,
                            year=year, year_to=year_to, classes=classes,
                            layers=layers, vectors=vectors, within=within)
    return out if detail == "full" else shape.shape(out, "pack")


@mcp.tool()
def verify_groundedness(draft: str, pack_id: str) -> dict:
    """Gate a draft brief against the evidence pack it was written from. The
    verdict is computed SERVER-SIDE (you cannot pass a "passed" flag) and a
    resolvable report_id is persisted.

    The draft must be built from a pack minted by assemble_pack, cite its items
    as [n], and contain the required sections (returned as `required_sections` —
    conform to them or the gate fails "missing sections"). Blocking failures:
    missing sections, a model-written "## Sources", no citations, phantom
    citations (numbers not in the pack), uncited paragraphs. Recorded but not
    blocking: numbers in the draft absent from the evidence.

    Returns: {status, report_id, passed, evidence_tier, required_sections,
    failures, cited, phantom_citations, missing_sections, uncited_paragraphs,
    numbers_unverified_recorded}. status "declined" -> unknown pack_id (`note`).
    """
    return verify.groundedness(draft, pack_id)


@mcp.tool()
def resolve_place_time(country: str | None = None, crop: str | None = None,
                       region: str | None = None, when: str | None = None,
                       place: str | None = None, list: str | None = None,
                       detail: str = "concise") -> dict:
    """Turn human place-and-time into machine place-and-time. Pass whichever half
    your question has — a `place` ("Battambang, Cambodia", "Bang Sue District,
    Bangkok"), a `country`+`crop` season, or both.

    `list="all"` (or a layer id) LISTS what can be named instead: every contributed
    boundary layer with its names grouped by size class — the menu to pick an area
    from before asking, so a question never has to carry coordinates. Call it when
    someone asks what areas, boundaries or study sites the platform knows, or which
    are small enough to answer quickly. Every name with its km²: detail='full' or
    the resource servirplatform://places.

    A `place` (or a `region`, resolved against its country) comes back as the area
    the platform would actually analyse: its canonical name, area in km², bounding
    box, centroid, and whether it is a real administrative boundary or a box at a
    centre point. When a named area is too large to analyse whole, `how` says so by
    name and size rather than letting a province quietly become a town.

    `country`+`crop` plus a time expression (`when`: a month name, 1-12, or "this
    season" — the default) returns the season windows and which phase that month
    falls in.

    Returns: {status, place, asked_month, month_name, seasons, active_seasons,
    region_resolution}. status "declined" -> unparseable `when`, an unfindable
    place, or neither half given (`note`); "empty" -> no calendar for that
    country/crop.
    """
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


@mcp.tool()
def compose_run(composition: str = "foodsecurity.brief", question: str = "",
                override: list[dict] | None = None,
                override_country: str | None = None,
                override_crop: str | None = None,
                provider: str | None = None, model: str | None = None) -> dict:
    """Run a pre-wired COMPOSITION end-to-end — for consumers with NO LLM of their
    own (dashboards, cron jobs, automated bulletins).

    NOTE this runs the PLATFORM's LLM server-side, on the platform's key and bill.
    `provider`/`model` pick among the platform's CONFIGURED providers — they do not
    accept your credentials. If you have your own model, prefer the loop —
    assemble_pack → you draft → verify_groundedness — which uses YOUR model for the
    same evidence, the same gate and the same receipt. Use this tool when you have
    no model credentials or nowhere to run code.

    Compositions are registry rows (see `compositions` in platform_capabilities), so
    new ones add no tools. Returns: {status, brief, citations, gaps, grounded,
    pack_id, report_id, receipt_id, draft_sha256, provider, model}. status
    "declined" -> `note` (unknown composition / no question / ungroundable).
    """
    return shape.shape(compose.run(composition=composition, question=question, override=override,
                                   override_country=override_country,
                                   override_crop=override_crop, provider=provider, model=model),
                       "pack")


# `meta.ui.resourceUri` is how MCP Apps links a result to a renderable surface:
# a host that advertises io.modelcontextprotocol/ui fetches ui://servirplatform/evidence and
# renders it in a sandboxed iframe beside the text. Hosts that do not understand
# _meta ignore it, so the text answer is unchanged for everyone else.
_RECORD_RECEIPT = """Mint or resolve a replayable RECEIPT — the shareable proof of an answer.

    Mint (pass pack_id, and report_id from verify_groundedness): ties the question,
    the evidence pack, and the verdict into one persisted receipt_id. Resolve (pass
    receipt_id): returns that receipt. The receipt is claim-scoped — it attests that
    every cited claim traces to the pack, NOT the truth of the underlying sources.

    Returns: {status, receipt_id, question, pack_id, report_id, passed,
    evidence_tier, sources, claim_scope, minted_at, public_resolver, render_with}.
    `public_resolver` is a hosted URL that survives copy-paste and resolves for an
    unauthenticated reader — EXCEPT for a receipt citing a staged contribution,
    which stays visible to its contributor and reviewers only and says so in
    `staged_note`. status "declined" -> `note` (no pack for mint / unknown
    receipt_id). The reply is budgeted: a spilled section names its resource
    (servirplatform://receipt/<id>/<section>); resolving with detail='full' returns every
    section, the published insight (brief, map, layers, series) included, and mints nothing.
    """


@mcp.tool(description=_describe("record_receipt", lambda: inspect.cleandoc(_RECORD_RECEIPT)),
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
@mcp.tool(description=_describe("publish_answer", publish.describe),
          meta={"ui": {"resourceUri": app_ui.UI_URI}})
def publish_answer(pack_id: str, draft: str,
                   question: str | None = None, detail: str = "concise") -> dict[str, Any]:
    if detail not in shape.DETAIL:
        return shape.bad_detail(detail)
    out = publish.answer(pack_id=pack_id, draft=draft, question=question)
    return out if detail == "full" else shape.shape(out, "receipt")


@mcp.tool(description=_describe("feeds_query", feeds.describe))
def feeds_query(dataset: str, params: dict | None = None) -> dict:
    return shape.shape(loop.with_entry_hint(feeds.query(dataset=dataset, params=params)), "feed")


@mcp.tool(description=_describe("ui_design", ui.describe_design))
def ui_design(format: str = "all") -> dict:
    return ui.design(fmt=format)


@mcp.tool(description=_describe("ui_catalog", ui.describe_catalog))
def ui_catalog() -> dict:
    return ui.catalog()


@mcp.tool(description=_describe("ui_component", ui.describe_component))
def ui_component(name: str, treatment: str | None = None,
                 layout: str | None = None) -> dict:
    return ui.component(name, treatment=treatment, layout=layout)


@mcp.tool(description=_describe("ui_embed", ui.describe_embed))
def ui_embed(component: str, receipt_id: str | None = None, place: str | None = None) -> dict:
    return ui.embed(component, receipt_id=receipt_id, place=place)


# --- the contribute bone: staged, previewable, reviewed -----------------------
from ..contrib import staging as _staging  # noqa: E402  (after mcp exists)


@mcp.tool(description=_describe("contribute_submit", _staging.describe_submit))
def contribute_submit(kind: str = "", manifest: dict | str | None = None) -> dict:
    from ..contrib import jobs
    return jobs.submit(kind, manifest)


@mcp.tool(description=_describe("risk_weights", weights_ui.describe),
          meta={"ui": {"resourceUri": weights_ui.UI_URI}})
def risk_weights(hazard: str | None = None) -> dict:
    return {"status": "ok", "weights_picker": weights_ui.payload(hazard)}


@mcp.tool()
def contribute_status(contribution_id: str | None = None, action: str = "show",
                      offset: int = 0) -> dict:
    """Your contributions and their review state.

    No arguments: ONE LINE per contribution you submitted, newest first — id, kind,
    name, status, when, and one line of what was prepared — plus `preparing`, the
    submissions still being worked on (reviewers also get `pending_review`, the queue).
    It fits one reply: `more` says how many older ones are left, and offset=<n> pages
    on. The full record (preview, landing, every prepared step) only with a
    contribution_id. With the
    contribution_id of a submission that answered `preparing`: {status: preparing,
    step, elapsed_s, queue_position, message} while the server works on it, then the
    submission's own result (staged/approved/declined, with its real contribution_id);
    `interrupted` if the server restarted first — resubmit then, never before.
    action="withdraw" with a contribution_id: take back your own contribution. A
    pending one's preview is removed; an APPROVED one (auto-approved included) stops
    being served to every caller — its layer, table, feed, document, recipe or
    correction leaves the platform and the name is free to resubmit — and `unlanded`
    says what was undone. Only its contributor may withdraw it (a reviewer removes
    anyone's with contribute_review). Packs and receipts already built on it stay
    replayable: removal stops future use, it does not rewrite history.

    action="audit": every APPROVED contribution and whether the platform is
    actually serving it — `serving`, `inert` (approved, not served) or
    `unverifiable` (not checked, with `why`); `note` counts each. Open to anyone,
    because an approved contribution is public and "is what we approved being
    used?" is a question a hub lead has to be able to ask. An approval whose
    landing never happened changes no answer and is otherwise invisible.

    A contribution held for a human reviewer (even with auto-approve on: a licence
    that restricts redistribution, or a document holding e-mail addresses) shows
    `held_for_review` with the reason.

    Returns {status: ok|withdrawn|declined, ...}; `note` says why on a decline.
    """
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


@mcp.tool()
def contribute_review(action: str = "list", contribution_id: str | None = None,
                      note: str | None = None, status_filter: str = "pending",
                      offset: int = 0) -> dict:
    """The review queue — reviewers only (GRP_REVIEWERS); anyone else is declined.

    action="list": contributions with the given status_filter (pending | approved |
    rejected | withdrawn | failed | all), each with its preview so you can test it
    exactly as the contributor did before deciding. action="approve" with a
    contribution_id (optional note): lands it for everyone through the same gate.
    action="reject" with a contribution_id and a note the contributor can act on:
    a pending one's preview is removed; an APPROVED one (auto-approved included) is
    taken down for every caller — `unlanded` says what was undone — and its record
    keeps the note. action="remove" is the same for an approved one. Packs and
    receipts already built on it stay replayable. Returns {status, ...}; on a decline
    `note` says why. The list fits one reply: `shown` names the offset for the next page.
    """
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


@mcp.tool()
def corpus_document(doc_id: str | None = None, pack: str | None = None,
                    offset: int = 0) -> dict:
    """Trace a passage back to its source document.

    `pack` picks the library, as it does for corpus_search; omit it for
    food-security. A doc_id from a risk citation needs pack="risk".

    Returns: {status, ...}.
      No doc_id, status "ok" -> {documents, inventory: [{doc_id, chunks, passport}]}
      With a doc_id, status "ok" -> {doc_id, chunks, passport, extracted_text} (the
      trace-back terminus)
      status "declined" -> `note` says WHY (unreadable corpus / unknown doc_id)
    Whenever status != "ok", render `note`. A long text or inventory comes in parts:
    `shown` names the `offset` (chars of the text, or rows of the inventory) to read on.
    """
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


@mcp.resource("servirplatform://how-to-use", mime_type="text/markdown")
async def how_to_use() -> str:
    """How to use the Global Risk Platform (human-readable) — attach/open in the host."""
    return instructions.how_to_use([(p.name, p.description) for p in await mcp.list_prompts()])


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
            "verify_groundedness(draft, pack_id) and publish ONLY if it passes. "
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
            "BEFORE you tell me it is ready, check every file you wrote and fix what "
            "fails: every figure it shows comes from a platform response it fetches when "
            "it runs (a receipt, a pack or a feed), never typed into the code or computed "
            "by the app itself; every publisher it names is one a receipt cites; every "
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
            "now: assemble_pack, draft from the pack citing [n] in the required_sections, "
            "verify_groundedness, and give me the brief plus the report_id — or the "
            "decline reason if it can't be grounded. Do not fabricate beyond the pack.\n\n"
            "RENDER IT, do not just print prose. Give me an artifact with the brief and "
            "its numbered sources side by side, each source showing who published it, "
            "when, and how it was validated; any index or series as a small chart; and "
            "the declared gaps shown as their own panel rather than a footnote — what "
            "the pack could NOT find is part of the answer.\n"
            "Show the groundedness result as UNVERIFIED with a link to the receipt "
            "resolver, or embed it with ui_embed so it re-resolves live. Do not paint a "
            "verdict into the page.")


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


mcp.add_middleware(_LiveText())
mcp.add_middleware(_ErrorBudget())


if __name__ == "__main__":
    main()
