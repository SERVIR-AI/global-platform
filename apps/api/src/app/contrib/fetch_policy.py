"""What the server may fetch on a contributor's behalf.

The platform runs on a shared network. A contribution that names a URL makes
the SERVER fetch it, so an unchecked URL is a way to reach hosts the contributor
cannot: private ranges, loopback, link-local metadata endpoints. Every hop of a
fetch is checked here, redirects included; the connection is PINNED to the
address that passed the check (a name that answers a public address to the
check and a private one to the connect — DNS rebinding — gains nothing); the
body is capped and the whole fetch has a wall-clock deadline so one
contribution can neither fill the disk nor hold a worker forever.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urlparse, urljoin

from ..config import get_settings

SCHEMES = ("http", "https")
MAX_REDIRECTS = 5
TIMEOUT = (10, 30)      # connect, per-read (seconds)
DEADLINE = 120          # whole fetch, seconds
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class FetchRefused(Exception):
    """A fetch the policy does not allow; the message names why."""


def _addresses(host: str) -> list[ipaddress._BaseAddress]:
    """Every address the name resolves to, IPv4 first (the connect uses [0])."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchRefused(f"host {host!r} does not resolve ({exc})") from None
    out = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if addr not in out:
            out.append(addr)
    if not out:
        raise FetchRefused(f"host {host!r} resolved to no usable address")
    return sorted(out, key=lambda a: a.version)


def _refuse_private(host: str, addrs) -> str | None:
    for addr in addrs:
        if not addr.is_global:
            return (f"host {host!r} resolves to {addr}, a private, loopback or link-local "
                    "address — the platform never fetches inside its own network")
    return None


def check_url(url: str) -> list[str]:
    """Every reason this URL may not be fetched, all at once. Empty = allowed."""
    problems = []
    try:
        parts = urlparse(str(url or "").strip())
    except ValueError as exc:
        return [f"url is not parseable: {exc}"]
    if parts.scheme not in SCHEMES:
        problems.append(f"url scheme must be http or https, got {parts.scheme or 'none'!r}")
    if not parts.hostname:
        problems.append("url has no host")
    if parts.username or parts.password:
        problems.append("url must not embed credentials")
    if problems:
        return problems
    try:
        bad = _refuse_private(parts.hostname, _addresses(parts.hostname))
        if bad:
            problems.append(bad)
    except FetchRefused as exc:
        problems.append(str(exc))
    return problems


def _session(hostname: str, scheme: str):
    """A requests session whose HTTPS connections present and verify `hostname`
    while the URL itself carries the pinned IP."""
    import requests
    from requests.adapters import HTTPAdapter

    class _Pinned(HTTPAdapter):
        def init_poolmanager(self, connections, maxsize, block=False, **kw):
            if scheme == "https":
                kw["server_hostname"] = hostname     # SNI
                kw["assert_hostname"] = hostname     # certificate name check
            super().init_poolmanager(connections, maxsize, block=block, **kw)

    s = requests.Session()
    s.mount(f"{scheme}://", _Pinned())
    return s


def _pinned_url(parts, addr) -> str:
    host = f"[{addr}]" if addr.version == 6 else str(addr)
    port = f":{parts.port}" if parts.port else ""
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{parts.scheme}://{host}{port}{path}{query}"


def fetch(url: str, max_bytes: int | None = None) -> tuple[bytes, str]:
    """Bytes + filename for an allowed URL. Each hop is checked and pinned; the
    body is streamed under the byte cap and the wall-clock deadline."""
    cap = int(max_bytes or get_settings().grp_contrib_max_bytes)
    started = time.monotonic()
    current = str(url).strip()
    for _ in range(MAX_REDIRECTS + 1):
        if time.monotonic() - started > DEADLINE:
            raise FetchRefused(f"fetch exceeded the {DEADLINE}s deadline")
        problems = check_url(current)
        if problems:
            raise FetchRefused("; ".join(problems))
        parts = urlparse(current)
        addr = _addresses(parts.hostname)[0]           # already verified global
        host_header = parts.hostname + (f":{parts.port}" if parts.port else "")
        session = _session(parts.hostname, parts.scheme)
        r = session.get(_pinned_url(parts, addr), timeout=TIMEOUT, stream=True,
                        allow_redirects=False,
                        headers={"User-Agent": USER_AGENT, "Host": host_header})
        try:
            if 300 <= r.status_code < 400:
                nxt = r.headers.get("location")
                if not nxt:
                    raise FetchRefused(f"{current} answered {r.status_code} without a location")
                current = urljoin(current, nxt)
                continue
            r.raise_for_status()
            declared = r.headers.get("content-length")
            if declared and str(declared).isdigit() and int(declared) > cap:
                raise FetchRefused(f"{current} declares {int(declared):,} bytes; the cap is {cap:,}")
            buf = bytearray()
            for chunk in r.iter_content(chunk_size=65536):
                buf.extend(chunk)
                if len(buf) > cap:
                    raise FetchRefused(f"{current} exceeds the {cap:,}-byte cap")
                if time.monotonic() - started > DEADLINE:
                    raise FetchRefused(f"fetch exceeded the {DEADLINE}s deadline")
            name = parts.path.rstrip("/").rsplit("/", 1)[-1] or "document"
            return bytes(buf), name
        finally:
            r.close()
            session.close()
    raise FetchRefused(f"{url} redirected more than {MAX_REDIRECTS} times")
