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

Opt-in: a manifest with none of the fields takes the existing path unchanged.

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

import functools
import hashlib
import importlib
import pkgutil
import re
import shutil
import tempfile
import time
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
SITUATIONS = ("stack_legend_mismatch", "duplicate_labels", "class_range", "url_refused",
              "shapefile_part", "zip_of_rasters")

ZIP_MEMBERS = 10_000         # files one zip may hold to be extracted
ZIP_RATIO = 200              # uncompressed / compressed, for a member over ZIP_RATIO_FROM
ZIP_RATIO_FROM = 1 << 20
DISK_RESERVE = 1 << 30       # free disk a preparation never eats into
STALE_SECONDS = 6 * 3600     # a directory a crashed worker left behind


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
        if not given:
            continue
        if kind not in op.KINDS:
            problems.append(f"{', '.join(given)} prepares a {' or '.join(op.KINDS)} "
                            f"contribution ({op.NAME}); it does not apply to a {kind} — drop "
                            f"{', '.join(given)}")
            continue
        problems += list(op.validate(manifest) or [])
    return problems


def suggest(situation: dict) -> str | None:
    """What the registered ops propose for a decline's situation, or None."""
    if "problems" in situation and not situation["problems"]:
        return None                          # every refusal answered: nothing to propose
    said = []
    for op in ops(situation.get("kind")):
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
                              f"one {self.step_name} step may touch — give a smaller or "
                              "simplified layer")

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
        if "read" not in fetched:
            fetched["read"] = "downloaded whole"
        # the host's name for the source while the file is still that kind of file, else
        # the last step's (its extension says what the file now is)
        name = ctx.filename if (ctx.filename and Path(ctx.filename).suffix.lower()
                                == final.suffix.lower()) else final.name
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
