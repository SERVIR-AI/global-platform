"""X2b: land a raster layer through the structural gate.

The catalog rows (conf/tiffs.yml) and declared contracts (conf/raster_schema.yml)
were hand-edited; this makes them a GATED command. The contributor DECLARES the
contract; the file is verified against the declaration before anything is
written — because filenames and source sheets lie about units and scale, which
is the exact lesson raster_schema.yml records at its top.

Which namespaces exist, and what each demands, is the schema's `namespaces` table
(graph/geo/schema.namespaces): nothing here lists a prefix.
"""

from __future__ import annotations

import math
import os
import re
import shutil
from pathlib import Path

import yaml

from . import catalog_io

REQUIRED = ("layer", "file", "title", "description", "source", "license",
            "vintage", "legend", "declared")
_DECLARED_REQUIRED = ("dtype", "valid_min", "valid_max")


CLASS_MAX = 5     # the class contract every reader indexes by: 1..5, 0 = nodata


def namespaces() -> dict:
    """{prefix: rule} — every namespace a contributed raster may take, each rule
    carrying its `role`; read from the schema on every call."""
    from ..graph.geo import schema
    return {str(r["prefix"]): {**r, "role": role}
            for role, r in schema.namespaces().items() if r.get("prefix")}


def namespace(layer: str) -> dict:
    """The rule of the namespace a layer name falls under, or {}."""
    for prefix, rule in namespaces().items():
        if str(layer or "").startswith(prefix):
            return {**rule, "prefix": prefix}
    return {}


def _role(layer: str) -> str:
    """The namespace IS the role. A vulnerability_* layer feeds the Layer-2 recipe
    as a weighted input; the gate used to refuse the very prefix the engine reads."""
    return namespace(layer).get("role") or "risk"


def namespace_text() -> str:
    """Every namespace and what goes in it, as the form and the declines say it."""
    return "; ".join(f"{p}<name> ({r.get('about') or r['role']})" for p, r in namespaces().items())


def _prefixes(values: str) -> str:
    ps = [f"{p}<name>" for p, r in namespaces().items() if r.get("values") == values]
    return ", ".join(ps[:-1]) + (" or " if len(ps) > 1 else "") + ps[-1] if ps else ""


def _isnum(v) -> bool:
    try:
        float(v)
        return not isinstance(v, bool)
    except (TypeError, ValueError):
        return False


def _num(v):
    f = float(v)
    return int(f) if f.is_integer() else f


def _isnan(v) -> bool:
    try:
        return math.isnan(float(v))
    except (TypeError, ValueError):
        return False


def outside_values(m: dict) -> list | None:
    """The values that mean 'no value' in this layer, as the gate stores them: the
    declared list; for a year of a land-cover stack that declares none, 0 and the
    declared nodata (the file's own no-data is outside the map, never a class). None
    for any other layer that declares none — its role's rule decides when it is read."""
    ov = m.get("outside_values")
    if ov is not None:
        return [_num(v) for v in ov]
    if not namespace(str(m.get("layer") or "")).get("stack"):
        return None
    out = [0]
    nod = (m.get("declared") or {}).get("nodata") if isinstance(m.get("declared"), dict) else None
    if _isnum(nod) and _num(nod) not in out:
        out.append(_num(nod))
    return out


def _bands(src) -> tuple[str | None, str | None]:
    """(a refusal, a note) for a file with more than one band. Every reader takes band
    1; a colour rendering (RGB, or 3-4 byte bands with alpha) holds colours, not values."""
    if src.count <= 1:
        return None, None
    from rasterio.enums import ColorInterp
    ci = set(src.colorinterp)
    photometric = str(src.tags(ns="IMAGE_STRUCTURE").get("PHOTOMETRIC") or "").upper()
    if ({ColorInterp.red, ColorInterp.green, ColorInterp.blue} <= ci
            or photometric in ("RGB", "YCBCR")
            or (src.count in (3, 4) and all(d == "uint8" for d in src.dtypes)
                and ColorInterp.alpha in ci)):
        kind = "RGBA" if ColorInterp.alpha in ci or src.count == 4 else "RGB"
        return (f"the file is a {src.count}-band {kind} rendering — colours drawn for display, "
                "not the values themselves (band 1 is the red channel). Contribute the "
                "single-band raster the colours were drawn from: the classified map (one "
                "class code per cell) or the measured grid, e.g. the producer's own .tif or "
                ".img"), None
    return None, (f"read band 1 of {src.count}: the platform reads one band, so the other "
                  f"{src.count - 1} band(s) were dropped — contribute any other band that "
                  "matters as its own layer")


# What a file IS, from its first bytes: a contributor's link names a zip, a picture or
# a NetCDF as often as a GeoTIFF, and GDAL's own word for each is a path and "not
# recognized". `sniff` names it; the gate says what to do about it.
_MAGIC = ((b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"), (b"\x1f\x8b", "gzip"),
          (b"7z\xbc\xaf\x27\x1c", "7z"), (b"Rar!", "rar"),
          (b"CDF\x01", "netcdf"), (b"CDF\x02", "netcdf"), (b"CDF\x05", "netcdf"),
          (b"\x89HDF\r\n\x1a\n", "hdf5"),
          (b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpeg"), (b"GIF8", "gif"),
          (b"II*\x00", "tiff"), (b"MM\x00*", "tiff"), (b"II+\x00", "tiff"), (b"MM\x00+", "tiff"),
          (b"%PDF", "pdf"), (b"SQLite format 3\x00", "sqlite"), (b"EHFA_HEADER_TAG", "img"))
# the suffix GDAL needs to pick the right driver (a netCDF-4 file is HDF5 inside)
SUFFIX = {"zip": ".zip", "netcdf": ".nc", "hdf5": ".nc", "png": ".png", "jpeg": ".jpg",
          "gif": ".gif", "tiff": ".tif", "sqlite": ".gpkg", "img": ".img"}
# GDAL drivers that read a picture: pixels are colours on a page, not values on the ground
_PICTURE_DRIVERS = {"PNG": "PNG", "JPEG": "JPEG", "GIF": "GIF", "BMP": "BMP", "WEBP": "WebP"}
_ARCHIVES = {"zip": "a ZIP archive", "gzip": "a gzip archive", "7z": "a 7-Zip archive",
             "rar": "a RAR archive", "pdf": "a PDF document"}


def sniff(head) -> str | None:
    """The kind of file `head` (bytes, or a path) begins like, or None."""
    if not isinstance(head, (bytes, bytearray)):
        try:
            with open(head, "rb") as f:
                head = f.read(64)
        except OSError:
            return None
    head = bytes(head[:64])
    for magic, kind in _MAGIC:
        if head.startswith(magic):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def picture_problem(src, always: bool = False) -> str | None:
    """A print-layout PNG or JPEG offered as a raster is a picture of a map, and no CRS
    can make it one — the old advice ("assign its CRS") sent a contributor round in
    circles. Returns the refusal, or None when the dataset is not a bare picture
    (`always`: a picture is refused whatever georeferencing a sidecar gave it)."""
    if src.driver not in _PICTURE_DRIVERS or (src.crs is not None and not always):
        return None
    bands = {1: "greyscale", 2: "greyscale with transparency", 3: "RGB", 4: "RGBA"}.get(src.count, "")
    return (f"the file is a picture ({_PICTURE_DRIVERS[src.driver]} image, {src.width:,} x "
            f"{src.height:,} pixels{', ' + bands if bands else ''}) — a map figure drawn for a "
            "page or a screen, not a georeferenced raster: it carries no coordinates, and its "
            "pixels are colours, not values. Contribute the layer the figure shows (the GeoTIFF, "
            ".img or vector map it was drawn from); to keep the figure itself, contribute the "
            "report it belongs to as a document")


_CF_LAT = {"degrees_north", "degree_north", "degree_n", "degrees_n", "degreen", "degreesn"}
_CF_LON = {"degrees_east", "degree_east", "degree_e", "degrees_e", "degreee", "degreese"}


def _cf_lonlat(src) -> tuple[str, str] | None:
    """(latitude name, longitude name) when the grid is laid on CF latitude/longitude
    coordinates in degrees — what an ERDDAP or THREDDS NetCDF subset carries instead of
    a grid_mapping — else None. GDAL builds the grid from those 1-D coordinates, so a
    regular (non-identity) transform inside the globe is required too."""
    units, names = {}, {}
    for k, v in (src.tags() or {}).items():
        var, _, attr = k.partition("#")
        if attr == "units":
            units[var] = str(v).strip().lower()
        elif attr == "standard_name":
            names[var] = str(v).strip().lower()

    def find(cf_units, words):
        for var, u in units.items():
            if u in cf_units or (u.startswith("degree") and (var.lower() in words
                                                             or names.get(var) in words)):
                return var
        return None
    lat, lon = find(_CF_LAT, ("lat", "latitude")), find(_CF_LON, ("lon", "long", "longitude"))
    if not (lat and lon) or src.transform.is_identity:
        return None
    b = src.bounds
    if not (-180 <= min(b.left, b.right) and max(b.left, b.right) <= 360
            and -90 <= min(b.bottom, b.top) and max(b.bottom, b.top) <= 90):
        return None
    return lat, lon


def packing(dtype: str, width: int, height: int) -> dict:
    """How the gate writes a GeoTIFF it keeps: DEFLATE with the predictor that suits the
    values (floating point, or integer differences), tiled when big enough. A clip cut
    to fit the size cap must not grow again when it is reprojected."""
    import numpy as np
    floats = np.dtype(dtype).kind == "f"
    out = {"compress": "deflate", "predictor": 3 if floats else 2}
    if width >= 256 and height >= 256:
        out.update(tiled=True, blockxsize=256, blockysize=256)
    return out


def unreadable(path: str, exc: Exception) -> str:
    """Why GDAL could not open a file, in the contributor's terms: what the file is when
    its first bytes say so, never the server's temporary path."""
    import zipfile
    kind = sniff(path)
    if kind == "zip":
        try:
            with zipfile.ZipFile(path) as z:
                names = [i.filename for i in z.infolist() if not i.is_dir()]
        except (zipfile.BadZipFile, OSError):
            names = []
        listed = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
        return (f"the file is a ZIP archive{f' of {len(names)} files ({listed})' if names else ''}, "
                "not a raster — name the raster inside it to contribute with clip_member "
                "(e.g. clip_member: 'folder/name.tif'), or end the url with '#<member name>'")
    if kind in _ARCHIVES:
        return (f"the file is {_ARCHIVES[kind]}, not a raster — give the raster itself (a GeoTIFF, "
                ".img, NetCDF or GeoPackage)")
    said = str(exc).replace(str(path), "the file")
    said = re.sub(r"'?/[^'\s]*staged-tmp-[^'\s]*'?", "the file", said)
    return f"the file could not be read as a raster: {said}"


def crs_of(src, what: str = "") -> tuple:
    """(the CRS the gate reads the grid on, a note or None). A picture is refused as a
    picture; a CF latitude/longitude grid with no CRS is WGS 84 by the convention's own
    default — ERDDAP and THREDDS subsets say it that way and never with a CRS — and the
    note says so; any other file with no CRS is refused."""
    from rasterio.crs import CRS
    if src.crs is not None:
        return src.crs, None
    picture = picture_problem(src)
    if picture:
        raise ValueError(picture)
    cf = _cf_lonlat(src)
    if not cf:
        raise ValueError("the file carries no coordinate reference system" + what + " — assign "
                         "its CRS (gdal_edit -a_srs, or QGIS: Layer -> Set CRS) and resubmit")
    west, _, east, _ = src.bounds
    if max(west, east) > 180 + abs(src.transform.a):
        raise ValueError(f"the grid's longitudes run {min(west, east):g} to {max(west, east):g} "
                         "(0 to 360); the platform reads -180 to 180 — shift it (cdo "
                         "sellonlatbox,-180,180,-90,90, or gdalwarp -t_srs EPSG:4326 --config "
                         "CENTER_LONG 0) and resubmit")
    return CRS.from_epsg(4326), (f"the file carries no CRS, but its grid is laid on CF "
                                 f"coordinates {cf[0]!r} and {cf[1]!r} in degrees (latitude/"
                                 "longitude), so it was read as EPSG:4326 (WGS 84); values "
                                 "unchanged")


# GDAL handed a file tries every driver it has, and some read the files or services the
# bytes NAME (a VRT, a label beside its data, a tile index, a service description) — on
# this server or over HTTP; a list of those is never complete. So the gate names the
# format from the file's first bytes and opens it with that ONE driver; anything else is
# declined. Pictures open only to be declined as pictures.
DRIVERS = {"tiff": "GTiff", "img": "HFA", "netcdf": "netCDF", "hdf5": "netCDF", "sqlite": "GPKG",
           "png": "PNG", "jpeg": "JPEG", "gif": "GIF", "webp": "WEBP"}
FORMATS = "GeoTIFF, ERDAS .img, NetCDF or GeoPackage"
# Matches no path: with it set, GDAL opens nothing over HTTP (no /vsicurl/, /vsis3/, ...).
NO_REMOTE = "/vsicurl/grp-no-remote-read"


def gdal_env(allowed: str | None = None) -> dict:
    """The GDAL config every open of contributed bytes runs under: over the network only
    the `allowed` path (a remote clip's one url) — for a local file, nothing at all, so
    no format can send GDAL to a url it names — and never a .netrc."""
    return {"CPL_VSIL_CURL_ALLOWED_FILENAME": allowed or NO_REMOTE, "GDAL_HTTP_NETRC": "NO"}


def driver_for(head) -> str:
    """The one GDAL driver for a file beginning with `head` (bytes, or a local path).
    ValueError, in words, for an archive, a recipe (a VRT, a service description — even
    behind leading bytes) or any format the gate does not open."""
    from . import remote_clip
    if not isinstance(head, (bytes, bytearray)):
        path = head
        try:
            with open(path, "rb") as f:
                head = f.read(4096)
        except OSError as exc:
            raise ValueError(f"the file could not be read ({type(exc).__name__})") from None
    else:
        path = None
    kind = sniff(head)
    said = remote_clip.recipe(head)
    if said:
        raise ValueError(f"the file is {said} — a description that points at other files or "
                         f"services, which the platform does not follow; give the raster file "
                         f"itself ({FORMATS})")
    if kind in DRIVERS:
        return DRIVERS[kind]
    if kind == "zip" and path:
        raise ValueError(unreadable(path, None))
    if kind == "zip" or kind in _ARCHIVES:
        raise ValueError(f"the file is {_ARCHIVES[kind]}, not a raster — give the raster itself "
                         f"({FORMATS})")
    text = bytes(head[:512]).lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")
    raise ValueError(f"the file is {'an XML/text file' if text else 'not in a raster format the platform opens'}"
                     f" — it reads {FORMATS} (or a zip holding one); convert it (gdal_translate "
                     "-of GTiff) and resubmit")


def _open_target(path: str, layer_name, driver: str | None = None) -> tuple[str, str | None, str]:
    """(what GDAL opens, a note, the one driver it opens with): the file itself, or the
    raster named `layer_name` inside a container that holds several (a GeoPackage with
    several gridded tables, a NetCDF of several variables). `driver`: chosen from the
    file's first bytes when not given (driver_for)."""
    import rasterio
    driver = driver or driver_for(path)
    with rasterio.open(path, driver=driver) as src:
        picture = picture_problem(src, always=True)
        if picture:
            raise ValueError(picture)
        subs, count = list(src.subdatasets or []), src.count
    if not subs:
        return path, None, driver   # a plain raster: there is nothing to choose
    names = [s.rsplit(":", 1)[-1] for s in subs]
    if layer_name:
        for sub, name in zip(subs, names):
            if name == str(layer_name):
                return sub, f"read the raster {name!r}, one of {len(subs)} in the file", driver
        raise ValueError(f"layer_name {layer_name!r} is not one of the rasters in the file — "
                         f"it holds {', '.join(names)}")
    if count == 0:
        raise ValueError(f"the file holds {len(subs)} rasters ({', '.join(names)}) — name the "
                         f"one to contribute with layer_name, e.g. layer_name: {names[0]!r}, "
                         "and submit each one you need as its own layer")
    return path, None, driver


def normalise(path: str, role: str, declared: dict | None = None,
              layer_name=None, driver: str | None = None) -> tuple[str, list[str]]:
    """The file the platform will verify and keep: a single-band GeoTIFF on EPSG:4326.

    Hubs hand over what they have — an ERDAS .img in UTM, a CRTM05 GeoTIFF, a
    GeoPackage of several grids. The counting primitives (stacks.row_pixel_areas_km2,
    every clip) assume a geographic grid, so a projected raster is re-gridded here the
    way its namespace says (`regrid`): nearest for classes (a class never blends),
    bilinear for an amount (a surface is interpolated), and a COUNT grid (population_*)
    is refused in words — re-gridding moves people between cells. A file with no nodata
    tag is tagged with the declared one. The passport says what was done. Returns (path
    to use, notes); the path is the input when nothing changed."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject
    from ..graph.geo import schema
    rule = schema.namespaces().get(str(role)) or {}
    regrid = str(rule.get("regrid") or "refuse")
    notes = []
    target, picked, driver = _open_target(path, layer_name, driver)
    if picked:
        notes.append(picked)
    with rasterio.open(target, driver=driver) as src:
        crs, said = crs_of(src)
        if said:
            notes.append(said)
        refusal, band_note = _bands(src)
        if refusal:
            raise ValueError(refusal)
        if band_note:
            notes.append(band_note)
        epsg = crs.to_epsg()
        geographic = bool(crs.is_geographic)
        driver = src.driver
        same_crs = epsg == 4326 or (geographic and epsg is None and "WGS 84" in crs.to_wkt())
        declared_nodata = (declared or {}).get("nodata")
        tag = (float(declared_nodata) if src.nodata is None and _isnum(declared_nodata)
               else None)
        if same_crs and driver == "GTiff" and src.count == 1 and tag is None and target == path:
            return path, notes
        if not same_crs and regrid == "refuse":
            raise ValueError(
                f"the file is on {crs.to_string()} (EPSG:{epsg}); a {role}_* layer is a count "
                "grid, which the platform never re-grids (reprojecting moves counts between "
                "cells) — reproject it yourself with a sum-preserving method and resubmit on "
                "EPSG:4326")
        out = str(path) + ".norm.tif"
        nodata = src.nodata if src.nodata is not None else tag
        if tag is not None:
            notes.append(f"the file carries no nodata tag; tagged {_num(tag)} at ingest, as "
                         "declared — values unchanged")
        dtype = src.dtypes[0]
        if same_crs:
            if driver != "GTiff":
                notes.append(f"converted from {driver} to GeoTIFF at ingest; values unchanged")
            with rasterio.open(out, "w", driver="GTiff", width=src.width, height=src.height,
                               count=1, dtype=dtype, crs=crs, transform=src.transform,
                               nodata=nodata, **packing(dtype, src.width, src.height)) as dst:
                dst.write(src.read(1), 1)
            return out, notes
        transform, width, height = calculate_default_transform(
            crs, "EPSG:4326", src.width, src.height, *src.bounds)
        fill = nodata
        if fill is None:
            fill = 0
            notes.append("the file declares no nodata; cells outside its original grid are 0 "
                         "after reprojection")
        method = Resampling.bilinear if regrid == "bilinear" else Resampling.nearest
        arr = np.full((height, width), fill, dtype=dtype)
        reproject(source=rasterio.band(src, 1), destination=arr,
                  src_transform=src.transform, src_crs=crs,
                  dst_transform=transform, dst_crs="EPSG:4326",
                  src_nodata=nodata, dst_nodata=nodata, resampling=method)
        with rasterio.open(out, "w", driver="GTiff", width=width, height=height, count=1,
                           dtype=dtype, crs="EPSG:4326", transform=transform, nodata=nodata,
                           **packing(dtype, width, height)) as dst:
            dst.write(arr, 1)
        how = ("bilinear — the surface is interpolated between cell centres, so a cell's "
               "value may differ slightly from any one source cell"
               if method == Resampling.bilinear else
               "nearest neighbour — class values unchanged, cell edges moved")
        notes.insert(0, f"reprojected at ingest from {crs.to_string()} (EPSG:{epsg}, "
                        f"{abs(src.transform.a):g} {crs.linear_units or 'unit'} cells) to "
                        f"EPSG:4326, {how}"
                        + ("; converted from " + driver if driver != "GTiff" else ""))
    return out, notes


def hints(m: dict) -> list[str]:
    """Advice the gate gives WITHOUT refusing: shapes of a manifest that pass every
    check and still give a wrong answer later. Shown with the preview."""
    out = []
    layer = str(m.get("layer") or "")
    try:
        legend = _norm_legend(m.get("legend")) if isinstance(m.get("legend"), dict) else {}
    except (TypeError, ValueError):
        legend = {}
    if namespace(layer).get("stack") and len(legend) == 1:
        code, only = next(iter(legend.items()))
        bg = 0 if code != 0 else code + 1
        out.append(
            f"ONE class in this stack's legend ({only}): the map says where {only} is, not "
            f"where it is not, so change between years will NOT be computed on this stack — "
            "every other cell counts as outside the map, and loss cannot be told from no "
            f"data. A single-year presence map is fine as it is. To get change, declare the "
            f"background value as a class, e.g. legend {{{bg}: 'Not {only.lower()}', "
            f"{code}: '{only}'}}, and keep outside_values for true no-data only (the file's "
            "nodata, or [] when every cell is mapped)")
    return out


def _legend_problem(m: dict) -> list[str]:
    """A legend's keys are class numbers; a class is never also an outside value."""
    legend = m.get("legend")
    if not isinstance(legend, dict) or not legend:
        return []
    try:
        legend = _norm_legend(legend)
    except (TypeError, ValueError):
        return ["legend keys must be class numbers, e.g. {1: Mangrove, 2: Non-mangrove}"]
    colors = m.get("colors")
    if colors is not None:
        names = {str(v).strip().lower() for v in legend.values()}
        codes = {float(k) for k in legend}
        bad = [k for k in (colors if isinstance(colors, dict) else {})
               if not (_isnum(k) and float(k) in codes) and str(k).strip().lower() not in names]
        hexes = [v for v in (colors.values() if isinstance(colors, dict) else [])
                 if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(v).strip())]
        if not isinstance(colors, dict) or bad or hexes:
            return [f"colors must map a legend class (its number or its label) to '#rrggbb' — "
                    + (f"{bad} name no class of the legend; " if bad else "")
                    + (f"{hexes} are not '#rrggbb'; " if hexes else "")
                    + "the legend: " + ", ".join(f"{k}: {v}" for k, v in legend.items())]
    ov = m.get("outside_values")
    if ov is not None and (not isinstance(ov, (list, tuple)) or not all(_isnum(v) for v in ov)):
        return [f"outside_values must be a list of pixel values, e.g. [255] or [] — got {ov!r}"]
    fails = []
    for v in outside_values(m) or []:
        if float(v) in {float(k) for k in legend}:
            k = next(k for k in legend if float(k) == float(v))
            why = ("" if ov is not None else
                   f" (outside_values is not given, so it defaults to {outside_values(m)}: 0 "
                   "and the declared nodata)")
            nod = (m.get("declared") or {}).get("nodata") if isinstance(m.get("declared"), dict) else None
            eg = (f"[{_num(nod)}], the file's nodata" if _isnum(nod) and float(nod) not in
                  {float(x) for x in legend} else "[255]")
            fails.append(f"class {k} {legend[k]!r} is also listed as outside the map{why} — a "
                         "value is either a class or outside the map, never both: declare "
                         f"outside_values as the values that mark no data (e.g. {eg}), or [] "
                         "when every cell is mapped")
    if namespace(str(m.get("layer") or "")).get("unique_labels"):
        # Change compares codes: {1: Mangrove, 10: Mangrove} landed, and 1956->2014 then
        # said every cell changed.
        from ..graph.geo import stacks
        dup = stacks.duplicate_labels_text(legend)
        if dup:
            said = _suggested({"event": "duplicate_labels", "manifest": m, "legend": legend})
            fails.append(f"{dup} — {said}" if said else dup)
    return fails


def _year_num(v):
    """A year as an int, or None when `v` is not a whole number."""
    if isinstance(v, bool) or not _isnum(v) or not float(v).is_integer():
        return None
    return int(float(v))


def _field_problems(m: dict, rule: dict) -> list[str]:
    """The fields a namespace lets a layer declare (schema `namespaces.<role>.fields`):
    another namespace's field is refused, never dropped; from_year/to_year go together,
    in order; `compares` names a registered stack holding those years. A layer that is
    not a year of a stack takes no stack or year."""
    layer, prefix = str(m.get("layer") or ""), rule.get("prefix", "")
    mine = rule.get("fields") or {}
    fails = []
    for p, r in namespaces().items():
        for k in (r.get("fields") or {}):
            if k not in mine and m.get(k) not in (None, ""):
                fails.append(f"{k} is a field of a {p}<name> layer; {layer!r} is not one — drop "
                             f"{k}, or name the layer {p}<name> if that is what it is")
    if rule and not rule.get("stack"):
        given = [k for k in ("stack", "year") if m.get(k) not in (None, "")]
        stack_ns = [p for p, r in namespaces().items() if r.get("stack")]
        if given:
            fails.append(f"{' and '.join(given)} place one year of a land-cover stack "
                         f"({', '.join(f'{p}<name>' for p in stack_ns) or 'a stack namespace'}); "
                         f"a {prefix}* layer is not one — drop {' and '.join(given)}"
                         + (f" and say what it covers with {', '.join(mine)}" if mine else
                            "; the year a layer describes goes in its title or description"))
    fy, ty = m.get("from_year"), m.get("to_year")
    if "from_year" in mine or "to_year" in mine:
        if (fy in (None, "")) != (ty in (None, "")):
            fails.append("from_year and to_year go together: give both years the map compares "
                         f"(got from_year {fy!r}, to_year {ty!r}), or neither")
        elif fy not in (None, ""):
            a, b = _year_num(fy), _year_num(ty)
            if a is None or b is None:
                fails.append(f"from_year and to_year must be years, e.g. 1985 and 2014 — got "
                             f"{fy!r} and {ty!r}")
            elif a >= b:
                fails.append(f"from_year {a} must come before to_year {b}")
    sid = m.get("compares")
    if "compares" in mine and sid not in (None, ""):
        from ..graph.geo import tiffs
        stacks_now = {k: v for k, v in tiffs.catalog().items() if v.get("kind") == "stack"}
        st = stacks_now.get(str(sid))
        if st is None:
            fails.append(f"compares names {sid!r}, which is not a registered land-cover stack — "
                         "registered: " + (", ".join(sorted(stacks_now)) or "none")
                         + "; leave compares out when the maps it compares are not on the platform")
        elif _year_num(fy) is not None and _year_num(ty) is not None:
            have = [int(y) for y in st.get("years") or []]
            off = [y for y in (_year_num(fy), _year_num(ty)) if y not in have]
            if off:
                fails.append(f"{sid} has no {' or '.join(map(str, off))} map (its years: "
                             f"{', '.join(map(str, sorted(have))) or 'none'}) — name the stack "
                             "whose maps it compares, or leave compares out")
    return fails


def declared_fields(m: dict) -> dict:
    """The namespace fields a manifest declares, as the staged and landed rows keep them
    (years as integers) — every read of the layer states them."""
    out = {}
    for k in namespace(str(m.get("layer") or "")).get("fields") or {}:
        v = m.get(k)
        if v not in (None, ""):
            out[k] = _year_num(v) if k.endswith("_year") and _year_num(v) is not None else v
    return out


def stack_legend_problem(m: dict, cat: dict | None = None) -> str | None:
    """A year joining an existing stack must carry the stack's legend — checked before
    the file is fetched, because a reviewer cannot fix a file. Returns the failure."""
    sid = str(m.get("stack") or "")
    if not sid or not isinstance(m.get("legend"), dict):
        return None
    if cat is None:
        from ..graph.geo import tiffs
        cat = tiffs.catalog()
    st = cat.get(sid)
    if not st or st.get("kind") != "stack":
        return None
    try:
        have, want = _norm_legend(st.get("legend")), _norm_legend(m.get("legend"))
    except (TypeError, ValueError):
        return None
    if have == want:
        return None
    return _legend_mismatch(sid, have, want, m)


def _legend_text(leg: dict) -> str:
    return "{" + ", ".join(f"{k}: {v}" for k, v in sorted(leg.items())) + "}"


def _suggested(situation: dict) -> str | None:
    """What the server's preparation ops (contrib/prepare) propose for a decline."""
    try:
        from . import prepare
        return prepare.suggest({"kind": "raster", **situation})
    except Exception:                       # a suggestion never breaks a decline
        return None


def _legend_mismatch(sid: str, have: dict, want: dict, manifest: dict | None = None) -> str:
    """Why a year's legend cannot join its stack, and how to make it — when the labels
    are the stack's under other codes, the exact recode, which the server can apply."""
    norm = lambda s: " ".join(str(s).lower().split())
    code_of = {norm(v): k for k, v in have.items()}
    recode = {k: code_of.get(norm(v)) for k, v in want.items()}
    head = (f"legend disagrees with stack {sid!r}: the stack maps {_legend_text(have)}, this "
            f"year declares {_legend_text(want)} — one legend per stack")
    new_stack = ("or name a new stack (stack: landcover_<another name>; a new stack cannot be "
                 "compared with this one year to year)")
    said = _suggested({"event": "stack_legend_mismatch", "manifest": manifest or {},
                       "stack": sid, "have": have, "want": want})
    if (all(c is not None for c in recode.values()) and len(set(recode.values())) == len(recode)
            and len(recode) == len(have)):
        if said:
            return (f"{head}. The labels are the stack's under other codes: {said}; {new_stack}")
        pairs = ", ".join(f"{a} -> {b}" for a, b in sorted(recode.items()) if a != b)
        return (f"{head}. The labels are the stack's under other codes: recode {pairs} to match "
                f"the stack's legend {_legend_text(have)}, {new_stack}")
    unknown = [want[k] for k, c in recode.items() if c is None]
    missing = [v for k, v in sorted(have.items()) if k not in set(recode.values())]
    detail = "; ".join(x for x in (
        f"not in the stack's legend: {', '.join(unknown)}" if unknown else "",
        f"in the stack's legend but not this year's: {', '.join(missing)}" if missing else "") if x)
    return (f"{head} ({detail}). Recode the file to the stack's classes and codes "
            f"{_legend_text(have)}{f' — {said} —' if said else ','} {new_stack}")


def validate_manifest(m: dict) -> list[str]:
    """Every problem at once. license/vintage are REQUIRED for new layers — the
    existing catalog's unrecorded vintages are a declared gap we do not grow.
    'unstated' is accepted, silence is not."""
    fails = []
    if not isinstance(m, dict):
        return ["manifest is not a mapping"]
    layer = str(m.get("layer") or "")
    rule = namespace(layer)
    prefix = rule.get("prefix", "")
    for k in REQUIRED:
        if k == "legend" and rule and not rule.get("legend", True):
            continue          # a count or a measure has no classes to label
        if not m.get(k):
            fails.append(f"missing required field '{k}'")
    if layer and not rule:
        fails.append("layer must be namespaced — the platform's namespaces: " + namespace_text())
    if rule.get("stack"):
        # One year of a yearly categorical STACK: the year names its stack so the
        # series' legend and years live once, and the layer name must be what the
        # stack's year pattern resolves to.
        for k in ("stack", "year"):
            if not m.get(k):
                fails.append(f"missing required field '{k}' — a land-cover year names its "
                             f"stack ({prefix}<name>) and its map year")
        if m.get("stack") and not str(m["stack"]).startswith(prefix):
            fails.append(f"stack must be namespaced {prefix}<name>")
        if m.get("stack") and m.get("year") and layer != f"{m['stack']}_{m['year']}":
            fails.append(f"layer must be '{m['stack']}_{m['year']}' (<stack>_<year>) so the "
                         "stack resolves it by year")
    if m.get("file") and not Path(str(m["file"])).is_file():
        fails.append(f"file {m['file']!r} does not exist")
    if m.get("legend") is not None and not isinstance(m.get("legend"), dict):
        fails.append("legend must map class number -> label")
    decl = m.get("declared")
    if isinstance(decl, dict):
        # Normalised in place, so the stored manifest, the staged contract and the landed
        # one say the same: null is an omitted nodata (the file's own tag is used) — a
        # standard MCP client sends a float NaN as null — and NaN in any form ("nan",
        # "NaN", a JSON NaN) is kept as the string "nan", which JSON carries.
        if "nodata" in decl and decl["nodata"] is None:
            del decl["nodata"]
        elif _isnan(decl.get("nodata")):
            decl["nodata"] = "nan"
        for k in _DECLARED_REQUIRED:
            if decl.get(k) is None:
                fails.append(f"declared.{k} is required — the contract is what the "
                             "file gets verified AGAINST; observation cannot write it")
    elif decl is not None:
        fails.append("declared must be a mapping (dtype/valid_min/valid_max/...)")
    kind = rule.get("values")
    if kind == "amount":
        if m.get("legend"):
            fails.append(f"a {prefix}* layer holds amounts, not classes — drop the legend "
                         "(declared.units says what one value means), or contribute a classed "
                         f"map as {_prefixes('classes')}")
        if isinstance(decl, dict) and not str(decl.get("units") or "").strip():
            fails.append(f"declared.units is required for a {prefix}* layer — what one value "
                         "means, e.g. 'Mg/ha', 'm', 't C/ha', 'degC', 'mm'")
    if isinstance(decl, dict):
        for k in ("scale_factor", "offset"):
            if decl.get(k) is not None and not _isnum(decl[k]):
                fails.append(f"declared.{k} must be a number, got {decl[k]!r}")
        if _isnum(decl.get("scale_factor")) and float(decl["scale_factor"]) == 0:
            fails.append("declared.scale_factor cannot be 0 — every value would read as the offset")
        if (decl.get("scale_factor") is not None or decl.get("offset") is not None) \
                and kind not in ("amount", "count"):
            fails.append(f"declared.scale_factor/offset turn stored integers into amounts; a "
                         f"{prefix or 'classed '}* layer holds classes, which are never scaled")
    cls = rule.get("classes")
    if isinstance(decl, dict) and cls:
        # Classes are the contract every reader indexes by. A hazard declared
        # 0..300 landed, and the first count died on class 300 — the gate holds
        # the line, in words, before a file is fetched.
        lo, hi = int(cls[0]), int(cls[-1])
        try:
            vmin, vmax = float(decl.get("valid_min")), float(decl.get("valid_max"))
        except (TypeError, ValueError):
            vmin = vmax = None
        if vmax is not None and (vmax > hi or vmin < 0):
            amounts = _prefixes("amount")
            said = _suggested({"event": "class_range", "manifest": m, "classes": [lo, hi],
                               "declared": decl})
            fails.append(f"declared valid range {vmin:g}..{vmax:g}: a {prefix}* layer carries "
                         f"integer classes {lo}..{hi} (0 = nodata) — reclassify a continuous "
                         "raster into those classes before contributing it"
                         + (f", or contribute the continuous values themselves as {amounts} "
                            "with their units" if amounts else "")
                         + (f"; {said}" if said else ""))
    fails += _legend_problem(m)
    fails += _field_problems(m, rule)
    if rule.get("stack") and m.get("stack") and isinstance(m.get("legend"), dict):
        clash = stack_legend_problem(m)
        if clash:
            fails.append(clash)
    from . import notes
    fails += notes.validate(m.get("usage_notes"))
    return fails


def _explain(mismatch: str, decl: dict, obs: dict | None = None) -> str:
    """A contract mismatch, with what to do about the usual causes."""
    if mismatch.startswith(("max ", "min ")):
        if decl.get("scale_factor") is not None or decl.get("offset") is not None:
            mismatch += (" — valid_min/valid_max are checked against the values as STORED "
                         "in the file, before scale_factor/offset")
        mismatch += (". If that value marks no data (open sea, outside the map, a fill such as "
                     "-9999), list it under outside_values or declare it as nodata: those are "
                     "not readings, so the range check skips them")
    elif mismatch.startswith("nodata "):
        mismatch += (" — declare the nodata the file is tagged with, or list other no-data "
                     "values under outside_values")
        if _isnan((obs or {}).get("nodata")) and not _isnan(decl.get("nodata")):
            mismatch += '; for a NaN fill, send the string "nan"'
    return mismatch


def add(manifest: dict, dry_run: bool = False, review: str | None = None,
        keep_prepared: bool = False, prepared_before: list | None = None,
        fetched: dict | None = None, contribution_id: str | None = None) -> dict:
    """Gate then land: validate -> verify file against the DECLARED contract ->
    copy into TIFFS_DIR -> append tiffs.yml row + raster_schema.yml contract.
    A mismatch refuses and writes NOTHING. `prepared_before`: what was done to the file
    when it was staged (reprojected, a band dropped) — the landed row keeps saying it.
    `fetched`: where a clip or a zip member was read from (url, box, member, sha256) —
    the row keeps it so the stored file can be cut again and compared.
    `contribution_id`: the gate's record that landed it — a take-down checks the row is
    still that one's before removing it."""
    from ..config import get_settings

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
    import rasterio
    # every open of the contributed bytes: one driver, and nothing over the network
    with rasterio.Env(**gdal_env()):
        return _verify_and_land(manifest, dry_run, review, keep_prepared, prepared_before,
                                fetched, contribution_id, decl, layer, settings)


def _verify_and_land(manifest, dry_run, review, keep_prepared, prepared_before, fetched,
                     contribution_id, decl, layer, settings):
    from ..graph.geo import schema as schema_mod, verify
    try:
        src_file, prepared = normalise(str(manifest["file"]), decl["role"], declared=decl,
                                       layer_name=manifest.get("layer_name"))
    except ValueError as exc:
        return {"status": "declined", "verified": False, "failures": [str(exc)]}
    except Exception as exc:
        return {"status": "declined", "verified": False,
                "failures": [unreadable(str(manifest["file"]), exc)]}
    original = str(manifest["file"])
    manifest = {**manifest, "file": src_file}
    prepared = [*(prepared_before or []), *(n for n in prepared if n not in (prepared_before or []))]

    def _drop_prepared():
        # the converted copy lives beside the input; it never outlives this call
        # unless the caller (the staging path) takes it over
        if src_file != original and os.path.exists(src_file):
            os.unlink(src_file)
    legend_keys = tuple(_norm_legend(manifest.get("legend")))
    outside = outside_values(manifest)
    # A declared range is of READINGS: the layer's outside values and its nodata
    # (255 over open sea) are skipped, never a reason to widen the range.
    skip = [*(outside or []), *([decl["nodata"]] if _isnum(decl.get("nodata")) else [])]
    # normalise leaves a GeoTIFF: the contributor's own (checked GTiff by its bytes) or
    # the one it wrote — read with that one driver from here on
    obs = verify.windowed_stats(str(manifest["file"]), keep_values=legend_keys,
                                skip_values=tuple(skip), driver="GTiff")
    if _isnan(obs.get("nodata")):
        obs["nodata"] = "nan"           # a NaN number reaches the reply as null: 'no tag'
    tag = obs.get("nodata")
    if (manifest.get("outside_values") is None and outside is not None and _isnum(tag)
            and not math.isnan(float(tag)) and _num(tag) not in outside
            and float(tag) not in {float(k) for k in legend_keys}):
        # a stack year that declares no outside values: the file's own nodata tag is
        # outside the map too, whether or not the contract repeats it
        outside = [*outside, _num(tag)]
    obs["outside_values"] = outside
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
                                    row={"outside_values": outside,
                                         "legend": manifest.get("legend") or {}},
                                    driver="GTiff")
    if gran:
        obs["granularity"] = gran
    mismatches = verify._check({**(schema_mod._doc().get("defaults") or {}), **decl}, obs)
    if mismatches:
        _drop_prepared()
        return {"status": "declined", "verified": False,
                "failures": [f"file does not satisfy the DECLARED contract: {_explain(m, decl, obs)}"
                             for m in mismatches],
                "observed": obs}
    if legend_keys and namespace(layer).get("values") == "classes":
        # the readers' own count, run on the file before it is approved: they mask the
        # outside values, the declared nodata and the file's own fill tag
        from ..graph.geo import stacks as _stacks
        unread = _stacks.read_check(str(manifest["file"]), _norm_legend(manifest.get("legend")),
                                    [*skip, *([_num(tag)] if _isnum(tag) else [])], driver="GTiff")
        if unread:
            _drop_prepared()
            return {"status": "declined", "verified": False, "observed": obs,
                    "failures": [f"the file does not read as a class map: {unread}"]}
    if dry_run:
        if not keep_prepared:
            _drop_prepared()
        return {"status": "valid (dry-run, nothing written)", "verified": True,
                "observed": obs, "file": str(manifest["file"]) if keep_prepared else original}

    # The catalog files are read, changed and written as one step (catalog_io.LOCK).
    with catalog_io.LOCK:
        cat_path = Path(settings.tiffs_contrib_path)
        cat = (yaml.safe_load(cat_path.read_text()) or {}) if cat_path.exists() else {}
        if layer in cat:
            # checked again where it is written: two landings of one name at once both
            # passed the first check, and the second's file replaced the first's
            _drop_prepared()
            return {"status": "declined", "verified": True, "observed": obs,
                    "failures": [f"layer {layer!r} already in the catalog — contributions "
                                 "add layers, they do not overwrite them"]}
        if namespace(layer).get("stack"):
            conflict = _stack_conflict(cat, manifest)
            if conflict:
                _drop_prepared()
                return {"status": "declined", "verified": True, "failures": [conflict], "observed": obs}

        dest = Path(settings.tiffs_dir) / f"{layer}.tif"
        dest.parent.mkdir(parents=True, exist_ok=True)
        from ..graph.geo.ingest import atomic_path
        with atomic_path(str(dest)) as tmp:          # a reader never opens half a copy
            shutil.copyfile(manifest["file"], tmp)
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
               **({"fetched": fetched} if fetched else {}),
               **({"outside_values": outside} if outside is not None else {}),
               **({"countries": list(manifest["countries"])} if manifest.get("countries") else {}),
               **declared_fields(manifest),
               **({"contribution_id": contribution_id} if contribution_id else {}),
               "contributed": True}
        cat[layer] = row
        if namespace(layer).get("stack"):
            _land_stack_year(cat, layer, manifest, dest, outside or [])
        catalog_io.write_text(cat_path, "# Contributed raster rows — machine-owned; written by the\n"
                              "# contribution gate. Hand-edit conf/tiffs.yml, never this.\n"
                              + yaml.safe_dump(cat, sort_keys=False, allow_unicode=True))

        sch_path = Path(settings.raster_schema_contrib_path)
        sch = (yaml.safe_load(sch_path.read_text()) or {}) if sch_path.exists() else {}
        sch.setdefault("layers", {})[layer] = decl
        catalog_io.write_text(sch_path, "# Contributed raster contracts — machine-owned.\n"
                              + yaml.safe_dump(sch, sort_keys=False, allow_unicode=True))

        return {"status": "landed", "layer": layer, "verified": True,
                "file": str(dest), "observed": obs,
                "passport": {k: row[k] for k in ("title", "source", "license", "vintage")}}


def _norm_legend(legend):
    return {int(k): str(v).strip() for k, v in (legend or {}).items()}


def _stack_conflict(cat, manifest):
    """A year joining an existing stack must share its legend — one legend per series
    is the whole point of a stack. Returns the failure text, or None. The gate checks
    the legend before fetching (stack_legend_problem); this holds the line at landing
    against a stack that changed in between."""
    st = cat.get(str(manifest.get("stack")))
    if not st or st.get("kind") != "stack":
        return None
    have, want = _norm_legend(st.get("legend")), _norm_legend(manifest.get("legend"))
    if have != want:
        return _legend_mismatch(str(manifest["stack"]), have, want, manifest)
    if int(manifest["year"]) in [int(y) for y in st.get("years") or []]:
        return (f"stack {manifest['stack']!r} already has a {manifest['year']} map — "
                "contributions add years, they do not overwrite them")
    return None


def merge_outside(stack_row: dict, year_outside) -> None:
    """A stack's outside values are every year's: a year marking outside 255 joins a
    stack whose first year marked it 0, and both stay outside for every year."""
    have = [_num(v) for v in stack_row.get("outside_values") or []]
    for v in year_outside or []:
        if _num(v) not in have:
            have.append(_num(v))
    stack_row["outside_values"] = have


def _land_stack_year(cat, layer, manifest, dest, outside):
    """A landed land-cover year joins (or starts) its STACK row in the contributed
    catalog: the series' legend, years and extent live once; the per-year row points
    at its file and carries the stack id so every primitive can find its contract."""
    from ..graph.geo import stacks as _stacks
    sid, year = str(manifest["stack"]), int(manifest["year"])
    b = _stacks.header(str(dest), driver="GTiff")["bounds"]      # the GeoTIFF the gate verified
    row = cat[layer]
    row.update({"stack": sid, "year": year, "role": _role(layer), "internal": True,
                "outside_values": outside})
    for k in ("authority", "accuracy"):
        if manifest.get(k) is not None:
            row[k] = manifest[k]
    st = cat.get(sid) or {
        "kind": "stack", "role": _role(layer), "internal": True, "contributed": True,
        "title": manifest.get("stack_title") or f"{sid} land cover",
        "description": manifest["description"], "source": manifest["source"],
        "license": manifest["license"], "legend": manifest.get("legend") or {},
        "vintage": manifest.get("vintage"),
        **({"url": manifest["url"]} if manifest.get("url") else {}),
        # [] is a declaration ("the whole grid is mapped"), not an omission
        "outside_values": list(outside),
        **({"countries": list(manifest["countries"])} if manifest.get("countries") else {}),
        "authority": manifest.get("authority") or "contributed; authority unstated",
        "accuracy": manifest.get("accuracy") or "unstated",
        **({"colors": manifest["colors"]} if manifest.get("colors") else {}),
        "layer_pattern": f"{sid}_{{year}}", "years": [],
        "extent": {"lon": [b[0], b[2]], "lat": [b[1], b[3]]}}
    merge_outside(st, outside)
    st["years"] = sorted(set(int(y) for y in st.get("years") or []) | {year})
    ext = st.get("extent") or {"lon": [b[0], b[2]], "lat": [b[1], b[3]]}
    st["extent"] = {"lon": [min(ext["lon"][0], b[0]), max(ext["lon"][1], b[2])],
                    "lat": [min(ext["lat"][0], b[1]), max(ext["lat"][1], b[3])]}
    cat[sid] = st
