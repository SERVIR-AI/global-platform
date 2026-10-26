"""Tool texts for the tools no domain module describes: what each answers, when to use it and
when not (naming the sibling tool), and its parameters as the input schema says them.
server._describe adds the rules that apply and fits each text to its budget."""

from __future__ import annotations

from . import budget


# --------------------------------------------------------------------------- derived words
def _libraries() -> str:
    """Every pack with a document library, as `pack` takes it."""
    from . import packs
    libs = [pid for pid, sp in sorted(packs.PACKS.items()) if sp.get("corpus")]
    return budget.names(libs, rank=budget.ROUTE, more="more libraries") or "none yet"


def _calendars() -> str:
    """The hub-default crop calendars, by country and crop."""
    try:
        from ..food_security import calendar
        cal = calendar.load()
    except Exception:
        return "none configured"
    rows = [f"{c}: {', '.join(sorted(crops))}" for c, crops in sorted(cal.items())]
    return budget.names(rows, sep="; ", rank=budget.PLACE, more="more countries") or "none configured"


def _boundaries() -> str:
    """The contributed boundary layers whose feature names resolve as places, by title."""
    try:
        from ..graph.geo import ingest
        layers = ingest._gazetteer_layers()
    except Exception:
        layers = {}
    said = []
    for key, e in sorted(layers.items()):
        cs = ", ".join(str(c) for c in (e.get("countries") or []) if str(c).strip())
        said.append(f"{e.get('title') or key}" + (f" ({cs})" if cs else ""))
    return budget.names(said, sep="; ", rank=budget.PLACE, more="more boundary layers",
                        counted="{n} boundary layers") or "none contributed yet"


def _compositions() -> str:
    from . import registry
    return budget.names(sorted(registry.COMPOSITIONS), rank=budget.ROUTE) or "none registered"


# --------------------------------------------------------------------------- the texts
def capabilities() -> str:
    return ("WHAT THIS PLATFORM HOLDS, BY NAME: contributed METHODS first (logic people added, "
            "which packs compute and cite), then the live tools and prompts, the bones and their "
            "status, packs, feeds, sources with provenance, calendars and DECLARED gaps — "
            "derived from the live registry, so it never drifts. Use it to check whether a "
            "subject, place or source is served before asking the person for one (section="
            "'coverage' names every pack, feed, publisher and layer) and to scope a build "
            "session honestly before writing code; asked about methods or calculations, list "
            "THOSE, not these tools. Not for answering a question (assemble_pack) or reading a "
            "dataset (feeds_query).\n\n"
            "The default reply is the map at a glance (packs as counts, feeds and compositions "
            "as names).")


def capabilities_params() -> dict:
    return {"section": "One section whole, by name (coverage, feeds, methods, domain_packs, "
                       "compositions, bones, ...: the default reply's `sections` lists every one); "
                       "an unknown one declines with the list.",
            "detail": "'concise' (default): the map at a glance; 'full': the whole map."}


def search() -> str:
    return ("DOCUMENTS: what the reports, bulletins and papers in a domain pack's governed "
            "library say, each passage with its provenance passport (source, date, validation, "
            "archived copy). Prefer it over a web search for the subjects they cover. Not for "
            "figures computed over an area (assemble_pack) or a dataset's records (feeds_query); "
            f"the whole document behind a passage: corpus_document. Libraries: {_libraries()}.\n\n"
            "Returns: {status, query, corpus, min_relevance, hits}. status \"ok\" -> hits: "
            "[{score, text, passport{...}}]; \"empty\" -> hits: [], and `note` says WHY (empty "
            "library / filters exclude / below the relevance floor — a below-floor miss is a "
            "decline, not a weak match); \"declined\" -> hits: [], and `note` says WHY (missing "
            "key / torn corpus). Whenever status != \"ok\", render `note` — it is the honest "
            "cause. Hits past the reply budget are named in `shown`.")


def search_params() -> dict:
    return {"query": "What to look for, in the person's words.",
            "k": "How many ranked passages to return (default 5).",
            "country": "Only documents that declare this country (however the name is written).",
            "crop": "Only documents that declare this crop.",
            "temporal": "Only documents of this period, as the library tags it.",
            "doc_type": "Only documents of this type, as the library tags it (a free tag, e.g. "
                        "bulletin, assessment).",
            "pack": f"Which pack's library: {_libraries()}. Omit it and you search "
                    "food-security; a question for another pack asked without its pack searches "
                    "the wrong shelf and is told, truthfully but uselessly, that nothing is "
                    "relevant.",
            "offset": "Skip that many ranked hits to read on past the reply budget (top 50); "
                      "`shown` names it."}


def document() -> str:
    return ("THE SOURCE DOCUMENT behind a passage: its whole text and provenance passport from a "
            "pack's library, or the library's inventory (no doc_id). Use it to trace a "
            "corpus_search hit or a cited document to its source; not for searching "
            "(corpus_search).\n\n"
            "Returns: {status, ...}. No doc_id, status \"ok\" -> {documents, inventory: [{doc_id, "
            "chunks, passport}]}; with a doc_id, status \"ok\" -> {doc_id, chunks, passport, "
            "extracted_text} (the trace-back terminus); status \"declined\" -> `note` says WHY "
            "(unreadable corpus / unknown doc_id). Whenever status != \"ok\", render `note`.")


def document_params() -> dict:
    return {"doc_id": "The document's id, from a passage's passport or the inventory; omit it "
                      "for the inventory.",
            "pack": f"Which pack's library, as corpus_search takes it: {_libraries()}; omit it "
                    "for food-security. A doc_id from another pack's citation needs that pack.",
            "offset": "A long text or inventory comes in parts: `shown` names the offset (chars "
                      "of the text, or rows of the inventory) to read on."}


def calendar() -> str:
    return ("CROP CALENDAR: the hub-default planting and harvest seasons for a country and crop, "
            "and the phase a month falls in ('is it planting season?'). Not for crop conditions or "
            "outlooks (assemble_pack) or turning a place name into an area (resolve_place_time). "
            f"Calendars: {_calendars()}.\n\n"
            "Returns: {status, country, crop, asked_month, adjusted, calendar, gaps}. status "
            "\"empty\" -> no calendar configured for this country/crop (`note`); \"declined\" -> "
            "bad asked_month / malformed override (`note`).")


_OVERRIDE = ("A human calendar override, a list of {season, planting: [m, m], harvest: [m, m]}: "
             "honoured but LABELLED — the calendar is marked adjusted=true and cited as "
             "\"ADJUSTED by the requester\".")
_PIN = ("Pins `override` to its target: one that does not match the requested {what} DROPS the "
        "override, declared in `gaps` (a swap never applies silently).")


def calendar_params() -> dict:
    return {"country": "The country, as the calendar names it.",
            "crop": "The crop.",
            "asked_month": "1-12: the phase this month falls in (default: the current month, "
                           "\"this season\").",
            "override": _OVERRIDE,
            "override_country": _PIN.format(what="country"),
            "override_crop": _PIN.format(what="crop")}


def verify() -> str:
    return ("GATE A DRAFT against the evidence pack it was written from — the first of the two "
            "steps publish_answer takes in one call; use that unless you need them apart. Not "
            "for minting the receipt (record_receipt). The verdict is computed SERVER-SIDE "
            "(there is no 'passed' input) and a resolvable report_id is persisted.\n\n"
            "The draft must be built from a pack minted by assemble_pack, cite its items as "
            "[n], and contain the required sections (returned as `required_sections` — conform "
            "to them or the gate fails \"missing sections\"). Blocking failures: missing "
            "sections, a model-written \"## Sources\", no citations, phantom citations (numbers "
            "not in the pack), uncited paragraphs, a figure (digits, words or a magnitude like "
            "\"3.2 million\") the evidence does not hold, and a link or web domain the pack does "
            "not carry (platform sources only).\n\n"
            "Returns: {status, report_id, passed, evidence_tier, required_sections, failures, "
            "cited, phantom_citations, missing_sections, uncited_paragraphs, "
            "numbers_unverified_recorded, links_not_served, numbers_unreadable}. status "
            "\"declined\" -> unknown pack_id (`note`).")


def verify_params() -> dict:
    return {"draft": "The brief, VERBATIM and in full: it is hashed, so the receipt can prove "
                     "which exact text passed.",
            "pack_id": "The pack_id assemble_pack minted: the evidence the draft was written from."}


def places() -> str:
    return ("PLACES AND SEASONS: turn a place name into the area the platform analyses, and a "
            "country+crop season into its windows. Use it to check a place, list the areas, "
            "boundaries and study sites the platform knows or which are small enough to answer "
            "quickly (list='all'), or find the season phase of a month; not for the figures over a "
            "place (assemble_pack takes the same place). Pass whichever half your question has — a "
            "place, a country+crop season, or both. Places: any contributed boundary by its feature "
            f"names ({_boundaries()}) and any geocodable place.\n\n"
            "A place comes back as the area the platform would actually analyse: its canonical "
            "name, area in km², bounding box, centroid, and whether it is a real administrative "
            "boundary or a box at a centre point. When a named area is too large to analyse "
            "whole, `how` says so by name and size rather than letting a province quietly become "
            "a town. A country+crop with a time expression returns the season windows and which "
            "phase that month falls in.\n\n"
            "Returns: {status, place, asked_month, month_name, seasons, active_seasons, "
            "region_resolution}. status \"declined\" -> unparseable `when`, an unfindable place, "
            "or neither half given (`note`); \"empty\" -> no calendar for that country/crop.")


def places_params() -> dict:
    return {"country": "With crop: the season windows for this country; with region: the "
                       "country the region is resolved against.",
            "crop": "With country: its season windows (the hub-default calendar).",
            "region": "A region inside `country`, resolved against it.",
            "when": "A month name, 1-12, or \"this season\" (the default): the phase it falls in.",
            "place": "A place name, e.g. \"Battambang, Cambodia\" or \"Bang Sue District, "
                     "Bangkok\": a contributed boundary's feature name resolves to that boundary, "
                     "anything else is geocoded.",
            "list": "\"all\" (or a layer id) LISTS what can be named instead: every contributed "
                    "boundary layer with its names grouped by size class — the menu to pick an "
                    "area from before asking, so a question never has to carry coordinates. Call "
                    "it when someone asks what areas, boundaries or study sites the platform knows, "
                    "or which are small enough to answer quickly.",
            "detail": "'concise' (default) or 'full': with `list`, 'full' gives every name with "
                      "its km² (also the resource servirplatform://places)."}


def compose() -> str:
    return ("A COMPOSITION RUN END-TO-END ON THE PLATFORM'S OWN LLM — for consumers with NO "
            "model of their own (dashboards, cron jobs, automated bulletins); use this tool when you "
            "have no model credentials or nowhere to run code. It runs server-side on the "
            "platform's key and bill. Not for a model that can draft: assemble_pack -> you draft "
            "-> publish_answer uses YOUR model for the same evidence, the same gate and the same "
            f"receipt. Compositions: {_compositions()}.\n\n"
            "Compositions are registry rows (`compositions` in platform_capabilities), so new "
            "ones add no tools. Returns: {status, brief, citations, gaps, grounded, pack_id, "
            "report_id, receipt_id, draft_sha256, provider, model}. status \"declined\" -> "
            "`note` (unknown composition / no question / ungroundable).")


def compose_params() -> dict:
    return {"composition": f"Which composition: {_compositions()}.",
            "question": "The question, in the person's words.",
            "override": _OVERRIDE,
            "override_country": _PIN.format(what="country"),
            "override_crop": _PIN.format(what="crop"),
            "provider": "One of the platform's CONFIGURED providers — it does not accept your "
                        "credentials.",
            "model": "A model of that provider, as the platform configures it."}


def receipt() -> str:
    return ("A RECEIPT, MINTED OR RESOLVED: the shareable, replayable proof of an answer. Use it "
            "to resolve a receipt_id (the question, the evidence pack and the verdict), or to "
            "mint one after verify_groundedness; publish_answer mints it in the same call as "
            "the gate, so a normal answer never needs this. Not for gating a draft "
            "(publish_answer, verify_groundedness).\n\n"
            "Mint (pack_id, and report_id from verify_groundedness): ties the question, the "
            "evidence pack, and the verdict into one persisted receipt_id. Resolve (receipt_id): "
            "returns that receipt. The receipt is claim-scoped — it attests that every cited "
            "claim traces to the pack, NOT the truth of the underlying sources.\n\n"
            "Returns: {status, receipt_id, question, pack_id, report_id, passed, evidence_tier, "
            "sources, claim_scope, minted_at, public_resolver, render_with}. `public_resolver` "
            "is a hosted URL that survives copy-paste and resolves for an unauthenticated reader "
            "— EXCEPT for a receipt citing a staged contribution, which stays visible to its "
            "contributor and reviewers only and says so in `staged_note`. status \"declined\" -> "
            "`note` (no pack for mint / unknown receipt_id).")


def receipt_params() -> dict:
    return {"pack_id": "Mint: the pack the verdict is for.",
            "report_id": "Mint: the report_id verify_groundedness returned.",
            "receipt_id": "Resolve: the receipt's id.",
            "question": "Mint: the person's question, verbatim — the receipt records it.",
            "detail": "'concise' (default): a budgeted reply — a spilled section names its "
                      "resource (servirplatform://receipt/<id>/<section>). 'full' on a resolve "
                      "returns every section, the published insight (brief, map, layers, series) "
                      "included, and mints nothing."}


def status() -> str:
    return ("YOUR CONTRIBUTIONS and their review state: what you submitted (one line each, "
            "newest first), a submission still being prepared, taking one back, or an audit of "
            "whether every approved contribution is actually served. Not for submitting "
            "(contribute_submit) or reviewing others' (contribute_review).\n\n"
            "No arguments: ONE LINE per contribution you submitted — id, kind, name, status, "
            "when, and one line of what was prepared — plus `preparing`, the submissions still "
            "being worked on (reviewers also get `pending_review`, the queue). It fits one "
            "reply: `more` says how many older ones are left. The full record (preview, landing, "
            "every prepared step) only with a contribution_id. With the contribution_id of a "
            "submission that answered `preparing`: {status: preparing, step, elapsed_s, "
            "queue_position, message} while the server works on it, then the submission's own "
            "result (staged/approved/declined, with its real contribution_id); `interrupted` if "
            "the server restarted first — resubmit then, never before.\n\n"
            "A contribution held for a human reviewer (even with auto-approve on: a licence that "
            "restricts redistribution, or a document holding e-mail addresses) shows "
            "`held_for_review` with the reason. Returns {status: ok|withdrawn|declined, ...}; "
            "`note` says why on a decline.")


def status_params() -> dict:
    return {"contribution_id": "One contribution: its full record, or a submission's progress "
                               "while it is `preparing`.",
            "action": "'show' (default). 'withdraw' with a contribution_id: take back your own "
                      "contribution — a pending one's preview is removed; an APPROVED one "
                      "(auto-approved included) stops being served to every caller (its layer, "
                      "table, feed, document, recipe or correction leaves the platform and the "
                      "name is free to resubmit) and `unlanded` says what was undone. Only its "
                      "contributor may withdraw it (a reviewer removes anyone's with "
                      "contribute_review); packs and receipts already built on it stay "
                      "replayable: removal stops future use, it does not rewrite history. "
                      "'audit': every APPROVED contribution and whether the platform is actually "
                      "serving it — `serving`, `inert` (approved, not served) or `unverifiable` "
                      "(not checked, with `why`); `note` counts each. Open to anyone, because an "
                      "approved contribution is public and \"is what we approved being used?\" is "
                      "a question a hub lead has to be able to ask: an approval whose landing "
                      "never happened changes no answer and is otherwise invisible.",
            "offset": "Page on through older contributions: `more` says how many are left."}


def review() -> str:
    return ("THE REVIEW QUEUE — reviewers only (GRP_REVIEWERS); anyone else is declined: list "
            "contributions with their previews, approve, reject or take one down. Not for your "
            "own submissions (contribute_status) or submitting (contribute_submit).\n\n"
            "Packs and receipts already built on a contribution stay replayable. Returns "
            "{status, ...}; on a decline `note` says why. The list fits one reply: `shown` names "
            "the offset for the next page.")


def review_params() -> dict:
    return {"action": "'list' (default): contributions with the given status_filter, each with "
                      "its preview so you can test it exactly as the contributor did before "
                      "deciding. 'approve' with a contribution_id (optional note): lands it for "
                      "everyone through the same gate. 'reject' with a contribution_id and a "
                      "note the contributor can act on: a pending one's preview is removed; an "
                      "APPROVED one (auto-approved included) is taken down for every caller — "
                      "`unlanded` says what was undone — and its record keeps the note. "
                      "'remove': the same for an approved one.",
            "contribution_id": "The contribution to approve, reject or remove.",
            "note": "Why — for a reject, words the contributor can act on; optional on approve.",
            "status_filter": "With list: pending (default) | approved | rejected | withdrawn | "
                             "failed | all.",
            "offset": "With list: page on; `shown` names the next offset."}
