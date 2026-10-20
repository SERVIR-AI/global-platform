"""A public Google Drive folder listed as a browser sees it: the folder's embedded view
(drive.google.com/embeddedfolderview?id=<id>) names every file and subfolder with its id,
subfolders included (a client that reads Drive's JSON listing missed one in the sprint
folders). Each hop is checked and pinned by fetch_policy, a page is read up to PAGE_BYTES,
and the walk is bounded by MAX_FOLDERS and MAX_DEPTH. Not an op (the registry skips '_')."""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

LISTING = "https://drive.google.com/embeddedfolderview?id={id}"
FILE = "https://drive.google.com/file/d/{id}/view"
PAGE_BYTES = 5 << 20
MAX_FOLDERS = 200
MAX_DEPTH = 8
RETRY_SECONDS = 2.0     # the pause before listing a folder again after Drive answered 5xx

_ID = r"[A-Za-z0-9_-]{10,}"
_FOLDER_PATH = re.compile(rf"/folders/({_ID})")
_ENTRY = re.compile(r'<div class="flip-entry" id="entry-([A-Za-z0-9_-]+)"')
_HREF = re.compile(r'<a href="([^"]*)"')
_TITLE = re.compile(r'<div class="flip-entry-title">(.*?)</div>', re.S)

NOT_SHARED = ("Google Drive did not list the folder {url}: it is not shared with 'Anyone "
              "with the link', or the link is wrong. Share the folder (Share -> General access "
              "-> Anyone with the link) and resubmit")


@dataclass(frozen=True)
class Entry:
    path: str          # inside the folder, '/'-separated: 'Costa Rica/cuencas/x.shp'
    id: str
    folder: bool = False

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def parent(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""


def folder_id(url) -> str | None:
    """The folder id of a Google Drive folder link (…/drive/folders/<id>, …/drive/u/0/
    folders/<id>, embeddedfolderview?id=<id>), else None."""
    from .. import fetch_policy
    try:
        parts = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in fetch_policy._DRIVE_HOSTS:
        return None
    m = _FOLDER_PATH.search(parts.path or "")
    if m:
        return m.group(1)
    if (parts.path or "").rstrip("/").endswith("/embeddedfolderview"):
        ids = parse_qs(parts.query).get("id") or []
        return ids[0] if ids and re.fullmatch(_ID, ids[0]) else None
    return None


def entries(page: str) -> list[tuple[str, str, bool]]:
    """(id, name, is a folder) for each entry of an embedded folder view page."""
    out = []
    starts = [m.start() for m in _ENTRY.finditer(page)] + [len(page)]
    for a, b in zip(starts, starts[1:]):
        chunk = page[a:b]
        eid = _ENTRY.match(chunk).group(1)
        href = _HREF.search(chunk)
        title = _TITLE.search(chunk)
        if not title:
            continue
        name = html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip()
        name = re.sub(r"[/\\]", "_", name)          # a '/' in a Drive name is no path
        link = html.unescape(href.group(1)) if href else ""
        out.append((eid, name, f"/folders/{eid}" in link))
    return out


def walk(fid: str, url: str, ctx) -> tuple[list[Entry], int]:
    """Every file under the folder (subfolders too, breadth first) and how many folders
    were listed. A subfolder deeper than MAX_DEPTH, or more than MAX_FOLDERS folders,
    is declined rather than silently cut."""
    from . import PrepRefused
    files: list[Entry] = []
    queue, seen = [(fid, "", 0)], {fid}
    listed = 0
    while queue:
        ctx.check_time()
        current, prefix, depth = queue.pop(0)
        listed += 1
        if listed > MAX_FOLDERS:
            raise PrepRefused(f"the folder {url} holds more than {MAX_FOLDERS} subfolders — give "
                              "the link of the subfolder that holds the layer")
        for eid, name, is_folder in entries(page(LISTING.format(id=current), url, ctx)):
            path = f"{prefix}{name}"
            if is_folder:
                if eid in seen:
                    continue
                if depth + 1 > MAX_DEPTH:
                    raise PrepRefused(f"the folder {url} nests subfolders deeper than {MAX_DEPTH} "
                                      f"levels ({path}) — give the link of the subfolder that "
                                      "holds the layer")
                seen.add(eid)
                queue.append((eid, path + "/", depth + 1))
            else:
                files.append(Entry(path, eid))
    return files, listed


def page(listing: str, url: str, ctx) -> str:
    """The HTML of a listing page, through fetch_policy's per-hop checks and pinning (no
    private address, no redirect off to one); a sign-in, 401/403/404 says the folder is not
    shared. `url` is the contributor's folder link, quoted in a decline."""
    import requests
    from .. import fetch_policy as fp
    from . import PrepRefused
    current, hops = listing, []
    for _ in range(fp.MAX_REDIRECTS + 1):
        ctx.check_time()
        problems = fp.check_url(current)
        if problems:
            raise PrepRefused(f"the platform cannot list {url}: " + "; ".join(problems))
        hops.append(current)
        if fp._login_host([current]):
            raise PrepRefused(NOT_SHARED.format(url=url))
        parts = urlparse(current)
        try:
            addr = fp._pin(parts.hostname)                # resolved and checked again
        except fp.FetchRefused as exc:
            raise PrepRefused(f"the platform cannot list {url}: {exc}") from None
        session = fp._session(parts.hostname, parts.scheme)
        host = parts.hostname + (f":{parts.port}" if parts.port else "")
        for attempt in (1, 2):                    # once more if Drive stalls or answers 5xx
            try:
                r = session.get(fp._pinned_url(parts, addr), timeout=fp.TIMEOUT, stream=True,
                                allow_redirects=False,
                                headers={"User-Agent": fp.USER_AGENT, "Host": host})
            except requests.RequestException as exc:
                if attempt == 2:
                    session.close()
                    raise PrepRefused(f"Google Drive did not answer when the platform listed "
                                      f"{url} ({type(exc).__name__}) — try again") from None
                ctx.check_time()
                continue
            if r.status_code >= 500 and attempt == 1:
                r.close()
                time.sleep(RETRY_SECONDS)
                ctx.check_time()
                continue
            break
        try:
            if 300 <= r.status_code < 400 and r.headers.get("location"):
                current = urljoin(current, r.headers["location"])
                continue
            if r.status_code in (401, 403, 404):
                raise PrepRefused(NOT_SHARED.format(url=url))
            if r.status_code >= 300:
                raise PrepRefused(f"Google Drive answered HTTP {r.status_code} when the platform "
                                  f"listed {url} — try again later")
            body = bytearray()
            for chunk in r.iter_content(chunk_size=65536):
                body.extend(chunk)
                if len(body) > PAGE_BYTES:
                    raise PrepRefused(f"the listing of {url} is over {PAGE_BYTES >> 20} MB — give "
                                      "the link of the subfolder that holds the layer")
                ctx.check_time()
        finally:
            r.close()
            session.close()
        text = body.decode("utf-8", "replace")
        if "flip-entries" not in text:
            raise PrepRefused(NOT_SHARED.format(url=url))
        return text
    raise PrepRefused(f"Google Drive redirected more than {fp.MAX_REDIRECTS} times when the "
                      f"platform listed {url}")
