"""rasterise: a polygon map landed as a class raster, at ingest.

The case: official land-use maps published as polygons (MAGA Guatemala's 1:50,000 land use
and land cover for 2003, 2010, 2020 and 2025, served over WFS) compare year to year only
as a land-cover stack, and a stack is rasters — so each year was rasterised by hand onto
one 30 m grid with one grouped legend. Now each year goes in from its own link with
rasterise: {field, resolution, classes}: the server reads the polygons (a WFS paged, only
the clip's box asked for), writes each polygon's code into the cells whose CENTRE it holds
(all-touched off, so a class's area is kept, give or take a cell's edge along its
boundary), on an EPSG:4326 grid of the declared cell size over the clip (or over the
polygons, snapped to that cell size so the years of a stack line up), and the gate then
verifies the raster as it verifies any other. Applies to every namespace whose values are
classes (rasters.namespace(...)["values"]), read from the schema, never a list here.
"""

from __future__ import annotations

import math
import re

NAME = "rasterise"
STEP = "convert"
KINDS = ("raster",)
NEEDS_INPUT = False          # it reads the polygons itself: a WFS is paged, not downloaded whole
FIELDS = {
    "rasterise": "for a POLYGON map (GeoJSON, zipped shapefile, GeoPackage, or a WFS "
                 "GetFeature link) that lands as a CLASS raster — a land-cover year joining a "
                 "stack, a change_* map, any classed layer: {field: '<the attribute naming each "
                 "polygon's class>', resolution: <the cell size — degrees as a number, e.g. "
                 "0.0002695, or metres as text, e.g. '30 m'>, classes: {<attribute value>: "
                 "<code>, ...}}. The url is the polygon layer itself. The platform rasterises it "
                 "at ingest — nothing to do by hand: each cell takes the code of the polygon "
                 "holding its centre (all-touched off, so class areas are kept, within a cell's "
                 "edge along each boundary), on an EPSG:4326 grid over `clip` when given (a WFS "
                 "is then asked only for that box, page by page) or else over the polygons; "
                 "cells no polygon covers take declared.nodata, or the first outside value (0). "
                 "classes maps each value of the field to the code it lands as; several values "
                 "may share one code (a grouping); omitted, a value that is a legend label lands "
                 "as that label's code, and a whole number that is a legend code as itself. A "
                 "value classes does not map is declined, "
                 "naming every value the layer holds — ASK the contributor which code each "
                 "means, never guess; years of one stack map their own values onto the stack's "
                 "codes. legend and declared describe the raster AS IT LANDS (declared.dtype "
                 "e.g. uint8). Optional `layer`: the shapefile or GeoPackage table to read when "
                 "the file holds several. The record keeps the field, the mapping, the grid, the "
                 "polygons' and the raster's area per class and every input's sha256; preview: "
                 "true shows the classes and km² first",
}

_EG = "e.g. {field: 'C_USO', resolution: '30 m', classes: {314: 6, 521: 12}}"
_KEYS = ("field", "resolution", "classes", "layer")
M_PER_DEG = 2 * math.pi * 6378137.0 / 360        # metres in one degree along the equator
BAND_CELLS = 8_000_000                           # cells one burn of a row band holds
_SHOWN = 150


# --------------------------------------------------------------------------- the fields

def _whole(v):
    """v as an int when it is a whole number (10, '10', 10.0), else None."""
    if isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else None


def key(v) -> str | None:
    """A value as `classes` matches it: a whole number by its digits (314, 314.0 and '314'
    alike), text trimmed with case and runs of spaces folded."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        f = float(v)
        return str(int(f)) if f.is_integer() else repr(f)
    s = " ".join(str(v).split())
    if re.fullmatch(r"[+-]?\d+(\.0*)?", s):
        return str(int(float(s)))
    return s.casefold()


def resolution(r) -> tuple[float, str]:
    """(degrees, how it was given); ValueError in words."""
    if isinstance(r, (int, float)) and not isinstance(r, bool):
        num, unit = float(r), ""
    else:
        m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)\s*(m|metres?|meters?|deg|"
                         r"degrees?|°)?\s*", str(r or ""))
        if not m:
            raise ValueError(f"rasterise.resolution must be the cell size, degrees as a number "
                             f"(0.0002695) or metres as text ('30 m') — got {r!r}")
        num, unit = float(m.group(1)), (m.group(2) or "").lower()
    if unit.startswith("m"):
        if not 0.1 <= num <= 100_000:
            raise ValueError(f"rasterise.resolution {r!r}: a cell of {num:g} m is not a map grid "
                             "— give the cell size, e.g. '30 m'")
        return float(f"{num / M_PER_DEG:.4g}"), f"{num:g} m"
    if not 0 < num < 1:
        raise ValueError(f"rasterise.resolution {r!r} reads as degrees ({num:g}° is about "
                         f"{num * M_PER_DEG / 1000:,.0f} km) — give metres as text, e.g. '30 m', "
                         "or degrees below 1, e.g. 0.0002695")
    return num, f"{num:g}°"


def spec(m) -> dict:
    """The rasterise field read: {field, degrees, given, classes {key: code} | None,
    named {key: value as given}, layer}; ValueError naming what is wrong."""
    r = m.get("rasterise")
    if not isinstance(r, dict):
        raise ValueError(f"rasterise must be a mapping, {_EG} — got {r!r}")
    extra = sorted(set(r) - set(_KEYS))
    if extra:
        raise ValueError(f"rasterise takes {', '.join(_KEYS)} — not {', '.join(map(str, extra))}")
    field = r.get("field")
    if not isinstance(field, str) or not field.strip():
        raise ValueError(f"rasterise.field must name the attribute that holds each polygon's "
                         f"class, {_EG} — ASK the contributor which attribute it is")
    deg, given = resolution(r.get("resolution"))
    classes = named = None
    if r.get("classes") is not None:
        if not isinstance(r["classes"], dict) or not r["classes"]:
            raise ValueError(f"rasterise.classes must map each value of {field} to the code it "
                             f"lands as, e.g. {{314: 6, 'Estero': 12}} — got {r['classes']!r}")
        classes, named, bad = {}, {}, []
        for k, v in r["classes"].items():
            code, kk = _whole(v), key(k)
            if code is None or kk is None:
                bad.append(f"{k!r}: {v!r}")
            elif kk in classes and classes[kk] != code:
                bad.append(f"{k!r} twice ({classes[kk]} and {code})")
            else:
                classes[kk], named[kk] = code, k
        if bad:
            raise ValueError("rasterise.classes maps a value to a whole-number code — not "
                             + ", ".join(bad))
    layer = r.get("layer")
    if layer is not None and not (isinstance(layer, str) and layer.strip()):
        raise ValueError(f"rasterise.layer names the shapefile or GeoPackage table to read — got "
                         f"{layer!r}")
    return {"field": field.strip(), "degrees": deg, "given": given, "classes": classes,
            "named": named, "layer": layer.strip() if layer else None}


def _fill(m: dict):
    """The value cells no polygon covers take, and whether the file is tagged with it as
    nodata: declared.nodata, else the first outside value (a land-cover year: 0), else 0."""
    from .. import rasters
    decl = m.get("declared") if isinstance(m.get("declared"), dict) else {}
    if rasters._isnum(decl.get("nodata")):
        return rasters._num(decl["nodata"]), True
    out = rasters.outside_values(m)
    return (rasters._num(out[0]) if out else 0), False


def _fits(dtype: str, value) -> bool:
    import numpy as np
    try:
        dt = np.dtype(dtype)
    except TypeError:
        return True                          # the contract check names a bad dtype
    if dt.kind in "iu":
        info = np.iinfo(dt)
        return info.min <= value <= info.max
    return dt.kind == "f"


def validate(manifest) -> list[str]:
    from .. import rasters
    m = manifest
    rule = rasters.namespace(str(m.get("layer") or ""))
    if rule and rule.get("values") != "classes":
        return [f"rasterise makes a class map; a {rule['prefix']}* layer holds "
                f"{rule.get('values') or 'values'}, not classes — drop rasterise"]
    try:
        sp = spec(m)
    except ValueError as exc:
        return [str(exc)]
    problems = []
    decl = m.get("declared") if isinstance(m.get("declared"), dict) else {}
    dtype = str(decl.get("dtype") or "")
    fill, _ = _fill(m)
    try:
        legend = rasters._norm_legend(m.get("legend")) if isinstance(m.get("legend"), dict) else {}
    except (TypeError, ValueError):
        legend = {}
    outside = {rasters._num(v) for v in (rasters.outside_values(m) or [])} | {fill}
    codes = set((sp["classes"] or {}).values()) or set(legend)
    off = sorted(c for c in codes | {fill} if dtype and rasters._isnum(c) and not _fits(dtype, c))
    if off:
        problems.append(f"rasterise writes {', '.join(map(str, off))}, which declared.dtype {dtype} "
                        "cannot hold — declare the type the raster lands as (e.g. uint8 for codes "
                        "0-255)")
    if fill in legend:
        problems.append(f"cells no polygon covers take {fill}, which the legend names "
                        f"{legend[fill]!r} — declare declared.nodata as a value that is no class "
                        "(e.g. 255), or outside_values")
    if sp["classes"] and legend:
        stray = sorted(c for c in set(sp["classes"].values()) if c not in legend and c not in outside)
        if stray:
            problems.append(f"rasterise.classes writes {', '.join(map(str, stray))}, which is no "
                            f"class of the legend {rasters._legend_text(legend)} (nor an outside "
                            "value): legend and declared describe the raster AS IT LANDS — map "
                            "each value onto a legend code")
    clip = m.get("clip")
    if isinstance(clip, (list, tuple)) and len(clip) == 4 and all(rasters._isnum(v) for v in clip):
        from ...config import get_settings
        w, s, e, n = (float(v) for v in clip)
        cells = max(1, round((e - w) / sp["degrees"])) * max(1, round((n - s) / sp["degrees"]))
        most = int(get_settings().grp_prep_max_cells)
        if e > w and n > s and cells > most:
            problems.append(f"rasterising the clip at {sp['given']} makes {cells:,} cells, over the "
                            f"{most:,} one preparation step may write — give a coarser resolution "
                            "or a smaller clip")
    return problems


# --------------------------------------------------------------------------- the work

class _Lookup:
    """value -> code: `classes` when given, else the legend (a value that is a label lands
    as its code; a whole number that is a code, as itself)."""

    def __init__(self, sp: dict, legend: dict):
        self.classes = sp["classes"]
        self.by_label = {key(v): c for c, v in legend.items()}
        self.by_code = {str(c): c for c in legend}

    def __call__(self, k):
        if self.classes is not None:
            return self.classes.get(k)
        return self.by_label.get(k, self.by_code.get(k))


class _Values:
    """What the layer's field holds: polygons and km² per value, and a describing text
    (another attribute that says the same for every polygon of a value, e.g. its label)."""

    def __init__(self, field: str):
        self.field, self.n, self.km2, self.shown, self.desc = field, {}, {}, {}, {}

    def add(self, k, given, props: dict, km2: float) -> None:
        self.n[k] = self.n.get(k, 0) + 1
        self.km2[k] = self.km2.get(k, 0.0) + km2
        self.shown.setdefault(k, given)
        d = self.desc.setdefault(k, {})
        for p, v in props.items():
            if p == self.field or not isinstance(v, str) or not v.strip() or len(v) > 80:
                continue
            if p not in d:
                d[p] = v
            elif d[p] != v:
                d[p] = None

    def label_of(self) -> dict:
        """{value key: describing text} by the attribute that tells the most values apart
        (one text per value, a different text for each), the longer texts on a tie."""
        texts: dict = {}
        for d in self.desc.values():
            for p, v in d.items():
                if v is not None:
                    texts.setdefault(p, []).append(v)
        if not texts:
            return {}
        best = max(sorted(texts), key=lambda p: (len(set(texts[p])), sum(map(len, texts[p]))))
        if len(set(texts[best])) < 2 and len(self.desc) > 1:
            return {}                    # the same text for every value names none of them
        return {k: d.get(best) for k, d in self.desc.items() if d.get(best)}

    def order(self, keys) -> list:
        def sort_key(k):
            v = self.shown.get(k)
            return (0, float(k), "") if re.fullmatch(r"[+-]?\d+", k or "") else (1, 0.0, str(v))
        return sorted(keys, key=sort_key)


def run(ctx):
    """Read the polygons, burn them a row band at a time into a GeoTIFF on the grid (the
    raster never whole in memory), and say what landed against what the polygons hold."""
    import numpy as np
    import shapely
    from shapely.geometry import box as shapely_box
    from .. import rasters, remote_clip
    from ...graph.geo import vectors
    from . import PrepRefused, PreparedFile
    from ._features import Reader
    m = ctx.manifest
    sp = spec(m)
    deg, field = sp["degrees"], sp["field"]
    url, frag = remote_clip.split_member(str(m.get("url") or ""))
    box = where = None
    if m.get("clip") is not None:
        box, where = remote_clip.box_of(m["clip"])
    legend = rasters._norm_legend(m["legend"]) if isinstance(m.get("legend"), dict) else {}
    lookup, seen = _Lookup(sp, legend), _Values(field)
    fill, tagged = _fill(m)
    decl = m.get("declared") or {}
    dtype = str(decl.get("dtype") or "uint8")
    clip_geom = shapely_box(*box) if box else None

    spool = ctx.tmp(".wkb", "polygons")
    offsets, lengths, codes, bounds = [], [], [], []
    poly_km2: dict = {}
    vertices = has_field = polygons = 0
    other = {"not_polygons": 0, "no_value": 0, "outside_box": 0, "unmapped": 0}
    unmapped: set = set()
    # polygons are cut to the clip (a cell beyond it kept) as they are read: a box asked of
    # a service answers whole national polygons, worked on here only where the grid lies
    reader = Reader(ctx, url, box, sp["layer"] or frag, asker="rasterise",
                    layer_field="rasterise.layer",
                    cut=[box[0] - deg, box[1] - deg, box[2] + deg, box[3] + deg] if box else None)
    attributes: set = set()
    total = 0
    with open(spool, "wb") as out:
        for chunk in reader:
            ctx.check_time()
            for props, geom in chunk:
                total += 1
                if len(attributes) < 64:
                    attributes.update(list(props)[:64])
                if geom.is_empty:                     # cut away: it lies outside the clip
                    other["outside_box"] += 1
                    continue
                if geom.geom_type not in ("Polygon", "MultiPolygon"):
                    other["not_polygons"] += 1
                    continue
                vertices += shapely.get_num_coordinates(geom)
                if clip_geom is not None:
                    x0, y0, x1, y1 = geom.bounds
                    if x1 <= box[0] or x0 >= box[2] or y1 <= box[1] or y0 >= box[3] \
                            or not geom.intersects(clip_geom):
                        other["outside_box"] += 1
                        continue
                polygons += 1
                if field in props:
                    has_field += 1
                k = key(props.get(field))
                if k is None or k == "":
                    other["no_value"] += 1
                    continue
                part = shapely.clip_by_rect(geom, *box) if box else geom
                km2 = vectors.area_km2(part) if not part.is_empty else 0.0
                seen.add(k, props.get(field), props, km2)
                code = lookup(k)
                if code is None:
                    other["unmapped"] += 1
                    unmapped.add(k)
                    continue
                wkb = shapely.to_wkb(geom)
                offsets.append(out.tell())
                lengths.append(len(wkb))
                out.write(wkb)
                codes.append(code)
                bounds.append(geom.bounds)
                poly_km2[code] = poly_km2.get(code, 0.0) + km2
            ctx.check_vertices(vertices, "the polygon layer")
            if polygons and not has_field:          # the attribute is not there: say so now
                raise PrepRefused(_no_field(field, attributes, url))
    if total == 0:
        raise PrepRefused(f"the layer at {url} holds no features"
                          + (" in the clip's box" if reader.asked_box else ""))
    if unmapped:
        raise PrepRefused(_unmapped(sp, seen, unmapped, legend, fill, m))
    if not codes:
        said = (f"none of the {total:,} feature(s) is a polygon (they are points or lines) — "
                "rasterise makes a class map from polygons" if other["not_polygons"] == total
                else f"none of the {total:,} polygon(s) lies in the clip box {box} — check the "
                     "box ([minLon, minLat, maxLon, maxLat]); if the layer is on the right area, "
                     "the service may have sent latitude/longitude in the other order"
                if other["outside_box"] else "no polygon carries a value of " + field)
        raise PrepRefused(said)

    # the grid: the clip's box from its north-west corner, or the polygons' extent snapped
    # to the cell size (the years of a stack given at one resolution line up)
    if box:
        x0, y0 = float(box[0]), float(box[3])
        width = max(1, round((box[2] - box[0]) / deg))
        height = max(1, round((box[3] - box[1]) / deg))
    else:
        b = np.asarray(bounds, dtype="float64")
        x0 = math.floor(b[:, 0].min() / deg) * deg
        y0 = math.ceil(b[:, 3].max() / deg) * deg
        width = max(1, math.ceil((b[:, 2].max() - x0) / deg - 1e-9))
        height = max(1, math.ceil((y0 - b[:, 1].min()) / deg - 1e-9))
    ctx.check_cells(width * height, "the raster to rasterise")
    dest = ctx.tmp(".tif", "rasterised")
    raster_cells, raster_km2 = _burn(ctx, spool, offsets, lengths, codes, bounds, dest, x0, y0,
                                     deg, width, height, fill, tagged, dtype)
    if box:
        ctx.clipped = True                       # the grid IS the clip: nothing left to cut
        ctx.record.update({"clip": [round(float(v), 6) for v in box],
                           **({"clip_place": m["clip"]} if isinstance(m["clip"], str) else {})})
    ctx.record["read"] = reader.read

    # what landed, in words and in the record
    lat = y0 - height * deg / 2
    ew = deg * M_PER_DEG * math.cos(math.radians(lat))
    names = {c: legend.get(c, str(c)) for c in set(codes)}
    kept = len(codes)
    given = "" if sp["given"] == f"{deg:g}°" else f"given as {sp['given']}; "
    note = (f"rasterised at ingest from {kept:,} polygon(s)' {field} onto an EPSG:4326 grid of "
            f"{deg:g}° cells ({given}{deg * M_PER_DEG:,.1f} m north-south, "
            f"{ew:,.1f} m east-west at {abs(lat):.1f}°{'N' if lat >= 0 else 'S'}), {width:,} x "
            f"{height:,} cells over [{x0:g}, {y0 - height * deg:g}, {x0 + width * deg:g}, {y0:g}]"
            f"{where or ''}: each cell takes the code of the polygon holding its centre "
            f"(all-touched off), a cell no polygon covers is {fill}"
            + (" (nodata)" if tagged else "") + " — so a class's area in the raster differs "
            "slightly from its polygons' (a cell's edge along each boundary)")
    pairs = "; ".join(f"{names[c]} {poly_km2.get(c, 0):,.2f} / {raster_km2.get(c, 0):,.2f}"
                      for c in sorted(names))
    notes = [note, f"area per class, polygons / raster (km², ellipsoid): {pairs}"]
    if sp["classes"] is not None:
        groups: dict = {}
        for k, c in sp["classes"].items():
            if k in seen.n:
                groups.setdefault(c, []).append(str(sp["named"][k]))
        notes.append(f"{len(seen.n)} value(s) of {field} mapped onto {len(set(codes))} code(s) "
                     "(every pair on the record): " + "; ".join(
                         f"{', '.join(v[:5])}{f' +{len(v) - 5} more' if len(v) > 5 else ''} -> "
                         f"{names.get(c, c)}" for c, v in sorted(groups.items())))
        absent = [str(sp["named"][k]) for k in sp["classes"] if k not in seen.n]
        if absent:
            notes.append(f"rasterise.classes lists {', '.join(absent[:20])}"
                         + (f" and {len(absent) - 20} more" if len(absent) > 20 else "")
                         + f", which no polygon of the layer carries")
    if other["no_value"]:
        notes.append(f"{other['no_value']:,} polygon(s) with no {field} were left out (their "
                     f"cells are {fill})")
    if other["not_polygons"]:
        notes.append(f"{other['not_polygons']:,} point or line feature(s) were left out — "
                     "rasterise burns polygons")
    if other["outside_box"]:
        notes.append(f"{other['outside_box']:,} polygon(s) outside the clip's box were left out")
    notes += reader.notes
    mapping = ({str(sp["named"][k]): c for k, c in sp["classes"].items()} if sp["classes"] is not None
               else {str(seen.shown[k]): lookup(k) for k in seen.order(seen.n)})
    return PreparedFile(path=dest, notes=notes, kind_hint="raster", provenance={"inputs": {
        "field": field, "resolution": {"given": sp["given"], "degrees": deg},
        "classes": mapping, "all_touched": False, "fill": fill,
        **({"layer": sp["layer"] or frag} if (sp["layer"] or frag) else {}),
        "grid": {"crs": "EPSG:4326", "transform": [deg, 0.0, x0, 0.0, -deg, y0],
                 "width": width, "height": height},
        "features": {"read": total, "rasterised": kept,
                     **{k: v for k, v in other.items() if v and k != "unmapped"}},
        "km2": {"polygons": {str(c): round(poly_km2.get(c, 0.0), 4) for c in sorted(names)},
                "raster": {str(c): round(raster_km2.get(c, 0.0), 4) for c in sorted(names)}},
        "cells": {str(c): int(raster_cells.get(c, 0)) for c in sorted(names)}}})


def _burn(ctx, spool, offsets, lengths, codes, bounds, dest, x0, y0, deg, width, height,
          fill, tagged, dtype):
    """Burn the spooled polygons a band of rows at a time into `dest`; ({code: cells},
    {code: km²}) of what was written. Bands are whole tiles high, so each tile is written
    once and compressed once."""
    import warnings
    import numpy as np
    import rasterio
    import shapely
    from rasterio import features
    from rasterio.transform import Affine
    from rasterio.windows import Window
    from .. import rasters
    from ...graph.geo import stacks
    burn_dtype = dtype if dtype in ("uint8", "uint16", "int16", "uint32", "int32", "float32",
                                    "float64") else ("float64" if np.dtype(dtype).kind == "f"
                                                     else "int32")
    band = max(256, (BAND_CELLS // max(1, width)) // 256 * 256)
    b = np.asarray(bounds, dtype="float64")
    west, east = x0 - deg, x0 + (width + 1) * deg
    cells: dict = {}
    km2: dict = {}
    profile = {"driver": "GTiff", "width": width, "height": height, "count": 1, "dtype": dtype,
               "crs": "EPSG:4326", "transform": Affine(deg, 0, x0, 0, -deg, y0),
               "nodata": fill if tagged else None, "BIGTIFF": "IF_SAFER",
               **rasters.packing(dtype, width, height)}
    with open(spool, "rb") as src, rasterio.Env(**rasters.gdal_env()), \
            rasterio.open(dest, "w", **profile) as dst:
        for r0 in range(0, height, band):
            ctx.check_time()
            h = min(band, height - r0)
            top, bottom = y0 - r0 * deg, y0 - (r0 + h) * deg
            tr = Affine(deg, 0, x0, 0, -deg, top)
            sel = np.nonzero((b[:, 1] < top) & (b[:, 3] > bottom))[0]
            if len(sel):
                # a polygon reaching beyond the band is cut to it (a cell's margin kept), so
                # a huge one is not handed whole to every band; cell centres are unchanged
                lo, hi = bottom - deg, top + deg
                shapes = []
                for i in sel:
                    src.seek(offsets[i])
                    g = shapely.from_wkb(src.read(lengths[i]))
                    if b[i, 1] < lo or b[i, 3] > hi or b[i, 0] < west or b[i, 2] > east:
                        g = shapely.clip_by_rect(g, west, lo, east, hi)
                        if g.is_empty:
                            continue
                    shapes.append((g, codes[i]))
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    arr = (features.rasterize(shapes, out_shape=(h, width), transform=tr,
                                              fill=fill, all_touched=False, dtype=burn_dtype)
                           if shapes else np.full((h, width), fill, dtype=burn_dtype))
                del shapes
            else:
                arr = np.full((h, width), fill, dtype=burn_dtype)
            rows = stacks.row_pixel_areas_km2(tr, h)
            for s0 in range(0, h, 256):              # counted a slab at a time: little memory
                a = arr[s0:s0 + 256]
                vals, inv = np.unique(a, return_inverse=True)
                inv = inv.ravel()
                n = np.bincount(inv, minlength=len(vals))
                area = np.bincount(inv, weights=np.repeat(rows[s0:s0 + 256], width),
                                   minlength=len(vals))
                for v, c, k in zip(vals.tolist(), n.tolist(), area.tolist()):
                    v = int(v) if float(v).is_integer() else v
                    if v != fill:
                        cells[v] = cells.get(v, 0) + int(c)
                        km2[v] = km2.get(v, 0.0) + float(k)
            dst.write(arr.astype(dtype, copy=False), 1, window=Window(0, r0, width, h))
    return cells, km2


# --------------------------------------------------------------------------- the declines

def _no_field(field: str, attributes: set, url: str) -> str:
    have = sorted(attributes)
    return (f"no feature of the layer at {url} has an attribute {field!r} — its attributes are "
            f"{', '.join(have[:40]) or 'none'}; set rasterise.field to the one naming each "
            "polygon's class (ASK the contributor which it is)")


def _unmapped(sp: dict, seen: _Values, unmapped: set, legend: dict, fill, m: dict) -> str:
    """Every value the classes do not map, with what each holds and what it reads as, and
    the classes to fill in — the codes are the contributor's to choose."""
    from .. import rasters
    field = sp["field"]
    labels = seen.label_of()
    keys = seen.order(unmapped)

    def shown(k):
        v = seen.shown[k]
        return repr(v) if isinstance(v, str) and not re.fullmatch(r"[+-]?\d+", v) else str(v)

    rows = [f"{shown(k)}" + (f" {labels[k]!r}" if k in labels else "")
            + f" ({seen.n[k]:,} polygon(s), {seen.km2[k]:,.2f} km²)" for k in keys[:_SHOWN]]
    more = f" and {len(keys) - _SHOWN} more" if len(keys) > _SHOWN else ""
    skeleton = ", ".join(f"{shown(k)}: <code>" for k in keys[:_SHOWN])
    head = (f"rasterise needs a code for every value of {field}; "
            f"{len(keys)} of the {len(seen.n)} value(s) the polygons hold "
            f"{'have none' if sp['classes'] is not None else 'are no label or code of the legend'}"
            f": {'; '.join(rows)}{more}")
    ident = (len(seen.n) > 50 and len(seen.n) >= 0.9 * sum(seen.n.values()))
    if ident:
        return (f"{head}. {field} looks like an identifier ({len(seen.n):,} values over "
                f"{sum(seen.n.values()):,} polygons), not a class — set rasterise.field to the "
                "attribute naming each polygon's class (ASK the contributor which it is)")
    leg = rasters._legend_text(legend) if legend else "{}"
    said = (f". {'Add' if sp['classes'] is not None else 'Give'} each the code it lands as — "
            f"rasterise.classes: {{{skeleton}}} — ASK the contributor which class of the legend "
            f"{leg} each is (never guess); several values may share a code, and a value mapped "
            f"to {fill} is left outside the map")
    if sp["classes"] is None:
        one = {k: i + 1 for i, k in enumerate(seen.order(seen.n))}
        said += ("; or, for a new map (a layer of its own, or the first year of a new stack), "
                 "one code per value: classes {"
                 + ", ".join(f"{shown(k)}: {c}" for k, c in list(one.items())[:_SHOWN])
                 + "} with legend {" + ", ".join(f"{c}: {labels.get(k) or seen.shown[k]}"
                                                for k, c in list(one.items())[:_SHOWN]) + "}")
    return head + said


# --------------------------------------------------------------------------- suggestions

_NOT_A_RASTER = ("not in a raster format", "holds vector data", "is an XML/text file")
_VECTOR_EXT = (".geojson", ".json", ".shp", ".kml", ".kmz", ".gml", ".fgb")


def _features_link(url: str) -> bool:
    """A link that, by its own form, serves features: a WFS GetFeature, an ArcGIS layer
    query, an OGC API Features items list, or a vector file's name."""
    from urllib.parse import urlsplit
    from ._features import wfs_query
    if wfs_query(url) is not None:
        return True
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    path, query = parts.path.lower(), parts.query.lower()
    return (path.endswith(_VECTOR_EXT) or path.endswith("/items")
            or (path.endswith("/query") and "f=geojson" in query))


def suggest(situation: dict) -> str | None:
    """On a raster declined for holding features rather than cells: the rasterise that
    makes the class raster on the server — every choice in it the contributor's."""
    m = situation.get("manifest") or {}
    if (m.get("rasterise") is not None or situation.get("event") != "declined"
            or situation.get("kind") != "raster"):
        return None
    problem = str(situation.get("problem") or "")
    if not (_features_link(str(m.get("url") or ""))
            or any(p in problem for p in _NOT_A_RASTER)):
        return None
    clip = "" if m.get("clip") is not None else ", clip: <the area>"
    return ("or, if it is a POLYGON map of classes (land cover or land use, zoning, a hazard "
            "map drawn as polygons), the platform makes the class raster itself: resubmit the "
            "same link with rasterise: {field: '<the attribute naming each polygon's class>', "
            "resolution: '<the cell size, e.g. 30 m>', classes: {<value>: <legend code>, ...}}"
            f"{clip} — ASK the contributor which attribute holds the class, which code each of "
            "its values lands as, the cell size and the area (never guess); preview: true shows "
            "the classes and km² before anything lands")
