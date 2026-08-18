"""Live design tokens from the Figma file, read at call time.

WHY THIS IS HARDER THAN IT SHOULD BE. Figma's Variables endpoint is the only one
that returns the designers' DECLARED tokens, and it is Enterprise-only. This plan
does not offer the `file_variables:read` scope, and the file has 0 published styles
and 0 published components, so there is no clean declared source anywhere. We
therefore read the document tree, which means INFERRING tokens from a rendered
page.

That inference is lossy, so this module reports what it could not resolve instead
of guessing. Two real conflicts already exist in the file: two different names
carry the same hex, and one label is typed with a doubled hash. Both are surfaced
as `conflicts`, the same discipline the platform applies to declared gaps.

Colour swatches are a RECTANGLE (the true fill) beside a TEXT label carrying the
name and the hex a human typed. Those can disagree. They are paired by geometry
and compared, because a label that has drifted from its swatch is exactly the kind
of silent wrongness this platform exists to catch.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import requests

from ..config import get_settings

_API = "https://api.figma.com/v1"
_TIMEOUT = (10, 45)
_TTL = 600                       # 10 min: designers edit in bursts, hubs call in loops
_CACHE: dict = {}                # key -> (fetched_at, payload)
_LABEL = re.compile(r"^\s*(?P<name>[^\n#]+?)\s*\n\s*#{1,2}(?P<hex>[0-9A-Fa-f]{6})\s*$")


class FigmaUnavailable(Exception):
    """Upstream refused or the file is unreadable. Callers fall back to config."""


def _hex(color: dict) -> str:
    return "#%02X%02X%02X" % tuple(round(color[c] * 255) for c in "rgb")


def _get(path: str, token: str) -> dict:
    try:
        r = requests.get(f"{_API}/{path}", headers={"X-Figma-Token": token},
                         timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise FigmaUnavailable(f"figma unreachable: {exc}") from exc
    if r.status_code != 200:
        raise FigmaUnavailable(f"figma returned HTTP {r.status_code} for {path}")
    return r.json()


def cached(key: str, build, ttl: int = _TTL) -> dict:
    """Short cache in front of Figma, with LAST-GOOD fallback.

    A design file is not a live datafeed; re-fetching per `ui_design` call makes a
    third party's uptime into ours and burns rate limit for nothing. On failure the
    last good copy is served FLAGGED rather than dropping the caller to config,
    because a ten-minute-old design file beats a hand-maintained copy of unknown age.
    """
    hit = _CACHE.get(key)
    now = datetime.now(timezone.utc).timestamp()
    if hit and now - hit[0] < ttl:
        return {**hit[1], "cache": {"hit": True, "age_s": int(now - hit[0]),
                                    "served_stale": False}}
    try:
        out = build()
    except FigmaUnavailable:
        if hit is None:
            raise
        age = int(now - hit[0])
        return {**hit[1], "cache": {"hit": True, "age_s": age, "served_stale": True,
                                    "reason": f"figma unreachable; last good copy is "
                                              f"{age // 60} min old"}}
    _CACHE[key] = (now, out)
    return {**out, "cache": {"hit": False, "age_s": 0, "served_stale": False}}


def invalidate(reason: str = "manual") -> dict:
    """Drop the cache. Wired to a Figma webhook later; `webhooks:write` is in scope."""
    n = len(_CACHE)
    _CACHE.clear()
    return {"status": "ok", "cleared": n, "reason": reason}


def _walk(node: dict, out: list) -> list:
    out.append(node)
    for child in node.get("children", []):
        _walk(child, out)
    return out


def _box(n: dict) -> dict:
    return n.get("absoluteBoundingBox") or {}


def _swatch_hexes(nodes: list) -> set:
    """Every solid fill actually rendered on the page."""
    out = set()
    for n in nodes:
        if n.get("type") == "RECTANGLE":
            c = (n.get("fills") or [{}])[0].get("color")
            if c:
                out.add(_hex(c))
    return out


def _read_tokens_uncached(file_key: str | None = None, token: str | None = None) -> dict:
    """Colour tokens as the design file currently defines them, plus what is
    ambiguous about them."""
    s = get_settings()
    token = token or s.figma_token
    file_key = file_key or s.figma_file_key
    if not token:
        raise FigmaUnavailable("no FIGMA_TOKEN configured")

    doc = _get(f"files/{file_key}", token)
    nodes = _walk(doc["document"], [])
    swatches = _swatch_hexes(nodes)

    colors, conflicts, seen = {}, [], {}
    for n in nodes:
        if n.get("type") != "TEXT":
            continue
        m = _LABEL.match(n.get("characters", ""))
        if not m:
            continue
        name, typed = m.group("name").strip(), "#" + m.group("hex").upper()
        raw = n["characters"]
        if "##" in raw:
            conflicts.append({"kind": "malformed_label", "name": name,
                              "detail": "hex is written with a doubled hash in the file",
                              "raw": raw.replace("\n", " ")})
        # The LABEL is the declaration: a human typed both the name and the hex, and
        # it is self-contained. Pairing a label to its swatch by geometry looked
        # obvious and was wrong — swatches sit BETWEEN labels here, so one label's
        # swatch is to its right and the next one's is to its left. Rather than
        # guess a layout, cross-check against the page as a whole: a label whose hex
        # is rendered nowhere is drift worth reporting.
        value = typed
        if value not in swatches:
            conflicts.append({"kind": "value_not_rendered", "name": name,
                              "detail": f"{value} is labelled but no swatch on the page uses it",
                              "using": value})
        if value in seen and seen[value] != name:
            conflicts.append({"kind": "duplicate_value", "name": name,
                              "detail": f"{value} is also named {seen[value]!r}",
                              "using": value})
        seen.setdefault(value, name)
        colors[name] = value

    # Type scale, gathered from what the file actually sets on text. Same caveat as
    # colour: with no Variables API these are observed, not declared, so they are
    # reported as a scale a builder can read rather than as named tokens.
    seen_type: dict = {}
    for n in nodes:
        st = n.get("style") or {}
        if n.get("type") != "TEXT" or not st.get("fontFamily"):
            continue
        key = (st["fontFamily"], st.get("fontWeight"), round(st.get("fontSize", 0)))
        seen_type.setdefault(key, 0)
        seen_type[key] += 1
    typography = [{"family": f, "weight": w, "size_px": s, "used_on_nodes": c}
                  for (f, w, s), c in sorted(seen_type.items(), key=lambda kv: -kv[0][2])]

    # The file's own declared styles. Only 4 here, but they carry descriptions and
    # are the closest thing to an intentional token in a file with no Variables.
    styles = [{"name": v.get("name"), "type": v.get("styleType"),
               "description": v.get("description") or None}
              for v in (doc.get("styles") or {}).values()]

    return {
        "source": "figma",
        "colors": colors,
        "typography": typography,
        "declared_styles": styles,
        "conflicts": conflicts,
        "provenance": {
            "file": doc.get("name"), "file_key": file_key,
            "version": doc.get("version"),
            "last_modified": doc.get("lastModified"),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "method": "document-tree inference",
            "caveat": ("Figma's Variables API is Enterprise-only and unavailable on this "
                       "plan, and the file publishes no library styles or components. "
                       "These tokens are read off a rendered page, so they inherit "
                       "whatever is on it. See `conflicts`."),
        },
    }


def _variants(name: str) -> dict:
    """Figma encodes variant props in the component name: "Treatment=Black,
    Layout=Stacked". Parsed so a builder can ask for what they need instead of
    matching a string."""
    out = {}
    for part in name.split(","):
        k, _, v = part.partition("=")
        if v:
            out[k.strip().lower().replace(" ", "_")] = v.strip()
    return out


def _read_components_uncached(file_key: str | None = None, token: str | None = None) -> dict:
    """The design file's published UI, as sets with their variants.

    This matters more than colour here: the platform REQUIRES every UI built on it
    to show the SERVIR identity, and the file carries the whole logo system with
    treatments for light, dark and mono grounds. Without this a builder gets one
    hardcoded URL from config and no way to ask for the right treatment.
    """
    s = get_settings()
    token = token or s.figma_token
    file_key = file_key or s.figma_file_key
    if not token:
        raise FigmaUnavailable("no FIGMA_TOKEN configured")

    doc = _get(f"files/{file_key}", token)
    nodes = _walk(doc["document"], [])
    parent = {c.get("id"): n.get("id") for n in nodes for c in n.get("children", [])}

    sets: dict[str, dict] = {}
    for n in nodes:
        if n.get("type") == "COMPONENT_SET":
            sets[n.get("id")] = {"name": n.get("name"), "node_id": n.get("id"),
                                 "variants": []}
    for n in nodes:
        if n.get("type") != "COMPONENT":
            continue
        pid = parent.get(n.get("id"))
        entry = {"node_id": n.get("id"), "label": n.get("name"),
                 **_variants(n.get("name", ""))}
        if pid in sets:
            sets[pid]["variants"].append(entry)
        else:                                   # a standalone component, no set
            sets.setdefault(n.get("id"), {"name": n.get("name"), "node_id": n.get("id"),
                                      "variants": []})["variants"].append(entry)

    return {"source": "figma", "sets": sorted(sets.values(), key=lambda s: s["name"] or ""),
            "provenance": {"file": doc.get("name"), "file_key": file_key,
                           "version": doc.get("version"),
                           "last_modified": doc.get("lastModified"),
                           "fetched_at": datetime.now(timezone.utc).isoformat()}}


def render(node_ids: list[str], fmt: str = "svg", file_key: str | None = None,
           token: str | None = None) -> dict:
    """Figma-rendered images for nodes, as SHORT-LIVED URLs.

    Figma expires these (roughly 30 days), so they are never a substitute for a
    hosted asset — a page that hotlinks one will break silently later. Callers get
    the expiry stated rather than discovering it.
    """
    s = get_settings()
    token = token or s.figma_token
    file_key = file_key or s.figma_file_key
    if not token:
        raise FigmaUnavailable("no FIGMA_TOKEN configured")
    if fmt not in ("svg", "png", "pdf", "jpg"):
        raise FigmaUnavailable(f"unsupported image format {fmt!r}")
    ids = ",".join(node_ids)
    out = _get(f"images/{file_key}?ids={ids}&format={fmt}", token)
    if out.get("err"):
        raise FigmaUnavailable(f"figma could not render: {out['err']}")
    return {"images": out.get("images") or {}, "format": fmt,
            "caveat": ("These URLs are generated by Figma and EXPIRE (~30 days). "
                       "Download and host the asset; do not hotlink it into a page "
                       "you expect to keep working.")}


def read_tokens(file_key: str | None = None, token: str | None = None) -> dict:
    """Cached colour + type tokens. See `cached` for the staleness contract."""
    return cached(f"tokens:{file_key or get_settings().figma_file_key}",
                  lambda: _read_tokens_uncached(file_key, token))


def read_components(file_key: str | None = None, token: str | None = None) -> dict:
    """Cached component sets. See `cached` for the staleness contract."""
    return cached(f"components:{file_key or get_settings().figma_file_key}",
                  lambda: _read_components_uncached(file_key, token))
