"""Contributed POINT layers — evacuation centres, shelters, clinics a hub knows about.

The built-in assets (hospitals, schools, buildings) come from OSM and are hard-wired.
A hub's own point layer lands once, as a master GeoJSON, and is clipped into each
AOI bundle beside the OSM layers under the same name — so the exposure count that
crosses hospitals with a hazard crosses these with no new code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from shapely.geometry import Point, shape

REGISTRY = "vectors/registry.json"          # under cache_dir: {layer: entry}
NAME_RE = r"^[a-z][a-z0-9_]{2,40}$"


def _dir() -> Path:
    from ...config import get_settings
    d = Path(get_settings().cache_dir) / "vectors"
    d.mkdir(parents=True, exist_ok=True)
    return d


def landed() -> dict:
    """{layer: entry} for layers that passed review. Entry carries `local_path`."""
    p = _dir() / "registry.json"
    if not p.is_file():
        return {}
    try:
        return json.load(open(p)) or {}
    except (OSError, ValueError):
        return {}


def _save(reg: dict) -> None:
    json.dump(reg, open(_dir() / "registry.json", "w"), indent=2)


def register(layer: str, entry: dict) -> None:
    reg = landed()
    reg[layer] = entry
    _save(reg)


def unregister(layer: str) -> None:
    reg = landed()
    if reg.pop(layer, None) is not None:
        _save(reg)


def visible() -> dict:
    """Landed layers plus the staged ones THIS caller may see. Same visibility rule
    as staged rasters: contributor and reviewers, nobody else."""
    out = dict(landed())
    try:
        from ...contrib import staging
        out.update(staging.visible_staged_vectors())
    except Exception:
        pass
    return out


def master_path(entry: dict) -> Path:
    from ...config import get_settings
    return Path(get_settings().cache_dir) / entry["local_path"]


def clip_into(adir: str, boundary, layer: str, entry: dict) -> int:
    """Write {adir}/{layer}.geojson holding the master's points inside `boundary`.
    Returns how many. A layer with no points inside still writes an empty file,
    so a count of zero is a real zero, not a missing layer."""
    src = master_path(entry)
    try:
        fc = json.load(open(src))
    except (OSError, ValueError):
        return 0
    keep = []
    for ft in fc.get("features") or []:
        g = ft.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        if boundary.contains(Point(g["coordinates"])):
            keep.append({"type": "Feature", "geometry": g,
                         "properties": ft.get("properties") or {}})
    json.dump({"type": "FeatureCollection", "features": keep},
              open(os.path.join(adir, f"{layer}.geojson"), "w"))
    return len(keep)


def purge_clips(layer: str) -> int:
    """Per-AOI clips are cached by layer NAME; a layer that changes identity must not
    be served from an old clip."""
    from ...config import get_settings
    root = Path(get_settings().cache_dir)
    n = 0
    for clip in root.glob(f"*/{layer}.geojson"):
        if clip.parent.name == "vectors":
            continue
        try:
            clip.unlink()
            n += 1
        except OSError:
            pass
    return n


def inspect(raw: bytes) -> dict:
    """Parse a candidate GeoJSON and say what it is. Raises ValueError with the
    reason when it is not a FeatureCollection of Points."""
    try:
        fc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"not valid JSON: {exc}") from None
    if not isinstance(fc, dict) or fc.get("type") != "FeatureCollection":
        raise ValueError("must be a GeoJSON FeatureCollection")
    feats = fc.get("features")
    if not isinstance(feats, list) or not feats:
        raise ValueError("FeatureCollection has no features")
    types = {}
    bad = 0
    for ft in feats:
        g = (ft or {}).get("geometry") or {}
        t = g.get("type")
        types[t] = types.get(t, 0) + 1
        if t != "Point":
            bad += 1
            continue
        c = g.get("coordinates")
        if not (isinstance(c, (list, tuple)) and len(c) >= 2
                and -180 <= float(c[0]) <= 180 and -90 <= float(c[1]) <= 90):
            raise ValueError("a Point has coordinates outside lon/lat range — "
                             "the layer must be EPSG:4326 (lon, lat)")
    if bad:
        raise ValueError(f"only Point features are accepted; found {types}")
    xs = [ft["geometry"]["coordinates"][0] for ft in feats]
    ys = [ft["geometry"]["coordinates"][1] for ft in feats]
    props = sorted({k for ft in feats for k in ((ft.get("properties") or {}).keys())})
    return {"features": len(feats), "bbox": [min(xs), min(ys), max(xs), max(ys)],
            "properties": props[:20]}
