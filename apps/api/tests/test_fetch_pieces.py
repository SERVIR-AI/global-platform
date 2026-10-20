"""Big downloads come down in pieces at once.

The sprint case: Zenodo throttles one connection to ~25 KB/s (measured 2026-10-19), and
Global Mangrove Watch v4.1 is a 168 MB GeoPackage there — ~2 hours on one stream, over a
preparation step's 600 s. A file of PIECES_FROM bytes or more from a host that answers
range requests is fetched in pieces at once: byte-identical, every piece on the same
pinned address with the name as Host, a dropped piece resumed where it stopped, a piece
failing twice sending the fetch back to one stream; TooLarge is still raised before any
piece, the deadline is still final, and a host without ranges is unchanged.

Everything is synthetic and local: a loopback HTTP server in this file serves the files,
and no test touches the network."""

import hashlib
import ipaddress
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.config import get_settings
from app.contrib import fetch_policy


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


# --------------------------------------------------------------------------- in pieces
@pytest.fixture
def small_pieces(monkeypatch):
    monkeypatch.setattr(fetch_policy, "PIECES_FROM", 1 << 20)
    monkeypatch.setattr(fetch_policy, "PIECE_MIN", 128 << 10)
    monkeypatch.setattr(get_settings(), "grp_contrib_allow_private_hosts", True)


def _blob(n=(2 << 20) + 12345):
    return hashlib.sha256(b"seed").digest() * (n // 32) + b"tail"[: n % 32]


def _step(data):
    """The length of one piece, as fetch_policy lays them out."""
    n = min(fetch_policy.MAX_PIECES, max(2, len(data) // fetch_policy.PIECE_MIN))
    return -(-len(data) // n)


def _ranges(entries):
    out = []
    for e in entries:
        if e["range"]:
            a, b = re.match(r"bytes=(\d+)-(\d+)", e["range"]).groups()
            out.append((int(a), int(b)))
    return sorted(out)


def test_a_big_file_comes_down_in_pieces_byte_identical(host, small_pieces, tmp_path, log):
    data = _blob()
    url = host.put("big.gpkg.gz", data, ranges=True)
    dest = tmp_path / "big.part"
    n, name = fetch_policy.fetch_to_file(url, dest, max_bytes=8 << 20)
    assert n == len(data) and name == "big.gpkg.gz"
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest()
    asked = host.asked("big.gpkg.gz")
    log("OUTPUT", f"{len(asked)} requests: {[e['range'] for e in asked]}")
    assert len(asked) == 16 and asked[0]["range"] is None      # the first answer is piece 0
    spans = _ranges(asked)
    assert len(spans) == 15 and spans[-1][1] == len(data) - 1
    assert all(b + 1 == a2 for (_, b), (a2, _) in zip(spans, spans[1:]))


def test_every_piece_goes_to_the_same_pinned_address(host, small_pieces, tmp_path, monkeypatch):
    """A name that answers another address after the first check (DNS rebinding) gains
    nothing: every piece connects to the address that passed, with the name as Host."""
    answers = iter([[ipaddress.ip_address("127.0.0.1")]] * 2)     # the check, the connect
    later = [ipaddress.ip_address("127.0.0.2")]
    monkeypatch.setattr(fetch_policy, "_addresses", lambda h: next(answers, later))
    data = _blob()
    url = host.put("pinned.bin", data, ranges=True, host="files.hub.test")
    dest = tmp_path / "pinned.part"
    fetch_policy.fetch_to_file(url, dest, max_bytes=8 << 20)
    assert dest.read_bytes() == data
    asked = host.asked("pinned.bin")
    assert len(asked) == 16
    assert {e["to"] for e in asked} == {"127.0.0.1"}
    assert {e["host"] for e in asked} == {f"files.hub.test:{host.port}"}


def test_a_dropped_piece_resumes_where_it_stopped(host, small_pieces, tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_policy, "PIECE_CHUNK", 16 << 10)
    data = _blob()
    dropped = []

    one = f"bytes={_step(data)}-"

    def fault(path, rng):
        if rng and rng.startswith(one) and not dropped:
            dropped.append(rng)
            return "drop"
        return None
    host.fault = fault
    url = host.put("resume.bin", data, ranges=True)
    dest = tmp_path / "resume.part"
    fetch_policy.fetch_to_file(url, dest, max_bytes=8 << 20)
    assert dest.read_bytes() == data and dropped
    a, b = map(int, re.match(r"bytes=(\d+)-(\d+)", dropped[0]).groups())
    retry = [e["range"] for e in host.asked("resume.bin")
             if e["range"] and e["range"].endswith(f"-{b}") and e["range"] != dropped[0]]
    assert retry and a < int(re.match(r"bytes=(\d+)", retry[0]).group(1)) <= b


@pytest.mark.parametrize("fault", ["error", "whole"])
def test_a_piece_failing_twice_falls_back_to_one_stream(host, small_pieces, tmp_path, fault):
    data = _blob()
    two = f"bytes={2 * _step(data)}-"
    host.fault = lambda path, rng: fault if rng and rng.startswith(two) else None
    url = host.put("flaky.bin", data, ranges=True)
    dest = tmp_path / "flaky.part"
    n, _ = fetch_policy.fetch_to_file(url, dest, max_bytes=8 << 20)
    assert n == len(data) and dest.read_bytes() == data
    asked = host.asked("flaky.bin")
    assert sum(1 for e in asked if e["range"] is None) == 2     # the first, then one stream
    assert sum(1 for e in asked if e["range"] and e["range"].startswith(two)) == 2


def test_too_large_is_still_raised_before_any_piece(host, small_pieces, tmp_path):
    data = _blob()
    url = host.put("huge.bin", data, ranges=True)
    with pytest.raises(fetch_policy.TooLarge) as exc:
        fetch_policy.fetch_to_file(url, tmp_path / "huge.part", max_bytes=1 << 20)
    assert exc.value.size == len(data) and not (tmp_path / "huge.part").exists()
    assert [e["range"] for e in host.asked("huge.bin")] == [None]


def test_a_host_without_ranges_and_a_small_file_are_one_stream(host, small_pieces, tmp_path):
    data = _blob()
    for name, ranges, blob in (("noranges.bin", False, data), ("small.bin", True, data[:500_000])):
        url = host.put(name, blob, ranges=ranges)
        dest = tmp_path / f"{name}.part"
        n, _ = fetch_policy.fetch_to_file(url, dest, max_bytes=8 << 20)
        assert n == len(blob) and dest.read_bytes() == blob
        assert [e["range"] for e in host.asked(name)] == [None]


def test_the_deadline_is_final_in_pieces_too(host, small_pieces, tmp_path, monkeypatch):
    data = _blob()
    host.fault = lambda path, rng: None
    url = host.put("late.bin", data, ranges=True, delay=0.0)
    real = fetch_policy._in_pieces

    def late(*a, **k):
        a = list(a)
        a[8] = time.monotonic() - 10            # started: already past the deadline
        return real(*a, **k)
    monkeypatch.setattr(fetch_policy, "_in_pieces", late)
    with pytest.raises(fetch_policy.FetchRefused, match="deadline"):
        fetch_policy.fetch_to_file(url, tmp_path / "late.part", max_bytes=8 << 20, deadline=5)
    assert not (tmp_path / "late.part").exists()
