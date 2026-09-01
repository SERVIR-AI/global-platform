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


def validate_manifest(m: dict) -> list[str]:
    """Every problem at once. license/vintage are REQUIRED for new layers — the
    existing catalog's unrecorded vintages are a declared gap we do not grow.
    'unstated' is accepted, silence is not."""
    fails = []
    if not isinstance(m, dict):
        return ["manifest is not a mapping"]
    for k in REQUIRED:
        if not m.get(k):
            fails.append(f"missing required field '{k}'")
    layer = str(m.get("layer") or "")
    if layer and not layer.startswith(("hazard_", "risk_")):
        fails.append("layer must be namespaced hazard_* or risk_*")
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
    from . import notes
    fails += notes.validate(m.get("usage_notes"))
    return fails


def add(manifest: dict, dry_run: bool = False) -> dict:
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
    decl.setdefault("role", "hazard" if layer.startswith("hazard_") else "risk")
    obs = verify.windowed_stats(str(manifest["file"]))
    mismatches = verify._check({**(schema_mod._doc().get("defaults") or {}), **decl}, obs)
    if mismatches:
        return {"status": "declined", "verified": False,
                "failures": [f"file does not satisfy the DECLARED contract: {m}"
                             for m in mismatches],
                "observed": obs}
    if dry_run:
        return {"status": "valid (dry-run, nothing written)", "verified": True,
                "observed": obs}

    dest = Path(settings.tiffs_dir) / f"{layer}.tif"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest["file"], dest)

    # Contributed rows live in their own MACHINE-OWNED files, merged at load.
    # The hand-authored conf files are never rewritten by code: a yaml round-trip
    # strips their comments, which carry the institutional lessons (caught when
    # X2b's first version silently deleted them).
    row = {"local_path": f"tiffs/{layer}.tif",
           "title": manifest["title"], "description": manifest["description"],
           "legend": manifest["legend"], "source": manifest["source"],
           "license": manifest["license"], "vintage": manifest["vintage"],
           **({"usage_notes": manifest["usage_notes"]}
              if manifest.get("usage_notes") else {}),
           "contributed": True}
    cat_path = Path(settings.tiffs_contrib_path)
    cat = (yaml.safe_load(cat_path.read_text()) or {}) if cat_path.exists() else {}
    cat[layer] = row
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
