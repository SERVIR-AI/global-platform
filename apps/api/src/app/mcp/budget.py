"""Context budgets as data: what the strictest known MCP client (Claude Code, docs for
v2.1.309) keeps of what this server sends. Every shaped surface measures against these;
an overrun is logged, never silent."""

from __future__ import annotations

import functools
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

# Server instructions kept since Claude Code v2.1.296 (chars).
INSTRUCTIONS = 4096
# Server instructions kept before v2.1.296: the core rules must still fit here.
INSTRUCTIONS_CORE = 2048
# A tool description loaded upfront; tool search matches only this much of one.
TOOL_DESCRIPTION = 4096
# A tool description loaded through tool search (on by default).
# A text reply over 50,000 chars is saved to a file and the model sees a 2 KB preview; 20% margin.
REPLY_CHARS = 40000
# An error reply over ~11,000 chars loses its middle.
ERROR_CHARS = 10000
# A contextual line riding a reply (the general rule lives in the instructions and the tool
# descriptions, not in every result).
NOTE_CHARS = 400
# Chars a derived list may take inside one section of a reply (coverage's raster layers): past
# it, a count and the section that lists every one.
LIST_CHARS = 6000
# How long a derived name list keeps its names as fit() tightens a text, by what a client
# routes on: PLACE (countries, regions) longest, then ROUTE (products, hazards, subjects), LIST
# (layers, assets, classes), FEED (feed titles), SOURCE (publishers) first. Contributions grow
# lists, not rules.
ROUTE, LIST, FEED, SOURCE, PLACE = 0, 1, 2, 3, 4
FIRST_TO_GO = (SOURCE, FEED, LIST, ROUTE, PLACE)
# The names a list of each rank keeps however tight the text: a bare count routes nothing.
KEEP = {ROUTE: 2, PLACE: 2}
# fit() settles a rank's cap to within this many chars of the most that fits.
GRAIN = 16
# A connect-time rebuild slower than this is logged as a warning (ms).
REBUILD_MS = 1000

_CAP: ContextVar[dict | None] = ContextVar("names_cap", default=None)
_READ: ContextVar[dict | None] = ContextVar("ranks_read", default=None)
_MEMO: ContextVar[dict | None] = ContextVar("rebuild_memo", default=None)
# Every source read once per snapshot() (registered by once()), for the test that counts them.
SOURCES: list = []


def serialise(obj: Any) -> str:
    """The text a client reads for a result: a string as is, a dict as fastmcp's own
    serialiser writes it (compact JSON, non-ASCII kept) — json.dumps' default
    separators over-count by ~4%."""
    if isinstance(obj, str):
        return obj
    try:
        from fastmcp.tools.base import default_serializer
        return default_serializer(obj)
    except Exception:
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def measure(obj: Any) -> int:
    return len(serialise(obj))


def fits(obj: Any, limit: int) -> bool:
    return measure(obj) <= limit


def check(kind: str, obj: Any, limit: int) -> int:
    """Measure, and WARN when over: a shaped surface that still overruns is a defect to
    see in the log, not a silent cut at the client."""
    n = measure(obj)
    if n > limit:
        log.warning("context budget: %s is %s chars, over its %s-char budget",
                    kind, f"{n:,}", f"{limit:,}")
    return n


@contextmanager
def capped(chars: int | dict | None):
    """Inside: every names() list keeps what fits its cap and counts the rest — `chars` for
    every rank, or {rank: chars} (a rank left out keeps every name)."""
    tok = _CAP.set(chars if chars is None or isinstance(chars, dict)
                   else {r: chars for r in FIRST_TO_GO})
    try:
        yield
    finally:
        _CAP.reset(tok)


def current_cap(rank: int = LIST, longest: int = 0) -> int | None:
    """The cap on `rank` now, noted for fitted() with the longest whole list it bounds (0: a
    text that only branches on whether there is a cap)."""
    seen = _READ.get()
    if seen is not None:
        seen[rank] = max(seen.get(rank, 0), longest)
    return (_CAP.get() or {}).get(rank)


def names(items: Iterable[Any], sep: str = ", ", last: str | None = None,
          more: str = "more", rank: int = LIST, keep: int = 0,
          counted: str | None = None) -> str:
    """A derived list in words: whole, or under capped() the names that fit its rank's cap and a
    count ('a, b and 3 more'; `counted`, e.g. '{n} feeds', when none fits), the first `keep`
    (at least KEEP[rank]) always. Never a count longer than the list it stands for."""
    items = [str(i) for i in items if str(i)]
    if not items:
        return ""
    whole = (sep.join(items[:-1]) + last + items[-1]) if last and len(items) > 1 else sep.join(items)
    cap = current_cap(rank, len(whole))
    if cap is None or len(whole) <= cap:
        return whole
    keep = max(keep, KEEP.get(rank, 0))
    kept, used = items[:keep], len(sep.join(items[:keep]))
    for it in items[keep:]:
        add = len(it) + (len(sep) if kept else 0)
        if used + add > cap:
            break
        kept.append(it)
        used += add
    rest = len(items) - len(kept)
    if not rest:
        return whole
    if kept:
        out = sep.join(kept) + f" and {rest} {_one(more, rest)}"
    else:
        out = _one(counted.format(n=rest), rest) if counted else f"{rest} {_one(more, rest)}"
    return whole if len(out) > len(whole) else out


def _one(phrase: str, n: int) -> str:
    """`phrase` for a count of `n`: its last noun before any bracket singular when n is 1
    ('more countries' -> 'more country'; '{n} publishers (...)' -> 'publisher (...)')."""
    if n != 1:
        return phrase
    head, br, tail = phrase.partition("(")
    words = head.rstrip().split(" ")
    w = words[-1]
    if w.isalpha() and len(w) > 3 and w.lower().endswith("s") and not w.lower().endswith("ss"):
        words[-1] = (w[:-3] + "y" if w.lower().endswith("ies") else
                     w[:-2] if w.lower().endswith(("sses", "xes", "ches", "shes")) else w[:-1])
    return " ".join(words) + (head[len(head.rstrip()):] + br + tail if br else "")


def fitted(render: Callable[[], Any], fits: Callable[[Any], bool]) -> tuple[Any, bool]:
    """(render() with the most names that fits(), whether it does): every name, else rank by
    rank in FIRST_TO_GO its lists counted and, once that fits, the largest cap that still does
    (bisected to GRAIN); nothing fits: every rank counted."""
    def run(caps):
        seen: dict = {}
        tok = _READ.set(seen)
        try:
            with capped(caps):
                return render(), seen
        finally:
            _READ.reset(tok)
    caps: dict = {}
    out, read = run(caps)
    if fits(out):
        return out, True
    for rank in FIRST_TO_GO:
        if rank not in read:
            continue
        hi = read[rank]                      # this rank's lists whole did not fit
        caps = {**caps, rank: 0}
        out, read = run(caps)
        if not fits(out):
            continue                         # even counted: tighten the next rank too
        lo = 0
        while hi - lo > GRAIN:
            mid = (lo + hi) // 2
            got, _ = run({**caps, rank: mid})
            if fits(got):
                lo, out = mid, got
            else:
                hi = mid
        return out, True
    return out, False


def fit(kind: str, build: Callable[[], str], limit: int) -> str:
    """build() with the most names that fits `limit` (fitted()); the tightest, logged, when
    nothing does. Inside one snapshot(): live state is read once, each try only re-renders."""
    with snapshot():
        text, ok = fitted(build, lambda t: len(t) <= limit)
    if not ok:
        check(kind, text, limit)
    return text


# --------------------------------------------------------------------------- one look per rebuild
@contextmanager
def snapshot():
    """One look at live state: inside, each once() source is read on first use and that value
    reused, cleared on exit. Re-entrant: a snapshot inside one shares it."""
    if _MEMO.get() is not None:
        yield
        return
    tok = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(tok)


def once(fn: Callable) -> Callable:
    """`fn` read once per snapshot() and argument set, its failure too; a plain call outside one.
    A dict or list comes back as a shallow copy, so a reader's edit never reaches the next."""
    @functools.wraps(fn)
    def read(*args, **kwargs):
        memo = _MEMO.get()
        if memo is None:
            return read.__wrapped__(*args, **kwargs)
        key = (read, args, tuple(sorted(kwargs.items())))
        try:
            hit = memo.get(key)
        except TypeError:                          # arguments that cannot be keyed
            return read.__wrapped__(*args, **kwargs)
        if hit is None:
            try:
                hit = memo[key] = (True, read.__wrapped__(*args, **kwargs))
            except Exception as exc:
                hit = memo[key] = (False, exc)
        ok, value = hit
        if not ok:
            raise value
        return value.copy() if isinstance(value, (dict, list)) else value
    SOURCES.append(read)
    return read
