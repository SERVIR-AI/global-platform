"""Long submissions finish in the background; big downloads come down in pieces.

The sprint cases: a GBIF page-through took 157 s, a MAGA rasterise 159 s, an HDX admin
zip 131 s, the IGM Ecuador WCS ~60 s — and an MCP client abandons a tool call at about a
minute. Here contribute_submit waits a (monkeypatched) moment, answers `preparing` with
a job id, and contribute_status(<job id>) says the step and then returns the very result
a synchronous submit gives. A fast submission is unchanged. Only its submitter sees a
job; a restart marks it interrupted; the preparation lane queues with a position and a
plain submission never waits behind it; the hourly cap counts once. Beyond the asking
pack: the slow contribution is a vulnerability map the risk pack and a contributed
extension pack read, and a slow TABLE goes the same way.

Everything is synthetic and local: a loopback HTTP server in this file serves the files,
and no test touches the network."""

import asyncio
import json
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
import yaml
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from app.config import get_settings
from app.contrib import fetch_policy, identity, jobs, staging
from app.graph.geo import ingest, schema, tiffs
from app.mcp import assemble, feeds, packs, registry, store
from app.risk import synthesis as risk

OWNER = identity.Caller("user_owner", "gis@hub.test", False, "bound")
OTHER = identity.Caller("user_other", "analyst@hub.test", False, "bound")
REVIEWER = identity.Caller("user_rev", "reviewer@hub.test", True, "bound")
B = (103.0, 13.0, 103.02, 13.02)              # the risk world: 20 x 20 cells of 0.001 deg
RISK_POLY = {"type": "Polygon", "coordinates": [[[103.005, 13.005], [103.015, 13.005],
             [103.015, 13.015], [103.005, 13.015], [103.005, 13.005]]]}


# --------------------------------------------------------------------------- a host
class _Host:
    """Files on a loopback server. A gated file is answered only once its gate opens; a
    delayed one after `delay` seconds; `ranges` files answer Range requests and say so;
    `fault(path, range)` may answer 'error' (HTTP 500), 'drop' (half the body, then the
    connection closes) or 'whole' (200 and the whole file whatever was asked)."""

    def __init__(self):
        self.files, self.log, self.port = {}, [], None
        self.gates, self.delay, self.ranges, self.types = {}, {}, set(), {}
        self.fault = None
        self.lock = threading.Lock()

    def put(self, name, data, ranges=False, gate=False, delay=0.0, ctype=None, host=None):
        path = "/" + name
        self.files[path] = data
        if ranges:
            self.ranges.add(path)
        if gate:
            self.gates[path] = threading.Event()
        if delay:
            self.delay[path] = delay
        if ctype:
            self.types[path] = ctype
        return f"http://{host or '127.0.0.1'}:{self.port}/{name}"

    def open(self, name):
        self.gates["/" + name].set()

    def asked(self, name):
        return [e for e in self.log if e["path"] == "/" + name]


def _handler(host):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            data = host.files.get(path)
            rng = self.headers.get("Range")
            with host.lock:
                host.log.append({"path": path, "range": rng, "host": self.headers.get("Host"),
                                 "to": self.connection.getsockname()[0]})
            if path in host.gates:
                host.gates[path].wait(60)
            if host.delay.get(path):
                time.sleep(host.delay[path])
            if data is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            fault = host.fault(path, rng) if host.fault else None
            if fault == "error":
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            a, b, code = 0, len(data) - 1, 200
            if rng and path in host.ranges and fault != "whole":
                m = re.match(r"bytes=(\d+)-(\d*)", rng)
                a = int(m.group(1))
                b = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
                code = 206
            part = data[a:b + 1]
            self.send_response(code)
            if code == 206:
                self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
            if path in host.ranges:
                self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", host.types.get(path, "application/octet-stream"))
            self.send_header("Content-Disposition", f'attachment; filename="{path[1:]}"')
            self.send_header("Content-Length", str(len(part)))
            self.end_headers()
            try:
                if fault == "drop":
                    self.wfile.write(part[:len(part) // 2])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(part)
            except (BrokenPipeError, ConnectionResetError):
                pass
    return Handler


class _Server(ThreadingHTTPServer):
    request_queue_size = 64
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass                        # a piece stopped mid-answer closes its connection


@pytest.fixture
def host():
    h = _Host()
    srv = _Server(("127.0.0.1", 0), _handler(h))
    h.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield h
    for g in h.gates.values():
        g.set()
    srv.shutdown()
    srv.server_close()


# --------------------------------------------------------------------------- a world
def _gtiff(arr, dtype, nodata=None, bounds=B):
    h, w = arr.shape
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=w, height=h, count=1, dtype=dtype, crs="EPSG:4326",
                     transform=from_bounds(*bounds, w, h), nodata=nodata) as d:
            d.write(arr.astype(dtype), 1)
        return mf.read()


def _write(path, arr, dtype, nodata=None):
    with open(path, "wb") as f:
        f.write(_gtiff(arr, dtype, nodata))


def _drain(timeout=60):
    """Every job this process runs, finished — before the hermetic store goes away."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with jobs._lock:
            live = list(jobs._live.values())
        if not live:
            return
        for f in live:
            try:
                f.result(timeout=max(0.1, end - time.monotonic()))
            except Exception:
                pass


@pytest.fixture
def env(tmp_path, monkeypatch, host):
    """A hermetic platform: the risk pack's toy world (a flood hazard and a 1-5
    vulnerability layer near 103 E, 13 N), auto-approve, the real fetch policy with the
    loopback host allowed, a hermetic store — and fresh job lanes."""
    s = get_settings()
    cache = tmp_path / "cache"
    tdir = cache / "tiffs"
    tdir.mkdir(parents=True)
    haz = np.zeros((20, 20))
    haz[:, :10] = 3
    _write(tdir / "hazard_toyflood.tif", haz, "int16")
    vul = np.ones((20, 20))
    vul[5:15, 5:15] = 4
    _write(tdir / "vulnerability_toy.tif", vul, "uint8")
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "tiffs.yml").write_text(yaml.safe_dump({
        "hazard_toyflood": {"local_path": "tiffs/hazard_toyflood.tif", "title": "Toy flood",
                            "legend": {1: "Very Low", 3: "Moderate", 5: "Very High"}},
        "vulnerability_toy": {"local_path": "tiffs/vulnerability_toy.tif", "internal": True,
                              "title": "Toy vulnerability", "source": "test fixture",
                              "license": "CC0-1.0", "vintage": "2026-10"}}))
    (conf / "raster_schema.yml").write_text(yaml.safe_dump({
        "defaults": {"crs": "EPSG:4326", "float_tol": 0.0001},
        "roles": schema._shipped()["roles"],
        "layers": {
            "hazard_toyflood": {"role": "hazard", "dtype": "int16", "valid_min": 0,
                                "valid_max": 5, "units": "class"},
            "vulnerability_toy": {"role": "vulnerability_reclass", "dtype": "uint8",
                                  "valid_min": 0, "valid_max": 5, "units": "class",
                                  "nodata": None}}}))
    (conf / "risk_l2.yml").write_text(yaml.safe_dump(
        {"class_max": 5, "weights": {"toyflood": {"vulnerability_toy": 1.0}}}))
    fdir = tmp_path / "feeds"
    fdir.mkdir()
    for k, v in {"tiffs_config_path": conf / "tiffs.yml",
                 "raster_schema_path": conf / "raster_schema.yml",
                 "risk_l2_config_path": conf / "risk_l2.yml",
                 "tiffs_contrib_path": conf / "tiffs.contrib.yml",
                 "raster_schema_contrib_path": conf / "raster_schema.contrib.yml",
                 "risk_l2_contrib_path": conf / "risk_l2.contrib.yml",
                 "feeds_conf_dir": fdir, "cache_dir": cache, "tiffs_dir": tdir,
                 "grp_mattermost_webhook": "", "grp_auto_approve": True,
                 "grp_contrib_allow_private_hosts": True, "grp_contrib_hourly_cap": 0,
                 "grp_prep_max_jobs": 2, "grp_prep_wait_seconds": 40,
                 "grp_prep_status_wait_seconds": 0}.items():
        monkeypatch.setattr(s, k, v)
    monkeypatch.setattr(store, "_db_path", lambda: tmp_path / "store.sqlite")
    monkeypatch.setattr(ingest, "_fetch_layer", lambda layer, bbox, boundary: [])
    monkeypatch.setattr(risk, "_pack_feeds", lambda *a, **k: [])
    monkeypatch.setattr(risk, "_document_hits", lambda *a, **k: (None, []))
    for g in ("STAGED_RASTERS", "STAGED_WEIGHTS", "STAGED_FEEDS", "STAGED_VECTORS",
              "STAGED_AMENDS"):
        monkeypatch.setattr(staging, g, {}, raising=False)
    monkeypatch.setattr(staging, "_STAGED_LOADED", True)
    monkeypatch.setattr(staging, "_STAGED_VECTORS_LOADED", True)
    monkeypatch.setattr(jobs, "_lanes", {})
    # every return of staging.submit, as the job saw it: what status must hand back
    seen = []
    real = staging.submit

    def recording(*a, **k):
        out = real(*a, **k)
        seen.append(out)
        return out
    monkeypatch.setattr(staging, "submit", recording)
    registry.reload_declarative_feeds()
    yield {"tmp": tmp_path, "cache": cache, "host": host, "seen": seen}
    for g in host.gates.values():
        g.set()
    _drain()
    monkeypatch.undo()
    registry.reload_declarative_feeds()


def _call(tool, args, caller=OWNER):
    """A tool called through the real FastMCP server, as a client would."""
    from fastmcp import Client
    from app.mcp.server import mcp

    async def go():
        async with Client(mcp) as c:
            r = await c.call_tool(tool, args)
            return r.structured_content
    tok = identity.bind(caller)
    try:
        return asyncio.run(go())
    finally:
        identity.unbind(tok)


def _status(cid, caller=OWNER):
    return staging.status(cid, caller)


def _until(cid, caller=OWNER, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        out = _status(cid, caller)
        if out.get("status") != "preparing":
            return out
        time.sleep(0.05)
    raise AssertionError(f"job {cid} still preparing after {timeout} s")


def _keep_checking(jid, wait=0):
    """The tail of a 'preparing' message, as a regex."""
    return (rf"keep checking with contribute_status, contribution_id {jid}, until it finishes "
            rf"\(each check waits up to {wait} s\) — tell the person it is still being prepared, "
            r"do not end your answer while it is, and do not resubmit")


def _json(x):
    return json.loads(json.dumps(x, default=str))


def _vuln(url, layer="vulnerability_toy_coded", **over):
    """A vulnerability map delivered coded 10-50, recoded to 1-5 on the server."""
    return {"layer": layer, "url": url, "title": "Coded vulnerability",
            "description": "five classes coded 10-50", "source": "s", "license": "CC0-1.0",
            "vintage": "2026-10", "legend": {1: "Very Low", 3: "Moderate", 5: "Very High"},
            "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 5, "nodata": 0},
            "recode": {10: 1, 30: 3, 50: 5}, **over}


def _coded():
    arr = np.full((20, 20), 10, "uint8")
    arr[:, 10:] = 30
    arr[8:12, 8:12] = 50
    return _gtiff(arr, "uint8", 0)


def _plain(url, layer="vulnerability_toy_plain"):
    """A vulnerability map already coded 1-5: no preparation."""
    arr = np.full((20, 20), 1, "uint8")
    arr[:, 10:] = 3
    return {"layer": layer, "url": url, "title": "Plain vulnerability",
            "description": "five classes coded 1-5", "source": "s", "license": "CC0-1.0",
            "vintage": "2026-10", "legend": {1: "Very Low", 3: "Moderate", 5: "Very High"},
            "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 5, "nodata": 0}}, \
        _gtiff(arr, "uint8", 0)


def _toy_pack(monkeypatch):
    def gather(target, focus, trace, extras):
        stats = {"queries": None, "_area": {"name": "Toy site", "how": "test polygon",
                                            "geometry": RISK_POLY}}
        return ([{"n": 1, "kind": "measurement", "retrieval": "computed-at-pack-time",
                  "source": "toy", "title": "reading", "text": "42 units."}], [], stats)
    monkeypatch.setitem(packs.PACKS, "toy", {
        "display_name": "Toy", "version": "v0", "target_keys": ("place",),
        "target_doc": {"place": "site"}, "gather": gather,
        "sections": lambda: ["## Reading"], "corpus": None,
        "default_focus": lambda t: t.get("place", "")})


# --------------------------------------------------------------------------- 1. fast: unchanged
def test_a_fast_submission_answers_exactly_what_submit_returns(env, log):
    """Done within the wait: the reply is staging.submit's own, through the real server."""
    url = env["host"].put("coded.tif", _coded())
    out = _call("contribute_submit", {"kind": "raster", "manifest": _vuln(url)})
    log("OUTPUT", json.dumps(out)[:300])
    assert out["status"] == "approved", out
    assert len(env["seen"]) == 1 and out == _json(env["seen"][0])
    # and the job module hands back the very object submit returned
    url2 = env["host"].put("coded2.tif", _coded())
    tok = identity.bind(OWNER)
    try:
        got = jobs.submit("raster", _vuln(url2, layer="vulnerability_toy_coded_two"), OWNER)
    finally:
        identity.unbind(tok)
    assert got is env["seen"][-1] and got["status"] == "approved"
    # a decline is unchanged too: every problem, at once
    out = _call("contribute_submit", {"kind": "raster", "manifest": {"layer": "x"}})
    assert out == _json(env["seen"][-1]) and out["status"] == "declined"


# --------------------------------------------------------------------------- 2. slow: preparing
def test_a_slow_preparation_answers_preparing_then_status_gives_the_final_result(
        env, monkeypatch, log):
    """Past the wait: `preparing` with a job id and the step; status follows the job and
    then returns exactly what submit returned. The layer lands: the risk pack and a
    contributed extension pack read it as classes 1-5."""
    monkeypatch.setattr(get_settings(), "grp_prep_wait_seconds", 0.3)
    host = env["host"]
    url = host.put("slow_coded.tif", _coded(), gate=True)
    out = _call("contribute_submit", {"kind": "raster", "manifest": _vuln(url)})
    log("OUTPUT", json.dumps(out))
    assert out["status"] == "preparing" and out["kind"] == "raster"
    jid = out["contribution_id"]
    assert re.fullmatch(r"[0-9a-f]{16}", jid) and store.load_contribution(jid) is None
    assert out["step"] == "fetching the source"         # the prepare Context's own step
    assert re.fullmatch(r"the platform is still preparing this \(fetching the source, \d+ s so "
                        r"far\); " + _keep_checking(jid), out["message"])
    mid = _call("contribute_status", {"contribution_id": jid})
    assert mid["status"] == "preparing" and mid["step"] == "fetching the source"
    assert "queue_position" not in mid and mid["elapsed_s"] >= 0
    host.open("slow_coded.tif")
    final = _until(jid)
    log("OUTPUT", json.dumps(final)[:300])
    assert final["status"] == "approved"
    assert final == _json(env["seen"][-1])                # exactly what submit returned
    assert _call("contribute_status", {"contribution_id": jid}) == final
    real = final["contribution_id"]
    assert real != jid and _status(real)["contribution"]["status"] == "approved"
    # beyond the asking pack
    p = assemble.assemble(pack="risk", place="Toyville", hazard="toyflood", geometry=RISK_POLY,
                          layers=["vulnerability_toy_coded"])
    assert p["status"] == "ok", p.get("note")
    classes = p["layers"]["vulnerability_toy_coded"]["summary"]["classes"]
    assert set(classes) == {"1", "3", "5"} and classes["5"]["cells"] == 16
    _toy_pack(monkeypatch)
    t = assemble.assemble(pack="toy", place="Toy site", layers=["vulnerability_toy_coded"])
    assert t["status"] == "ok" and t["layers"]["vulnerability_toy_coded"]["summary"]["classes"] \
        == classes


def test_a_client_that_never_polls_loses_nothing(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "grp_prep_wait_seconds", 0.2)
    url = env["host"].put("never.tif", _coded(), gate=True)
    out = _call("contribute_submit", {"kind": "raster", "manifest": _vuln(url)})
    assert out["status"] == "preparing"
    env["host"].open("never.tif")
    _drain()
    assert "vulnerability_toy_coded" in tiffs.catalog()
    assert [r["status"] for r in store.list_contributions(contributor_id=OWNER.id)] == ["approved"]


def test_preview_works_the_same_in_the_background(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "grp_prep_wait_seconds", 0.2)
    url = env["host"].put("preview.tif", _coded(), gate=True)
    out = _call("contribute_submit", {"kind": "raster", "manifest": {**_vuln(url), "preview": True}})
    assert out["status"] == "preparing"
    env["host"].open("preview.tif")
    final = _until(out["contribution_id"])
    assert final["status"] == "preview" and final == _json(env["seen"][-1])
    assert final["would_land"]["fetched"]["steps"][-1]["op"] == "recode"
    assert store.list_contributions() == [] and "vulnerability_toy_coded" not in tiffs.catalog()


def test_a_slow_table_goes_the_same_way(env, monkeypatch):
    """Generic: a table whose host is slow is no different from a preparation."""
    monkeypatch.setattr(get_settings(), "grp_prep_wait_seconds", 0.2)
    csv = "Month,Stage_m\n2026-06,3.9\n2026-07,4.8\n"
    url = env["host"].put("stage.csv", csv.encode(), gate=True, ctype="text/csv")
    m = {"dataset": "jobs_river_stage", "title": "River stage (SYNTHETIC)",
         "description": "SYNTHETIC series", "source": "hub demonstration series",
         "validation": "unvalidated", "license": "CC0-1.0", "vintage": "2026-09",
         "cadence": "monthly", "columns": {"month": "Month", "stage_m": "Stage_m"},
         "units": "metres", "url": url}
    out = _call("contribute_submit", {"kind": "table", "manifest": m})
    assert out["status"] == "preparing" and out["step"] == "checking the submission"
    env["host"].open("stage.csv")
    final = _until(out["contribution_id"])
    assert final["status"] == "approved" and final == _json(env["seen"][-1])
    tok = identity.bind(OWNER)
    try:
        rows = feeds.query("jobs_river_stage", {"limit": 5})
    finally:
        identity.unbind(tok)
    assert rows["status"] == "ok", rows


# --------------------------------------------------------------------------- 3. who sees a job
def test_a_status_call_for_an_unknown_id_changes_nothing(env):
    """The existing path: an unknown id is declined as before, and no table is made."""
    assert _status("0123456789abcdef") == {"status": "declined",
                                           "note": "unknown contribution '0123456789abcdef'"}
    con = store._connect()
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert "contribution_jobs" not in names


def test_only_the_submitter_sees_a_job(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "grp_prep_wait_seconds", 0.2)
    url = env["host"].put("mine.tif", _coded(), gate=True)
    out = _call("contribute_submit", {"kind": "raster", "manifest": _vuln(url)})
    jid = out["contribution_id"]
    for who in (OTHER, REVIEWER):
        seen = _call("contribute_status", {"contribution_id": jid}, caller=who)
        assert seen == {"status": "declined", "note": f"unknown contribution {jid!r}"}
    assert _status(jid)["status"] == "preparing"
    env["host"].open("mine.tif")
    final = _until(jid)
    assert final["status"] == "approved"
    assert _status(jid, OTHER) == {"status": "declined", "note": f"unknown contribution {jid!r}"}


# --------------------------------------------------------------------------- 4. a restart
def test_a_restart_marks_an_unfinished_job_interrupted(env, monkeypatch):
    monkeypatch.setattr(get_settings(), "grp_prep_wait_seconds", 0.2)
    url = env["host"].put("cut.tif", _coded(), gate=True)
    out = _call("contribute_submit", {"kind": "raster", "manifest": _vuln(url)})
    jid = out["contribution_id"]
    # the process restarts: a new instance, nothing of the old one's in memory
    running = jobs._live                      # still this process's own, finished before teardown
    monkeypatch.setattr(jobs, "INSTANCE", "0" * 16)
    monkeypatch.setattr(jobs, "_live", {})
    got = _status(jid)
    assert got == {"status": "interrupted", "contribution_id": jid, "kind": "raster",
                   "message": "interrupted — the platform restarted before it finished "
                              "preparing this; resubmit the same manifest with "
                              "contribute_submit"}
    # a job a dead process left behind, found by the sweep at the next submission
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    now = time.time()
    jobs._write("INSERT INTO contribution_jobs (id, owner_id, kind, lane, state, created, beat, "
                "instance, pid) VALUES (?, ?, 'raster', 'prepare', 'running', ?, ?, ?, ?)",
                ("ab" * 8, OWNER.id, now, now, "f" * 16, dead.pid))
    assert jobs.recover() == 1
    assert _status("ab" * 8)["status"] == "interrupted"
    # a job a live process is beating stays its own
    jobs._write("INSERT INTO contribution_jobs (id, owner_id, kind, lane, state, created, beat, "
                "instance, pid) VALUES (?, ?, 'raster', 'plain', 'running', ?, ?, ?, ?)",
                ("cd" * 8, OWNER.id, now, now, "e" * 16, 1))
    assert jobs.recover() == 0 and _status("cd" * 8)["status"] == "preparing"
    monkeypatch.setattr(jobs, "_live", running)
    env["host"].open("cut.tif")
    _drain()                                  # into the hermetic store, never the real one


def test_finished_jobs_are_kept_a_day(env, monkeypatch):
    url = env["host"].put("kept.tif", _coded())
    monkeypatch.setattr(get_settings(), "grp_prep_wait_seconds", 0)
    out = _call("contribute_submit", {"kind": "raster", "manifest": _vuln(url)})
    jid = out["contribution_id"]
    assert _until(jid)["status"] == "approved"
    jobs._write("UPDATE contribution_jobs SET finished = ? WHERE id = ?",
                (time.time() - jobs.KEEP + 60, jid))
    jobs.recover()
    assert _status(jid)["status"] == "approved"
    jobs._write("UPDATE contribution_jobs SET finished = ? WHERE id = ?",
                (time.time() - jobs.KEEP - 60, jid))
    jobs.recover()
    assert _status(jid) == {"status": "declined", "note": f"unknown contribution {jid!r}"}


# --------------------------------------------------------------------------- 5. the queue
def test_preparations_queue_with_a_position_and_plain_submissions_do_not_wait(env, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "grp_prep_max_jobs", 1)
    monkeypatch.setattr(s, "grp_prep_wait_seconds", 0.3)
    host = env["host"]
    ids = []
    for n in range(3):
        url = host.put(f"q{n}.tif", _coded(), gate=True)
        out = _call("contribute_submit", {"kind": "raster",
                                          "manifest": _vuln(url, layer=f"vulnerability_q{n}")})
        assert out["status"] == "preparing"
        ids.append(out["contribution_id"])
    first, second, third = (_status(i) for i in ids)
    assert "queue_position" not in first and first["step"] == "fetching the source"
    assert second["queue_position"] == 1 and third["queue_position"] == 2
    assert "waiting for a free slot, position 2 in the queue" in third["message"]
    # a submission declaring no preparation runs at once, beside the full lane
    m, raw = _plain(host.put("plain.tif", b""))
    host.files["/plain.tif"] = raw
    monkeypatch.setattr(s, "grp_prep_wait_seconds", 30)
    out = _call("contribute_submit", {"kind": "raster", "manifest": m})
    assert out["status"] == "approved", out
    host.open("q0.tif")
    assert _until(ids[0])["status"] == "approved"
    deadline = time.monotonic() + 10
    while _status(ids[2]).get("queue_position") != 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _status(ids[2])["queue_position"] == 1
    host.open("q1.tif")
    host.open("q2.tif")
    assert [_until(i)["status"] for i in ids[1:]] == ["approved", "approved"]


# --------------------------------------------------------------------------- 6. the cap
def test_the_hourly_cap_counts_a_submission_once(env, monkeypatch):
    """Polling counts nothing, an agent resubmitting while it prepares gets the same job,
    and the submission itself counts once."""
    s = get_settings()
    monkeypatch.setattr(s, "grp_contrib_hourly_cap", 2)
    monkeypatch.setattr(s, "grp_prep_wait_seconds", 0.3)
    host = env["host"]
    url = host.put("capped.tif", _coded(), gate=True)
    out = _call("contribute_submit", {"kind": "raster", "manifest": _vuln(url)})
    jid = out["contribution_id"]
    for _ in range(3):
        assert _call("contribute_status", {"contribution_id": jid})["status"] == "preparing"
    again = _call("contribute_submit", {"kind": "raster", "manifest": _vuln(url)})
    assert again["status"] == "preparing" and again["contribution_id"] == jid
    host.open("capped.tif")
    assert _until(jid)["status"] == "approved"
    m, raw = _plain(host.put("second.tif", b""))
    host.files["/second.tif"] = raw
    monkeypatch.setattr(s, "grp_prep_wait_seconds", 30)
    assert _call("contribute_submit", {"kind": "raster", "manifest": m})["status"] == "approved"
    m3, raw3 = _plain(host.put("third.tif", b""), layer="vulnerability_toy_third")
    host.files["/third.tif"] = raw3
    third = _call("contribute_submit", {"kind": "raster", "manifest": m3})
    assert third["status"] == "declined"
    assert third["problems"][0].startswith("2 submissions in the last hour — the cap is 2")


# --------------------------------------------------------------------------- 7. a patient read
def test_a_host_slow_to_answer_is_waited_for_in_the_background(env, monkeypatch):
    """The IGM WCS shape: the first byte comes after the per-read timeout. A submission in
    a job waits for it; the same fetch outside a job is declined as before."""
    monkeypatch.setattr(fetch_policy, "TIMEOUT", (5, 0.3))
    monkeypatch.setattr(fetch_policy, "PATIENT_READ", 5)
    m, raw = _plain(env["host"].put("wcs.tif", b"", delay=0.8), layer="measure_slow_wcs")
    env["host"].files["/wcs.tif"] = raw
    m = {**m, "declared": {"dtype": "uint8", "valid_min": 1, "valid_max": 5, "nodata": 0,
                           "units": "m"}}
    m.pop("legend")
    tok = identity.bind(OWNER)
    try:
        before = staging.submit("raster", m, OWNER)          # in the call, as before
    finally:
        identity.unbind(tok)
    assert before["status"] == "declined" and "ReadTimeout" in before["problems"][0]
    out = _call("contribute_submit", {"kind": "raster", "manifest": m})
    assert out["status"] == "approved", out
