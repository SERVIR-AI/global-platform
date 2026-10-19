"""What the server may fetch on a contributor's behalf.

The platform runs on a shared network. A contribution that names a URL makes
the SERVER fetch it, so an unchecked URL is a way to reach hosts the contributor
cannot: private ranges, loopback, link-local metadata endpoints. Every hop of a
fetch is checked here, redirects included; the connection is PINNED to the
address that passed the check (a name that answers a public address to the
check and a private one to the connect — DNS rebinding — gains nothing); the
body is capped and the whole fetch has a wall-clock deadline so one
contribution can neither fill the disk nor hold a worker forever.

What comes back must be the FILE. A login page, a bot challenge (answered 2xx
with an empty body, often), or an error page is refused here in the
contributor's terms — never handed on to be "unreadable CSV" downstream. And a
decline quotes the contributor's own URL, or a redirect target with its query
string cut off: a pre-signed storage URL carries credentials in its query.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
from urllib.parse import parse_qs, urlparse, urljoin

from ..config import get_settings

SCHEMES = ("http", "https")
MAX_REDIRECTS = 5
TIMEOUT = (10, 30)      # connect, per-read (seconds)
DEADLINE = 120          # whole fetch, seconds
# Say who is asking. Measured 2026-10-19: zenodo.org answers a browser string from
# a server 403, figshare's WAF answers it a 202 challenge, Springer a challenge
# page — and all three hand the file to an honest product string. Some hosts still
# refuse non-browsers, so a 401/403 or a challenge is retried ONCE as a browser.
USER_AGENT = "SERVIR-GlobalRiskPlatform/1.0 (fetches a file a contributor named)"
BROWSER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_SHARE = "share it from Google Drive (Anyone with the link)"


class FetchRefused(Exception):
    """A fetch the policy does not allow; the message names why."""


class _Retryable(FetchRefused):
    """A refusal another User-Agent might not get (401/403, a bot challenge)."""


# Google Drive is where hubs keep their maps. A share link ("/file/d/<id>/view")
# answers a web page, not the file; the direct form answers the bytes for any
# file shared with "Anyone with the link". Rewriting it here means every kind —
# document, table, raster, vector — takes the link a contributor actually has.
_DRIVE_HOSTS = ("drive.google.com", "docs.google.com")
_DRIVE_CONTENT_HOST = "drive.usercontent.google.com"
_DRIVE_ID = re.compile(r"/(?:file/)?d/([A-Za-z0-9_-]{10,})")
# Files made IN Google Docs/Sheets/Slides live on docs.google.com too, but they are
# not uploaded files: a Doc is fetched through its export URL, the others refused.
_DOCS_EDITORS = ("/document/", "/spreadsheets/", "/presentation/", "/forms/")
_DOCS_DOCUMENT = re.compile(r"^/document/(?:u/\d+/)?d/([A-Za-z0-9_-]{10,})")


def drive_file_id(url: str) -> str | None:
    """The file id in a Google Drive link, or None when the link is not one."""
    try:
        parts = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in _DRIVE_HOSTS:
        return None
    if any(p in (parts.path or "") for p in _DOCS_EDITORS):
        return None
    m = _DRIVE_ID.search(parts.path or "")
    if m:
        return m.group(1)
    ids = parse_qs(parts.query).get("id") or []
    return ids[0] if ids and re.fullmatch(r"[A-Za-z0-9_-]{10,}", ids[0]) else None


def docs_document_id(url: str) -> str | None:
    """The id of a Google Docs DOCUMENT link (edit, view or export), else None."""
    try:
        parts = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if (parts.hostname or "").lower() != "docs.google.com":
        return None
    m = _DOCS_DOCUMENT.match(parts.path or "")
    return m.group(1) if m else None


def direct_url(url: str) -> str:
    """A Google Drive share link rewritten to its direct-download form, a Google Doc
    to its export (docx keeps the tables); any other URL unchanged."""
    url = str(url).strip()
    did = docs_document_id(url)
    if did:
        if "/export" in urlparse(url).path:          # the contributor chose a format
            return url
        return f"https://docs.google.com/document/d/{did}/export?format=docx"
    fid = drive_file_id(url)
    if not fid:
        return url
    return f"https://drive.google.com/uc?export=download&id={fid}"


def _drive_problem(parts) -> str | None:
    """Why a Google Drive link cannot be fetched as a file, in the contributor's terms."""
    host = (parts.hostname or "").lower()
    if host not in _DRIVE_HOSTS:
        return None
    path = parts.path or ""
    if "/folders/" in path or "/drive/" in path and "/file/" not in path:
        return ("this is a Google Drive FOLDER link; the platform fetches one file per "
                "contribution — open the folder, open the file, Share -> Copy link (access: "
                "Anyone with the link) and give that link. A shapefile's parts go in one .zip "
                "first. If the folder itself asks you to sign in or says it does not exist, it "
                "is not shared or the link is wrong")
    if "/document/" in path:
        # A Doc is fetched as an export; a published (/d/e/.../pub) page goes through
        # as a web page and the gate for its kind says what that means.
        return None
    if any(p in path for p in _DOCS_EDITORS):
        return ("this is a Google Sheets/Slides/Forms link, not an uploaded file; export it "
                "(File -> Download, e.g. as .csv) and share the exported file, or give a "
                "public http(s) URL")
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


def shown(url: str) -> str:
    """A URL as a decline may quote it: scheme, host and path. A redirect target's
    query is where a pre-signed storage URL carries its key and signature."""
    try:
        p = urlparse(str(url))
    except ValueError:
        return "the link"
    return f"{p.scheme}://{p.hostname or ''}{p.path or ''}"


def _quote(current: str, url: str) -> str:
    """The contributor's own URL when this hop is the one they gave, else the
    redirect target without its query."""
    return str(url).strip() if current in (str(url).strip(), direct_url(url)) else shown(current)


# Where a fetch with no account ends up when the file needs one. A redirect THROUGH
# an identity provider that hands straight back (Springer's idp) is not a wall; a
# chain that ENDS on a page or a 401 after touching one of these is.
_LOGIN_HOSTS = ("urs.earthdata.nasa.gov", "accounts.google.com", "login.microsoftonline.com",
                "login.live.com", "auth.globus.org")
_LOGIN_PATH = re.compile(r"/(?:login|log-in|signin|sign-in|sign_in|servicelogin|oauth2?/authorize"
                         r"|cas/login|saml2?/sso)(?:[/?.;]|$)", re.I)


def _login_host(hops: list[str]) -> str | None:
    for h in hops:
        p = urlparse(h)
        if (p.hostname or "").lower() in _LOGIN_HOSTS or _LOGIN_PATH.search(p.path or ""):
            return p.hostname
    return None


# A bot challenge answers the request — often with a 2xx — and is not the file.
_CHALLENGE_BODY = ((b"<title>just a moment", "a Cloudflare 'Just a moment' page"),
                   (b"<title>client challenge", "a 'Client Challenge' page"),
                   (b"challenge-platform", "a Cloudflare challenge page"),
                   (b"cf-chl-", "a Cloudflare challenge page"),
                   (b"<title>attention required! | cloudflare", "a Cloudflare block page"),
                   (b"_incapsula_resource", "an Incapsula challenge page"),
                   (b"ddos-guard", "a DDoS-Guard page"),
                   (b"awswafintegration", "an AWS WAF challenge page"),
                   (b"<title>human verification", "a human-verification page"))


def _challenge(status: int, headers, body: bytes, html: bool) -> str | None:
    """What marks this answer as a bot challenge, or None. Body markers are read
    on a web page only — a PDF may hold any bytes."""
    waf = headers.get("x-amzn-waf-action")
    if waf:
        return f"x-amzn-waf-action: {waf}"
    if str(headers.get("cf-mitigated") or "").lower() == "challenge":
        return "cf-mitigated: challenge"
    if status == 202:
        return "HTTP 202 instead of the file"
    head = body[:8192].lower() if html else b""
    for marker, label in _CHALLENGE_BODY:
        if marker in head:
            return label
    if 200 <= status < 300 and not body:
        return "an empty response, the usual sign of one"
    return None


def _excerpt(headers, body: bytes) -> str | None:
    """The upstream's own words for an error, short and safe to quote: JSON or
    plain-text bodies only, control characters and URL query strings removed."""
    ctype = str(headers.get("content-type") or "").lower()
    if not body or not ("json" in ctype or ctype.startswith("text/plain") or "xml" in ctype):
        return None
    text = body[:4096].decode("utf-8", "replace")
    if "json" in ctype:
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if isinstance(doc, dict):
            for k in ("message", "error_description", "detail", "error", "title", "msg"):
                if isinstance(doc.get(k), str) and doc[k].strip():
                    text = doc[k]
                    break
    elif "xml" in ctype:
        m = re.search(r"<Message>(.*?)</Message>", text, re.S)
        if not m:
            return None
        text = m.group(1)
    text = re.sub(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]*", r"\1", text)
    text = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())
    return (text[:197] + "...") if len(text) > 200 else (text or None)


def fetch(url: str, max_bytes: int | None = None) -> tuple[bytes, str]:
    """Bytes + filename for an allowed URL. Each hop is checked and pinned; the
    body is streamed under the byte cap and the wall-clock deadline. A 401/403 or
    a bot challenge is retried once under the browser User-Agent; when that fails
    too, the first answer's reason is the one given."""
    cap = int(max_bytes or get_settings().grp_contrib_max_bytes)
    started = time.monotonic()
    try:
        return _fetch(url, cap, started, USER_AGENT)
    except _Retryable as first:
        try:
            return _fetch(url, cap, started, BROWSER_AGENT)
        except FetchRefused:
            raise FetchRefused(str(first)) from None


def _fetch(url: str, cap: int, started: float, agent: str) -> tuple[bytes, str]:
    import requests
    current = direct_url(url)
    drive_id = drive_file_id(url)
    is_doc = bool(docs_document_id(url))
    confirmed = False
    hops: list[str] = []
    for _ in range(MAX_REDIRECTS + 2):
        if time.monotonic() - started > DEADLINE:
            raise FetchRefused(f"fetch exceeded the {DEADLINE}s deadline")
        problems = check_url(current)
        if problems:
            raise FetchRefused("; ".join(problems))
        hops.append(current)
        parts = urlparse(current)
        addr = _addresses(parts.hostname)[0]           # already verified global
        host_header = parts.hostname + (f":{parts.port}" if parts.port else "")
        session = _session(parts.hostname, parts.scheme)
        quoted = _quote(current, url)
        try:
            r = session.get(_pinned_url(parts, addr), timeout=TIMEOUT, stream=True,
                            allow_redirects=False,
                            headers={"User-Agent": agent, "Host": host_header})
        except requests.RequestException as exc:
            session.close()
            # requests names the pinned URL, query and all; say it in the site's terms
            raise FetchRefused(f"could not fetch {quoted}: {type(exc).__name__} — the site "
                               "did not answer; check the link in a browser") from None
        try:
            if 300 <= r.status_code < 400:
                nxt = r.headers.get("location")
                if not nxt:
                    raise FetchRefused(f"{quoted} answered {r.status_code} without a location")
                current = urljoin(current, nxt)
                continue
            ctype = (r.headers.get("content-type") or "").lower()
            if r.status_code >= 400:
                body = b""
                for chunk in r.iter_content(chunk_size=8192):
                    body += chunk
                    if len(body) >= 8192:
                        break
                _refuse_page(r.status_code, r.headers, body, hops, url, quoted, drive_id, is_doc,
                             html=ctype.startswith("text/html"))
                # Name the site, not the pinned address, and say what a contributor can
                # do: publishers that refuse automated downloads (403) are the common case.
                said = _excerpt(r.headers, body)
                hint = (" — the site refuses automated downloads; download the file yourself "
                        f"and {_SHARE} or another public host" if r.status_code in (403, 429) else
                        " — check the link in a browser" if r.status_code == 404 else "")
                msg = (f"{quoted} answered HTTP {r.status_code} "
                       f"{getattr(r, 'reason', '') or ''}".rstrip()
                       + (f" — the site says: \"{said}\"" if said else "") + hint)
                raise (_Retryable if r.status_code == 403 else FetchRefused)(msg)
            declared = r.headers.get("content-length")
            # Sized against the link the contributor gave: the hop that answers may be
            # a pre-signed storage URL, and its name means nothing to them anyway.
            if declared and str(declared).isdigit() and int(declared) > cap:
                raise FetchRefused(f"{str(url).strip()} declares {int(declared):,} bytes; the cap "
                                   f"is {cap:,} — " + _TOO_BIG)
            buf = bytearray()
            for chunk in r.iter_content(chunk_size=65536):
                buf.extend(chunk)
                if len(buf) > cap:
                    raise FetchRefused(f"{str(url).strip()} exceeds the {cap:,}-byte cap — "
                                       + _TOO_BIG)
                if time.monotonic() - started > DEADLINE:
                    raise FetchRefused(f"fetch exceeded the {DEADLINE}s deadline")
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
            page = (ctype.startswith("text/html")
                    or bytes(buf[:256]).lstrip().lower().startswith((b"<!doctype html", b"<html")))
            _refuse_page(r.status_code, r.headers, bytes(buf), hops, url, quoted, drive_id,
                         is_doc, html=page)
            return bytes(buf), _filename(r.headers, parts.path)
        finally:
            r.close()
            session.close()
    raise FetchRefused(f"{url} redirected more than {MAX_REDIRECTS} times")


_TOO_BIG = ("contribute one layer per file (an archive of several is fetched whole), or clip "
            "it to your area, and give that file's link")


def _refuse_page(status, headers, body, hops, url, quoted, drive_id, is_doc, html=True) -> None:
    """Refuse an answer that is a login wall or a bot challenge rather than the
    file — login first: a sign-in page is not a 'portal page' to find a link on."""
    if drive_id:
        return                      # Drive's own pages are read by the Drive branch
    who = _login_host(hops) if (html or status in (401, 403)) else None
    if who or status == 401:
        who = who or urlparse(hops[-1]).hostname
        if is_doc:
            raise FetchRefused(
                f"this Google Doc needs a login at {who} — it is not shared publicly. Share it "
                "(Share -> General access -> Anyone with the link) and give the link again, or "
                f"export it (File -> Download) and {_SHARE}")
        raise FetchRefused(f"this file needs a login at {who} — download it with an account "
                           f"and {_SHARE}, then give that link (you gave {url})")
    why = _challenge(status, headers, body, html)
    if why:
        raise _Retryable(f"{quoted} answered with a bot challenge ({why}), not the file — the "
                         f"site will not hand it to a server. Download it yourself and {_SHARE}, "
                         "then give that link")
