"""X2b: land a raster layer through the structural gate.

The catalog rows (conf/tiffs.yml) and declared contracts (conf/raster_schema.yml)
were hand-edited; this makes them a GATED command. The contributor DECLARES the
contract; the file is verified against the declaration before anything is
written — because filenames and source sheets lie about units and scale, which
is the exact lesson raster_schema.yml records at its top.
"""

from __future__ import annotations

import os

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


_CLASS_ROLES = ("hazard", "risk", "vulnerability", "landcover")


def normalise(path: str, role: str) -> tuple[str, list[str]]:
    """The file the platform will verify and keep: a GeoTIFF on EPSG:4326.

    Hubs hand over what they have — an ERDAS .img in UTM, a CRTM05 GeoTIFF. The
    counting primitives (stacks.row_pixel_areas_km2, every clip) assume a geographic
    grid, so a projected class raster is reprojected here, nearest-neighbour (a class
    never blends), and the passport says so. A COUNT grid (population_*) cannot be
    re-gridded without moving people between cells, so a projected one is refused in
    words. Returns (path to use, notes); the path is the input when nothing changed."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject
    notes = []
    with rasterio.open(path) as src:
        if src.crs is None:
            raise ValueError("the file carries no coordinate reference system — assign its CRS "
                             "(gdal_edit -a_srs, or QGIS: Layer -> Set CRS) and resubmit")
        epsg = src.crs.to_epsg()
        geographic = bool(src.crs.is_geographic)
        driver = src.driver
        same_crs = epsg == 4326 or (geographic and epsg is None and "WGS 84" in src.crs.to_wkt())
        if same_crs and driver == "GTiff":
            return path, notes
        if not same_crs and role not in _CLASS_ROLES:
            raise ValueError(
                f"the file is on {src.crs.to_string()} (EPSG:{epsg}); a {role}_* layer is a count "
                "grid, which the platform never re-grids (reprojecting moves counts between "
                "cells) — reproject it yourself with a sum-preserving method and resubmit on "
                "EPSG:4326")
        out = str(path) + ".norm.tif"
        profile = src.profile.copy()
        for k in ("blockxsize", "blockysize", "tiled", "interleave"):
            profile.pop(k, None)        # the source's block layout is not ours to keep
        if same_crs:
            notes.append(f"converted from {driver} to GeoTIFF at ingest; values unchanged")
            profile.update(driver="GTiff", compress="lzw",
                           tiled=(src.width >= 256 and src.height >= 256))
            with rasterio.open(out, "w", **profile) as dst:
                for b in range(1, src.count + 1):
                    dst.write(src.read(b), b)
            return out, notes
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:4326", src.width, src.height, *src.bounds)
        nodata = src.nodata
        fill = nodata
        if fill is None:
            fill = 0
            notes.append("the file declares no nodata; cells outside its original grid are 0 "
                         "after reprojection")
        profile.update(driver="GTiff", crs="EPSG:4326", transform=transform, width=width,
                       height=height, compress="lzw", nodata=nodata,
                       tiled=(width >= 256 and height >= 256))
        with rasterio.open(out, "w", **profile) as dst:
            for b in range(1, src.count + 1):
                arr = np.full((height, width), fill, dtype=src.dtypes[b - 1])
                reproject(source=rasterio.band(src, b), destination=arr,
                          src_transform=src.transform, src_crs=src.crs,
                          dst_transform=transform, dst_crs="EPSG:4326",
                          src_nodata=nodata, dst_nodata=nodata,
                          resampling=Resampling.nearest)
                dst.write(arr, b)
        notes.insert(0, f"reprojected at ingest from {src.crs.to_string()} (EPSG:{epsg}, "
                        f"{abs(src.transform.a):g} {src.crs.linear_units or 'unit'} cells) to "
                        f"EPSG:4326, nearest neighbour — class values unchanged, cell edges moved"
                        + ("; converted from " + driver if driver != "GTiff" else ""))
    return out, notes


def hints(m: dict) -> list[str]:
    """Advice the gate gives WITHOUT refusing: shapes of a manifest that pass every
    check and still give a wrong answer later. Shown with the preview."""
    out = []
    layer = str(m.get("layer") or "")
    legend = _norm_legend(m.get("legend")) if isinstance(m.get("legend"), dict) else {}
    outside = m.get("outside_values")
    outside = [0] if outside is None else [int(v) for v in outside]
    if layer.startswith("landcover_") and len(legend) == 1 and 0 in outside and 0 not in legend:
        only = next(iter(legend.values()))
        out.append(
            f"one legend class ({only}) with 0 outside the map: every other pixel counts as "
            "'outside the footprint', so change between years cannot tell loss or gain from "
            f"no data. If 0 means 'not {only.lower()}' INSIDE the mapped footprint, declare it "
            f"as a class — legend {{0: 'Not {only.lower()}', 1: '{only}'}} — and set "
            "outside_values to the value that marks outside the map (or [] when the whole "
            "grid is mapped)")
    return out


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


def add(manifest: dict, dry_run: bool = False, review: str | None = None,
        keep_prepared: bool = False) -> dict:
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
    try:
        src_file, prepared = normalise(str(manifest["file"]), decl["role"])
    except ValueError as exc:
        return {"status": "declined", "verified": False, "failures": [str(exc)]}
    except Exception as exc:
        return {"status": "declined", "verified": False,
                "failures": [f"the file could not be read as a raster: {type(exc).__name__}: {exc}"]}
    original = str(manifest["file"])
    manifest = {**manifest, "file": src_file}

    def _drop_prepared():
        # the converted copy lives beside the input; it never outlives this call
        # unless the caller (the staging path) takes it over
        if src_file != original and os.path.exists(src_file):
            os.unlink(src_file)
    obs = verify.windowed_stats(str(manifest["file"]),
                                keep_values=tuple(_norm_legend(manifest.get("legend"))))
    if prepared:
        obs["prepared"] = prepared
    advice = hints(manifest)
    if advice:
        obs["hints"] = advice
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
        _drop_prepared()
        return {"status": "declined", "verified": False,
                "failures": [f"file does not satisfy the DECLARED contract: {m}"
                             for m in mismatches],
                "observed": obs}
    if dry_run:
        if not keep_prepared:
            _drop_prepared()
        return {"status": "valid (dry-run, nothing written)", "verified": True,
                "observed": obs, "file": str(manifest["file"]) if keep_prepared else original}

    cat_path = Path(settings.tiffs_contrib_path)
    cat = (yaml.safe_load(cat_path.read_text()) or {}) if cat_path.exists() else {}
    if decl.get("role") == "landcover":
        conflict = _stack_conflict(cat, manifest)
        if conflict:
            _drop_prepared()
            return {"status": "declined", "verified": True, "failures": [conflict], "observed": obs}

    dest = Path(settings.tiffs_dir) / f"{layer}.tif"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest["file"], dest)
    _drop_prepared()

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
           **({"prepared": prepared} if prepared else {}),
           **({"countries": list(manifest["countries"])} if manifest.get("countries") else {}),
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
        "vintage": manifest.get("vintage"),
        **({"url": manifest["url"]} if manifest.get("url") else {}),
        # [] is a declaration ("the whole grid is mapped"), not an omission
        "outside_values": ([0] if manifest.get("outside_values") is None
                           else [int(v) for v in manifest["outside_values"]]),
        **({"countries": list(manifest["countries"])} if manifest.get("countries") else {}),
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
