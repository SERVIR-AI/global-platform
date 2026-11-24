"""A6 — the synthesis engine: question -> evidence pack -> grounded, cited brief.

The LLM is called exactly twice: parse the question (tool-call extraction, no
data access) and write the brief FROM THE PACK ONLY. Everything between is
deterministic; a blocking groundedness check runs after, with one retry — a
brief whose citations don't resolve does not ship.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..config import get_settings
from ..gate import grounded
from ..llm import build_client, default_model
from ..rag.store import Corpus, source_block
from . import calendar as crop_calendar
from . import cropmonitor

CORPUS = "food-security"

_PARSE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "plan_brief",
        "description": "Extract the target of an in-scope food-security question.",
        "parameters": {
            "type": "object",
            "properties": {
                "crop": {"type": "string", "description": "crop asked about (e.g. maize); '' if none"},
                "country": {"type": "string", "description": "country/region asked about; '' if none"},
                "focus": {"type": "string", "description": "one line: what the asker wants to know"},
            },
            "required": ["focus"],
        },
    },
}]

def _parse_system() -> str:
    """The routing prompt, with the library's actual coverage in it. It used to
    freeze "El Nino impacts, food security in East/Southern Africa": a hub's own
    Cambodia rice bulletins would have been declined out of scope by the router
    before the library was read."""
    from ..mcp import registry
    try:
        c = registry.coverage()
    except Exception:
        c = {}
    crops = ", ".join(c.get("crops") or []) or "crops"
    where = ", ".join(c.get("countries") or []) or "the countries hubs contribute documents for"
    return (
        "You route questions for a food-security briefing service: crop conditions, seasonal "
        "forecasts, climate-driver (ENSO, IOD) impacts and food security. The document library "
        f"currently holds {crops} in {where}; any crop or country a hub has contributed "
        "documents for is in scope, and a question about crop conditions or food security "
        "anywhere is in scope — the evidence pack says what the library lacks. If the question "
        "is in scope, call plan_brief with its crop/country/focus. If it is not about food "
        "security or crop conditions at all, do not call the tool — reply with one sentence "
        "saying why you cannot brief on it.")

SECTIONS = ("## What history says", "## The current signal",
            "## What's missing and how to weigh it", "## Season timing caveat")

_SYNTH_SYSTEM = """You write food-security briefs for hub analysts and ministry advisors.

Non-negotiable rules:
- Use ONLY the numbered evidence provided. Every paragraph must carry at least one citation marker like [3]. Never use a citation number that is not in the evidence list.
- Never state a number, rating, or projection that does not appear in the evidence.
- Attribute every claim to its authority by name ("The GEOGLAM Crop Monitor rates...", "FEWS NET reported...", "ICPAC's outlook projects..."). Never say "we computed" or "our model predicts"; never present a projection as fact.
- Forecast/outlook statements may only cite evidence marked temporal=forecast, presented as dated projections of the issuing authority.
- The Season timing caveat MUST be based on the crop-calendar evidence entry (when present) and the asked-in month; if that entry is marked ADJUSTED, the caveat must say the calendar was adjusted by the requester. If there is NO crop-calendar entry — for example the question names no crop — still write the section, and say plainly that no crop calendar applies so timing cannot be pinned to a season. Never omit a required section.
- Evidence marked DRIVER SIGNAL ONLY (ENSO/ONI, IOD/DMI, the model plume, the IRI outlook) describes the ocean/atmosphere state. You may report its status, strength, timing and historical analogues, and you MUST attribute it. You may NOT use it on its own to claim any local rainfall, crop or food-security outcome — for a local claim, cite evidence that is itself about that place. If the only evidence for a local effect is a driver index, say the link is not established by this pack.
- Model counts in the plume are COUNTS, not probabilities. Never convert "N of M models" into a percentage or a chance.
- The "Known gaps" in the request MUST be reflected in the What's missing section.
- If the evidence cannot support an answer, write no sections; reply with one paragraph starting "DECLINE:" naming exactly what is missing.

Write EXACTLY these markdown sections and nothing else:
""" + "\n".join(SECTIONS) + """

Do not write a Sources section — the system appends it."""


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    return {"in": getattr(u, "prompt_tokens", 0) or 0,
            "out": getattr(u, "completion_tokens", 0) or 0} if u else {"in": 0, "out": 0}


def _parse(client, model, question, trace, usage):
    resp = client.chat.completions.create(
        model=model, max_tokens=300, tools=_PARSE_TOOLS,
        messages=[{"role": "system", "content": _parse_system()},
                  {"role": "user", "content": question}])
    usage.append(_usage(resp))
    msg = resp.choices[0].message
    if not msg.tool_calls:
        trace.append("parse -> out of scope (no tool call)")
        return None, msg.content or "This question is outside the food-security brief's scope."
    try:
        args = json.loads(msg.tool_calls[0].function.arguments)
        if not isinstance(args, dict):
            raise ValueError("tool arguments are not an object")
    except (ValueError, TypeError) as exc:      # truncated/malformed model output
        trace.append(f"parse -> malformed tool arguments ({exc})")
        return None, ("The question could not be parsed reliably (malformed model "
                      "response) — please try rephrasing it.")
    parsed = {"crop": (args.get("crop") or "").strip(),
              "country": (args.get("country") or "").strip(),
              "focus": (args.get("focus") or question).strip()}
    trace.append(f"parse -> {parsed}  [the model extracts the target; it fetches nothing]")
    return parsed, None


def _conditions_citation(crop, country, trace):
    """The GEOGLAM feed as one evidence entry, or an honest gap line."""
    try:
        res = cropmonitor.conditions(crop=crop or None, place=country or None)
    except cropmonitor.CropMonitorError as exc:
        trace.append(f"conditions -> unavailable: {exc}")
        return None, f"current Crop Monitor conditions unavailable ({exc})"
    if not res["records"]:
        trace.append("conditions -> no assessment rows")
        return None, res.get("note", "no current Crop Monitor assessment for the target")
    regions = "; ".join(f"{r['region'] or r['country']}: {r['conditions'] or 'out of season'}"
                        for r in res["records"][:12])
    if res["count"] > 12:                       # truncation must be visible evidence
        regions += f" (first 12 of {res['count']} rows)"
    drivers = sorted({r["drivers"] for r in res["records"] if r["drivers"]})
    text = (f"GEOGLAM Crop Monitor synthesis as of {res['as_of']} for "
            f"{(country or 'all monitored countries')} {(crop or 'all crops')}: "
            f"summary {res['summary']}; regions: {regions}; "
            f"drivers: {', '.join(drivers) if drivers else 'none stated'}.")
    trace.append(f"conditions -> {res['count']} rows as of {res['as_of']} "
                 f"(receipt: {res['query']['where']})")
    # Staleness must travel with the evidence: served-from-cache data presented as
    # live is exactly the dishonesty the wrapper exists to prevent (rule 1).
    stale = res.get("stale_data")
    if stale:
        text += (f" NOTE: this feed was UNAVAILABLE and these values are served from "
                 f"the last-good cache (last successful fetch {stale.get('last_good_fetch')}) "
                 f"— treat as possibly out of date.")
        trace.append(f"conditions -> STALE (last good {stale.get('last_good_fetch')})")
    return {"kind": "conditions", "retrieval": "pulled-at-pack-time",
            "source": "GEOGLAM Crop Monitor (CMET)",
            "title": f"{country or 'Global'} {crop or 'crop'} conditions".strip(),
            "pub_date": res["as_of"], "validation": "multi-agency-consensus",
            "url": res["query"]["url"], "query": res["query"]["where"],
            "stale_data": stale, "text": text}, None


def _driver_citations(trace):
    """The Pillar-1 climate DRIVER signal (ENSO/IOD) as evidence entries.

    Registry-driven, not hardcoded: any FEEDS row tagged `brief_role: driver` lands
    here, so a sixth driver feed is a config row. Goes through `feeds.query` rather
    than the fetchers so the passport, declines and staleness are the same ones the
    tool surface reports — one contract, not two.

    Each entry carries the use-case doc's hard constraint in its own text: Phase 1
    describes the SIGNAL and must not infer local agricultural impact. Putting that
    on the evidence itself (not only in the system prompt) means it survives into
    the pack and the receipt, where a reader can check it.
    """
    from ..mcp import feeds, registry     # local: mirrors registry's own cycle guard

    def render(records, budget=1800):
        """Feed rows -> evidence prose, shape-driven rather than feed-name-driven.

        A summary line alone is not evidence: `enso_outlook`'s whole purpose is the
        official probability wording, and a citation that said "11 narrative
        sections" carried none of it — the model could not quote what it could not
        see. Budgeted, because an unbounded narrative would crowd the pack.
        """
        parts = []
        for r in records or []:
            if isinstance(r, dict) and "paragraphs" in r:          # narrative sections
                parts.append(f"{r.get('heading')}: " + " ".join(r["paragraphs"]))
            elif isinstance(r, dict) and "forecast" in r:          # per-model plume rows
                continue                                           # summarised, not quoted
            elif isinstance(r, dict):
                parts.append("; ".join(f"{k}={v}" for k, v in r.items()))
            else:
                parts.append(str(r))
            if sum(len(p) for p in parts) > budget:
                parts.append(f"[... {len(records) - len(parts)} further rows not shown]")
                break
        return " | ".join(parts)[:budget]

    out, gaps = [], []
    specs = {n: s for n, s in registry.visible_feeds().items()
             if s.get("status") == "available" and s.get("brief_role") == "driver"}
    try:          # a contributor's own STAGED driver previews AS a driver, not as a table
        from ..contrib import staging
        specs.update({n: s for n, s in staging.visible_staged_feeds_for_pack("food-security").items()
                      if s.get("brief_role") == "driver" and n not in specs})
    except Exception:
        pass
    rows = sorted(specs.items())
    for name, spec in rows:
        res = feeds.query(name)
        if res.get("status") != "ok":
            trace.append(f"driver[{name}] -> {res.get('status')}: {res.get('note')}")
            gaps.append(f"{name}: {res.get('note') or 'no data'}")
            continue
        p = res.get("passport") or {}
        body = render(res.get("records"))
        basis = (f" [SST basis: {p['sst_basis']}]" if p.get("sst_basis") else "")
        text = (f"{spec.get('source')} {name} as of {res.get('as_of')}{basis}: "
                f"{res.get('summary')}."
                + (f" {body}" if body else "") +
                " DRIVER SIGNAL ONLY — this describes the ocean/atmosphere state and says "
                "nothing about rainfall, crops or food security at any particular place.")
        if res.get("note"):
            text += f" NOTE: {res['note']}"
        stale = p.get("stale_data") or {}
        if stale.get("served_stale"):
            text += (f" NOTE: served from last-good cache, not a live read "
                     f"({stale.get('reason')}) — treat as possibly out of date.")
            trace.append(f"driver[{name}] -> STALE ({stale.get('reason')})")
        else:
            trace.append(f"driver[{name}] -> {res.get('count')} rows as of {res.get('as_of')}")
        # Keep the STRUCTURED rows alongside the flattened text. `render` exists to
        # give the LLM prose it can cite, and it truncates at a character budget —
        # so until now the actual series was thrown away the moment it was
        # stringified, and nothing downstream could plot a number. The text stays
        # exactly as it was; this is an addition, not a change to what is cited.
        series = _series(name, res.get("records"))
        out.append({"kind": "index", "brief_role": "driver", "retrieval": "pulled-at-pack-time",
                    **({"review_status": spec["review"]} if spec.get("review") else {}),
                    **({"staged_by": spec["staged_by"], "contribution_id": spec.get("contribution_id")}
                       if spec.get("staged_by") else {}),
                    **({"series": series} if series else {}),
                    "source": spec.get("source"), "title": spec.get("title") or spec.get("description", name),
                    "pub_date": res.get("as_of"), "validation": spec.get("validation"),
                    "residency": spec.get("residency"), "url": p.get("url"),
                    "query": p.get("query"), "stale_data": stale or None,
                    "temporal": "forecast" if "forecast" in name or "plume" in name
                                or "outlook" in name else "observation",
                    "text": text})
    return out, gaps


def _series(name: str, records) -> dict | None:
    """A plottable view of a monthly index, or None if these rows are not a series.

    Deliberately narrow: {season|year|month, value, classification} is the shape the
    climate-index adapter returns, and anything else (verbatim narrative sections,
    the derived event catalogue) has no y-axis and is left alone. Capped, because
    this rides in a tool result that a host renders.
    """
    pts = []
    for r in records or []:
        if not isinstance(r, dict) or r.get("value") is None:
            continue
        label = (f"{r['season']} {r['year']}" if r.get("season")
                 else f"{r.get('year')}-{r.get('month'):02d}" if r.get("month")
                 else str(r.get("year") or ""))
        pts.append({"t": label, "v": r["value"], "c": r.get("classification")})
    if len(pts) < 2:
        return None
    # Threshold bands are INDEX knowledge, carried on the series so the renderer
    # never invents them: the panel used to hardcode +/-0.5 "El Nino/La Nina" on
    # EVERY chart — approximately right for ONI, wrong for DMI (+/-0.4 is the IOD
    # convention), and nonsense for any other pack's series.
    bands = {"oni": [{"v": 0.5, "label": "+0.5 El Nino"},
                     {"v": -0.5, "label": "-0.5 La Nina"}],
             "enso_oni": [{"v": 0.5, "label": "+0.5 El Nino"},
                          {"v": -0.5, "label": "-0.5 La Nina"}],
             "dmi": [{"v": 0.4, "label": "+0.4 positive IOD"},
                     {"v": -0.4, "label": "-0.4 negative IOD"}],
             "iod_dmi": [{"v": 0.4, "label": "+0.4 positive IOD"},
                         {"v": -0.4, "label": "-0.4 negative IOD"}]}
    out = {"id": name, "points": pts[-24:], "unit": "degrees C anomaly"}
    if name in bands:
        out["bands"] = bands[name]
    return out


_CONTEXT_ONLY = ("calendar", "index", "gaps")


def _has_evidence(citations: list, country: str | None = None) -> bool:
    """Can these citations carry a brief? Context alone cannot: a calendar, the
    global driver indices, the gaps entry. A source BOUND to this pack can when
    it is ABOUT the place asked — a hub's Malawi rainfall table declaring
    countries [Malawi] is evidence about Malawi — but it shares the `index` kind
    with the drivers and used to be barred with them, so a fresh library declined
    "No evidence available" over a country-matching table. A pack source that
    declares no countries is global as far as the platform knows, and a global
    table can no more carry a brief about one country than an ocean index can."""
    from ..geo import vectors as _vectors
    for c in citations:
        if c.get("kind") not in _CONTEXT_ONLY:
            return True
        if (c.get("brief_role") == "pack-source" and country and c.get("countries")
                and _vectors.country_matches(c["countries"], country)):
            return True
    return False


def _pack_bound_sources(trace, gaps, country=None):
    """Feeds and tables bound to this pack that are NOT seasonal drivers.

    The brief read only `brief_role: driver` rows, so a contributed table bound to
    food-security — the DEFAULT pack for a contribution — staged, answered
    feeds_query, and could never be cited in an answer. The risk pack already
    reads its own pack-bound sources; this is the same path, which food-security
    simply never had.
    """
    from ..contrib import staging
    from ..mcp import feeds, registry
    rows = {n: sp for n, sp in registry.visible_feeds().items()
            if sp.get("status") == "available"
            and sp.get("pack", "food-security") == "food-security"
            and sp.get("brief_role") != "driver"}
    try:
        rows.update({n: sp for n, sp
                     in staging.visible_staged_feeds_for_pack("food-security").items()
                     if n not in rows})
    except Exception:
        pass
    out, elsewhere = [], {}
    for ds in sorted(rows):
        # Is this source about the country asked about? A Battambang rainfall table was
        # cited into a KENYA maize brief. Countries and a box, the rule every pack
        # applies (mcp.feeds.scope_miss); this pack answers for a whole country, so a
        # box is placed through the countries the source also declares, never guessed.
        miss = feeds.scope_miss(rows[ds], [country] if country else [], None)
        if miss:
            elsewhere[ds] = miss
            continue
        res = feeds.query(ds, {"limit": 4})
        if res.get("status") != "ok":
            gaps.append(f"source {ds} bound to this pack did not answer: "
                        f"{res.get('note', 'no reason given')}")
            continue
        spec, records = rows[ds], (res.get("records") or [])
        from ..risk.synthesis import _is_time_series
        per_row_time, _af = _is_time_series(spec, records)
        if per_row_time and records:
            last = records[-1]
            body = (f"{spec.get('title', ds)} ({ds}), latest reading as of "
                    f"{res.get('as_of')}: "
                    + ", ".join(f"{k} {v}" for k, v in last.items() if v is not None))
        else:
            shown = records[:4]
            body = (f"{spec.get('title', ds)} ({ds}) is a LOOKUP TABLE, not a time "
                    f"series — no row is 'the latest'. {len(records)} row(s); showing "
                    f"{len(shown)}: "
                    + ("; ".join(", ".join(f"{k} {v}" for k, v in r.items()
                                           if v is not None) for r in shown)
                       or "no values returned"))
        pp = res.get("passport") or {}
        out.append({
            "kind": "index", "retrieval": "pulled-at-pack-time",
            # A standing seasonal DRIVER and a table bound to this pack are
            # different kinds of evidence, and a reader should be able to tell
            # which one they are looking at.
            "brief_role": "pack-source",
            "countries": spec.get("countries"),
            "source": pp.get("source") or spec.get("source"),
            "title": spec.get("title", ds),
            "validation": pp.get("validation") or spec.get("validation", "unvalidated"),
            "url": pp.get("url"),
            **({"staged_by": spec["staged_by"],
                "contribution_id": spec.get("contribution_id")}
               if spec.get("staged_by") else {}),
            **({"review_status": spec["review"]} if spec.get("review") else {}),
            "text": body + ". " + (res.get("summary") or "")
                    + (f" Review: {spec['review']}." if spec.get("review") else "")
                    + (f" Contributor guidance: {spec['usage_notes']}"
                       if spec.get("usage_notes") else ""),
        })
        trace.append(f"pack_source[{ds}] {res.get('count')} rows")
    if elsewhere:
        trace.append(f"pack_sources[skipped:{','.join(sorted(elsewhere))}] scoped elsewhere")
        gaps.append(feeds.scope_gap(elsewhere))
    return out


def gather_evidence(parsed, trace, calendar_override=None, calendar_target=(None, None)):
    """Deterministic evidence assembly: two retrieval slices + the conditions feed
    + the Pillar-1 climate drivers + the crop calendar. Returns (citations, gaps,
    stats), citations numbered. A calendar override made for one country/crop is
    never silently applied to another — a mismatch drops it and declares the drop
    as a gap."""
    corpus = Corpus(CORPUS)
    crop, country, focus = parsed["crop"], parsed["country"], parsed["focus"]
    gaps = []
    # BOTH slices have to be about what was ASKED. The retrospective query used to
    # be a fixed sentence — "impact of past El Nino events on <crop> production and
    # food security in <country>" — with no trace of the question in it, so half of
    # every brief's document evidence was retrieved blind. Measured: for Kenya it
    # returned the SAME three documents whether the analyst asked about post-harvest
    # storage losses or about early signs of crop failure. A platform whose promise
    # is that an answer traces to its evidence cannot retrieve that evidence against
    # a question nobody asked.
    q_now = f"El Nino seasonal rainfall forecast outlook {country} {crop} {focus}".strip()
    q_past = (f"{focus} — impact of past events on {crop or 'crop'} production and "
              f"food security in {country}").strip()
    # A document about another country is passed over and the next one down takes its
    # place, so k stays filled with this country's (or undeclared) documents.
    where, skipped = ([country] if country else []), {}
    forecast_hits = corpus.search(q_now, k=5, temporal="forecast", where=where, skipped=skipped)
    retro_hits = corpus.search(q_past, k=5, temporal="retrospective", where=where, skipped=skipped)
    trace.append(f"retrieve[forecast] {q_now!r} -> {len(forecast_hits)} hits")
    trace.append(f"retrieve[retrospective] {q_past!r} -> {len(retro_hits)} hits")
    if not retro_hits:
        # A narrow question can put every retrospective document below the floor.
        # Losing the whole historical slice is worse than answering the general
        # question, so fall back — and SAY which query produced the evidence, or
        # the citation list quietly stops matching the question it is filed under.
        # The corpus was assembled around El Nino, so this is the question it was
        # built to answer — the right thing to fall back TO, and it was the only
        # query the platform ever ran until now.
        q_general = (f"impact of past El Nino events on {crop or 'crop'} production "
                     f"and food security in {country}").strip()
        retro_hits = corpus.search(q_general, k=5, temporal="retrospective", where=where,
                                   skipped=skipped)
        trace.append(f"retrieve[retrospective:fallback] {q_general!r} -> "
                     f"{len(retro_hits)} hits")
        if retro_hits:
            gaps.append(
                f"no retrospective document matched the specific question ({focus!r}); "
                "the historical evidence below answers the general question about "
                f"{crop or 'this crop'} in {country} instead")
    # THE CURRENT SEASON, which neither query above could reach. `temporal` marks a
    # document observational-vs-forecast; `q_past` reads that same tag as meaning
    # HISTORICAL. A country brief published during this season is observational but
    # not historical, so the only query that could see it asked about past events.
    # Measured on the flagship question: the FAO GIEWS Kenya brief (04-May-2026)
    # scores 0.716 and 0.679 on a current-season query for the two chunks describing
    # THIS season's rains, and 0.611 for its price table — and the price table was
    # the one that reached the pack, so the brief reported no current-season
    # evidence while the library held it. Additive: this can only ADD observational
    # evidence already tagged the same way, never displace what was retrieved.
    q_current = (f"{country} {crop or 'crop'} current season to date — rainfall "
                 f"received, crop and rangeland condition, harvest prospects "
                 f"{focus}").strip()
    current_hits = corpus.search(q_current, k=3, temporal="retrospective", where=where,
                                 skipped=skipped)
    already = {h["id"] for h in retro_hits}
    added = [h for h in current_hits if h["id"] not in already]
    retro_hits = retro_hits + added
    trace.append(f"retrieve[current-season] {q_current!r} -> {len(current_hits)} hits, "
                 f"{len(added)} not already retrieved")
    if not current_hits:
        gaps.append("no document in the library describes the CURRENT season's "
                    "observed conditions for this country and crop")

    if not forecast_hits:
        gaps.append("no forecast/outlook document in the library matched the question")
    if not retro_hits:
        gaps.append("no analog-year/retrospective document in the library matched the question")

    # WHICH COUNTRY ARE THESE DOCUMENTS ACTUALLY ABOUT? Retrieval is by meaning,
    # so a question about a country the library does not cover comes back full of
    # confident, well-scored documents about a different one. Asked about Ethiopia,
    # the pack returned status ok with ten document citations, every one tagged
    # Kenya or Zambia, and declared no gap at all. A brief drafted from that reads
    # as authoritative and is about the wrong country — the failure that moves
    # emergency resources to the wrong place.
    # Documents about other countries are now passed over at retrieval (above), so
    # the library's silence about this country is read from what was skipped.
    if country:
        about = [h for h in forecast_hits + retro_hits if h["metadata"].get("countries")]
        others = sorted({str(c).strip() for w in (skipped.get("elsewhere") or {})
                         for c in w.split(", ") if str(c).strip()})
        if others and not about:
            n_cited = len(forecast_hits) + len(retro_hits)
            gaps.insert(0, (
                f"NO DOCUMENT IN THE LIBRARY IS ABOUT {country.upper()}. Its closest "
                f"matches are about {', '.join(others)} and were NOT cited"
                + (f"; the {n_cited} document(s) cited below declare no country. They may "
                   "describe a shared driver, but nothing here observes this country. Do "
                   f"not read any of it as evidence about {country}." if n_cited else ".")))

    # The same check for CROP, which was never written. Country was varied all
    # through testing and crop never was — every test asked about maize — so an
    # uncovered crop declared no gap at all: asking for Kenya RICE returned maize
    # documents with the word rice around them. Silence is worse than the country
    # case, which at least announced itself.
    if crop:
        covered_crops = {str(x).strip().lower()
                         for h in forecast_hits + retro_hits
                         for x in (h["metadata"].get("crops") or [])}
        if covered_crops and crop.strip().lower() not in covered_crops:
            gaps.insert(0, (
                f"NO DOCUMENT IN THE LIBRARY IS ABOUT {crop.upper()}. The "
                f"{len(forecast_hits) + len(retro_hits)} document(s) cited below are "
                f"about {', '.join(sorted(x.title() for x in covered_crops))}. Their "
                f"findings do not transfer to {crop}: sowing windows, water demand and "
                f"failure modes differ by crop. Do not read any of it as evidence "
                f"about {crop}."))

    from ..contrib import staging
    citations = []
    for h in forecast_hits + retro_hits:
        m = h["metadata"]
        citations.append({
            "kind": "document", "retrieval": "archived-document",
            "source": m.get("source"), "title": m.get("title"),
            "pub_date": m.get("pub_date"), "validation": m.get("validation"),
            "temporal": m.get("temporal"), "url": m.get("url"), "score": h["score"],
            # The countries this document is actually about. Without it the drafter
            # cannot see a geography mismatch it is about to write over.
            "countries": m.get("countries"),
            "doc_id": h["doc_id"], "chunk_id": h["id"],
            **({"review_status": staging.cited_review(m)} if staging.cited_review(m) else {}),
            "archived_copy": (f"/api/food-security/rag/document/{h['doc_id']}"
                              if corpus.raw_path(h["doc_id"]) else None),
            "usage_notes": m.get("usage_notes"),
            # a preview citation says so, so the pack, report and receipt built
            # on it inherit the contributor's visibility (contrib/staging.py)
            **({"staged_by": m["staged_by"], "contribution_id": m.get("contribution_id")}
               if m.get("staged_by") else {}),
            "text": h["text"] + (f" Review: {staging.cited_review(m)}."
                                 if staging.cited_review(m) else "")})
    cond, gap = _conditions_citation(crop, country, trace)
    if cond:
        citations.append(cond)
    else:
        gaps.append(gap)
    drivers, driver_gaps = _driver_citations(trace)
    citations.extend(drivers)
    gaps.extend(driver_gaps)
    citations.extend(_pack_bound_sources(trace, gaps, country=country))
    asked_month = datetime.now(timezone.utc).month
    t_country, t_crop = calendar_target
    if calendar_override and (
            (t_country and t_country.lower() != (country or "").lower())
            or (t_crop and t_crop.lower() != (crop or "").lower())):
        gaps.append(f"a calendar adjustment made for {t_country or '?'} {t_crop or '?'} "
                    f"was NOT applied — the question is about {country or '?'} "
                    f"{crop or '?'}; the hub-default calendar was used instead")
        trace.append("calendar -> override target mismatch; ignored")
        calendar_override = None
    cal = crop_calendar.citation(country, crop, asked_month, override=calendar_override)
    if cal:
        citations.append(cal)
        trace.append("calendar -> " + ("ADJUSTED by requester" if cal["adjusted"]
                                       else "hub default") + f" ({country} {crop})")
    elif calendar_override:
        gaps.append("a calendar adjustment was sent but no country/crop could be "
                    "resolved to apply it to")
    for n, c in enumerate(citations, 1):
        c["n"] = n
    stats = {"forecast_hits": len(forecast_hits), "retrospective_hits": len(retro_hits),
             "conditions": cond is not None, "drivers": len(drivers),
             "calendar": (cal or {}).get("adjusted") is not None and (
                 "adjusted" if (cal or {}).get("adjusted") else "default"),
             # the literal retrieval queries — provenance for the retrieval step itself
             "queries": {"forecast": q_now, "retrospective": q_past}}
    return citations, gaps, stats


def _render_pack(citations):
    """The numbered evidence block: documents via the source_block seam (E3),
    the conditions feed in the same shape."""
    def _text(c):
        t = (f"[temporal={c['temporal']}] {c['text']}"
             if c.get("temporal") else c["text"])
        # contributor guidance rides WITH the evidence, where the drafter reads it
        if c.get("usage_notes"):
            t = f"[contributor guidance: {c['usage_notes']}] {t}"
        return t
    doc_like = [{"metadata": {k: c.get(k) for k in
                              ("source", "title", "pub_date", "validation", "url")},
                 "text": _text(c)}
                for c in citations]
    return source_block(doc_like)


def _sources_md(citations):
    lines = []
    for c in citations:
        bits = [str(c.get("source") or "unknown source")]
        bits += [str(c[k]) for k in ("title", "pub_date") if c.get(k)]
        if c.get("validation"):
            bits.append(f"validation: {c['validation']}")
        line = f"[{c['n']}] " + " — ".join(bits)
        if c.get("url"):
            line += f"\n    source: {c['url']}"
        if c.get("archived_copy"):
            line += f"\n    archived: {c['archived_copy']}"
        if c.get("query"):
            line += f"\n    query: {c['query']}"
        lines.append(line)
    return "\n".join(lines)


def _declined(reason, *, trace, usage, citations=None, check=None, stats=None):
    return {"declined": True, "brief": None, "decline_reason": reason,
            "citations": citations or [], "evidence": stats or {},
            "grounded": check, "trace": trace, "usage": usage}


def synthesize(question, provider=None, model=None, calendar=None,
               calendar_target=(None, None)):
    """The full pipeline. Returns a dict the route serves as-is."""
    settings = get_settings()
    provider = provider or settings.default_provider
    model = model or default_model(provider)
    client = build_client(provider)
    trace, usage = [f'question: "{question}"'], []

    parsed, decline = _parse(client, model, question, trace, usage)
    if decline:
        return _declined(decline, trace=trace, usage=usage) | {
            "provider": provider, "model": model}

    citations, gaps, stats = gather_evidence(parsed, trace, calendar_override=calendar,
                                             calendar_target=calendar_target)
    # A calendar is context, not evidence — it cannot carry a brief alone. Neither
    # can the driver indices: they are GLOBAL, so on a question about one country
    # they establish the ocean state and nothing about that place. Letting them
    # clear the bar would let the engine answer "maize in Kenya" out of an ENSO
    # index alone, which is exactly the inference the use case forbids in Phase 1.
    if not _has_evidence(citations, parsed.get("country")):
        trace.append("evidence -> empty; declining without a synthesis call")
        return _declined(
            "No evidence available: " + "; ".join(gaps), trace=trace, usage=usage,
            stats=stats) | {"provider": provider, "model": model}

    # Declared gaps (or their explicit absence) as a citable entry — appended
    # AFTER the evidence bar so it can never carry a brief alone. Observed: the
    # model honestly wrote "there are no identified gaps..." and the gate blocked
    # it as uncited, costing a retry.
    from ..mcp import packs as mcp_packs
    citations = [*citations, mcp_packs.gaps_citation(citations, gaps)]

    asked_on = datetime.now(timezone.utc).strftime("%B %Y")
    user_msg = (
        f"Question: {question}\n"
        f"Asked in: {asked_on} (state the season-timing caveat relative to this)\n"
        f"Parsed target: crop={parsed['crop'] or '?'}, country={parsed['country'] or '?'}, "
        f"focus={parsed['focus']}\n"
        "Known gaps (must appear in What's missing, citing the declared-gaps "
        "evidence entry): "
        + ("; ".join(gaps) if gaps else "none identified") + "\n\n"
        "Numbered evidence (the ONLY permissible sources):\n\n" + _render_pack(citations))

    messages = [{"role": "system", "content": _SYNTH_SYSTEM},
                {"role": "user", "content": user_msg}]
    check = None
    for attempt in (1, 2):
        # 1500 was set when a pack held ~4 citations. With the Pillar-1 drivers a
        # brief runs longer, and a TRUNCATED draft fails the gate as "missing
        # sections" — a length problem wearing a groundedness problem's clothes,
        # which cost real debugging time. Headroom, plus an explicit truncation
        # check below so the next occurrence names itself.
        resp = client.chat.completions.create(model=model, max_tokens=3000,
                                              messages=messages)
        usage.append(_usage(resp))
        draft = (resp.choices[0].message.content or "").strip()
        if getattr(resp.choices[0], "finish_reason", None) == "length":
            trace.append(f"synthesis attempt {attempt} -> TRUNCATED at max_tokens; "
                         "the draft is incomplete, not ungrounded")
        if draft.startswith("DECLINE:"):
            trace.append(f"synthesis attempt {attempt} -> model declined")
            return _declined(draft, trace=trace, usage=usage, citations=citations,
                             stats=stats) | {"provider": provider, "model": model}
        check = grounded.check_grounded(draft, citations)
        check["attempts"] = attempt
        trace.append(f"groundedness attempt {attempt} -> "
                     + ("PASS" if check["passed"] else "FAIL: " + "; ".join(check["failures"])))
        if check["passed"]:
            brief = draft + "\n\n## Sources\n" + _sources_md(citations)
            return {"declined": False, "brief": brief, "citations": citations,
                    "parsed": parsed, "evidence": stats, "gaps": gaps, "grounded": check,
                    "provider": provider, "model": model, "trace": trace, "usage": usage}
        messages += [{"role": "assistant", "content": draft},
                     {"role": "user", "content":
                      "Your draft failed the groundedness check — "
                      + "; ".join(check["failures"])
                      + ". Rewrite following the rules exactly."}]
    return _declined(
        "The draft could not be grounded in the evidence after a retry — refusing to "
        "ship an uncited brief.", trace=trace, usage=usage, citations=citations,
        check=check, stats=stats) | {"provider": provider, "model": model}
