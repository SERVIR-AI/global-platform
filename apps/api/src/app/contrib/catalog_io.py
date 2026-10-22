"""The machine-owned files the gate rewrites (conf/*.contrib.yml, conf/risk_l2.contrib.yml,
the contributed feed and table YAMLs): written whole, one writer at a time.

Each landing, correction and removal reads a file, changes it and writes it back. Two
approvals at once each read the old file and the second write dropped the first one's
row; a reader between open and write found the file empty. `LOCK` covers the whole
read-change-write (it is re-entrant: removing a stack removes its years), and
`write_text` replaces the file in one rename."""

import fcntl
import functools
import os
import threading
from pathlib import Path


class _Lock:
    """Re-entrant across threads AND across worker processes. The thread lock serialises
    the threads of one process; the first holder in a process also takes an exclusive
    file lock next to the catalogs, which serialises the four uvicorn workers. Without
    the file part, two landings seconds apart on different workers each read the old
    file and the second write dropped the first one's row (a contributor lost 1 of 13
    rasters that way on 21 Oct)."""

    def __init__(self):
        self._thread = threading.RLock()
        self._depth = 0
        self._fh = None

    @staticmethod
    def _path() -> Path:
        from ..config import get_settings
        d = Path(get_settings().tiffs_contrib_path).parent
        d.mkdir(parents=True, exist_ok=True)
        return d / ".contrib.lock"

    def __enter__(self):
        self._thread.acquire()
        if self._depth == 0:
            self._fh = open(self._path(), "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        self._depth += 1
        return self

    def _is_owned(self) -> bool:
        """True when the calling thread holds the lock (what a test's spy checks)."""
        return self._thread._is_owned()

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0 and self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
        self._thread.release()
        return False


LOCK = _Lock()


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
