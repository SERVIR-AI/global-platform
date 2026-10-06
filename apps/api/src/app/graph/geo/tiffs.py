"""Read the tiff catalog (conf/tiffs.yml): where each hazard raster lives, how to
fetch it if missing, and what it represents. The route node uses the descriptions
to pick which layer a question needs; ingest uses the entry to locate/download it.
"""
import yaml

from ...config import get_settings


def catalog(include_staged: bool = True):
    """Hand-authored rows + contributed rows (conf/tiffs.contrib.yml — machine-
    owned, written only by the contribution gate; contrib cannot shadow) + the
    STAGED layers the current caller may see (contrib/staging.py: a contributor
    previews their own layer; nobody else sees it until approval)."""
    with open(get_settings().tiffs_config_path) as f:
        cat = yaml.safe_load(f) or {}
    contrib_path = get_settings().tiffs_contrib_path
    try:
        with open(contrib_path) as f:
            contrib = yaml.safe_load(f) or {}
        cat.update({k: v for k, v in contrib.items() if k not in cat})
    except FileNotFoundError:
        pass
    if include_staged:
        try:
            from ...contrib import staging
            cat.update({k: v for k, v in staging.visible_staged_rasters().items() if k not in cat})
        except Exception:                       # the catalog never fails on staging
            pass
    _expand_stacks(cat)
    return cat


def stack_root(row):
    """Where a stack's files live: the env override, else the row's root, resolved
    against the repo root when relative."""
    import os
    root = (os.environ.get(str(row.get("data_root_env") or "")) or row.get("data_root") or "")
    if root and not os.path.isabs(root):
        root = os.path.join(os.path.dirname(os.path.dirname(
            str(get_settings().tiffs_config_path))), root)
    return root


def _expand_stacks(cat):
    """A `kind: stack` row (one legend, one contract, one file per year) becomes one
    ordinary row per year, so every raster primitive works on `landcover_hkh_2022`
    without knowing it belongs to a series. The stack row itself stays in the catalog
    (stacks.py reads it); per-year rows are `internal`, so they never reach the
    route node's layer menu. A per-year row carries `path` — the file as GDAL opens
    it: a plain path under the stack's directory, or `/vsizip/<zip>/<entry>` when the
    stack is delivered as a zip whose entries are stored uncompressed (read in place,
    no 10 GB copy). The directory can be overridden by the environment variable the
    row names in `data_root_env`, so one catalog serves a laptop and a server."""
    import os
    for sid, row in list(cat.items()):
        if row.get("kind") != "stack":
            continue
        if not row.get("file_pattern"):
            continue            # a contributed stack: its years were landed as rows already
        root = stack_root(row)
        inherited = {k: row[k] for k in ("legend", "source", "license", "vintage", "url",
                                         "pub_date", "authority", "accuracy",
                                         "outside_values", "usage_notes") if k in row}
        for y in row.get("years") or []:
            key = str(row["layer_pattern"]).format(year=int(y))
            if key in cat:
                continue
            entry_in = str(row["file_pattern"]).format(year=int(y))
            path = (f"/vsizip/{root}/{entry_in}" if root.lower().endswith(".zip")
                    else os.path.join(root, entry_in))
            cat[key] = {"stack": sid, "year": int(y), "internal": True, "path": path,
                        "local_path": f"stacks/{os.path.basename(entry_in)}",
                        "title": f"{row.get('title', sid)} — {int(y)}",
                        "description": row.get("description", ""), **inherited}


def entry(layer):
    cat = catalog()
    if layer not in cat:
        raise ValueError(f"unknown tiff layer: {layer}")
    return cat[layer]


def descriptions():
    """{layer: description} — the only fields the route node sees when choosing a layer.
    `internal: true` entries (e.g. the precomputed risk offered via the L1/L2 question) are hidden."""
    return {name: (meta.get("description") or "").strip()
            for name, meta in catalog().items() if not meta.get("internal")}


def legend(layer):
    """{class:int -> 'label (range)'} for a hazard layer, or {} if it has none or isn't in
    the catalog (e.g. a user-uploaded BYOD layer — the caller falls back to a default ramp)."""
    try:
        leg = entry(layer).get("legend") or {}
    except ValueError:
        return {}
    return {int(k): str(v).strip() for k, v in leg.items()}


def resolve(name):
    """A loose hazard name ('flood', 'hazard_flood', 'landslide') -> a catalog key, or None."""
    if not name:
        return None
    cat = catalog()
    name = str(name).strip().lower()
    if name in cat:
        return name
    if f"hazard_{name}" in cat:
        return f"hazard_{name}"
    return next((k for k in cat if name in k), None)
