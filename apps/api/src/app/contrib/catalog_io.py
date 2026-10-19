"""The machine-owned files the gate rewrites (conf/*.contrib.yml, conf/risk_l2.contrib.yml,
the contributed feed and table YAMLs): written whole, one writer at a time.

Each landing, correction and removal reads a file, changes it and writes it back. Two
approvals at once each read the old file and the second write dropped the first one's
row; a reader between open and write found the file empty. `LOCK` covers the whole
read-change-write (it is re-entrant: removing a stack removes its years), and
`write_text` replaces the file in one rename."""

import functools
import os
import threading
from pathlib import Path

LOCK = threading.RLock()


def write_text(path, text: str) -> None:
    from ..graph.geo.ingest import atomic_path
    with LOCK, atomic_path(os.fspath(path)) as tmp:
        Path(tmp).write_text(text, encoding="utf-8")


def serialised(fn):
    """Run `fn` holding LOCK: its reads and writes of the catalog files are one step."""
    @functools.wraps(fn)
    def inner(*a, **k):
        with LOCK:
            return fn(*a, **k)
    return inner
