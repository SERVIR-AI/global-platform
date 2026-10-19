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
import re
import socket
import time
from urllib.parse import parse_qs, urlparse, urljoin

from ..config import get_settings

SCHEMES = ("http", "https")
MAX_REDIRECTS = 5
TIMEOUT = (10, 30)      # connect, per-read (seconds)
DEADLINE = 120          # whole fetch, seconds
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class FetchRefused(Exception):
    """A fetch the policy does not allow; the message names why."""


# Google Drive is where hubs keep their maps. A share link ("/file/d/<id>/view")
# answers a web page, not the file; the direct form answers the bytes for any
# file shared with "Anyone with the link". Rewriting it here means every kind —
# document, table, raster, vector — takes the link a contributor actually has.
_DRIVE_HOSTS = ("drive.google.com", "docs.google.com")
_DRIVE_CONTENT_HOST = "drive.usercontent.google.com"
_DRIVE_ID = re.compile(r"/(?:file/)?d/([A-Za-z0-9_-]{10,})")


def drive_file_id(url: str) -> str | None:
    """The file id in a Google Drive link, or None when the link is not one."""
    try:
        parts = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in _DRIVE_HOSTS:
        return None
    m = _DRIVE_ID.search(parts.path or "")
    if m:
        return m.group(1)
    ids = parse_qs(parts.query).get("id") or []
    return ids[0] if ids and re.fullmatch(r"[A-Za-z0-9_-]{10,}", ids[0]) else None


def direct_url(url: str) -> str:
    """A Google Drive share link rewritten to its direct-download form; any other
    URL unchanged."""
    fid = drive_file_id(url)
    if not fid:
        return str(url).strip()
    return f"https://drive.google.com/uc?export=download&id={fid}"


def _drive_problem(parts) -> str | None:
    """Why a Google Drive link cannot be fetched as a file, in the contributor's terms."""
    host = (parts.hostname or "").lower()
    if host not in _DRIVE_HOSTS:
        return None
    path = parts.path or ""
    if "/folders/" in path or "/drive/" in path and "/file/" not in path:
        return ("this is a Google Drive FOLDER link; the platform fetches one file per "
                "contribution — open the file, Share -> Copy link (access: Anyone with the "
                "link) and give that link")
    if "/document/" in path or "/spreadsheets/" in path or "/presentation/" in path:
        return ("this is a Google Docs/Sheets/Slides link, not an uploaded file; export it "
                "(File -> Download) and share the exported file, or give a public http(s) URL")
    if not drive_file_id(parts.geturl()):
        return ("this Google Drive link carries no file id; use the file's share link "
                "(.../file/d/<id>/view) or the direct form (...?id=<id>)")
    return None


def _confirm_url(html: str, fallback_id: str | None) -> str | None:
    """The direct link inside Google Drive's 'can't scan for viruses' page, or None."""
    m = re.search(r'<form[^>]+action="([^"]+)"', html)
    if not m:
        return None
    action = m.group(1).replace("&amp;", "&")
    fields = dict(re.findall(r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html))
    fields.setdefault("export", "download")
    fields.setdefault("confirm", "t")
    if fallback_id:
        fields.setdefault("id", fallback_id)
    query = "&".join(f"{k}={v}" for k, v in fields.items() if k in ("id", "export", "confirm", "uuid"))
    return f"{action}?{query}" if query else action


def _filename(headers, url_path: str) -> str:
    """The name the server gives the file, else the URL's last path segment. The
    extension decides the parser downstream, so a Drive link must not be archived as
    'uc' or 'view'."""
    cd = headers.get("content-disposition") or ""
    m = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;]+)", cd)
    if m:
        from urllib.parse import unquote
        name = unquote(m.group(1)).strip().strip('"')
        if name:
            return name
    m = re.search(r'filename="?([^";]+)"?', cd)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return url_path.rstrip("/").rsplit("/", 1)[-1] or "document"


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
    if get_settings().grp_contrib_allow_private_hosts:      # development only (config.py)
        return None
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
    drive = _drive_problem(parts)
    if drive:
        return [drive]
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
    current = direct_url(url)
    drive_id = drive_file_id(url)
    confirmed = False
    for _ in range(MAX_REDIRECTS + 2):
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
            if r.status_code >= 400:
                # Name the site, not the pinned address, and say what a contributor can
                # do: publishers that refuse automated downloads (403) are the common case.
                where = f"{parts.scheme}://{parts.hostname}{parts.path}"
                hint = (" — the site refuses automated downloads; download the file yourself "
                        "and share it from Google Drive (Anyone with the link) or another "
                        "public host" if r.status_code in (401, 403, 429) else
                        " — check the link in a browser" if r.status_code == 404 else "")
                raise FetchRefused(f"{where} answered HTTP {r.status_code} "
                                   f"{getattr(r, 'reason', '') or ''}".rstrip() + hint)
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
            host = (parts.hostname or "").lower()
            ctype = (r.headers.get("content-type") or "").lower()
            # A Drive link that ends on ANY web page — the confirm page, or a redirect to
            # accounts.google.com because the file is not shared — never passes as the file.
            if drive_id and ctype.startswith("text/html"):
                # Drive answered a page instead of the file: either the "can't scan
                # for viruses" confirm page (follow its form once) or a sign-in /
                # not-shared page (refuse, in the contributor's terms).
                html = bytes(buf[:200_000]).decode("utf-8", "replace")
                nxt = _confirm_url(html, drive_id) if not confirmed else None
                if nxt and "confirm" in nxt:
                    confirmed = True
                    current = nxt
                    continue
                raise FetchRefused(
                    "Google Drive answered with a web page, not the file — the file is not "
                    "shared with 'Anyone with the link', or the link is a Google Doc/Sheet "
                    "rather than an uploaded file. Share it (Share -> General access -> "
                    "Anyone with the link) and give the file's link again")
            return bytes(buf), _filename(r.headers, parts.path)
        finally:
            r.close()
            session.close()
    raise FetchRefused(f"{url} redirected more than {MAX_REDIRECTS} times")
