"""Submissions that outlive the tool call that made them.

An MCP client abandons a tool call at about a minute; a submission can take several
(measured: GBIF points 157 s, a rasterise 159 s, an HDX zip 131 s, a WCS ~60 s).
contribute_submit hands the submission — staging.submit, unchanged — to a background
lane and waits up to grp_prep_wait_seconds. Done by then: its result, exactly as before.
Not done: {status: preparing, contribution_id: <job id>, message}; the job goes on, and
contribute_status(<job id>) says the step it is on, then returns the submission's own
final result (with the real contribution_id). A client that never asks loses nothing:
the submission lands, or is declined, as it would have.

Lanes: a submission that declares a preparation (contrib/prepare), or whose url's shape
alone has the platform prepare it by itself (prepare.detect: a Drive folder, a service's
query), runs in the preparation lane — at most grp_prep_max_jobs at once (a rasterise can
peak near 3 GB), the rest queued in order with their position; any other in the plain
lane (PLAIN_WORKERS at once), so a document never waits behind a rasterise. A preparation
the platform applies by itself only after a decline (prepare.auto: an archive over the
cap read whole, tiles joined) starts in the plain lane: it waits for one of the same
grp_prep_max_jobs slots first (prepare.GATE), so at most that many prepare at once.
Workers are daemon threads outside the tool call's own thread pool: the call returning,
or its client going away, stops nothing. The job runs in a copy of the calling context
with the submitter bound, as the call itself would have.

State: table contribution_jobs in the store the gate already uses (sqlite), so any
thread — or a status call long after — reads it. Only the submitter's identity sees a
job. A job whose process is gone (a restart) answers 'interrupted' — resubmit; finished
jobs are kept KEEP seconds. An identical submission while the first is still working is
that job, not a second one (an agent retrying after its client gave up). The hourly cap
is staging.submit's own, counted once when the submission runs; a status call counts
nothing.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import queue
import re
import sqlite3
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as _Timeout

from ..mcp import store
from . import fetch_policy, identity, prepare, staging

PLAIN_WORKERS = 8
KEEP = 24 * 3600            # seconds a finished job is kept
BEAT = 10                   # seconds between this process's heartbeats on its jobs
STALE = 120                 # a job no process has beaten for this long was interrupted
INSTANCE = os.urandom(8).hex()
UNFINISHED = ("queued", "running")
FIRST_STEP = "checking the submission"
SLOT_STEP = "waiting for a free slot to prepare it in"

_ID = re.compile(r"[0-9a-f]{16}")
_lock = threading.Lock()
_lanes: dict = {}
_live: dict = {}            # job id -> Future, for the jobs this process runs
_started = {"beat": False}


# --------------------------------------------------------------------------- lanes

class _Lane:
    """`size` daemon workers taking jobs in the order they came."""

    def __init__(self, name: str, size: int):
        self.name, self.size = name, max(1, int(size))
        self.q: queue.Queue = queue.Queue()
        self.workers: list = []
        self.lock = threading.Lock()
        self.slots = threading.Semaphore(self.size)   # preparations at once, any lane

    def put(self, fn) -> Future:
        fut: Future = Future()
        self.q.put((fut, fn))
        with self.lock:
            self.workers = [t for t in self.workers if t.is_alive()]
            if len(self.workers) < self.size:
                t = threading.Thread(target=self._work, name=f"grp-{self.name}", daemon=True)
                t.start()
                self.workers.append(t)
        return fut

    def _work(self) -> None:
        while True:
            fut, fn = self.q.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BaseException as exc:          # the waiter re-raises it, as the call would
                fut.set_exception(exc)


def _lane(name: str) -> _Lane:
    from ..config import get_settings
    size = max(1, int(get_settings().grp_prep_max_jobs if name == "prepare" else PLAIN_WORKERS))
    with _lock:
        lane = _lanes.get(name)
        if lane is None or lane.size != size:
            lane = _lanes[name] = _Lane(name, size)
        return lane


def _sorted(kind: str, manifest) -> tuple[str, str]:
    """(the submission's kind as staging.submit will read it, its lane): 'prepare' when
    it declares a preparation, or its url's shape has the platform apply one by itself
    (prepare.detect, nothing fetched) — the lane bounded for memory — else 'plain'."""
    from ..config import get_settings
    try:
        m = manifest
        if isinstance(m, str):
            m, _ = staging._manifest_from_text(m)
        if not isinstance(m, dict):
            return str(kind or ""), "plain"
        k = str(kind or m.get("kind") or "")
        prepares = prepare.requested(k, m) or (get_settings().grp_prep_auto
                                               and prepare.detect(k, m))
        return k, ("prepare" if prepares else "plain")
    except Exception:
        return str(kind or ""), "plain"


# --------------------------------------------------------------------------- the store

_COLUMNS = ("id", "owner_id", "kind", "lane", "digest", "state", "step", "created",
            "started", "finished", "beat", "instance", "pid", "result")


def _connect() -> sqlite3.Connection:
    con = store._connect()
    con.execute("CREATE TABLE IF NOT EXISTS contribution_jobs (id TEXT PRIMARY KEY, "
                "owner_id TEXT NOT NULL, kind TEXT, lane TEXT NOT NULL, digest TEXT, "
                "state TEXT NOT NULL, step TEXT, created REAL NOT NULL, started REAL, "
                "finished REAL, beat REAL, instance TEXT, pid INTEGER, result TEXT)")
    return con


def _write(sql: str, args=(), tries: int = 1) -> None:
    for n in range(tries):
        try:
            con = _connect()
            try:
                with con:
                    con.execute(sql, args)
            finally:
                con.close()
            return
        except sqlite3.OperationalError:
            if n == tries - 1:
                raise
            time.sleep(0.5)


def _row(ident: str) -> dict | None:
    """The job's row, or None — read only: a status call never creates the table."""
    con = store._connect()
    try:
        r = con.execute(f"SELECT {', '.join(_COLUMNS)} FROM contribution_jobs WHERE id = ?",
                        (ident,)).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    finally:
        con.close()
    return dict(zip(_COLUMNS, r)) if r else None


def _set_step(ident: str, step: str) -> None:
    try:
        _write("UPDATE contribution_jobs SET step = ?, beat = ? WHERE id = ?",
               (str(step), time.time(), ident))
    except sqlite3.Error:
        pass                                  # a progress report never stops the work


def _finish(ident: str, result: dict) -> None:
    now = time.time()
    try:
        _write("UPDATE contribution_jobs SET state = 'done', finished = ?, beat = ?, result = ? "
               "WHERE id = ?", (now, now, json.dumps(result, default=str), ident), tries=5)
    except sqlite3.Error:
        pass                # the submission stands; its own record says what became of it


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, TypeError, ValueError):
        return True
    return True


def _gone(row: dict) -> bool:
    """An unfinished job whose process is not running it any more."""
    if row["instance"] == INSTANCE:
        return False
    return (row["pid"] == os.getpid() or not _alive(row["pid"])
            or time.time() - float(row["beat"] or row["created"]) > STALE)


def _interrupt(row: dict) -> dict:
    now = time.time()
    _write("UPDATE contribution_jobs SET state = 'interrupted', finished = ? WHERE id = ? "
           "AND state IN ('queued', 'running')", (now, row["id"]))
    return {**row, "state": "interrupted", "finished": now}


def recover() -> int:
    """Jobs a stopped process left unfinished, marked interrupted; finished jobs older
    than KEEP removed. Returns how many were interrupted."""
    con = _connect()
    try:
        rows = [dict(zip(_COLUMNS, r)) for r in con.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM contribution_jobs "
            "WHERE state IN ('queued', 'running')")]
        with con:
            con.execute("DELETE FROM contribution_jobs WHERE state NOT IN ('queued', 'running') "
                        "AND COALESCE(finished, created) < ?", (time.time() - KEEP,))
    finally:
        con.close()
    n = 0
    for row in rows:
        if _gone(row):
            _interrupt(row)
            n += 1
    return n


def _beat() -> None:
    while True:
        time.sleep(BEAT)
        with _lock:
            busy = bool(_live)
        if busy:
            try:
                _write("UPDATE contribution_jobs SET beat = ? WHERE instance = ? "
                       "AND state IN ('queued', 'running')", (time.time(), INSTANCE))
            except sqlite3.Error:
                pass


def _start_beating() -> None:
    with _lock:
        if _started["beat"]:
            return
        _started["beat"] = True
    threading.Thread(target=_beat, name="grp-jobs-beat", daemon=True).start()


# --------------------------------------------------------------------------- replies

def _step_words(step: str | None) -> str:
    step = step or FIRST_STEP
    if step == "fetch":
        return "fetching the source"
    return step if " " in step else f"the {step} step"


def _position(row: dict) -> int:
    con = _connect()
    try:
        ahead = con.execute("SELECT COUNT(*) FROM contribution_jobs WHERE lane = ? AND "
                            "instance = ? AND state = 'queued' AND created < ?",
                            (row["lane"], row["instance"], row["created"])).fetchone()[0]
    finally:
        con.close()
    return int(ahead) + 1


def _reply(row: dict) -> dict:
    """What the submitter is told about the job in `row`."""
    if row["state"] == "done":
        return json.loads(row["result"])
    if row["state"] == "interrupted":
        return {"status": "interrupted", "contribution_id": row["id"], "kind": row["kind"],
                "message": "interrupted — the platform restarted before it finished preparing "
                           "this; resubmit the same manifest with contribute_submit"}
    elapsed = max(0, int(time.time() - float(row["created"])))
    out = {"status": "preparing", "contribution_id": row["id"], "kind": row["kind"]}
    if row["state"] == "queued":
        pos = _position(row)
        step = f"waiting for a free slot, position {pos} in the queue"
        out["queue_position"] = pos
    else:
        step = _step_words(row["step"])
    out.update({"step": step, "elapsed_s": elapsed,
                "message": f"the platform is still preparing this ({step}, {elapsed} s so far); "
                           f"call contribute_status with contribution_id {row['id']} in about a "
                           "minute — do not resubmit"})
    return out


# --------------------------------------------------------------------------- the API

def _run(ident: str, kind: str, manifest, caller: identity.Caller):
    """The submission itself, in a lane worker, inside a copy of the caller's context."""
    try:
        _write("UPDATE contribution_jobs SET state = 'running', started = ?, beat = ?, step = ? "
               "WHERE id = ?", (time.time(), time.time(), FIRST_STEP, ident), tries=5)
    except sqlite3.Error:
        pass
    prepare.PROGRESS.set(lambda step: _set_step(ident, step))
    held: list = []

    def gate() -> None:
        """One of the preparation slots, this job's own from its first preparation until
        it ends (the prepared file is still read and checked after the steps)."""
        if held:
            return
        slots = _lane("prepare").slots
        if not slots.acquire(blocking=False):
            _set_step(ident, SLOT_STEP)
            slots.acquire()
        held.append(slots)

    prepare.GATE.set(gate)
    fetch_policy.PATIENT.set(True)       # nobody is waiting on this connection now
    token = identity.bind(caller)
    try:
        out = staging.submit(kind, manifest, caller)
    except Exception as exc:
        _finish(ident, {"status": "declined", "kind": kind, "problems": [
            f"the platform failed while working on this: {type(exc).__name__}: {exc}"]})
        raise
    finally:
        identity.unbind(token)
        for slots in held:
            slots.release()
    _finish(ident, out)
    return out


def _wait(ident: str, fut: Future) -> dict:
    from ..config import get_settings
    try:
        return fut.result(timeout=max(0.0, float(get_settings().grp_prep_wait_seconds)))
    except _Timeout:
        row = _row(ident)
        return _reply(row) if row else {"status": "preparing", "contribution_id": ident}


def submit(kind: str, manifest, caller: identity.Caller | None = None) -> dict:
    """staging.submit in the background: its result when it is done within
    grp_prep_wait_seconds, else {status: preparing, contribution_id: <job id>, ...}."""
    caller = caller or identity.current()
    digest = hashlib.sha256(json.dumps([kind, manifest], sort_keys=True, default=str)
                            .encode()).hexdigest()
    try:
        recover()
        con = _connect()
        try:
            same = con.execute("SELECT id FROM contribution_jobs WHERE owner_id = ? AND "
                               "digest = ? AND state IN ('queued', 'running') AND instance = ? "
                               "ORDER BY created DESC LIMIT 1",
                               (caller.id, digest, INSTANCE)).fetchone()
        finally:
            con.close()
        with _lock:
            fut = _live.get(same[0]) if same else None
        if fut is not None:                    # the same submission, still working
            return _wait(same[0], fut)
        said, lane = _sorted(kind, manifest)
        ident = os.urandom(8).hex()
        now = time.time()
        _write("INSERT INTO contribution_jobs (id, owner_id, kind, lane, digest, state, step, "
               "created, beat, instance, pid) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
               (ident, caller.id, said, lane, digest, FIRST_STEP, now, now, INSTANCE,
                os.getpid()))
    except sqlite3.Error:
        # the store is unreachable: submit in this call, exactly as before jobs existed
        return staging.submit(kind, manifest, caller)
    _start_beating()
    ctx = contextvars.copy_context()
    fut = _lane(lane).put(lambda: ctx.run(_run, ident, kind, manifest, caller))
    with _lock:
        _live[ident] = fut
    fut.add_done_callback(lambda _f: _forget(ident))
    return _wait(ident, fut)


def _forget(ident: str) -> None:
    with _lock:
        _live.pop(ident, None)


WORKING_SHOWN = 20


def working(caller: identity.Caller | None = None) -> list[dict]:
    """The caller's submissions still preparing in the background, or interrupted before
    they finished, newest first, a line each — read only: no jobs table, no jobs."""
    caller = caller or identity.current()
    try:
        con = store._connect()
        try:
            rows = [dict(zip(_COLUMNS, r)) for r in con.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM contribution_jobs WHERE owner_id = ? AND "
                "state IN ('queued', 'running', 'interrupted') ORDER BY created DESC LIMIT ?",
                (caller.id, WORKING_SHOWN))]
        finally:
            con.close()
    except sqlite3.Error:                     # "no such table": nothing was ever prepared
        return []
    out = []
    for row in rows:
        if not caller.owns(row["owner_id"]):
            continue
        if row["state"] in UNFINISHED and _gone(row):
            row = _interrupt(row)
        said = _reply(row)
        out.append({k: said[k] for k in ("contribution_id", "kind", "status", "step", "elapsed_s")
                    if k in said})
    return out


def lookup(ident: str | None, caller: identity.Caller | None = None) -> dict | None:
    """The job `ident` as its submitter sees it — preparing, interrupted, or the
    submission's final result — or None: no such job, or the caller did not submit it."""
    if not ident or not _ID.fullmatch(str(ident)):
        return None
    caller = caller or identity.current()
    try:
        row = _row(str(ident))
    except sqlite3.Error:
        return None
    if row is None or caller.id != row["owner_id"]:
        return None
    if row["state"] in UNFINISHED and _gone(row):
        row = _interrupt(row)
    return _reply(row)
