"""Preparation on the server: what a contributor would otherwise do to a file by hand
before it goes in (recode a class map, assemble a shapefile's parts, mosaic tiles, ...),
DECLARED in the manifest and done by the server. The contributor's agent computes
nothing: it fills a field, asking the person only for a real choice (an area, which code
means what); the server does the work, and the record says exactly what it did.

An op is a module in this package; the registry is derived from the modules, so a new op
is a new file and nothing else. A module is an op when it declares:

    NAME      the op's name, as provenance records it ("recode")
    STEP      where it runs, one of ORDER's keys (assemble -> mosaic -> convert -> recode
              -> clip); ops run in that order, then by NAME
    KINDS     the contribution kinds it prepares, e.g. ("raster",)
    FIELDS    {manifest field: the form text the contributor's agent reads} — the fields
              it owns; any of them present in a manifest asks for the op
    validate(manifest) -> [problem, ...]   checked before anything is fetched
    run(ctx) -> PreparedFile               does the work (see Context)
    suggest(situation) -> str | None       what to propose when the gate declines in a
                                           situation this op resolves (see SITUATIONS)
and optionally
    NEEDS_INPUT  False when the op makes its own input (an assemble step that fetches
                 several files); the framework then does not fetch the manifest's url first
    CELLWISE     True when the op changes each cell on its own (a recode): the result is the
                 same before or after a clip, so a raster with a `clip` is cut at the source
                 first (range reads, nothing else downloaded) and the op runs on the clip
    answers(manifest, problem) -> bool
                 True for a refusal of the manifest's url that the op answers for when it
                 is asked for (from_folder lists a Drive FOLDER link itself)
    detect(kind, manifest) -> {"fields": {...}, "why": "<words>"} | None
                 what the url's SHAPE alone says this op should do, before anything is
                 fetched, when no person's choice is involved (a Drive FOLDER link for a
                 vector: from_folder: true)
    auto(situation) -> {"fields": {...}, "why": "<words>"} | None
                 after a decline: the fields that resolve its situation when no person's
                 choice is involved (the zip's rasters are named tiles and a clip is given:
                 mosaic: 'auto'); a choice that is a person's stays a question (suggest).
                 A proposal may name `restates`: fields the contributor gave that it states
                 again as the prepared file lands (the stack's legend after a recode, the
                 API's own query url); their given values are kept on the record

Opt-in: a manifest with none of the fields takes the existing path unchanged — unless
an op's detect or auto applies one, which the platform does BY ITSELF only when nothing
in it is a person's choice: the reply says what it applied and why, the stored manifest
holds the fields (a receipt replays exactly) and the record marks them as the platform's
(Auto; settings.grp_prep_auto turns it off). A field the contributor gave is never
overridden. What detect applied, refused before it made its file (a service that will
not page), falls back to the manifest as given: the path it took before.

Budgets: an input downloaded whole is STREAMED TO DISK into a per-contribution directory
under the cache, removed when the preparation ends whatever happens; inputs total at
most grp_prep_max_input_bytes. Each step has grp_prep_op_seconds of wall clock; a raster
step may touch grp_prep_max_cells cells, a vector step grp_prep_max_vertices vertices.
The contribution cap (grp_contrib_max_bytes) applies to what is STORED, after the gate.
Every fetch goes through fetch_policy (each hop checked and pinned, no private host);
every GDAL open of contributed bytes runs under rasters.gdal_env with the one driver
rasters.driver_for names; a zip is extracted by Context.unzip, which refuses path
traversal, links and zip bombs.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import importlib
import json
import pkgutil
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ORDER = {"assemble": 10, "mosaic": 20, "convert": 30, "recode": 40, "clip": 50}

# The situations a decline raises, each a dict with "event", "kind" and "manifest":
#   stack_legend_mismatch  + stack, have (the stack's {code: label}), want (the manifest's)
#   duplicate_labels       + legend (the manifest's {code: label})
#   class_range            + classes ([lo, hi], the namespace's class contract), declared
#   url_refused            + url, problems (fetch_policy.check_url's words)
#   shapefile_part         + url, part (which part it is)
#   zip_of_rasters         + url, rasters (the zip's raster members), raised in remote_clip
#   vector_unreadable      + url, error (graph/geo/vecio's words: a truncated export, ...),
#                            head (the file's first bytes)
#   not_features           + url, problem (vectors.inspect's words), raw (the bytes fetched)
#   vector_file_too_large  + url, bytes (the file's declared size, or None), cap: a vector
#                            file over the cap as a download, before any preparation
#   vector_too_large       + url, bytes (the prepared layer's size), cap, geometry (its
#                            family when it was read: points, lines, polygons)
#   declined               + problem (the words of any decline after the fetch, every kind)
SITUATIONS = ("stack_legend_mismatch", "duplicate_labels", "class_range", "url_refused",
              "shapefile_part", "zip_of_rasters", "vector_unreadable", "vector_file_too_large",
              "vector_too_large", "not_features", "declined")
MAX_ROUNDS = 3               # attempts made again with what auto() proposes, at most

ZIP_MEMBERS = 10_000         # files one zip may hold to be extracted
ZIP_RATIO = 200              # uncompressed / compressed, for a member over ZIP_RATIO_FROM
ZIP_RATIO_FROM = 1 << 20
DISK_RESERVE = 1 << 30       # free disk a preparation never eats into
STALE_SECONDS = 6 * 3600     # a directory a crashed worker left behind
# Who hears the step a preparation is on: a callable taking the step's name, set by
# contrib/jobs.py for the thread running a submission (None: nobody asks).
PROGRESS: contextvars.ContextVar = contextvars.ContextVar("grp_prep_progress", default=None)
# What a preparation waits for before it starts: a callable returning once one of the
# preparation slots is the submission's own, set by contrib/jobs.py — a preparation the
# platform applies by itself may start in a lane not bounded for one (None: no wait).
GATE: contextvars.ContextVar = contextvars.ContextVar("grp_prep_gate", default=None)


def _tell(step: str) -> None:
    hook = PROGRESS.get()
    if hook is not None:
        try:
            hook(step)
        except Exception:                    # a progress report never breaks a step
            pass


class PrepRefused(ValueError):
    """A preparation that cannot run, in words its contributor can act on."""


@dataclass
class PreparedFile:
    """What an op returns: the file it made (inside ctx.workdir), what it did in words
    (`notes`, every pack reads them as 'At ingest: ...'), `provenance` — {"inputs":
    {urls, members, parameters}} as the record keeps them, so a receipt can replay the
    step; the framework adds op, sha256 and bytes — and `kind_hint` when the op changed
    what the file is ("raster" from a vector), checked against the contribution's kind."""
    path: str
    notes: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    kind_hint: str | None = None


@dataclass
class Prepared:
    """The prepared file handed to the kind's existing path, as if it had been fetched."""
    path: str
    filename: str | None
    notes: list
    fetched: dict


def size_text(n) -> str:
    from ..remote_clip import size_text as _size
    return _size(n)


# --------------------------------------------------------------------------- registry

@functools.lru_cache(maxsize=1)
def _modules() -> tuple:
    found = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if getattr(mod, "NAME", None) and callable(getattr(mod, "run", None)):
            if getattr(mod, "STEP", None) not in ORDER:
                raise RuntimeError(f"prepare op {mod.NAME!r}: STEP must be one of {list(ORDER)}")
            found.append(mod)
    return tuple(sorted(found, key=lambda m: (ORDER[m.STEP], m.NAME)))


def ops(kind: str | None = None) -> list:
    """Every registered op, in the order they run — those preparing `kind` when given."""
    return [op for op in _modules() if kind is None or kind in op.KINDS]


def fields(kind: str) -> dict:
    """{field: form text} of every op that prepares `kind` — the form derives from this."""
    return {f: text for op in ops(kind) for f, text in op.FIELDS.items()}


def all_fields() -> set:
    return {f for op in ops() for f in op.FIELDS}


def _given(op, manifest) -> list:
    return [f for f in op.FIELDS if isinstance(manifest, dict) and manifest.get(f) is not None]


def requested(kind: str, manifest) -> list:
    """The ops a manifest asks for that prepare `kind`, in the order they run."""
    return [op for op in ops(kind) if _given(op, manifest)]


def validate(kind: str, manifest) -> list:
    """Every problem with the preparation a manifest declares, before anything is fetched.
    A field of an op that does not prepare this kind says so, never dropped."""
    if not isinstance(manifest, dict):
        return []
    problems = []
    for op in ops():
        given = _given(op, manifest)
        if given and kind not in op.KINDS:
            given = [f for f in given if f not in _own_fields(kind)]
        if not given:
            continue
        if kind not in op.KINDS:
            problems.append(f"{', '.join(given)} prepares a {' or '.join(op.KINDS)} "
                            f"contribution ({op.NAME}); it does not apply to a {kind} — drop "
                            f"{', '.join(given)}")
            continue
        problems += list(op.validate(manifest) or [])
    return problems


def _own_fields(kind: str) -> set:
    """The fields `kind`'s own form lists: a field an op of another kind shares with it
    (a raster's own clip, the vector clip op's) is that kind's, not the op's."""
    try:
        from .. import staging
        form = (staging._KINDS.get(kind) or {}).get("fields") or {}
        return set(form.get("required") or {}) | set(form.get("optional") or {})
    except Exception:
        return set()


def declined(kind: str, manifest, text: str, event: str, **situation) -> str:
    """A decline's `text`, with what the registered ops propose for its situation."""
    s = suggest({"event": event, "kind": kind, "manifest": manifest, **situation})
    return f"{text} — {s}" if s else text


def suggest(situation: dict) -> str | None:
    """What the registered ops propose for a decline's situation, or None. Every op is
    asked and each says whether the situation is its own — the ops of the decline's kind
    first, then the others (a vector over the cap may go in as a raster: rasterise). The
    situation is recorded for the attempt under way (attempt), for auto() to read."""
    if "problems" in situation and not situation["problems"]:
        return None                          # every refusal answered: nothing to propose
    seen = _SEEN.get()
    if seen is not None:
        seen.append(situation)
    said = []
    kind = situation.get("kind")
    for op in sorted(ops(), key=lambda op: kind not in op.KINDS):
        try:
            s = op.suggest(situation)
        except Exception:                    # a suggestion never breaks a decline
            s = None
        if s:
            said.append(s)
    return "; ".join(said) or None


def answered(kind: str, manifest, problems: list) -> list:
    """fetch_policy's refusals of a manifest's url, less those an op it asks for answers
    for (op.answers)."""
    ans = [op for op in requested(kind, manifest) if callable(getattr(op, "answers", None))]
    return [p for p in problems if not any(op.answers(manifest, p) for op in ans)]


# --------------------------------------------------------------------------- by itself

# The situations the declines of ONE attempt raise (suggest records them) and what the
# platform applied to it by itself: context variables, so submissions running at once
# (threads, tasks) never share them.
_SEEN: contextvars.ContextVar = contextvars.ContextVar("grp_prepare_seen", default=None)
_APPLIED: contextvars.ContextVar = contextvars.ContextVar("grp_prepare_applied", default=())
_MADE: contextvars.ContextVar = contextvars.ContextVar("grp_prepare_made", default=None)


@contextmanager
def attempt(applied=(), made=None):
    """One attempt at a submission: yields the list its declines' situations go into;
    `applied` (the platform's own proposals) is what run() records; `made`, a list, gets
    an entry when run() made its file (the preparation itself worked)."""
    seen: list = []
    t1, t2, t3 = _SEEN.set(seen), _APPLIED.set(tuple(applied)), _MADE.set(made)
    try:
        yield seen
    finally:
        _SEEN.reset(t1)
        _APPLIED.reset(t2)
        _MADE.reset(t3)


def _proposal(op, p) -> dict | None:
    if not isinstance(p, dict) or not isinstance(p.get("fields"), dict) or not p["fields"]:
        return None
    why = " ".join(str(p.get("why") or "").split())
    if not why:
        return None
    return {"op": op.NAME, "fields": dict(p["fields"]), "why": why,
            **({"restates": sorted(p["restates"])} if p.get("restates") else {})}


def _asked(op, name: str, manifest, *args) -> dict | None:
    """The op's proposal — never when the manifest asks for the op already (its own checks
    speak), nor one the op itself would decline before fetching (a WFS bbox in another
    version's terms): what it would say is not the contributor's to hear unasked."""
    fn = getattr(op, name, None)
    if not callable(fn) or _given(op, manifest):
        return None
    try:
        p = _proposal(op, fn(*args))
        if p and list(op.validate({**manifest, **p["fields"]}) or []):
            return None
        return p
    except Exception:                        # a proposal never breaks a submission
        return None


def detect(kind: str, manifest) -> list:
    """What the url's shape alone says to prepare, before anything is fetched: each
    detect() of the ops preparing `kind` that the manifest does not ask for already."""
    if not isinstance(manifest, dict):
        return []
    return [p for op in ops(kind) if (p := _asked(op, "detect", manifest, kind, manifest))]


def auto(kind: str, manifest, situations) -> list:
    """What resolves the situations an attempt raised with no person's choice involved:
    each auto() of the ops preparing `kind` that the manifest does not ask for already."""
    out = []
    for s in situations:
        s = {**s, "kind": s.get("kind") or kind, "manifest": s.get("manifest") or manifest}
        for op in ops(kind):
            p = _asked(op, "auto", manifest, s)
            if p and p not in out:
                out.append(p)
    return out


def fields_text(fields: dict) -> str:
    return ", ".join(f"{k}: {json.dumps(v, ensure_ascii=False, default=str)}"
                     for k, v in fields.items())


class Auto:
    """What the platform prepares by itself for one submission: detect()'s fields before
    the first attempt; after a decline, again() merges what auto() proposes for the
    situations THAT attempt raised and says whether to try once more — at most
    MAX_ROUNDS times, never the same fields twice. A proposal touching a field the
    contributor gave is dropped, unless it restates it (and the given value is kept).
    What detect() applied never costs the path the manifest took before it: refused
    before it made its file, with nothing more to propose, the manifest as given is tried
    once (fall_back) — what that lands is the answer; declined too, the first decline is
    (declined). A file it made and the gate declined (over the cap: an area to ask for)
    stands: the url as given could only land less of the layer."""

    def __init__(self, kind: str, manifest, on: bool = True):
        self.kind, self.given, self.manifest = kind, manifest, manifest
        self.applied: list = []
        self.rounds = 0
        self.on = bool(on) and isinstance(manifest, dict) and bool(ops(kind))
        if self.on:
            self._take(detect(kind, manifest))
        self.detected = bool(self.applied)
        self.made: list = []                 # the attempt under way made its file
        self.first: dict | None = None       # the decline fall_back() set aside
        self.refused: list = []              # what detect() applied, before it fell back
        self.tried = {self._key()}

    def _key(self) -> str:
        return json.dumps({k: v for p in self.applied for k, v in p["fields"].items()},
                          sort_keys=True, default=str)

    def _take(self, proposals) -> bool:
        took = False
        for p in proposals:
            own = set(p.get("restates") or ())
            f = p["fields"]
            if any(k in self.given and k not in own and self.given[k] != v for k, v in f.items()):
                continue                     # the contributor's own field stands
            if any(k in self.manifest and k not in self.given and self.manifest[k] != v
                   for k, v in f.items()):
                continue                     # another proposal of the platform's set it
            if all(k in self.manifest and self.manifest[k] == v for k, v in f.items()):
                continue                     # nothing new
            given = {k: self.given[k] for k in f if k in self.given}
            self.manifest = {**self.manifest, **f}
            self.applied.append({**p, **({"given": given} if given else {})})
            took = True
        return took

    def attempt(self):
        self.made = []
        return attempt(self.applied, self.made)

    def again(self, seen) -> bool:
        """After a declined attempt: True when what auto() proposes for its situations
        is new — the manifest then holds it, for one more attempt."""
        if not self.on or not seen or self.rounds >= MAX_ROUNDS:
            return False
        before = (self.manifest, list(self.applied))
        if not self._take(auto(self.kind, self.manifest, seen)) or self._key() in self.tried:
            self.manifest, self.applied = before
            return False
        self.tried.add(self._key())
        self.rounds += 1
        return True

    def fall_back(self, result: dict) -> bool:
        """After a declined attempt carrying what detect() applied, refused before its
        file was made, with nothing more to propose: True, once — the manifest is the
        contributor's own again, for the path it took before the platform prepared
        anything by itself; `result` is set aside."""
        if not self.detected or self.first is not None or self.made:
            return False
        self.first, self.refused = result, list(self.applied)
        self.manifest, self.applied, self.on = self.given, [], False
        return True

    def declined(self, result: dict) -> dict:
        """The decline to answer: the one set aside when the manifest as given was declined
        too (it says what the platform's own preparation met), else `result`."""
        return self.first if self.first is not None else result

    def shown(self, applied=None) -> list:
        return [{"fields": p["fields"], "why": p["why"],
                 **({"given": p["given"]} if p.get("given") else {})}
                for p in (self.applied if applied is None else applied)]

    def record(self) -> dict:
        """What the stored record keeps: the fields the platform applied, marked as its own."""
        return {"prepared_automatically": self.shown()} if self.applied else {}

    def said(self, result: dict) -> dict:
        """`result` with what the platform applied by itself, in words its client relays."""
        ok = result.get("status") in ("staged", "approved", "preview")
        if not self.applied:
            if not (self.refused and ok):
                return result
            words = "; ".join(f"{p['why']}: {fields_text(p['fields'])}" for p in self.refused)
            why = "; ".join(str(x) for x in (self.first or {}).get("problems") or [])[:600]
            return {**result, "prepared_automatically_refused": [
                        {**p, "problems": list((self.first or {}).get("problems") or [])}
                        for p in self.shown(self.refused)],
                    "message": (f"The platform first prepared this by itself — {words} — and "
                                f"that was refused ({why}); it then read the url exactly as "
                                "given, as it did before it prepared anything by itself, and "
                                "this is what that gave. Nothing was prepared; tell the "
                                "contributor")}
        words = "; ".join(f"{p['why']}: {fields_text(p['fields'])}" for p in self.applied)
        if ok:
            msg = (f"The platform prepared this contribution by itself — {words}. No choice "
                   "of the contributor's was involved; tell them what was done. The stored "
                   "manifest holds these fields, marked as the platform's")
        else:
            msg = (f"The platform applied by itself — {words} — and the contribution was "
                   "still declined: the problems say why, and ask for any choice that is the "
                   "contributor's")
        return {**result, "prepared_automatically": self.shown(), "message": msg}


# --------------------------------------------------------------------------- context

@dataclass
class Context:
    """What an op works with. Read: kind, manifest, workdir, path (the current local
    file — the output of the step before, or the fetched source), filename (the name the
    host gave the source), cap (the stored-result cap). Use: tmp() for an output path,
    fetch() for another input, unzip() for an archive, check_time() inside long loops,
    check_cells()/check_vertices() before a big read."""
    kind: str
    manifest: dict
    workdir: Path
    cap: int
    max_input_bytes: int
    op_seconds: float
    max_cells: int
    max_vertices: int
    path: str | None = None
    filename: str | None = None
    clipped: bool = False
    fetched_bytes: int = 0
    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    record: dict = field(default_factory=dict)
    step_name: str = "preparation"
    deadline: float = 0.0
    _n: int = 0

    # ----------------------------------------------------------------- time, size
    def begin(self, name: str) -> None:
        self.step_name, self.deadline = name, time.monotonic() + self.op_seconds
        _tell(name)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check_time(self) -> None:
        if time.monotonic() > self.deadline:
            raise PrepRefused(f"the {self.step_name} step ran over its {self.op_seconds:g} s "
                              "budget — give a smaller file or area (clip), or try again if "
                              "the host was slow")

    def check_cells(self, n: int, what: str = "the raster") -> None:
        if int(n) > self.max_cells:
            raise PrepRefused(f"{what} has {int(n):,} cells, over the {self.max_cells:,} one "
                              f"{self.step_name} step may touch — clip it to the area that "
                              "matters (clip) or give a smaller file")

    def check_vertices(self, n: int, what: str = "the layer") -> None:
        if int(n) > self.max_vertices:
            raise PrepRefused(f"{what} has {int(n):,} vertices, over the {self.max_vertices:,} "
                              f"one {self.step_name} step may touch — ask the contributor for "
                              "the area that matters (clip) and resubmit with it, never guess "
                              "one; or give a smaller or simplified layer")

    # ----------------------------------------------------------------- files
    def tmp(self, suffix: str = ".tif", stem: str = "step") -> str:
        """A fresh path inside the workdir (removed with it)."""
        self._n += 1
        return str(self.workdir / f"{self._n:02d}-{stem}{suffix}")

    def _room(self) -> int:
        free = shutil.disk_usage(self.workdir).free - DISK_RESERVE
        return min(self.max_input_bytes - self.fetched_bytes, free)

    def fetch(self, url: str) -> str:
        """Stream `url` to a file in the workdir under the input budget and this step's
        time; records a 'fetch' step (url, the host's file name, sha256, bytes). Returns
        the path, named after the host's file name so its extension says what it is."""
        from .. import fetch_policy
        budget = self.max_input_bytes - self.fetched_bytes
        limit = self._room()
        if limit <= 0:
            raise PrepRefused(
                "the platform cannot download more to prepare this contribution: "
                + (f"its inputs reached the {size_text(self.max_input_bytes)} budget"
                   if budget <= 0 else "the server is short of disk space right now — "
                   "try again later, or give a smaller file"))
        part = self.tmp(".part", "download")
        try:
            size, name = fetch_policy.fetch_to_file(url, part, max_bytes=limit,
                                                    deadline=max(1.0, self.remaining()))
        except fetch_policy.TooLarge as exc:
            big = (f"{str(url).strip()} is {size_text(exc.size)}" if exc.size
                   else f"{str(url).strip()} is over {size_text(limit)}")
            if limit >= budget:
                raise PrepRefused(f"{big}, over the {size_text(self.max_input_bytes)} the "
                                  "platform downloads to prepare one contribution — give a "
                                  "smaller file (one tile, one year, one area)") from None
            raise PrepRefused(f"{big}, more than the server has room for right now — try "
                              "again later, or give a smaller file") from None
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(str(name or "download")).name)[-120:] or "download"
        path = self.workdir / f"{self._n:02d}-{safe.lstrip('.') or 'download'}"
        Path(part).rename(path)
        self.fetched_bytes += int(size)
        self.steps.append({"op": "fetch", "inputs": {"url": str(url).strip(), "filename": name},
                           **_digest(path)})
        return str(path)

    def unzip(self, path: str) -> dict:
        """{member name: extracted path} for every file in the zip at `path`, extracted into
        the workdir. Refused: a member reaching outside it (an absolute path, '..', a drive
        letter), a link, more than ZIP_MEMBERS files, more than the input budget or the free
        disk unpacked, a member compressed over ZIP_RATIO times, a member longer than its
        header says."""
        import stat
        import zipfile
        quoted = str(self.manifest.get("url") or "the zip")
        try:
            z = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError):
            raise PrepRefused(f"the zip at {quoted} cannot be read (it is truncated or "
                              "damaged); re-upload it and resubmit") from None
        root = Path(self.tmp("", "unzipped"))
        root.mkdir()
        out = {}
        with z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if len(infos) > ZIP_MEMBERS:
                raise PrepRefused(f"the zip at {quoted} holds {len(infos):,} files — over the "
                                  f"{ZIP_MEMBERS:,} the platform unpacks; zip the files of one "
                                  "layer and resubmit")
            total = sum(i.file_size for i in infos)
            if total > self._room():
                raise PrepRefused(f"the zip at {quoted} unpacks to {size_text(total)}, more than "
                                  "the platform unpacks to prepare one contribution — zip the "
                                  "files of one layer and resubmit")
            for i in infos:
                name = i.filename.replace("\\", "/")
                parts = PurePosixPath(name).parts
                if (name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", name)
                        or not parts):
                    raise PrepRefused(f"the zip at {quoted} names a file outside itself "
                                      f"({i.filename!r}) — the platform does not unpack it")
                if stat.S_ISLNK(i.external_attr >> 16):
                    raise PrepRefused(f"the zip at {quoted} holds a link ({i.filename!r}) — the "
                                      "platform does not unpack links; zip the files themselves")
                if (i.file_size > ZIP_RATIO_FROM
                        and i.file_size > ZIP_RATIO * max(1, i.compress_size)):
                    raise PrepRefused(f"{i.filename!r} in the zip at {quoted} unpacks "
                                      f"{i.file_size // max(1, i.compress_size):,} times its "
                                      "packed size — refused as a zip bomb")
                dest = root.joinpath(*parts)
                if root.resolve() not in dest.resolve().parents:
                    raise PrepRefused(f"the zip at {quoted} names a file outside itself "
                                      f"({i.filename!r}) — the platform does not unpack it")
                dest.parent.mkdir(parents=True, exist_ok=True)
                n = 0
                with z.open(i) as src, open(dest, "wb") as dst:
                    while True:
                        chunk = src.read(1 << 20)
                        if not chunk:
                            break
                        n += len(chunk)
                        if n > i.file_size:
                            raise PrepRefused(f"{i.filename!r} in the zip at {quoted} is longer "
                                              "than its header says — refused")
                        dst.write(chunk)
                        self.check_time()
                out[i.filename] = str(dest)
        return out

    def adopt(self, path: str, notes=(), step: dict | None = None) -> None:
        """Make `path` the current file; `step` ({op, inputs}) is recorded with the file's
        sha256 and bytes, `notes` said in the record."""
        self.path = str(path)
        self.notes += [n for n in notes if n]
        if step is not None:
            self.steps.append({**step, **_digest(path)})


def _digest(path) -> dict:
    with open(path, "rb") as f:
        sha = hashlib.file_digest(f, "sha256").hexdigest()
    return {"sha256": sha, "bytes": Path(path).stat().st_size}


# --------------------------------------------------------------------------- run

def _sweep(root: Path) -> None:
    now = time.time()
    for d in root.glob("prep-*"):
        try:
            if now - d.stat().st_mtime > STALE_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def _default_source(ctx: Context) -> None:
    """The manifest's url, downloaded whole (streamed to disk)."""
    url = str(ctx.manifest["url"]).split("#", 1)[0]
    ctx.path = ctx.fetch(url)
    ctx.filename = ctx.steps[-1]["inputs"].get("filename")


def run(kind: str, manifest: dict, *, cap: int, folder, source=None, finish=None) -> Prepared | None:
    """Run the preparation a manifest declares; None when it declares none.

    `source(ctx, cellwise)`: the kind's own reading of the manifest's url (a raster's clip
    at the source) — it sets ctx.path, or leaves it None for the default: the url
    downloaded whole. `finish(ctx)`: the kind's last step (a raster's local clip). The
    result is moved into `folder` and the workdir removed, whatever happens."""
    todo = requested(kind, manifest)
    if not todo:
        return None
    gate = GATE.get()
    if gate is not None:
        gate()
    from ...config import get_settings
    s = get_settings()
    root = Path(s.cache_dir) / "prep"
    root.mkdir(parents=True, exist_ok=True)
    _sweep(root)
    workdir = Path(tempfile.mkdtemp(prefix="prep-", dir=root))
    ctx = Context(kind=kind, manifest=manifest, workdir=workdir, cap=int(cap),
                  max_input_bytes=int(s.grp_prep_max_input_bytes),
                  op_seconds=float(s.grp_prep_op_seconds), max_cells=int(s.grp_prep_max_cells),
                  max_vertices=int(s.grp_prep_max_vertices))
    cellwise = all(getattr(op, "CELLWISE", False) for op in todo)
    try:
        for op in todo:
            if ctx.path is None and getattr(op, "NEEDS_INPUT", True):
                _source(ctx, source, cellwise)
            ctx.begin(op.NAME)
            try:
                out = op.run(ctx)
            except PrepRefused:
                raise
            except Exception as exc:
                from .. import fetch_policy, remote_clip
                if isinstance(exc, (fetch_policy.FetchRefused, remote_clip.ClipRefused)):
                    raise PrepRefused(str(exc)) from None
                raise PrepRefused(f"the {op.NAME} step failed: {type(exc).__name__}: "
                                  f"{_quiet(exc, workdir)}") from None
            ctx.check_time()
            if out.kind_hint and out.kind_hint != kind:
                raise PrepRefused(f"the {op.NAME} step made a {out.kind_hint}; this contribution "
                                  f"is a {kind} — submit it as kind {out.kind_hint!r}")
            ctx.adopt(out.path, out.notes, {"op": op.NAME, **(out.provenance or {})})
        if ctx.path is None:
            _source(ctx, source, cellwise)
        if finish is not None:
            ctx.begin("clip")
            finish(ctx)
        final = Path(ctx.path)
        Path(folder).mkdir(parents=True, exist_ok=True)
        fd, kept = tempfile.mkstemp(prefix="staged-tmp-", suffix=final.suffix or ".bin", dir=folder)
        import os
        os.close(fd)
        shutil.move(str(final), kept)
        names = " -> ".join(st["op"] for st in ctx.steps)
        fetched = {"url": manifest.get("url"), **ctx.record,
                   "prepared": f"on the server: {names}", "steps": ctx.steps}
        applied = [{"fields": p["fields"], "why": p["why"]} for p in _APPLIED.get()]
        if applied:
            fetched["prepared_automatically"] = applied
            ctx.notes.append("prepared by the platform itself, no person's choice involved — "
                             + "; ".join(f"{p['why']}: {fields_text(p['fields'])}"
                                         for p in applied))
        if "read" not in fetched:
            fetched["read"] = "downloaded whole"
        # the host's name for the source while the file is still that kind of file, else
        # the last step's (its extension says what the file now is)
        name = ctx.filename if (ctx.filename and Path(ctx.filename).suffix.lower()
                                == final.suffix.lower()) else final.name
        _tell("checking the prepared file")
        made = _MADE.get()
        if made is not None:
            made.append(kept)
        return Prepared(path=kept, filename=name, notes=ctx.notes, fetched=fetched)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _source(ctx: Context, source, cellwise: bool) -> None:
    ctx.begin("fetch")
    if source is not None:
        source(ctx, cellwise)
    if ctx.path is None:
        _default_source(ctx)


def _quiet(exc: Exception, workdir: Path) -> str:
    """An error's words without the server's paths."""
    return str(exc).replace(str(workdir), "<work>")[:300]
