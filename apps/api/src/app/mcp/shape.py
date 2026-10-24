"""Reply shaping at the TOOL boundary (server.py wrappers only): every reply in one declared
priority order; when one is over budget.REPLY_CHARS its lowest-priority bulk spills to an
MCP resource that serves it whole, and a long row list or text pages on with `offset`.
Internal callers never see a shaped reply."""

from __future__ import annotations

import json
from typing import Any

from . import budget

DETAIL = ("concise", "full")

# Key groups in reply order. A key not named here is BULK and keeps its own order after the
# citations; TAIL keys close the reply (the text nearest generation is what gets read last).
PRIORITY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("status", ("status",)),
    ("ids", ("receipt_id", "report_id", "pack_id", "contribution_id", "dataset", "doc_id",
             "composition", "corpus", "pack", "target", "focus", "country", "crop", "question",
             "filter")),
    ("verdict", ("public_resolver", "passed", "answer_status", "grounded", "evidence_tier",
                 "draft_sha256", "map_url", "resolve_with")),
    ("guidance", ("note", "next_step", "for_a_governed_answer", "usage_notes", "no_arithmetic",
                  "not_served", "method_first", "methods_reading", "staged_note", "render_with",
                  "shown", "how_to_use")),
    ("required_sections", ("required_sections",)),
    ("figures", ("brief", "count", "summary", "as_of", "stats", "numbers_unverified_recorded",
                 "failures", "links_not_served", "numbers_unreadable", "phantom_citations",
                 "missing_sections", "uncited_paragraphs", "verified_text", "minted_at",
                 "available", "available_packs", "candidates")),
    ("citations", ("citations", "passport")),
)
TAIL = ("your_next_output",)

PACK = "servirplatform://pack/{pack_id}/{section}"
RECEIPT = "servirplatform://receipt/{receipt_id}/{section}"
CAPABILITIES = "servirplatform://capabilities/{section}"
PLACES = "servirplatform://places"

# Per reply kind: the bulk paths that may spill, LOWEST priority first, each with the resource
# that serves it whole. A dotted path spills one part of a nested section; `<section>.*` spills
# any part no entry names (a part added later spills too). `citations` is compacted to its
# drafting essentials; `citations.text` then cuts each text to its share of the room left; an
# entry after that spills only when the reply then fits (a spill that gains nothing loses data).
SPILL: dict[str, tuple[tuple[str, str], ...]] = {
    "pack": (("exec", PACK), ("trace", PACK), ("queries", PACK), ("layers", PACK),
             ("vectors", PACK), ("methods", PACK), ("gaps", PACK), ("citations", PACK),
             ("citations.text", PACK), ("stats", PACK)),
    # The evidence panel (MCP Apps) draws sources, gaps and insight.map/layers/vectors from this
    # reply: those go last, and the panel fetches a spilled part back (record_receipt full).
    "receipt": (("insight.*", RECEIPT), ("queries", RECEIPT), ("claim_scope", RECEIPT),
                ("insight.context", RECEIPT), ("insight.prepared", RECEIPT),
                ("insight.series", RECEIPT), ("evidence_freshness", RECEIPT),
                ("insight.places", RECEIPT), ("sources", RECEIPT), ("gaps", RECEIPT),
                ("insight.vectors", RECEIPT), ("insight.layers", RECEIPT),
                ("insight.map", RECEIPT)),
    "capabilities": (("compositions", CAPABILITIES), ("domain_packs", CAPABILITIES),
                     ("feeds", CAPABILITIES), ("staged_feeds", CAPABILITIES),
                     ("design_language", CAPABILITIES), ("bones", CAPABILITIES),
                     ("coverage", CAPABILITIES), ("methods", CAPABILITIES)),
    "places": (("layers", PLACES),),
}
NEVER = ("brief",)        # a `.*` entry never spills these (publish: the gated text itself)

# Row lists paged to the budget with a steering line naming the next call; `keep` says which
# end survives a cut (feeds return newest last). `call` is the next call ({next}: its offset;
# the tool wrapper passes the real one as `again`), `more` the sentence around it ({call}).
CAP: dict[str, dict[str, str]] = {
    "feed": {"key": "records", "what": "rows", "keep": "tail",
             "more": "narrow with the feed's filters or a bbox, or set `limit` to the rows you need"},
    "search": {"key": "hits", "what": "hits", "keep": "head",
               "call": "corpus_search(<same args>, offset={next})",
               "more": "{call} returns the next ones; or lower `k` or add filters (country, crop, "
                       "temporal, doc_type)"},
    "document": {"key": "inventory", "what": "documents", "keep": "head",
                 "call": "corpus_document(<same pack>, offset={next})",
                 "more": "{call} lists the next ones; a doc_id gives one in full"},
    "review": {"key": "contributions", "what": "contributions", "keep": "head",
               "call": "contribute_review(action='list', <same status_filter>, offset={next})",
               "more": "{call} for the next; contribute_status(contribution_id=<id>) gives one in full"},
}
# A text paged to the budget the same way.
CUT: dict[str, dict[str, str]] = {
    "document": {"key": "extracted_text",
                 "call": "corpus_document(<same doc_id and pack>, offset={next})",
                 "more": "{call} returns the next part; corpus_search returns passages by relevance"},
}
CITATION_ESSENTIALS = ("n", "kind", "title", "source", "pub_date", "validation", "retrieval",
                       "review_status", "usage_notes", "text")
CUT_MARK = " …[cut to fit the reply: whole in citations_full]"
READER = ("read it with your host's MCP resource reader (Claude Code: ReadMcpResourceTool; "
          "Claude Desktop attaches resources)")
# How to get a spilled section back, per reply kind: the resource first, then a call that
# has no side effect where one exists.
HOW = {
    "pack": READER + "; assemble_pack(..., detail='full') instead re-gathers under a NEW pack_id "
                     "and its reply may pass 50,000 chars",
    "receipt": READER + "; or record_receipt(receipt_id='{receipt_id}', detail='full'), which "
                        "returns every section and mints nothing",
    "capabilities": READER + "; or platform_capabilities(section='{section}')",
    "places": READER + "; or resolve_place_time(list=<the same>, detail='full')",
}
_MIN_SPILL = 400          # a pointer is ~230 chars: spilling less saves nothing


def bad_detail(detail: Any) -> dict:
    return {"status": "declined",
            "note": f"detail must be one of {', '.join(DETAIL)} — got {detail!r}"}


def order(reply: dict) -> dict:
    named = [k for _, keys in PRIORITY for k in keys]
    head = {k: reply[k] for k in named if k in reply}
    tail = {k: reply[k] for k in TAIL if k in reply}
    bulk = {k: v for k, v in reply.items() if k not in head and k not in tail}
    return {**head, **bulk, **tail}


def groups(reply: dict) -> list[str]:
    """The priority group of each key in order ('bulk' for unnamed, 'tail' for TAIL)."""
    where = {k: g for g, keys in PRIORITY for k in keys}
    return [where.get(k, "tail" if k in TAIL else "bulk") for k in reply]


def pointer(uri: str, chars: int, how: str = READER) -> dict:
    return {"spilled_to": uri, "chars": chars, "how": how}


def is_pointer(v: Any) -> bool:
    return isinstance(v, dict) and "spilled_to" in v


def _fits(reply: dict) -> bool:
    return budget.fits(reply, budget.REPLY_CHARS)


def _esc_len(text: str) -> int:
    """Chars a text takes inside the serialised reply (escapes counted)."""
    return len(json.dumps(text, ensure_ascii=False)) - 2


def _clip(text: str, chars: int) -> str:
    """The longest start of `text` whose serialised length is at most `chars`."""
    cut = text[:max(0, chars)]
    while cut and _esc_len(cut) > chars:
        cut = cut[:len(cut) - max(1, (_esc_len(cut) - chars) // 2)]
    return cut


def _locate(out: dict, path: str):
    """(parent, key) for a dotted path, or (None, None) when absent. Every dict on the way
    is copied into `out` first, so the caller's reply is never written to."""
    parent: dict = out
    parts = path.split(".")
    for p in parts[:-1]:
        nxt = parent.get(p)
        if not isinstance(nxt, dict) or is_pointer(nxt):
            return None, None
        nxt = dict(nxt)
        parent[p] = nxt
        parent = nxt
    key = parts[-1]
    return (parent, key) if key in parent else (None, None)


def _insert_after(reply: dict, after: str, key: str, value: Any) -> dict:
    out = {}
    for k, v in reply.items():
        out[k] = v
        if k == after:
            out[key] = value
    return out


def _spill_one(out: dict, path: str, template: str, ids: dict, kind: str) -> dict:
    """`out` with the section at `path` replaced by its pointer (citations: compacted, the
    whole list one pointer away) — unchanged when absent, small, or no id to point at."""
    parent, key = _locate(out, path)
    if parent is None or is_pointer(parent[key]):
        return out
    value = parent[key]
    n = budget.measure(value)
    need = [k for k in ids if f"{{{k}}}" in template]
    if n < _MIN_SPILL or any(not ids[k] for k in need):
        return out
    uri = template.format(section=key, **{k: ids[k] for k in need})
    how = HOW.get(kind, READER).format(section=key, **{k: v or "" for k, v in ids.items()})
    if path == "citations":
        parent[key] = [{k: c[k] for k in CITATION_ESSENTIALS if k in c}
                       if isinstance(c, dict) else c for c in value]
        return _insert_after(out, "citations", "citations_full", pointer(uri, n, how))
    parent[key] = pointer(uri, n, how)
    return out


def _water(lengths: list[int], room: int) -> list[int]:
    """Each length's share of `room`: a short one keeps all of it, the rest share what is left."""
    shares = [0] * len(lengths)
    left = max(0, room)
    for k, i in enumerate(sorted(range(len(lengths)), key=lambda i: lengths[i])):
        shares[i] = min(lengths[i], left // (len(lengths) - k))
        left -= shares[i]
    return shares


def _cap_texts(out: dict, compacted: list) -> dict:
    """Each citation's text cut to its share of the room the reply has left, marked; the whole
    texts are at citations_full."""
    idx = [i for i, c in enumerate(compacted) if isinstance(c, dict) and isinstance(c.get("text"), str)]
    if not idx:
        return out
    bare = [{**c, "text": CUT_MARK} if i in idx else c for i, c in enumerate(compacted)]
    room = budget.REPLY_CHARS - budget.measure({**out, "citations": bare})
    lens = [_esc_len(compacted[i]["text"]) for i in idx]
    cits = list(compacted)
    for i, n, share in zip(idx, lens, _water(lens, room + len(idx) * len(CUT_MARK))):
        if n > share:
            cits[i] = {**compacted[i], "text": _clip(compacted[i]["text"],
                                                       share - len(CUT_MARK)) + CUT_MARK}
    return {**out, "citations": cits}


def _spill(reply: dict, kind: str) -> dict:
    ids = {k: reply.get(k) for k in ("pack_id", "receipt_id")}
    entries = SPILL.get(kind, ())
    named = {p for p, _ in entries}
    out = dict(reply)
    compacted = None                      # set once the citation texts have been cut
    for path, template in entries:
        if _fits(out):
            break
        if path.endswith(".*"):
            section = out.get(path[:-2])
            for part in list(section) if isinstance(section, dict) else []:
                if _fits(out):
                    break
                if part not in NEVER and f"{path[:-2]}.{part}" not in named:
                    out = _spill_one(out, f"{path[:-2]}.{part}", template, ids, kind)
            continue
        if path == "citations.text":
            if isinstance(out.get("citations"), list) and "citations_full" in out:
                compacted = out["citations"]
                out = _cap_texts(out, compacted)
            continue
        if compacted is None:
            out = _spill_one(out, path, template, ids, kind)
            continue
        trial = _spill_one(dict(out), path, template, ids, kind)
        trial = _cap_texts(trial, compacted)
        if _fits(trial):
            out = trial
    return out


def _next(spec: dict, again: str | None, nxt: int) -> str:
    """The steering sentence with the next call in it (`again`: the wrapper's real call)."""
    call = (again or spec.get("call") or "").format(next=nxt)
    return spec["more"].replace("{call}", call)


def _steer(shown: int, total: int, what: str, more: str, start: int = 0,
           recent: bool = False) -> str:
    span = (f"{'the most recent ' if recent else ''}{shown:,} of {total:,} {what} shown"
            + (f" (from {start:,})" if start else ""))
    return f"{span} — the reply budget is {budget.REPLY_CHARS:,} chars; {more}"


def _cap(reply: dict, spec: dict, offset: int = 0, again: str | None = None) -> dict:
    """The rows from `offset` that fit the budget — the newest ones for a feed — with a
    steering line naming the next call; `count` then counts the rows in the reply."""
    key, what, tail = spec["key"], spec["what"], spec.get("keep") == "tail"
    rows = reply.get(key)
    if not isinstance(rows, list):
        return reply
    total = len(rows)
    start = 0 if tail else min(max(0, int(offset or 0)), total)
    if not start and _fits(reply):
        return reply
    def more(kept: int) -> str:
        if tail:
            return spec["more"] if kept < total else "that is all of them"
        nxt = start + kept
        return _next(spec, again, nxt) if nxt < total else "that is the last of them"
    longest = spec["more"] if tail else _next(spec, again, total)
    probe = {**reply, key: [], "shown": _steer(total, total, what, longest, total, tail)}
    room = budget.REPLY_CHARS - budget.measure(probe)
    kept: list = []
    used = 0
    for r in (reversed(rows) if tail else rows[start:]):
        n = budget.measure(r) + 1
        if used + n > room:
            break
        kept.append(r)
        used += n
    if tail:
        kept.reverse()
    def build() -> dict:
        out = {**reply, key: kept, "shown": _steer(len(kept), total, what, more(len(kept)),
                                                  start, tail and len(kept) < total)}
        if tail and "count" in out:
            out["count"] = len(kept)
        return order(out)
    out = build()
    while kept and not _fits(out):           # a row's separator or a longer number tipped it
        kept.pop(0 if tail else -1)
        out = build()
    return out


def _cut(reply: dict, spec: dict, offset: int = 0, again: str | None = None) -> dict:
    """The text from `offset` that fits the budget (serialised length), with the next call."""
    key = spec["key"]
    text = reply.get(key)
    if not isinstance(text, str):
        return reply
    total = len(text)
    start = min(max(0, int(offset or 0)), total)
    if not start and _fits(reply):
        return reply

    def note(a: int, b: int) -> str:
        nxt = _next(spec, again, b) if b < total else "that is the end of it"
        return (f"chars {a:,}-{b:,} of {total:,} of {key} shown — the reply budget is "
                f"{budget.REPLY_CHARS:,} chars; {nxt}")
    room = max(0, budget.REPLY_CHARS - budget.measure({**reply, key: "", "shown": note(total, total)}))
    part = _clip(text[start:], room)
    out = order({**reply, key: part, "shown": note(start, start + len(part))})
    over = budget.measure(out) - budget.REPLY_CHARS
    while over > 0 and part:                 # the steering line grew with the numbers in it
        part = _clip(part, _esc_len(part) - over)
        out = order({**reply, key: part, "shown": note(start, start + len(part))})
        over = budget.measure(out) - budget.REPLY_CHARS
    return out


def shape(reply: Any, kind: str | None = None, offset: int = 0, again: str | None = None) -> Any:
    """Order by PRIORITY; page the row list or text a kind names from `offset` (`again`: the
    next call, with {next}); spill the kind's bulk, lowest priority first, until the reply
    fits; log (never swallow) a reply — or a decline — that still does not."""
    if not isinstance(reply, dict):
        return reply
    out = order(reply)
    if kind in CAP:
        out = _cap(out, CAP[kind], offset, again)
    if kind in CUT:
        out = _cut(out, CUT[kind], offset, again)
    if kind in SPILL:
        out = _spill(out, kind)
    if out.get("status") in ("declined", "blocked", "error"):
        budget.check(f"{kind or 'tool'} {out['status']} reply", out, budget.ERROR_CHARS)
    budget.check(f"{kind or 'tool'} reply", out, budget.REPLY_CHARS)
    return out


# --------------------------------------------------------------------------- concise projections
def concise_capabilities(caps: dict) -> dict:
    """The map at a glance: server, contract, usage, METHODS first, tools, builder requirements,
    coverage by name, packs as counts with their manifest resource, feeds and compositions as
    names; any section whole via platform_capabilities(section=...)."""
    feeds = caps.get("feeds") or {}
    available = sorted(k for k, v in feeds.items() if isinstance(v, dict) and v.get("status") == "available")
    pending = sorted(k for k in feeds if k not in available)
    packs = []
    for p in caps.get("domain_packs") or []:
        if not isinstance(p, dict):
            continue
        row = {k: p[k] for k in ("id", "display_name", "version", "status") if k in p}
        if isinstance(p.get("sources"), dict) and "count" in p["sources"]:
            row["sources"] = p["sources"]["count"]
        if isinstance(p.get("gaps"), list):
            row["declared_gaps"] = len(p["gaps"])
        row["manifest"] = f"servirplatform://pack/{p.get('id')}"
        packs.append(row)
    out = {k: caps[k] for k in ("server", "contract", "usage", "methods_note", "methods",
                                "tools_available", "builder_requirements") if k in caps}
    out["sections"] = ("any section whole: platform_capabilities(section=<name>) or "
                       "servirplatform://capabilities/<name> — " + ", ".join(caps)
                       + "; each pack's declared gaps in full: section='domain_packs'")
    if "coverage" in caps:
        out["coverage"] = caps["coverage"]
    out["domain_packs"] = packs
    if "feeds_note" in caps:
        out["feeds_note"] = caps["feeds_note"]
    out["feeds"] = {"available": available, "pending": pending,
                    "briefs": "platform_capabilities(section='feeds') or servirplatform://capabilities/feeds"}
    if caps.get("staged_feeds"):
        out["staged_feeds"] = sorted(caps["staged_feeds"])
    if "compositions" in caps:
        out["compositions"] = sorted(caps["compositions"] or [])
    for k in ("bones", "design_language"):
        if k in caps:
            out[k] = caps[k]
    return out


def complete_places(reply: dict) -> dict:
    """The full listing says it is complete (a model otherwise hedges that more may exist)."""
    if not isinstance(reply.get("layers"), list):
        return reply
    n = sum(len(r.get("names") or []) for r in reply["layers"] if isinstance(r, dict))
    how = str(reply.get("how_to_use") or "").rstrip()
    return {**reply, "how_to_use": (f"{how} Complete: every name in all {len(reply['layers'])} "
                                    f"boundary layer(s), {n} names; the same list: {PLACES}.").strip()}


def concise_places(reply: dict) -> dict:
    """A boundary listing as names grouped by size class per layer, no per-name figures; every
    name with its km² stays at servirplatform://places."""
    if not isinstance(reply.get("layers"), list):
        return reply
    layers = []
    for row in reply["layers"]:
        if not isinstance(row, dict):
            layers.append(row)
            continue
        by: dict[str, list[str]] = {}
        for r in row.get("names") or []:
            label = str(r.get("name")) + (f" ({r['qualifier']})" if r.get("qualifier") else "")
            by.setdefault(str(r.get("size") or "unsized"), []).append(label)
        layers.append({**{k: v for k, v in row.items() if k != "names"}, "names_by_size": by})
    how = str(reply.get("how_to_use") or "").rstrip()
    return {**reply, "layers": layers,
            "how_to_use": f"{how} Every name with its km² and size: {PLACES}.".strip()}
