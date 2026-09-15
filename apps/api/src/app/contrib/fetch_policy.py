"""What the server may fetch on a contributor's behalf.

The platform runs on a shared network. A contribution that names a URL makes
the SERVER fetch it, so an unchecked URL is a way to reach hosts the contributor
cannot: private ranges, loopback, link-local metadata endpoints. Every hop of a
fetch is checked here, redirects included, and the body is capped so one
contribution cannot fill the disk.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from ..config import get_settings

SCHEMES = ("http", "https")
MAX_REDIRECTS = 5
TIMEOUT = (10, 60)  # connect, read
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class FetchRefused(Exception):
    """A fetch the policy does not allow; the message names why."""


def _addresses(host: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchRefused(f"host {host!r} does not resolve ({exc})") from None
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not out:
        raise FetchRefused(f"host {host!r} resolved to no usable address")
    return out


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
        for addr in _addresses(parts.hostname):
            if not addr.is_global:
                problems.append(
                    f"host {parts.hostname!r} resolves to {addr}, a private, loopback or "
                    "link-local address — the platform never fetches inside its own network")
                break
    except FetchRefused as exc:
        problems.append(str(exc))
    return problems


def fetch(url: str, max_bytes: int | None = None) -> tuple[bytes, str]:
    """Bytes + filename for an allowed URL. Redirects are followed one hop at a
    time so each destination is checked; the body is streamed and capped."""
    import requests

    cap = int(max_bytes or get_settings().grp_contrib_max_bytes)
    current = str(url).strip()
    for _ in range(MAX_REDIRECTS + 1):
        problems = check_url(current)
        if problems:
            raise FetchRefused("; ".join(problems))
        r = requests.get(current, timeout=TIMEOUT, stream=True, allow_redirects=False,
                         headers={"User-Agent": USER_AGENT})
        if r.is_redirect or r.is_permanent_redirect:
            nxt = r.headers.get("location")
            r.close()
            if not nxt:
                raise FetchRefused(f"{current} redirected without a location")
            current = requests.compat.urljoin(current, nxt)
            continue
        r.raise_for_status()
        declared = r.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > cap:
            r.close()
            raise FetchRefused(f"{current} declares {int(declared):,} bytes; the cap is {cap:,}")
        buf = bytearray()
        for chunk in r.iter_content(chunk_size=65536):
            buf.extend(chunk)
            if len(buf) > cap:
                r.close()
                raise FetchRefused(f"{current} exceeds the {cap:,}-byte cap")
        r.close()
        name = urlparse(current).path.rstrip("/").rsplit("/", 1)[-1] or "document"
        return bytes(buf), name
    raise FetchRefused(f"{url} redirected more than {MAX_REDIRECTS} times")
