"""Published catalogs (STAC 1.1 in the Portolan layout) as a third source of raster
layers, after the hand-authored rows and the gate's contributed rows.

`GRP_CATALOG_URL` names one or more `catalog.json` roots (URLs or local paths). Each
root is walked (catalog -> child collections -> items) and every item that carries a
Cloud Optimized GeoTIFF becomes an ordinary catalog row, so every raster primitive,
every pack and `platform_capabilities` see it with no further code. A root cannot
shadow a row the hand or contributed files already hold.

A root that cannot be read is never a silent fallback: `status()` says so, the
capabilities map lists it, and every pack assembled meanwhile carries a declared
gap line (`gap_lines()`). A root read once keeps serving its last good rows while it
is unreachable, and says since when.

The data file of a catalog row is mirrored into `tiffs_dir` on first use and verified
against the item's `file:checksum` (`mirror()`); with GRP_CATALOG_MIRROR=0 it is opened
in place over HTTP range requests, and only under a configured root (`remote_allowed`).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ...config import get_settings

COG_TYPES = ("image/tiff; application=geotiff; profile=cloud-optimized",
             "image/tiff; application=geotiff")
USER_AGENT = "servirplatform (+https://github.com/SERVIR-AI/global-platform)"
_STATE: dict = {"key": None, "at": 0.0, "roots": {}, "stamp": ""}
_LOCK = threading.RLock()
_MAX_DOC = 64 * 1024 * 1024          # a catalog document larger than this is refused


def roots() -> list[str]:
    raw = str(get_settings().grp_catalog_url or "")
    return [r.strip() for r in raw.replace("\n", ",").split(",") if r.strip()]


def _is_url(s: str) -> bool:
    return s.startswith("http://") or s.startswith("https://")


def _join(base: str, href: str) -> str:
    """Resolve an href against the document it came from (URL or local path)."""
    if _is_url(href) or href.startswith("/vsi"):
        return href
    if _is_url(base):
        return urllib.parse.urljoin(base, href)
    if os.path.isabs(href):
        return href
    return os.path.normpath(os.path.join(os.path.dirname(base), href))


def _fetch(loc: str, etag: str | None = None, timeout: float = 15.0):
    """(status, bytes|None, etag) for a URL or a local file. 304 carries no body."""
    if _is_url(loc):
        req = urllib.request.Request(loc, headers={"User-Agent": USER_AGENT,
                                                   **({"If-None-Match": etag} if etag else {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(_MAX_DOC + 1)
                if len(body) > _MAX_DOC:
                    raise ValueError(f"{loc} is larger than {_MAX_DOC} bytes")
                return resp.status, body, resp.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 304, None, etag
            raise
    st = os.stat(loc)
    tag = f"{st.st_mtime_ns}:{st.st_size}"
    if etag and tag == etag:
        return 304, None, etag
    with open(loc, "rb") as f:
        return 200, f.read(), tag


def _json(loc: str) -> dict:
    status, body, _ = _fetch(loc)
    doc = json.loads(body or b"{}")
    if not isinstance(doc, dict):
        raise ValueError(f"{loc} is not a JSON object")
    return doc


def _links(doc: dict, rel: str) -> list[str]:
    return [str(link.get("href")) for link in doc.get("links") or []
            if isinstance(link, dict) and link.get("rel") == rel and link.get("href")]


def _checksum(asset: dict) -> str | None:
    """A sha256 hex from the file extension's multihash (0x12 0x20 + 32 bytes)."""
    raw = str(asset.get("file:checksum") or "").lower()
    if raw.startswith("1220") and len(raw) == 68:
        return raw[4:]
    return None


def _data_asset(doc: dict) -> tuple[str, dict] | None:
    """The one CURRENT raster data asset of an item or collection: the asset in the
    `data` role (never an `archive`, a superseded version kept for replay), else a COG
    by media type, else a .tif in the data role."""
    assets = doc.get("assets") or {}
    live = {k: a for k, a in assets.items() if isinstance(a, dict)
            and not ({"archive", "deprecated"} & set(a.get("roles") or []))}
    for key, asset in live.items():
        if "data" in (asset.get("roles") or []) and str(asset.get("type") or "").lower() in COG_TYPES:
            return key, asset
    for key, asset in live.items():
        if str(asset.get("type") or "").lower() in COG_TYPES:
            return key, asset
    for key, asset in live.items():
        if "data" in (asset.get("roles") or []) and str(asset.get("href") or "").lower().endswith((".tif", ".tiff")):
            return key, asset
    return None


def _band(asset: dict, props: dict) -> dict:
    bands = asset.get("bands") or asset.get("raster:bands") or props.get("bands") or props.get("raster:bands") or [{}]
    return bands[0] if isinstance(bands, list) and bands and isinstance(bands[0], dict) else {}


def _legend(asset: dict, props: dict) -> dict:
    """{class: label}: the platform's own `servir:legend` when present, else the band's
    classification classes."""
    own = props.get("servir:legend")
    if isinstance(own, dict) and own:
        out = {}
        for k, v in own.items():
            try:
                out[int(k)] = str(v).strip()
            except (TypeError, ValueError):
                continue
        return out
    classes = _band(asset, props).get("classification:classes") or props.get("classification:classes")
    out = {}
    for c in classes or []:
        if isinstance(c, dict) and c.get("value") is not None and c.get("name"):
            out[int(c["value"])] = str(c.get("description") or c["name"])
    return out


def _vintage(props: dict) -> str | None:
    v = props.get("servir:vintage")
    if v:
        return str(v)
    for k in ("datetime", "start_datetime"):
        if props.get(k):
            return str(props[k])[:7]
    return None


def _row(root: str, coll: dict, item: dict, item_loc: str, asset_key: str, asset: dict):
    """One catalog row (the shape every raster primitive reads) from a STAC item."""
    props = item.get("properties") or {}
    coll_id = str(coll.get("id") or "")
    item_id = str(item.get("id") or "")
    href = _join(item_loc, str(asset.get("href")))
    key = str(props.get("servir:layer") or coll_id or item_id)
    bbox = item.get("bbox") or coll.get("extent", {}).get("spatial", {}).get("bbox", [[None] * 4])[0]
    producers = [p.get("name") for p in coll.get("providers") or []
                 if isinstance(p, dict) and "producer" in (p.get("roles") or [])]
    licence = str(coll.get("license") or props.get("license") or "")
    if licence in ("other", "") and props.get("servir:license"):
        licence = str(props["servir:license"])
    row = {"title": str(props.get("title") or coll.get("title") or key),
           "description": str(props.get("description") or coll.get("description") or ""),
           "legend": _legend(asset, props),
           "source": str(props.get("servir:source") or ", ".join(p for p in producers if p) or ""),
           "license": licence,
           "contributed": True,
           "path": f"/vsicurl/{href}" if _is_url(href) else href,
           "local_path": f"tiffs/stac__{coll_id}__{item_id}.tif",
           "stac": {"root": root, "collection": coll_id, "item": item_id, "href": href,
                    "checksum": _checksum(asset), "size": asset.get("file:size"),
                    "item_url": item_loc, "asset": asset_key,
                    "updated": coll.get("updated") or props.get("updated")}}
    vintage = _vintage(props)
    if vintage:
        row["vintage"] = vintage
    if bbox and None not in bbox[:4]:
        row["extent"] = {"lon": [float(bbox[0]), float(bbox[2])],
                         "lat": [float(bbox[1]), float(bbox[3])]}
    for link in _links(coll, "cite-as") + _links(coll, "about") + _links(item, "cite-as"):
        row["url"] = link
        break
    for k in ("role", "countries", "outside_values", "usage_notes", "authority", "accuracy",
              "review", "contribution_id", "colors", "points", "pixel_size_deg"):
        if props.get(f"servir:{k}") is not None:
            row[k] = props[f"servir:{k}"]
    band = _band(asset, props)
    contract = dict(props.get("servir:contract") or {})
    if not contract:
        contract = {k: v for k, v in (("dtype", band.get("data_type")), ("nodata", band.get("nodata")),
                                      ("units", band.get("unit")),
                                      ("scale", band.get("raster:scale", band.get("scale"))),
                                      ("pixel_size_deg", band.get("raster:spatial_resolution",
                                                                  band.get("spatial_resolution"))))
                    if v is not None}
    if row.get("role") and "role" not in contract:
        contract["role"] = row["role"]
    stack = props.get("servir:stack")
    if stack and props.get("servir:year") is not None:
        row.update({"stack": str(stack), "year": int(props["servir:year"]), "internal": True})
    return key, row, contract


def _walk(root: str) -> dict:
    """Rows, contracts and stack rows of one root. Raises on anything unreadable."""
    cat = _json(root)
    kind = str(cat.get("type") or "")
    if kind not in ("Catalog", "Collection"):
        raise ValueError(f"{root}: type {kind!r} is not a STAC Catalog or Collection")
    coll_locs = [(_join(root, h)) for h in _links(cat, "child")] if kind == "Catalog" else [root]
    rows, contracts, stacks, n = {}, {}, {}, 0
    for loc in coll_locs:
        coll = cat if loc == root and kind == "Collection" else _json(loc)
        if str(coll.get("type")) != "Collection":
            continue
        n += 1
        found = []
        for ih in _links(coll, "item"):
            iloc = _join(loc, ih)
            item = _json(iloc)
            da = _data_asset(item)
            if da:
                found.append(_row(root, coll, item, iloc, *da))
        if not found:
            da = _data_asset(coll)
            if da:                                   # a single-file collection: the data sits on it
                interval = ((coll.get("extent") or {}).get("temporal") or {}).get("interval") or [[None, None]]
                props = {k: v for k, v in coll.items() if str(k).startswith("servir:")}
                props.update({"title": coll.get("title"), "description": coll.get("description"),
                              "datetime": interval[0][0] if interval and interval[0] else None,
                              "deprecated": bool(coll.get("deprecated"))})
                pseudo = {"id": str(coll.get("version") or coll.get("id")), "properties": props}
                found.append(_row(root, coll, pseudo, loc, *da))
        for key, row, contract in found:
            # a deprecated item (an older version of the same layer) never shadows the
            # current one; it stays reachable through the stac block of the current row
            if row.get("stac") and (key in rows) and not _is_current(row, rows[key]):
                continue
            rows[key] = row
            if contract:
                contracts[key] = contract
        if found and all(r.get("stack") for _, r, _ in found):
            sid = str(found[0][1]["stack"])
            years = sorted({int(r["year"]) for _, r, _ in found})
            first = found[0][1]
            stacks[sid] = {"kind": "stack", "years": years, "layer_pattern": sid + "_{year}",
                           "title": str(coll.get("title") or sid),
                           "description": str(coll.get("description") or ""),
                           **{k: first[k] for k in ("legend", "source", "license", "vintage",
                                                   "url", "extent", "countries", "role")
                              if k in first}}
            if found[0][2]:
                stacks[sid]["_contract"] = found[0][2]
    return {"rows": rows, "contracts": contracts, "stacks": stacks, "collections": n}


def _is_current(new: dict, old: dict) -> bool:
    """Of two rows for one key, the newer item wins: an item marked deprecated loses,
    else the later `datetime`/vintage, else the later item id."""
    nd, od = bool(new.get("deprecated")), bool(old.get("deprecated"))
    if nd != od:
        return od
    return (str(new.get("vintage") or ""), str(new["stac"]["item"])) >= (
        str(old.get("vintage") or ""), str(old["stac"]["item"]))


def refresh(force: bool = False) -> None:
    """Re-read every root at most every `grp_catalog_refresh_seconds` (one conditional
    GET of catalog.json each; a 304 keeps the last walk). Cheap when nothing is set."""
    settings = get_settings()
    key = tuple(roots())
    with _LOCK:
        now = time.monotonic()
        if (not force and _STATE["key"] == key
                and now - _STATE["at"] < float(settings.grp_catalog_refresh_seconds)):
            return
        if _STATE["key"] != key:
            _STATE["roots"] = {}
        for root in key:
            prev = _STATE["roots"].get(root) or {}
            entry = {**prev, "root": root}
            try:
                status, _, etag = _fetch(root, prev.get("etag"))
                if status == 304 and prev.get("rows") is not None:
                    entry.update({"state": "ok", "detail": "unchanged", "checked_at": _now()})
                else:
                    walked = _walk(root)
                    entry.update({**walked, "etag": etag, "state": "ok", "detail": "",
                                  "fetched_at": _now(), "checked_at": _now()})
            except Exception as exc:
                invalid = isinstance(exc, (ValueError, json.JSONDecodeError, KeyError, TypeError))
                entry.update({"state": ("stale" if prev.get("rows") is not None else
                                        "invalid" if invalid else "unreachable"),
                              "detail": f"{type(exc).__name__}: {exc}", "checked_at": _now()})
            _STATE["roots"][root] = entry
        _STATE["key"] = key
        _STATE["at"] = now
        _STATE["stamp"] = hashlib.sha1(json.dumps(
            {r: (e.get("rows"), e.get("state")) for r, e in _STATE["roots"].items()},
            sort_keys=True, default=str).encode()).hexdigest() if key else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp() -> str:
    """Changes whenever any root's rows or state change — part of the catalog memo key."""
    refresh()
    return _STATE["stamp"]


def rows() -> dict:
    """{layer: row} across every root, first root wins on a clash (recorded in status)."""
    refresh()
    out: dict = {}
    for root in roots():
        entry = _STATE["roots"].get(root) or {}
        shadowed = []
        for k, v in (entry.get("rows") or {}).items():
            if k in out:
                shadowed.append(k)
            else:
                out[k] = v
        for sid, srow in (entry.get("stacks") or {}).items():
            if sid not in out:
                out[sid] = {k: v for k, v in srow.items() if k != "_contract"}
        entry["shadowed_by_roots"] = shadowed
    return out


def contracts() -> tuple[dict, dict]:
    """({layer: contract}, {stack: contract}) across every root."""
    refresh()
    layers, stacks = {}, {}
    for root in roots():
        entry = _STATE["roots"].get(root) or {}
        for k, v in (entry.get("contracts") or {}).items():
            layers.setdefault(k, v)
        for sid, srow in (entry.get("stacks") or {}).items():
            if srow.get("_contract"):
                stacks.setdefault(sid, srow["_contract"])
    return layers, stacks


def status() -> list[dict]:
    """One line per configured root: state, since when, what it holds."""
    refresh()
    out = []
    for root in roots():
        e = _STATE["roots"].get(root) or {"state": "unknown"}
        out.append({"root": root, "state": e.get("state"), "detail": e.get("detail") or "",
                    "fetched_at": e.get("fetched_at"), "checked_at": e.get("checked_at"),
                    "collections": e.get("collections", 0),
                    "layers": sorted((e.get("rows") or {}).keys()),
                    "shadowed_by_roots": e.get("shadowed_by_roots") or []})
    return out


def gap_lines() -> list[str]:
    """What a pack must say when a root is not healthy: nothing is skipped silently."""
    lines = []
    for e in status():
        if e["state"] == "ok":
            continue
        n = len(e["layers"])
        if e["state"] == "stale":
            lines.append(f"published catalog {e['root']} unreachable since {e['checked_at']} "
                         f"({e['detail']}); its {n} layers are served from the copy read at "
                         f"{e['fetched_at']}")
        else:
            lines.append(f"published catalog {e['root']} could not be read "
                         f"({e['state']}: {e['detail']}); any layer it holds is unavailable")
    return lines


def shadowed(local_keys) -> list[str]:
    """Catalog layers a local row already names (the local row wins; said, not hidden)."""
    return sorted(k for k in rows() if k in set(local_keys))


# --- the read path -------------------------------------------------------------------

def _prefixes() -> list[str]:
    return [root.rsplit("/", 1)[0] + "/" if _is_url(root) else os.path.dirname(root) + os.sep
            for root in roots()]


def remote_allowed(path: str) -> bool:
    """May GDAL open this path? Only a file under a configured catalog root; a local
    file always. Nothing a row names can send the server to another host."""
    p = str(path or "")
    if p.startswith("/vsicurl/"):
        p = p[len("/vsicurl/"):]
    if not _is_url(p):
        return not p.startswith("/vsi") or p.startswith("/vsizip/")
    return any(p.startswith(pre) for pre in _prefixes() if _is_url(pre))


def remote_env() -> dict:
    """The GDAL settings a read-through open runs under: no directory listing, no HEAD
    probes, an explicit User-Agent (Cloudflare refuses the default), bounded waits."""
    return {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR", "CPL_VSIL_CURL_USE_HEAD": "NO",
            "GDAL_HTTP_USERAGENT": USER_AGENT, "GDAL_HTTP_TIMEOUT": "60",
            "GDAL_HTTP_CONNECTTIMEOUT": "10", "GDAL_HTTP_MAX_RETRY": "2",
            "GDAL_HTTP_RETRY_DELAY": "1", "GDAL_HTTP_NETRC": "NO"}


def apply_remote_env() -> None:
    """Set the read-through settings process-wide (GDAL reads them from the environment
    at every open), without overriding anything the operator set."""
    for k, v in remote_env().items():
        os.environ.setdefault(k, v)


def mirror(layer: str, row: dict, dest: str) -> str:
    """Copy a catalog row's file to `dest` once and verify it against the item's
    checksum; a mismatch is refused in plain words and leaves nothing behind."""
    if os.path.exists(dest):
        return dest
    info = row.get("stac") or {}
    href = str(info.get("href") or "")
    if _is_url(href) and not remote_allowed(href):
        raise RuntimeError(f"{layer}: {href} is outside every configured catalog root; refused")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    from .ingest import atomic_path, emit
    t0 = time.perf_counter()
    digest = hashlib.sha256()
    size = 0
    with atomic_path(dest) as tmp:
        if _is_url(href):
            req = urllib.request.Request(href, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        else:
            with open(href, "rb") as src, open(tmp, "wb") as out:
                while True:
                    chunk = src.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        expected = info.get("checksum")
        if expected and digest.hexdigest() != expected:
            raise RuntimeError(f"{layer}: the file served at {href} does not match the "
                               f"catalog's checksum (sha256 {digest.hexdigest()[:12]}… vs "
                               f"{expected[:12]}…); not used")
    emit({"kind": "download", "what": "source_raster", "api": "published catalog",
          "layer": layer, "url": href, "bytes": size, "sha256": digest.hexdigest(),
          "verified": bool(info.get("checksum")), "seconds": round(time.perf_counter() - t0, 2),
          "dest": dest, "was_cached": False})
    return dest
