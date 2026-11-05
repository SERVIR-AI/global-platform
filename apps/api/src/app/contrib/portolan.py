"""Publish an approved raster layer as a Portolan collection (STAC 1.1).

The landed row (conf/tiffs.contrib.yml) and its contract stay the system of record;
this writes the PUBLISHED form other people and tools read: a folder per layer with
`collection.json`, `README.md`, `AGENTS.md` and one immutable folder per version holding
the Cloud Optimized GeoTIFF and a thumbnail, under a root `catalog.json`. A single-file
collection carries its data as a collection-level asset (Portolan's rule; scenes of a
stack become items, which this publisher does not do yet). Every governance field the
platform reads back (role, countries, contract, review, outside values) travels as a
`servir:` field on the collection, so a server pointed at the published catalog
(graph/geo/stac_catalog.py) rebuilds the same row with nothing lost.

Versions are immutable: the version id carries the file's sha256, a re-publish of
changed bytes adds a new folder and demotes the previous data asset to an `archive`
asset that keeps its checksum, and nothing is overwritten, because receipts cite the
sha of the file they were cut from. Only approved layers reach here; staged rows never
leave the host (the spec defines no access control).

Switch: GRP_PORTOLAN_ROOT (a local directory; empty = off). Portolan schema v0.2.0 is
pinned here on purpose: the spec is pre-1.0 and breaking changes are expected.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings
from . import catalog_io

SPEC_SCHEMA = "https://schemas.portolan-sdi.org/portolan/v0.2.0/schema.json"
STAC_VERSION = "1.1.0"
EXT = {"proj": "https://stac-extensions.github.io/projection/v2.0.0/schema.json",
       "raster": "https://stac-extensions.github.io/raster/v2.0.0/schema.json",
       "file": "https://stac-extensions.github.io/file/v2.1.0/schema.json",
       "version": "https://stac-extensions.github.io/version/v1.2.0/schema.json"}
COG_TYPE = "image/tiff; application=geotiff; profile=cloud-optimized"
CATALOG_ID = "servir-global-platform"
# SPDX ids a contributor's licence text is accepted as; anything else publishes as
# `other` with the text kept beside it (Portolan: SPDX or other, never proprietary).
_SPDX = {s.lower(): s for s in (
    "CC0-1.0", "CC-BY-4.0", "CC-BY-3.0", "CC-BY-3.0-IGO", "CC-BY-SA-4.0", "CC-BY-NC-4.0",
    "CC-BY-NC-SA-4.0", "CC-BY-ND-4.0", "ODbL-1.0", "PDDL-1.0", "ODC-By-1.0", "MIT",
    "Apache-2.0", "GPL-3.0-only", "GPL-3.0-or-later", "BSD-3-Clause", "OGL-UK-3.0")}
_BITS = {"int8": 8, "uint8": 8, "int16": 16, "uint16": 16, "int32": 32, "uint32": 32,
         "float32": 32, "int64": 64, "uint64": 64, "float64": 64}
_PALETTE = ["#fde725", "#7ad151", "#22a884", "#2a788e", "#414487", "#440154", "#d7191c",
            "#fdae61", "#abd9e9", "#2c7bb6"]


def enabled() -> bool:
    return bool(str(get_settings().grp_portolan_root or "").strip())


def _root(root=None) -> Path:
    return Path(root or get_settings().grp_portolan_root).expanduser()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def license_id(text) -> tuple[str, str | None]:
    """(SPDX id or 'other', the contributor's text when it is not an SPDX id)."""
    t = str(text or "").strip()
    spdx = _SPDX.get(t.lower().replace(" ", "-"))
    if spdx:
        return spdx, None
    return "other", (t or None)


def _vintage_datetime(vintage) -> str | None:
    """A contributor's vintage ('2016-11', '2026', '2025-07-15') as an RFC 3339 instant."""
    v = str(vintage or "").strip()
    m = re.match(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?", v)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2) or "01", m.group(3) or "01"
    try:
        datetime(int(y), int(mo), int(d))
    except ValueError:
        return None
    return f"{y}-{mo}-{d}T00:00:00Z"


def _sha256(path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def source_url(row: dict) -> str | None:
    """The contributor's own link to the original source, when one was given: the
    `url` field, else where the gate fetched the file from. Never composed by us."""
    for cand in (row.get("url"), (row.get("fetched") or {}).get("url")
                 if isinstance(row.get("fetched"), dict) else None, row.get("download_url")):
        if cand and str(cand).startswith(("http://", "https://")):
            return str(cand)
    return None


def write_cog(src, dst, role=None) -> dict:
    """Write `src` as a Cloud Optimized GeoTIFF: 512 px tiles, DEFLATE, overviews down to
    one tile, statistics embedded. Classes are resampled nearest, measures averaged."""
    import rasterio
    from rasterio.shutil import copy as rcopy
    from ..graph.geo import schema
    with rasterio.open(src) as s:
        dtype = s.dtypes[0]
    is_float = str(dtype).startswith("float")
    kind = schema.values_kind(role) if role else None
    resampling = "NEAREST" if kind == "classes" or (kind is None and not is_float) else "AVERAGE"
    opts = {"COMPRESS": "DEFLATE", "PREDICTOR": "3" if is_float else "2", "BLOCKSIZE": "512",
            "OVERVIEWS": "IGNORE_EXISTING", "OVERVIEW_RESAMPLING": resampling,
            "BIGTIFF": "IF_SAFER", "NUM_THREADS": "ALL_CPUS", "STATISTICS": "YES"}
    with rasterio.Env(GDAL_PAM_ENABLED="NO"):
        rcopy(str(src), str(dst), driver="COG", **opts)
    return {"compression": "DEFLATE", "blocksize": 512, "overview_resampling": resampling.lower()}


def _header(path) -> dict:
    """What the collection states about the file: bounds in WGS84, projection, band facts."""
    import rasterio
    from rasterio.warp import transform_bounds
    with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(path) as ds:
        b = ds.bounds
        epsg = ds.crs.to_epsg() if ds.crs else None
        if ds.crs and epsg != 4326:
            w, s, e, n = transform_bounds(ds.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top)
        else:
            w, s, e, n = b.left, b.bottom, b.right, b.top
        tags = ds.tags(1)
        stats = {}
        for key, name in (("STATISTICS_MINIMUM", "minimum"), ("STATISTICS_MAXIMUM", "maximum"),
                          ("STATISTICS_MEAN", "mean"), ("STATISTICS_STDDEV", "stddev")):
            if key in tags:
                try:
                    stats[name] = float(tags[key])
                except ValueError:
                    pass
        if not stats:
            try:
                st = ds.statistics(1, approx=True)
                stats = {"minimum": float(st.min), "maximum": float(st.max),
                         "mean": float(st.mean), "stddev": float(st.std)}
            except Exception:
                stats = {}
        t = ds.transform
        return {"bbox": [round(w, 6), round(s, 6), round(e, 6), round(n, 6)],
                "epsg": epsg, "wkt": None if epsg else (ds.crs.to_wkt() if ds.crs else None),
                "shape": [ds.height, ds.width],
                "transform": [t.a, t.b, t.c, t.d, t.e, t.f, 0.0, 0.0, 1.0],
                "dtype": ds.dtypes[0], "nodata": ds.nodata,
                "resolution": abs(t.a), "stats": stats,
                "overviews": list(ds.overviews(1)), "tiled": bool(ds.profile.get("tiled"))}


def _hex(c) -> tuple[int, int, int] | None:
    s = str(c or "").strip().lstrip("#")
    if len(s) == 6:
        try:
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        except ValueError:
            return None
    return None


def write_thumbnail(cog, dst, row: dict, contract: dict, head: dict, size: int = 400) -> bool:
    """A small PNG of the layer: classes in the contributor's colours (or a fixed palette),
    measures as a grey ramp over the file's statistics, nodata transparent."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    h, w = head["shape"]
    scale = max(h, w) / float(size)
    oh, ow = max(1, int(round(h / scale))), max(1, int(round(w / scale)))
    with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(cog) as ds:
        arr = ds.read(1, out_shape=(oh, ow), resampling=Resampling.nearest)
        nodata = ds.nodata
    valid = np.ones(arr.shape, dtype=bool)
    if nodata is not None and not (isinstance(nodata, float) and nodata != nodata):
        valid &= arr != nodata
    if arr.dtype.kind == "f":
        valid &= np.isfinite(arr)
    rgba = np.zeros((oh, ow, 4), dtype=np.uint8)
    legend = row.get("legend") or {}
    if legend:
        colors = row.get("colors") if isinstance(row.get("colors"), dict) else {}
        for i, (k, _) in enumerate(sorted(legend.items(), key=lambda kv: float(kv[0]))):
            rgb = _hex(colors.get(k) or colors.get(str(k))) or _hex(_PALETTE[i % len(_PALETTE)])
            mask = valid & (arr == float(k))
            rgba[mask] = (*rgb, 255)
    else:
        lo = float((head.get("stats") or {}).get("minimum", np.nanmin(arr[valid]) if valid.any() else 0))
        hi = float((head.get("stats") or {}).get("maximum", np.nanmax(arr[valid]) if valid.any() else 1))
        span = (hi - lo) or 1.0
        grey = np.clip((arr.astype("float64") - lo) / span, 0, 1)
        shade = (30 + 225 * grey).astype(np.uint8)
        rgba[..., 0] = rgba[..., 1] = rgba[..., 2] = shade
        rgba[..., 3] = np.where(valid, 255, 0).astype(np.uint8)
    _write_png(dst, rgba)
    return True


def _write_png(dst, rgba) -> None:
    """A minimal PNG encoder (RGBA, 8 bit, filter 0): the standard library only."""
    import struct
    import zlib
    import numpy as np
    h, w = rgba.shape[0], rgba.shape[1]
    raw = b"".join(b"\x00" + np.ascontiguousarray(rgba[y]).tobytes() for y in range(h))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))
    with open(dst, "wb") as f:
        f.write(png)


def _classes(row: dict) -> list[dict]:
    out = []
    colors = row.get("colors") if isinstance(row.get("colors"), dict) else {}
    for k, v in (row.get("legend") or {}).items():
        try:
            value = int(k)
        except (TypeError, ValueError):
            continue
        name = str(v).strip()
        c = {"value": value, "name": re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:60] or f"class_{value}",
             "description": name}
        hint = colors.get(k) if colors else None
        hint = hint if hint is not None else (colors.get(str(k)) if colors else None)
        if hint:
            c["color_hint"] = str(hint).lstrip("#")
        out.append(c)
    return out


def _servir_fields(layer: str, row: dict, contract: dict) -> dict:
    """Every governance field the platform reads back from a row, under one prefix."""
    out = {"servir:layer": layer, "servir:contributed": True}
    for k in ("role", "countries", "outside_values", "usage_notes", "authority", "accuracy",
              "contribution_id", "source", "vintage", "colors", "pixel_size_deg", "points", "legend"):
        if row.get(k) not in (None, "", [], {}):
            out[f"servir:{k}"] = ({str(a): b for a, b in row[k].items()} if k in ("legend", "colors")
                                  and isinstance(row[k], dict) else row[k])
    if row.get("review"):
        rv = row["review"]
        out["servir:review"] = (rv if isinstance(rv, (str, int, float, bool))
                                else {k: v for k, v in dict(rv).items()
                                      if k in ("status", "decision", "reviewer_role", "note", "at",
                                               "auto_approved")} or str(rv))
    if contract:
        out["servir:contract"] = {k: v for k, v in contract.items()
                                  if k in ("dtype", "valid_min", "valid_max", "nodata", "role",
                                           "units", "scale", "pixel_size_deg")}
    if row.get("declared_fields"):
        out["servir:declared_fields"] = row["declared_fields"]
    return out


def _readme_collection(layer, row, contract, versions: list[dict], lic, lic_text, via) -> str:
    rows = "\n".join(f"| {v['version']} | `{v['sha256']}` | {int(v['bytes']):,} | {v['published']} |"
                     f" {'superseded' if v.get('deprecated') else 'current'} |" for v in versions)
    legend = row.get("legend") or {}
    leg = ("\n".join(f"- {k}: {v}" for k, v in legend.items()) if legend
           else f"- values: {contract.get('units') or contract.get('dtype') or 'see the contract'}")
    countries = ", ".join(str(c) for c in row.get("countries") or []) or "not declared"
    return f"""# {row.get('title') or layer}

{row.get('description') or ''}

- Layer key: `{layer}` · role: {row.get('role') or contract.get('role') or 'undeclared'} · countries: {countries}
- Source: {row.get('source') or 'not declared'}{f' ({via})' if via else ''}
- Licence: {lic}{f' ({lic_text})' if lic_text else ''}
- Vintage: {row.get('vintage') or 'not declared'}
- Contributed through the SERVIR Global Platform contribution gate{' (contribution ' + str(row.get('contribution_id')) + ')' if row.get('contribution_id') else ''}; the platform's own catalog row remains the system of record.

## Values

{leg}

Contract declared at landing: {json.dumps({k: v for k, v in contract.items() if k in ('dtype', 'valid_min', 'valid_max', 'nodata', 'units', 'scale')}, default=str)}

## Versions

| version | sha256 | bytes | published | status |
|---|---|---|---|---|
{rows}

Each version folder is immutable; a new version is a new folder, the previous file stays as
an `archive` asset so that earlier citations still resolve. The sha256 is the checksum the
platform's receipts cite.

## How to read

- GDAL or rasterio: open the `data` asset href directly (a Cloud Optimized GeoTIFF; range requests suffice).
- SERVIR Global Platform: `assemble_pack` cites this layer by key `{layer}` and `publish_answer` mints a receipt against it.
"""


def _agents_collection(layer, row, contract) -> str:
    legend = row.get("legend") or {}
    values = ("; ".join(f"{k} = {v}" for k, v in legend.items()) if legend
              else f"{contract.get('units') or 'values'} as {contract.get('dtype') or 'stored'}")
    return f"""# Agent notes: {layer}

- This collection holds ONE raster layer, "{row.get('title') or layer}" (role: {row.get('role') or contract.get('role') or 'undeclared'}). Read the `data` asset; `archive-*` assets are earlier versions kept so that earlier citations still resolve.
- Cell values: {values}. nodata: {contract.get('nodata')}. Values outside {contract.get('valid_min')}..{contract.get('valid_max')} are not valid data.
- Cite it as: {row.get('source') or 'source not declared'}, licence {row.get('license') or 'not declared'}, vintage {row.get('vintage') or 'not declared'}.
- Coverage is the collection's bbox; `servir:countries` names the countries the contributor declared.
- To compute over an area with provenance, prefer the SERVIR Global Platform MCP tool `assemble_pack` (it clips, cites and mints a replayable receipt) over reading the file yourself.
- The `servir:` fields are the platform's governance fields (contract, review, outside values); they are informative here and authoritative in the platform.
"""


def _readme_root() -> str:
    s = get_settings()
    return f"""# SERVIR Global Platform published layers

Raster layers approved through the platform's contribution gate, published in the
Portolan layout (STAC 1.1). One collection per layer; one immutable folder per version.

Host: {s.grp_portolan_host_name} ({s.grp_portolan_host_url}). The platform's own catalog
row remains the system of record; this tree is its published form.
"""


def _agents_root() -> str:
    return """# Agent notes

- Every child collection is one raster layer with its own README.md and AGENTS.md: read those first.
- Collections carry `servir:` fields (layer key, role, countries, contract, review). The `servir:layer` value is the key the SERVIR Global Platform's MCP tools use.
- `archive-*` assets are earlier versions kept for replay; use the `data` asset.
"""


def _write_json(path: Path, doc: dict) -> None:
    catalog_io.write_text(path, json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n")


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def publish_raster(layer: str, row: dict, contract: dict | None, file: str, *, root=None) -> dict:
    """Publish one landed raster as a Portolan collection under the configured root.
    Returns what happened (published / unchanged / skipped / failed) and never raises
    past its caller's passport: the landing already succeeded."""
    settings = get_settings()
    if not (root or settings.grp_portolan_root):
        return {"status": "skipped", "note": "GRP_PORTOLAN_ROOT is not set"}
    if row.get("stack") or row.get("kind") == "stack":
        return {"status": "skipped", "note": "stacks (one file per year) are not published yet; "
                                             "the layer is served by the platform as before"}
    contract = dict(contract or {})
    role = row.get("role") or contract.get("role") or layer.split("_", 1)[0]
    root_dir = _root(root)
    coll_dir = root_dir / layer
    tmp_dir = coll_dir / f".publishing.{os.getpid()}"
    try:
        root_dir.mkdir(parents=True, exist_ok=True)
        coll_dir.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir()
        cog = tmp_dir / f"{layer}.tif"
        profile = write_cog(file, cog, role)
        sha, size = _sha256(cog)
        vintage = str(row.get("vintage") or "").strip() or "undated"
        version = f"v{re.sub(r'[^0-9A-Za-z-]+', '-', vintage)}-{sha[:12]}"
        vdir = coll_dir / version
        if vdir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return {"status": "unchanged", "collection": layer, "version": version, "sha256": sha,
                    "note": "these bytes are already published under this version"}
        head = _header(cog)
        thumb = write_thumbnail(cog, tmp_dir / "thumbnail.png", row, contract, head)
        os.replace(tmp_dir, vdir)                      # the version folder appears in one rename
        lic, lic_text = license_id(row.get("license"))
        dt = _vintage_datetime(row.get("vintage")) or _now()
        now = _now()
        via = source_url(row)
        classes = _classes(row)
        band = {"name": "band_1", "data_type": head["dtype"],
                "raster:bits_per_sample": _BITS.get(str(head["dtype"]), 8),
                "raster:spatial_resolution": head["resolution"],
                **({"nodata": head["nodata"]} if head["nodata"] is not None else {}),
                **({"unit": contract["units"]} if contract.get("units") else {}),
                **({"raster:scale": contract["scale"]} if contract.get("scale") not in (None, 1) else {}),
                **({"statistics": head["stats"]} if head["stats"] else {}),
                **({"classification:classes": classes} if classes else {})}
        w, s, e, n = head["bbox"]
        with catalog_io.LOCK:                          # the catalog files change under the gate's lock
            existing = _load(coll_dir / "collection.json")
            assets = {k: v for k, v in (existing.get("assets") or {}).items() if k.startswith("archive-")}
            history = [h for h in (existing.get("servir:versions") or []) if h.get("version") != version]
            old = (existing.get("assets") or {}).get("data")
            if old and existing.get("version") and existing["version"] != version:
                assets[f"archive-{existing['version']}"] = {
                    **{k: v for k, v in old.items() if k not in ("roles", "title")},
                    "roles": ["archive"], "title": f"Superseded version {existing['version']}",
                    "deprecated": True, "superseded_at": now}
            for h in history:
                if h.get("version") == existing.get("version"):
                    h["deprecated"] = True
            history.append({"version": version, "sha256": sha, "bytes": size, "published": now,
                            "deprecated": False, "datetime": dt})
            times = sorted(t for t in (h.get("datetime") for h in history) if t)
            providers = [{"name": str(row.get("source") or "producer not declared by the contributor"),
                          "roles": ["producer", "licensor"], **({"url": via} if via else {})}]
            host = {"name": settings.grp_portolan_host_name, "roles": ["host"],
                    "url": settings.grp_portolan_host_url}
            if settings.grp_portolan_host_email:
                host["email"] = settings.grp_portolan_host_email
            providers.append(host)
            assets["data"] = {"href": f"./{version}/{layer}.tif", "type": COG_TYPE, "roles": ["data"],
                              "title": str(row.get("title") or layer), "created": now,
                              "file:checksum": "1220" + sha, "file:size": size,
                              **({"proj:code": f"EPSG:{head['epsg']}"} if head["epsg"] else {}),
                              "proj:shape": head["shape"], "proj:transform": head["transform"],
                              "bands": [band]}
            if thumb:
                tsha, tsize = _sha256(vdir / "thumbnail.png")
                assets["thumbnail"] = {"href": f"./{version}/thumbnail.png", "type": "image/png",
                                       "roles": ["thumbnail"], "title": "Thumbnail",
                                       "file:checksum": "1220" + tsha, "file:size": tsize}
            coll = {"type": "Collection", "stac_version": STAC_VERSION,
                    "stac_extensions": [SPEC_SCHEMA, EXT["raster"], EXT["proj"], EXT["file"], EXT["version"]],
                    "id": layer, "title": str(row.get("title") or layer),
                    "description": str(row.get("description") or row.get("title") or layer),
                    "license": lic, "keywords": [str(role), *[str(c) for c in row.get("countries") or []]],
                    "providers": providers,
                    "extent": {"spatial": {"bbox": [[w, s, e, n]]},
                               "temporal": {"interval": [[times[0] if times else dt, times[-1] if times else dt]]}},
                    "summaries": {"servir:role": [str(role)],
                                  **({"proj:code": [f"EPSG:{head['epsg']}"]} if head["epsg"] else {})},
                    "links": [{"rel": "root", "href": "../catalog.json", "type": "application/json"},
                              {"rel": "parent", "href": "../catalog.json", "type": "application/json"},
                              {"rel": "self", "href": "./collection.json", "type": "application/json"},
                              {"rel": "describedby", "href": "./README.md", "type": "text/markdown", "title": "README"},
                              {"rel": "agents", "href": "./AGENTS.md", "type": "text/markdown", "title": "AGENTS.md"},
                              *([{"rel": "via", "href": via, "type": "text/html", "title": "Original source"}] if via else []),
                              *([{"rel": "license", "href": via, "type": "text/html"}] if lic == "other" and via else [])],
                    "assets": assets,
                    "version": version, "deprecated": False,
                    "created": existing.get("created") or now, "updated": now,
                    **({"servir:license": lic_text} if lic_text else {}),
                    **_servir_fields(layer, row, contract),
                    "servir:versions": history}
            _write_json(coll_dir / "collection.json", coll)
            catalog_io.write_text(coll_dir / "README.md",
                                  _readme_collection(layer, row, contract, history, lic, lic_text, via))
            catalog_io.write_text(coll_dir / "AGENTS.md", _agents_collection(layer, row, contract))
            _update_root(root_dir, layer, str(row.get("title") or layer), now)
        return {"status": "published", "collection": layer, "version": version, "sha256": sha,
                "bytes": size, "cog": profile, "root": str(root_dir), "thumbnail": thumb,
                "superseded": [h["version"] for h in history if h.get("deprecated")],
                **({} if via else {"note": "the contributor gave no source URL, so the collection has "
                                            "no rel:via link; the Portolan validator flags that (PTL-PRO-001) "
                                            "until a `url` is added to the row"})}
    except Exception as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return {"status": "failed", "collection": layer, "note": f"{type(exc).__name__}: {exc}"}


def _update_root(root_dir: Path, layer: str, title: str, now: str) -> None:
    path = root_dir / "catalog.json"
    cat = _load(path) or {"type": "Catalog", "stac_version": STAC_VERSION,
                          "stac_extensions": [SPEC_SCHEMA], "id": CATALOG_ID,
                          "title": "SERVIR Global Platform published layers",
                          "description": ("Raster layers approved through the SERVIR Global Platform "
                                          "contribution gate, one collection per layer, one immutable "
                                          "folder per version."),
                          "created": now, "links": []}
    keep = [lk for lk in cat.get("links") or []
            if lk.get("rel") == "child" and lk.get("href") != f"./{layer}/collection.json"]
    cat["links"] = [{"rel": "root", "href": "./catalog.json", "type": "application/json"},
                    {"rel": "self", "href": "./catalog.json", "type": "application/json"},
                    {"rel": "describedby", "href": "./README.md", "type": "text/markdown", "title": "README"},
                    {"rel": "agents", "href": "./AGENTS.md", "type": "text/markdown", "title": "AGENTS.md"},
                    *keep, {"rel": "child", "href": f"./{layer}/collection.json",
                            "type": "application/json", "title": title}]
    cat["updated"] = now
    _write_json(path, cat)
    if not (root_dir / "README.md").exists():
        catalog_io.write_text(root_dir / "README.md", _readme_root())
    if not (root_dir / "AGENTS.md").exists():
        catalog_io.write_text(root_dir / "AGENTS.md", _agents_root())


def annotate(kind: str, landing: dict) -> dict:
    """Say, in a non-raster landing, that the publisher does not handle its kind yet
    (a declared gap, never a silent skip)."""
    if enabled() and isinstance(landing, dict) and kind != "raster" and "published" not in landing:
        landing["published"] = {"status": "not published",
                                "note": f"the catalog publisher handles rasters; this {kind} is "
                                        "served by the platform but not yet in the Portolan catalog"}
    return landing


def publish_approved(layers=None, root=None) -> list[dict]:
    """Publish every approved contributed raster row (or the named ones) that has its
    file on this server. Rows already published with the same bytes report unchanged."""
    from ..graph.geo import schema, tiffs
    settings = get_settings()
    cat = tiffs.catalog(include_staged=False)
    wanted = set(layers or [])
    out = []
    for layer, row in cat.items():
        row = row or {}
        if wanted and layer not in wanted:
            continue
        if not wanted and not row.get("contributed"):
            continue
        if row.get("stac"):
            continue                              # it came from a catalog already
        if row.get("kind") == "stack" or row.get("stack"):
            out.append({"status": "skipped", "collection": layer, "note": "stacks not published yet"})
            continue
        if not row.get("local_path"):
            out.append({"status": "skipped", "collection": layer, "note": "row names no file"})
            continue
        file = os.path.join(str(settings.tiffs_dir), os.path.basename(str(row["local_path"])))
        if not os.path.exists(file):
            out.append({"status": "skipped", "collection": layer, "note": f"file missing: {file}"})
            continue
        out.append(publish_raster(layer, row, schema.schema_for(layer) or {}, file, root=root))
    return out


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m app.contrib.portolan",
                                description="Publish approved raster layers as a Portolan catalog.")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("publish-approved", help="publish every approved contributed raster row")
    a.add_argument("--root", default=None, help="catalog root (default GRP_PORTOLAN_ROOT)")
    a.add_argument("--layer", action="append", default=[], help="only these layer keys")
    args = p.parse_args(argv)
    if args.cmd == "publish-approved":
        if not (args.root or get_settings().grp_portolan_root):
            print(json.dumps({"status": "failed", "note": "set GRP_PORTOLAN_ROOT or pass --root"}))
            return 2
        results = publish_approved(args.layer or None, args.root)
        for r in results:
            print(json.dumps(r, default=str))
        return 0 if all(r.get("status") in ("published", "unchanged", "skipped") for r in results) else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
