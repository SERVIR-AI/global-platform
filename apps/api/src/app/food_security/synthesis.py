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
- The Season timing caveat MUST be based on the crop-calendar evidence entry (when present) and the asked-in month; if that entry is marked ADJUSTED, the caveat must say the calendar was adjusted by the requester.
- The "Known gaps" in the request MUST be reflected in the What's missing section.
- If the evidence cannot support an answer, write no sections; reply with one paragraph starting "DECLINE:" naming exactly what is missing.

Write EXACTLY these markdown sections and nothing else:
""" + "\n".join(SECTIONS) + """

Do not write a Sources section — the system appends it."""

_CITE_GROUP = re.compile(r"\[([\d\s,–\-]+)\]")   # [3], [1, 9], [1-3], [2016]

# Thousands separators: a comma OR space between a digit and a 3-digit group
# (chained) — "800 000"/"800,000" are one number. UN/African docs use spaces,
# so a comma-only strip false-flagged legitimate figures as unverified.
_THOUSANDS = re.compile(r"(?<=\d)[ ,](?=\d{3}(?:[ ,]\d{3})*(?!\d))")


def _norm_nums(text: str) -> str:
    return _THOUSANDS.sub("", text)


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
    return {"kind": "conditions", "source": "GEOGLAM Crop Monitor (CMET)",
            "title": f"{country or 'Global'} {crop or 'crop'} conditions".strip(),
            "pub_date": res["as_of"], "validation": "multi-agency-consensus",
            "url": res["query"]["url"], "query": res["query"]["where"],
            "stale_data": stale, "text": text}, None


def gather_evidence(parsed, trace, calendar_override=None, calendar_target=(None, None)):
    """Deterministic evidence assembly: two retrieval slices + the conditions feed
    + the crop calendar. Returns (citations, gaps, stats), citations numbered.
    A calendar override made for one country/crop is never silently applied to
    another — a mismatch drops it and declares the drop as a gap."""
    corpus = Corpus(CORPUS)
    crop, country, focus = parsed["crop"], parsed["country"], parsed["focus"]
    gaps = []
    q_now = f"El Nino seasonal rainfall forecast outlook {country} {crop} {focus}".strip()
    q_past = (f"impact of past El Nino events on {crop or 'crop'} production and "
              f"food security in {country}").strip()
    forecast_hits = corpus.search(q_now, k=5, temporal="forecast")
    retro_hits = corpus.search(q_past, k=5, temporal="retrospective")
    trace.append(f"retrieve[forecast] {q_now!r} -> {len(forecast_hits)} hits")
    trace.append(f"retrieve[retrospective] {q_past!r} -> {len(retro_hits)} hits")
    if not forecast_hits:
        gaps.append("no forecast/outlook document in the library matched the question")
    if not retro_hits:
        gaps.append("no analog-year/retrospective document in the library matched the question")

    citations = []
    for h in forecast_hits + retro_hits:
        m = h["metadata"]
        citations.append({
            "kind": "document", "source": m.get("source"), "title": m.get("title"),
            "pub_date": m.get("pub_date"), "validation": m.get("validation"),
            "temporal": m.get("temporal"), "url": m.get("url"), "score": h["score"],
            "doc_id": h["doc_id"], "chunk_id": h["id"],
            "archived_copy": (f"/api/food-security/rag/document/{h['doc_id']}"
                              if corpus.raw_path(h["doc_id"]) else None),
            "text": h["text"]})
    cond, gap = _conditions_citation(crop, country, trace)
    if cond:
        citations.append(cond)
    else:
        gaps.append(gap)
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
             "conditions": cond is not None,
             "calendar": (cal or {}).get("adjusted") is not None and (
                 "adjusted" if (cal or {}).get("adjusted") else "default"),
             # the literal retrieval queries — provenance for the retrieval step itself
             "queries": {"forecast": q_now, "retrospective": q_past}}
    return citations, gaps, stats


def _render_pack(citations):
    """The numbered evidence block: documents via the source_block seam (E3),
    the conditions feed in the same shape."""
    doc_like = [{"metadata": {k: c.get(k) for k in
                              ("source", "title", "pub_date", "validation", "url")},
                 "text": (f"[temporal={c['temporal']}] {c['text']}"
                          if c.get("temporal") else c["text"])}
                for c in citations]
    return source_block(doc_like)


def check_grounded(draft, citations):
    """Blocking: required sections present, no model-written Sources, citations
    resolve, every paragraph cites. Recorded (not yet blocking): numbers absent
    from the evidence (whole-token compare over chunk text + citation metadata)."""
    valid = {c["n"] for c in citations}
    used = _cited_numbers(draft)
    phantom = sorted(used - valid)
    missing_sections = [s for s in SECTIONS if s not in draft]
    wrote_sources = "## Sources" in draft
    paragraphs = []                    # header lines don't shield the prose under them
    for block in draft.split("\n\n"):
        body = "\n".join(l for l in block.splitlines()
                         if not l.strip().startswith("#")).strip()
        if body:
            paragraphs.append(body)
    uncited = [p[:70] for p in paragraphs if not _CITE_GROUP.search(p)]
    evid_blob = _norm_nums(" ".join(" ".join(str(c.get(k) or "") for k in
                                  ("text", "pub_date", "title", "source"))
                         for c in citations))
    evid_nums = set(re.findall(r"\d+(?:\.\d+)?", evid_blob))
    draft_nums = set(re.findall(r"\d+(?:\.\d+)?",
                                _norm_nums(_CITE_GROUP.sub("", draft))))
    unverified = sorted(draft_nums - evid_nums)
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
    return {"passed": not failures, "failures": failures, "cited": sorted(used),
            "phantom_citations": phantom, "missing_sections": missing_sections,
            "uncited_paragraphs": uncited, "numbers_unverified": unverified}


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
    # A calendar is context, not evidence — it cannot carry a brief alone.
    if not any(c["kind"] != "calendar" for c in citations):
        trace.append("evidence -> empty; declining without a synthesis call")
        return _declined(
            "No evidence available: " + "; ".join(gaps), trace=trace, usage=usage,
            stats=stats) | {"provider": provider, "model": model}

    asked_on = datetime.now(timezone.utc).strftime("%B %Y")
    user_msg = (
        f"Question: {question}\n"
        f"Asked in: {asked_on} (state the season-timing caveat relative to this)\n"
        f"Parsed target: crop={parsed['crop'] or '?'}, country={parsed['country'] or '?'}, "
        f"focus={parsed['focus']}\n"
        "Known gaps (must appear in What's missing): "
        + ("; ".join(gaps) if gaps else "none identified") + "\n\n"
        "Numbered evidence (the ONLY permissible sources):\n\n" + _render_pack(citations))

    messages = [{"role": "system", "content": _SYNTH_SYSTEM},
                {"role": "user", "content": user_msg}]
    check = None
    for attempt in (1, 2):
        resp = client.chat.completions.create(model=model, max_tokens=1500,
                                              messages=messages)
        usage.append(_usage(resp))
        draft = (resp.choices[0].message.content or "").strip()
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
