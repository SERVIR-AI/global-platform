"""X2b: land a raster layer through the structural gate.

The catalog rows (conf/tiffs.yml) and declared contracts (conf/raster_schema.yml)
were hand-edited; this makes them a GATED command. The contributor DECLARES the
contract; the file is verified against the declaration before anything is
written — because filenames and source sheets lie about units and scale, which
is the exact lesson raster_schema.yml records at its top.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

REQUIRED = ("layer", "file", "title", "description", "source", "license",
            "vintage", "legend", "declared")
_DECLARED_REQUIRED = ("dtype", "valid_min", "valid_max")


CLASS_MAX = 5     # the class contract every reader indexes by: 1..5, 0 = nodata


def _role(layer: str) -> str:
    """The namespace IS the role. A vulnerability_* layer feeds the Layer-2 recipe
    as a weighted input; the gate used to refuse the very prefix the engine reads."""
    for prefix in ("hazard_", "risk_", "vulnerability_", "population_", "landcover_"):
        if layer.startswith(prefix):
            return prefix[:-1]
    return "risk"


def validate_manifest(m: dict) -> list[str]:
    """Every problem at once. license/vintage are REQUIRED for new layers — the
    existing catalog's unrecorded vintages are a declared gap we do not grow.
    'unstated' is accepted, silence is not."""
    fails = []
    if not isinstance(m, dict):
        return ["manifest is not a mapping"]
    layer = str(m.get("layer") or "")
    for k in REQUIRED:
        if k == "legend" and layer.startswith("population_"):
            continue          # a count raster has no classes to label
        if not m.get(k):
            fails.append(f"missing required field '{k}'")
    if layer and not layer.startswith(("hazard_", "risk_", "vulnerability_", "population_",
                                        "landcover_")):
        fails.append("layer must be namespaced hazard_*, risk_*, vulnerability_* or population_* "
                     "(or landcover_<stack>_<year> for one year of a land-cover stack)")
    if layer.startswith("landcover_"):
        # One year of a yearly categorical STACK: the year names its stack so the
        # series' legend and years live once, and the layer name must be what the
        # stack's year pattern resolves to.
        for k in ("stack", "year"):
            if not m.get(k):
                fails.append(f"missing required field '{k}' — a land-cover year names its "
                             "stack (landcover_<name>) and its map year")
        if m.get("stack") and not str(m["stack"]).startswith("landcover_"):
            fails.append("stack must be namespaced landcover_<name>")
        if m.get("stack") and m.get("year") and layer != f"{m['stack']}_{m['year']}":
            fails.append(f"layer must be '{m['stack']}_{m['year']}' (<stack>_<year>) so the "
                         "stack resolves it by year")
    if m.get("file") and not Path(str(m["file"])).is_file():
        fails.append(f"file {m['file']!r} does not exist")
    if m.get("legend") is not None and not isinstance(m.get("legend"), dict):
        fails.append("legend must map class number -> label")
    decl = m.get("declared")
    if isinstance(decl, dict):
        for k in _DECLARED_REQUIRED:
            if decl.get(k) is None:
                fails.append(f"declared.{k} is required — the contract is what the "
                             "file gets verified AGAINST; observation cannot write it")
    elif decl is not None:
        fails.append("declared must be a mapping (dtype/valid_min/valid_max/...)")
    if isinstance(decl, dict) and layer.startswith(("hazard_", "risk_", "vulnerability_")):
        # Classes are the contract every reader indexes by. A hazard declared
        # 0..300 landed, and the first count died on class 300 — the gate holds
        # the line, in words, before a file is fetched.
        try:
            vmin, vmax = float(decl.get("valid_min")), float(decl.get("valid_max"))
        except (TypeError, ValueError):
            vmin = vmax = None
        if vmax is not None and (vmax > CLASS_MAX or vmin < 0):
            fails.append(f"declared valid range {vmin:g}..{vmax:g}: a {layer.split('_', 1)[0]}_* "
                         f"layer carries integer classes 1..{CLASS_MAX} (0 = nodata) — reclassify "
                         "a continuous raster into those classes before contributing it")
    from . import notes
    fails += notes.validate(m.get("usage_notes"))
    return fails


def add(manifest: dict, dry_run: bool = False, review: str | None = None) -> dict:
    """Gate then land: validate -> verify file against the DECLARED contract ->
    copy into TIFFS_DIR -> append tiffs.yml row + raster_schema.yml contract.
    A mismatch refuses and writes NOTHING."""
    from ..config import get_settings
    from ..graph.geo import schema as schema_mod, verify

    fails = validate_manifest(manifest)
    if fails:
        return {"status": "declined", "failures": fails}
    layer = manifest["layer"]
    settings = get_settings()
    from ..graph.geo import tiffs
    if layer in tiffs.catalog():
        return {"status": "declined",
                "failures": [f"layer {layer!r} already in the catalog — contributions "
                             "add layers, they do not overwrite them"]}

    decl = {**manifest["declared"]}
    decl.setdefault("role", _role(layer))
    obs = verify.windowed_stats(str(manifest["file"]))
    # How coarse the values really are, measured at full resolution — advisory, shown
    # to the contributor and the reviewer before approval. A layer described as one
    # value per sub-district that repeats one value across a whole district landed
    # unquestioned because nothing ever looked.
    from ..graph.geo import layer_values
    gran = layer_values.granularity(str(manifest["file"]), decl,
                                    row={"outside_values": manifest.get("outside_values")})
    if gran:
        obs["granularity"] = gran
    mismatches = verify._check({**(schema_mod._doc().get("defaults") or {}), **decl}, obs)
    if mismatches:
        return {"status": "declined", "verified": False,
                "failures": [f"file does not satisfy the DECLARED contract: {m}"
                             for m in mismatches],
                "observed": obs}
    if dry_run:
        return {"status": "valid (dry-run, nothing written)", "verified": True,
                "observed": obs}

    cat_path = Path(settings.tiffs_contrib_path)
    cat = (yaml.safe_load(cat_path.read_text()) or {}) if cat_path.exists() else {}
    if decl.get("role") == "landcover":
        conflict = _stack_conflict(cat, manifest)
        if conflict:
            return {"status": "declined", "verified": True, "failures": [conflict], "observed": obs}

    dest = Path(settings.tiffs_dir) / f"{layer}.tif"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest["file"], dest)

    # Contributed rows live in their own MACHINE-OWNED files, merged at load.
    # The hand-authored conf files are never rewritten by code: a yaml round-trip
    # strips their comments, which carry the institutional lessons (caught when
    # X2b's first version silently deleted them).
    row = {"local_path": f"tiffs/{layer}.tif",
           "title": manifest["title"], "description": manifest["description"],
           "legend": (manifest.get("legend") or {}), "source": manifest["source"],
           "license": manifest["license"], "vintage": manifest["vintage"],
           **({"usage_notes": manifest["usage_notes"]}
              if manifest.get("usage_notes") else {}),
           **({"review": review} if review else {}),
           "contributed": True}
    cat[layer] = row
    if decl.get("role") == "landcover":
        _land_stack_year(cat, layer, manifest, dest)
    cat_path.write_text("# Contributed raster rows — machine-owned; written by the\n"
                        "# contribution gate. Hand-edit conf/tiffs.yml, never this.\n"
                        + yaml.safe_dump(cat, sort_keys=False, allow_unicode=True))

    sch_path = Path(settings.raster_schema_contrib_path)
    sch = (yaml.safe_load(sch_path.read_text()) or {}) if sch_path.exists() else {}
    sch.setdefault("layers", {})[layer] = decl
    sch_path.write_text("# Contributed raster contracts — machine-owned.\n"
                        + yaml.safe_dump(sch, sort_keys=False, allow_unicode=True))

    return {"status": "landed", "layer": layer, "verified": True,
            "file": str(dest), "observed": obs,
            "passport": {k: row[k] for k in ("title", "source", "license", "vintage")}}


def _norm_legend(legend):
    return {int(k): str(v).strip() for k, v in (legend or {}).items()}


def _stack_conflict(cat, manifest):
    """A year joining an existing stack must share its legend — one legend per series
    is the whole point of a stack. Returns the failure text, or None."""
    st = cat.get(str(manifest.get("stack")))
    if not st or st.get("kind") != "stack":
        return None
    have, want = _norm_legend(st.get("legend")), _norm_legend(manifest.get("legend"))
    if have != want:
        return (f"legend disagrees with stack {manifest['stack']!r}: the stack maps "
                f"{have}, this year declares {want} — one legend per stack; register a new "
                "stack for a different classification")
    if int(manifest["year"]) in [int(y) for y in st.get("years") or []]:
        return (f"stack {manifest['stack']!r} already has a {manifest['year']} map — "
                "contributions add years, they do not overwrite them")
    return None


def _land_stack_year(cat, layer, manifest, dest):
    """A landed land-cover year joins (or starts) its STACK row in the contributed
    catalog: the series' legend, years and extent live once; the per-year row points
    at its file and carries the stack id so every primitive can find its contract."""
    from ..graph.geo import stacks as _stacks
    sid, year = str(manifest["stack"]), int(manifest["year"])
    b = _stacks.header(str(dest))["bounds"]
    row = cat[layer]
    row.update({"stack": sid, "year": year, "role": "landcover", "internal": True})
    for k in ("authority", "accuracy", "outside_values"):
        if manifest.get(k) is not None:
            row[k] = manifest[k]
    st = cat.get(sid) or {
        "kind": "stack", "role": "landcover", "internal": True, "contributed": True,
        "title": manifest.get("stack_title") or f"{sid} land cover",
        "description": manifest["description"], "source": manifest["source"],
        "license": manifest["license"], "legend": manifest.get("legend") or {},
        "outside_values": manifest.get("outside_values") or [0],
        "authority": manifest.get("authority") or "contributed; authority unstated",
        "accuracy": manifest.get("accuracy") or "unstated",
        **({"colors": manifest["colors"]} if manifest.get("colors") else {}),
        "layer_pattern": f"{sid}_{{year}}", "years": [],
        "extent": {"lon": [b[0], b[2]], "lat": [b[1], b[3]]}}
    st["years"] = sorted(set(int(y) for y in st.get("years") or []) | {year})
    ext = st.get("extent") or {"lon": [b[0], b[2]], "lat": [b[1], b[3]]}
    st["extent"] = {"lon": [min(ext["lon"][0], b[0]), max(ext["lon"][1], b[2])],
                    "lat": [min(ext["lat"][0], b[1]), max(ext["lat"][1], b[3])]}
    cat[sid] = st
