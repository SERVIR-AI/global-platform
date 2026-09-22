"""A6 — the synthesis engine: question -> evidence pack -> grounded, cited brief.

The LLM is called exactly twice: parse the question (tool-call extraction, no
data access) and write the brief FROM THE PACK ONLY. Everything between is
deterministic; a blocking groundedness check runs after, with one retry — a
brief whose citations don't resolve does not ship.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from ..config import get_settings
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

_PARSE_SYSTEM = (
    "You route questions for a food-security briefing service (crop conditions, seasonal "
    "forecasts, El Nino impacts, food security in East/Southern Africa). If the question is "
    "in scope, call plan_brief with its crop/country/focus. If out of scope, do not call the "
    "tool — reply with one sentence saying why you cannot brief on it.")

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

_CITE_GROUP = re.compile(r"\[([\d\s,–\-]+)\]")   # [3], [1, 9], [1-3], [2016]

# Thousands separators: a comma OR space between a digit and a 3-digit group
# (chained) — "800 000"/"800,000" are one number. UN/African docs use spaces,
# so a comma-only strip false-flagged legitimate figures as unverified.
# Thousands separators, as they ACTUALLY occur in extracted source text — not just
# the ASCII two. UN and WMO house style uses U+00A0 NO-BREAK SPACE, and PDF
# extraction emits it and bare newlines; measured across stored citations: comma
# 1,083, space 298, NBSP 160, newline 21. Missing the last two made the gate read
# WMO's "more than 30\xa0000 livestock deaths and destroyed 170\xa0000\xa0ha of
# cropland" as the numbers 30, 000, 170, 000 — so a brief quoting it faithfully was
# scored as two fabrications and blocked. Roughly two thirds of the gate's blocks
# were correctly-sourced figures until this class was widened.
_SEP = "[ ,\u00a0\u202f\u2009]"
_THOUSANDS = re.compile(rf"(?<=\d){_SEP}(?=\d{{3}}(?:{_SEP}\d{{3}})*(?!\d))")


def _norm_nums(text: str) -> str:
    return _THOUSANDS.sub("", text)


def _number_set(text: str) -> set[str]:
    """Every number in `text`, keyed by VALUE rather than spelling.

    "0.40" and "0.4" are the same weight; "242.0" and "242" are the same
    kilometres. Comparing the strings made honest drafts look unsourced and, worse,
    buried the numbers that really were unsourced in that noise — which is how an
    invented figure rode through a gate that had actually noticed it.
    """
    out: set[str] = set()
    for tok in re.findall(r"\d+(?:\.\d+)?", _norm_nums(text)):
        if "." not in tok:
            # An integer keeps its exact digits. Float-normalising it turned a
            # 14-digit identifier into 2.02305e+13 and made two copies of the same
            # number stop matching — noise in exactly the signal that has to be
            # trustworthy before it can block anything.
            out.add(tok.lstrip("0") or "0")
            continue
        try:
            v = float(tok)
        except ValueError:                                  # pragma: no cover
            out.add(tok)
            continue
        out.add(f"{v:.10g}")
        out.add(str(int(v)) if v == int(v) else f"{v:.10g}")
        out.add(str(round(v)))              # a rounded quote of the same figure
    return out


def _cited_numbers(text: str) -> set[int]:
    """All citation numbers in bracket groups, ranges expanded; 4-digit values
    are years, not citations."""
    nums: set[int] = set()
    for group in _CITE_GROUP.findall(text):
        for part in re.split(r"[,\s]+", group.replace("–", "-")):
            if not part:
                continue
            a, dash, b = part.partition("-")
            if dash and a.isdigit() and b.isdigit() and int(a) <= int(b):
                nums.update(range(int(a), min(int(b), int(a) + 50) + 1))
            elif part.isdigit():
                nums.add(int(part))
    return {n for n in nums if n < 1000}


def _usage(resp) -> dict:
    u = getattr(resp, "usage", None)
    return {"in": getattr(u, "prompt_tokens", 0) or 0,
            "out": getattr(u, "completion_tokens", 0) or 0} if u else {"in": 0, "out": 0}


def _parse(client, model, question, trace, usage):
    resp = client.chat.completions.create(
        model=model, max_tokens=300, tools=_PARSE_TOOLS,
        messages=[{"role": "system", "content": _PARSE_SYSTEM},
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
    rows = sorted((n, s) for n, s in registry.FEEDS.items()
                  if s.get("status") == "available" and s.get("brief_role") == "driver")
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


def _source_speaks_to(spec: dict, country: str | None) -> bool:
    """Is this pack-bound source about the country being asked about?

    Added the moment the path existed: a cold contributor watched a Battambang
    rainfall table get cited into a KENYA maize brief. A citation list is a claim
    about what the answer rests on, and an unrelated source dilutes the ones that
    matter — the same failure as a flood brief citing global earthquakes, made on
    the food-security side by giving it the path without the filter.

    A source that declares its countries is filtered on them. One that declares
    none is global as far as the platform knows, so it is still included.
    """
    if not country:
        return True
    declared = spec.get("countries") or ([spec["country"]] if spec.get("country") else None)
    if not declared:
        return True
    return country.strip().lower() in {str(c).strip().lower() for c in declared}


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
    rows = {n: sp for n, sp in registry.FEEDS.items()
            if sp.get("status") == "available"
            and sp.get("pack", "food-security") == "food-security"
            and sp.get("brief_role") != "driver"}
    try:
        rows.update({n: sp for n, sp
                     in staging.visible_staged_feeds_for_pack("food-security").items()
                     if n not in rows})
    except Exception:
        pass
    out, off_topic = [], []
    for ds in sorted(rows):
        if not _source_speaks_to(rows[ds], country):
            off_topic.append(ds)
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
            "source": pp.get("source") or spec.get("source"),
            "title": spec.get("title", ds),
            "validation": pp.get("validation") or spec.get("validation", "unvalidated"),
            "url": pp.get("url"),
            **({"staged_by": spec["staged_by"],
                "contribution_id": spec.get("contribution_id")}
               if spec.get("staged_by") else {}),
            "text": body + ". " + (res.get("summary") or "")
                    + (f" Contributor guidance: {spec['usage_notes']}"
                       if spec.get("usage_notes") else ""),
        })
        trace.append(f"pack_source[{ds}] {res.get('count')} rows")
    if off_topic:
        gaps.append(f"{len(off_topic)} source(s) bound to this pack were NOT cited "
                    f"because they declare other countries ({', '.join(off_topic)}). "
                    "They are available through feeds_query if you want them.")
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
    forecast_hits = corpus.search(q_now, k=5, temporal="forecast")
    retro_hits = corpus.search(q_past, k=5, temporal="retrospective")
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
        retro_hits = corpus.search(q_general, k=5, temporal="retrospective")
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
    current_hits = corpus.search(q_current, k=3, temporal="retrospective")
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
    if country:
        covered = {str(c).strip().lower()
                   for h in forecast_hits + retro_hits
                   for c in (h["metadata"].get("countries") or [])}
        if covered and country.strip().lower() not in covered:
            gaps.insert(0, (
                f"NO DOCUMENT IN THE LIBRARY IS ABOUT {country.upper()}. The "
                f"{len(forecast_hits) + len(retro_hits)} document(s) cited below were "
                f"retrieved by meaning and are about "
                f"{', '.join(sorted(c.title() for c in covered))}. They may describe a "
                "shared driver, but nothing here observes this country. Do not read "
                "any of it as evidence about "
                f"{country}."))

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
            "archived_copy": (f"/api/food-security/rag/document/{h['doc_id']}"
                              if corpus.raw_path(h["doc_id"]) else None),
            "usage_notes": m.get("usage_notes"),
            # a preview citation says so, so the pack, report and receipt built
            # on it inherit the contributor's visibility (contrib/staging.py)
            **({"staged_by": m["staged_by"], "contribution_id": m.get("contribution_id")}
               if m.get("staged_by") else {}),
            "text": h["text"]})
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


def _citation_values(c: dict) -> list[float]:
    """Every number inside ONE citation's text."""
    out = []
    for tok in re.findall(r"\d+(?:\.\d+)?", _norm_nums(str(c.get("text") or ""))):
        try:
            out.append(float(tok))
        except ValueError:                                  # pragma: no cover
            pass
    return sorted(set(out))


_DRIVER_ONLY = re.compile(r"DRIVER SIGNAL ONLY|says nothing about rainfall", re.I)
_LOCAL_CLAIM = re.compile(
    r"\b(rainfall|rains|precipitation|harvest|crop|yield|planting|sowing|"
    r"food security|famine|hunger|waterlogg|flooding|drought)\w*\b", re.I)


def _driver_only_claims(draft: str, citations: list) -> list[dict]:
    """Paragraphs that make a LOCAL claim resting only on driver-signal evidence.

    The drafting rules already say it: evidence marked DRIVER SIGNAL ONLY describes
    the ocean and atmosphere, and "You may NOT use it on its own to claim any local
    rainfall, crop or food-security outcome". The citations say it too — they carry
    the sentence "says nothing about rainfall, crops or food security at any
    particular place". Nothing enforced it, and a brief told a ministry advisor that
    "the forecast rainfall peak lands immediately after" the harvest window, citing
    a NOAA CPC ENSO discussion that contains no rainfall forecast and explicitly
    disclaims that use. That sentence was the answer's only action item.

    It WARNS rather than blocks. Measured across 1,348 passing briefs it fires on
    0.8%, and reading them, some are a legitimate description of what a driver
    modulates rather than a claim about a place. A blocking check has to be right
    about every one of those, and this one is not yet.
    """
    by_n = {}
    for c in citations or []:
        try:
            by_n[int(c.get("n"))] = c
        except (TypeError, ValueError):
            continue
    out = []
    # SENTENCE level, not paragraph. The real case was
    # "September 2026 is inside that same harvest window [16], and the forecast
    # rainfall peak falls immediately after it [11]." — [16] is the crop calendar,
    # so a paragraph-level "are all of these driver-only?" test passes it. The
    # claim that matters is pinned to [11] alone, in its own clause.
    for para in re.split(r"\n\s*\n", draft.split("\n## Sources")[0]):
        for sentence in re.split(r"(?<=[.!?])\s+|(?<=\])\s*,\s+and\s+", para):
            cited = [n for n in _cited_numbers(sentence) if n in by_n]
            if not cited:
                continue
            body = _CITE_GROUP.sub("", sentence)
            if not _LOCAL_CLAIM.search(body):
                continue
            if all(_DRIVER_ONLY.search(str(by_n[n].get("text") or "")) for n in cited):
                out.append({"cites": sorted(cited), "claim": sentence.strip()[:220],
                            "why": ("every source cited for this claim is DRIVER "
                                    "SIGNAL ONLY and says nothing about rainfall, "
                                    "crops or food security at any particular place")})
    return out


def _index_scale(num: str) -> bool:
    """A small DECIMAL — the shape every climate index takes.

    `_load_bearing` skips everything under 10 so that counts ("3 districts") do
    not bury the figures that matter. That silently exempted exactly the numbers
    a driver claim turns on: an ONI of +1.8, an SOI of -0.5, a Nino-3.4 anomaly.
    A fabricated index value was never checked at all. An integer under 10 is
    still noise; a decimal under 10 is not.
    """
    try:
        v = float(num)
    except ValueError:                                      # pragma: no cover
        return False
    return abs(v) < 10 and v != int(v)


def _load_bearing(num: str) -> bool:
    """Is this the kind of number a decision would turn on?

    Years and small counts dominate any paragraph-level scan and are almost never
    the figure a reader acts on, so flagging them buries the ones that matter. A
    warning nobody reads protects nobody.
    """
    try:
        v = float(num)
    except ValueError:                                      # pragma: no cover
        return False
    if 1900 <= v <= 2100 and v == int(v):
        return False                                        # a year
    return v >= 10


def _attribution_warnings(draft: str, citations: list) -> list[dict]:
    """Numbers attributed to a citation that does not contain them.

    The blocking check asks whether a number exists ANYWHERE in the pack, so a
    figure lifted from citation [7] and attributed to [2] passes it. Existence is
    not attribution, and for a reader following a claim back to its source the
    difference is the whole point of the receipt.

    This WARNS rather than blocks: paragraph-level scoping mis-reads legitimately
    (a paragraph citing [2][3] and quoting a figure whose supporting citation is
    named in the next sentence), and a check that blocks honest work gets switched
    off. It is surfaced so a reader can see it, not used to refuse the brief.
    """
    out = []
    by_n = {}
    for c in citations or []:
        try:
            by_n[int(c.get("n"))] = c
        except (TypeError, ValueError):
            continue
    for para in re.split(r"\n\s*\n", draft.split("\n## Sources")[0]):
        cited = _cited_numbers(para)
        if not cited:
            continue
        local = set()
        for n in cited:
            c = by_n.get(n)
            if c:
                local |= _number_set(" ".join(str(c.get(k) or "") for k in
                                              ("text", "pub_date", "title", "source")))
        if not local:
            continue
        for num in sorted(_number_set(_CITE_GROUP.sub("", para)) - local):
            if not _load_bearing(num):
                continue
            if _cited_share(num, [by_n[n] for n in cited if n in by_n]):
                continue
            elsewhere = sorted(n for n, c in by_n.items()
                               if num in _number_set(str(c.get("text") or "")))
            if elsewhere:
                out.append({"number": num, "attributed_to": sorted(cited),
                            "actually_in": elsewhere,
                            "paragraph": para.strip()[:120]})
    return out


def _cited_share(target: str, citations: list, draft_nums: set | None = None) -> str | None:
    """Is `target` a share of a total stated in the SAME citation?

    The one piece of arithmetic a drafter legitimately does is "X of Y, which is
    Z%". Everything else it computes is the drafter doing analysis the platform did
    not do, and in a brief that allocates disaster response that has to be refused
    rather than reasoned about.

    The constraints are what make this safe, and they were all found by measurement:
    the target must actually BE a percentage (0-100), the two figures must come from
    the SAME citation, and the numerator cannot exceed the denominator. Without the
    percentage bound a fabricated "US$167.13 million" was "explained" as a share of
    two unrelated figures; without the single-citation bound, searching every number
    in the pack explained ALL 46 flagged numbers including the known fabrications,
    because a few hundred values combined pairwise can hit any target by chance.
    """
    try:
        t = float(target)
    except ValueError:                                      # pragma: no cover
        return None
    if not 0.0 <= t <= 100.0:
        return None
    for c in citations or []:
        vals = _citation_values(c)
        for a in vals:
            for b in vals:
                if not b or a > b or abs(a / b * 100 - t) > 0.6:
                    continue
                # BOTH figures must be on the page. "2 of 27 schools, 7.4%" is
                # checkable by a reader; a bare 7.4 that happens to equal some
                # ratio buried in a citation is not, and allowing it waved
                # through 88% of arbitrary percentages in a real pack.
                if draft_nums is not None and not (
                        _fmt(a) & draft_nums and _fmt(b) & draft_nums):
                    continue
                return f"{a} of {b} in [{c.get('n')}]"
    return None


def _fmt(v: float) -> set:
    """How a figure might be written, so "2" matches "2.0"."""
    out = {f"{v:.10g}", str(round(v))}
    if v == int(v):
        out.add(str(int(v)))
    return out



_GAP_SUBJECT = re.compile(r"NO DOCUMENT IN THE LIBRARY IS ABOUT ([A-Z][A-Z \-\'']+?)\.")
_ACK_PHRASES = ("no document", "no evidence", "nothing here observes", "not covered",
                "no source", "does not transfer", "no data", "outside the library",
                "not in the library", "library holds nothing", "cannot be answered")


def _unacknowledged_gaps(draft: str, gaps) -> list[str]:
    """Declared 'we hold nothing about X' gaps that the draft writes straight past.

    The pack already announces when it has no document about the country or crop
    asked for. Nothing made the draft repeat it, so a confident brief about an
    uncovered target passed the gate and minted a receipt. WARNING for now, not a
    block: an honest brief that DOES own the gap must never be punished, and the
    false-alarm rate has to be measured before this is made binding.
    """
    low = draft.lower()
    owned = any(p in low for p in _ACK_PHRASES)
    out = []
    for g in gaps or []:
        m = _GAP_SUBJECT.search(str(g))
        if not m:
            continue
        subject = m.group(1).strip()
        if subject.lower() not in low:
            continue        # the draft never claims anything about it at all
        if not owned:
            out.append(f"the pack declares it holds no document about {subject.title()}, "
                       f"and the draft makes claims about {subject.title()} without "
                       "stating that gap")
    return out


def check_grounded(draft, citations, sections=None, extra_evidence=None,
                   gaps=None):
    """Blocking: required sections present, no model-written Sources, citations
    resolve, every paragraph cites. Recorded (not yet blocking): numbers absent
    from the evidence (whole-token compare over chunk text + citation metadata).
    `sections` defaults to this pack's SECTIONS; a caller with another domain
    pack passes that pack's own contract."""
    valid = {c["n"] for c in citations}
    used = _cited_numbers(draft)
    phantom = sorted(used - valid)
    missing_sections = [s for s in (SECTIONS if sections is None else sections) if s not in draft]
    wrote_sources = "## Sources" in draft
    paragraphs = []                    # header lines don't shield the prose under them
    for block in draft.split("\n\n"):
        body = "\n".join(l for l in block.splitlines()
                         if not l.strip().startswith("#")).strip()
        if body:
            paragraphs.append(body)
    uncited = [p[:70] for p in paragraphs if not _CITE_GROUP.search(p)]
    evid_blob = " ".join(" ".join(str(c.get(k) or "") for k in
                                  ("text", "pub_date", "title", "source"))
                         for c in citations)
    # Numbers the PLATFORM computed — the area of interest, the asset totals, the
    # weights — are evidence as much as a citation is. They were not in the blob, so
    # quoting the platform's own figure back at it counted as unsourced.
    if extra_evidence:
        evid_blob += " " + str(extra_evidence)
    evid_nums = _number_set(evid_blob)
    # The bibliography is not a claim. A trailing "## Sources" block is the
    # platform's own rendering of the citations, and its digits are URLs, document
    # ids and dates — scanning them produced a steady drip of false "unverified
    # numbers" that buried the fabricated figures among the noise.
    claim_text = _CITE_GROUP.sub("", draft.split("\n## Sources")[0])
    draft_nums = _number_set(claim_text)
    flagged = sorted(draft_nums - evid_nums)
    # Separate the drafter's one legitimate calculation from figures that trace to
    # nothing at all. Measured across 1,288 previously-passing briefs: 1.3% newly
    # blocked, and the real fabrication ("US$167.13 million", "47281 displaced",
    # absent from every field of every citation in its pack) is among them.
    derived, unverified, index_scale = {}, [], []
    for num in flagged:
        if not _load_bearing(num):
            # Sub-threshold. A decimal down here is a climate-index value, not a
            # count, and was exempt from every check — WARN on it while the
            # false-alarm rate is measured, rather than block today.
            if _index_scale(num) and not _cited_share(num, citations, draft_nums):
                index_scale.append(num)
            continue          # a year or a count under 10 blocks nothing
        share = _cited_share(num, citations, draft_nums)
        if share:
            derived[num] = share
        else:
            unverified.append(num)
    misattributed = _attribution_warnings(draft, citations)
    driver_only = _driver_only_claims(draft, citations)
    unowned_gaps = _unacknowledged_gaps(draft, gaps)
    failures = []
    if missing_sections:
        failures.append(f"missing required sections: {missing_sections}")
    if wrote_sources:
        failures.append("draft wrote its own Sources section (the system appends it)")
    if not used:
        failures.append("no citations at all")
    if phantom:
        failures.append(f"citation numbers not in the evidence list: {phantom}")
    if uncited:
        failures.append(f"paragraphs without citations: {uncited}")
    if unverified:
        # BLOCKING. A number that traces to no citation and no platform-computed
        # figure is the failure this platform exists to prevent: it reads as
        # authoritative, it is precise, it carries a citation marker, and someone
        # allocates flood response with it. Recording it and passing anyway put a
        # PASSED badge above two fabricated figures in a real brief.
        failures.append(
            "numbers that appear in no citation and in no platform-computed figure: "
            f"{unverified} — quote the platform's own figure, cite a source that "
            "states it, or remove the number. If it is a share of a cited total, "
            "state both figures so it can be checked.")
    return {"passed": not failures, "failures": failures, "cited": sorted(used),
            "phantom_citations": phantom, "missing_sections": missing_sections,
            "uncited_paragraphs": uncited, "numbers_unverified": unverified,
            "numbers_derived": derived,
            # WARNING, not a failure: the figure is in the pack but not in the
            # citation the paragraph points at.
            "numbers_attributed_elsewhere": misattributed,
            "local_claims_on_driver_evidence": driver_only,
            "numbers_index_scale_unverified": index_scale,
            "gaps_not_acknowledged": unowned_gaps,
            # One place a caller can look for everything that did NOT block. These
            # are the checks queued for promotion once their false-alarm rate is
            # measured; keeping them in one list makes that promotion one edit.
            "warnings": ([f"claim about an uncovered target: {w}" for w in unowned_gaps]
                         + [f"climate-index figure in no citation: {n}" for n in index_scale]
                         + [f"local claim resting on driver evidence: {d}"
                            for d in (driver_only or [])]
                         + [f"number attributed to a citation that lacks it: {m}"
                            for m in (misattributed or [])])}


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
    _CONTEXT_ONLY = ("calendar", "index", "gaps")
    if not any(c["kind"] not in _CONTEXT_ONLY for c in citations):
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
        check = check_grounded(draft, citations)
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
