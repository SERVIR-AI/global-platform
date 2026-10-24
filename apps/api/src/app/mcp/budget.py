"""Context budgets as data: what the strictest known MCP client (Claude Code, docs for
v2.1.309) keeps of what this server sends. Every shaped surface measures against these;
an overrun is logged, never silent."""

from __future__ import annotations

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
# Chars a derived name list may take inside a generated text, tried in order by fit() until
# the text fits its budget (None: every name). Contributions grow the lists, not the rules.
CAPS: tuple[int | None, ...] = (None, 600, 300, 120, 0)

_CAP: ContextVar[int | None] = ContextVar("names_cap", default=None)


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
def capped(chars: int | None):
    """Inside: every names() list keeps what fits `chars` and counts the rest."""
    tok = _CAP.set(chars)
    try:
        yield
    finally:
        _CAP.reset(tok)


def current_cap() -> int | None:
    return _CAP.get()


def names(items: Iterable[Any], sep: str = ", ", last: str | None = None,
          more: str = "more") -> str:
    """A derived list in words — whole, or under capped() the names that fit and a count
    ('a, b and 3 more'). `last` joins the final pair of a whole list ('a, b and c')."""
    items = [str(i) for i in items if str(i)]
    cap = _CAP.get()
    if not items:
        return ""
    whole = (sep.join(items[:-1]) + last + items[-1]) if last and len(items) > 1 else sep.join(items)
    if cap is None or len(whole) <= cap:
        return whole
    kept, used = [], 0
    for it in items:
        add = len(it) + (len(sep) if kept else 0)
        if used + add > cap:
            break
        kept.append(it)
        used += add
    rest = len(items) - len(kept)
    return (sep.join(kept) + f" and {rest} {more}") if kept else f"{rest} {more}"


def fit(kind: str, build: Callable[[], str], limit: int) -> str:
    """The first build() under CAPS whose text fits `limit`; the tightest one, logged, when
    none does. A generated text keeps every name while it has room."""
    text = ""
    for cap in CAPS:
        with capped(cap):
            text = build()
        if len(text) <= limit:
            return text
    check(kind, text, limit)
    return text
