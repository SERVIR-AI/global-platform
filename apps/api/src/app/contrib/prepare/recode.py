"""recode: a class map's codes rewritten at ingest, cell for cell.

The case: a land-cover series delivered with other codes each year (1956: 1/2, 1985:
10/20, 2014: 100/200) — one stack has one legend, so the later years were recoded by hand
before they could join. Now the year goes in from its own link with recode: {10: 1, 20:
2}; the server writes each listed code as its new one, the file's nodata as the declared
nodata, every other value unchanged, in declared.dtype — the legend and the contract
describe the file AS IT LANDS. Applies to every namespace whose values are classes
(rasters.namespace(...)["values"]), read from the schema, never a list here.
"""

from __future__ import annotations

NAME = "recode"
STEP = "recode"
KINDS = ("raster",)
CELLWISE = True
FIELDS = {
    "recode": "for a CLASS map whose codes are not the ones it must carry (a year joining a "
              "stack whose legend uses other codes, a hazard map coded 10-50 instead of 1-5): "
              "{from_code: to_code, ...}, e.g. {10: 1, 20: 2}. The platform rewrites the cells "
              "at ingest — nothing to do by hand: each listed code becomes its new one, the "
              "file's nodata becomes declared.nodata, every other value stays as it is, and "
              "the file is written as declared.dtype. legend and declared describe the file "
              "AS IT LANDS, after the recode (legend {1: Mangrove, 2: Other}, valid_min 1, "
              "valid_max 2). Two codes may become one class; never guess what a code means — "
              "ask the contributor. The record keeps the mapping and every file's sha256",
}

_EG = "e.g. {10: 1, 20: 2}"
_SHOWN = 12


def _int(v):
    """v as an int when it is a whole number (10, '10', 10.0), else None."""
    if isinstance(v, bool):
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else None


def mapping(manifest) -> dict:
    """{from: to} as ints; raises ValueError naming what is not a whole number."""
    rc = manifest.get("recode")
    if not isinstance(rc, dict) or not rc:
        raise ValueError(f"recode must map each code in the file to the code it lands as, {_EG} "
                         f"— got {rc!r}")
    out, bad = {}, []
    for k, v in rc.items():
        a, b = _int(k), _int(v)
        if a is None or b is None:
            bad.append(f"{k!r}: {v!r}")
        else:
            out[a] = b
    if bad:
        raise ValueError(f"recode codes must be whole numbers ({_EG}) — not {', '.join(bad)}")
    return out


def _text(m: dict) -> str:
    return "{" + ", ".join(f"{a}: {b}" for a, b in sorted(m.items())) + "}"


def _fits(dtype: str, value) -> bool:
    import numpy as np
    try:
        dt = np.dtype(dtype)
    except TypeError:
        return True                          # the contract check names a bad dtype
    if dt.kind in "iu":
        info = np.iinfo(dt)
        return info.min <= value <= info.max
    return True


def validate(manifest) -> list[str]:
    from .. import rasters
    m = manifest
    layer = str(m.get("layer") or "")
    rule = rasters.namespace(layer)
    if rule and rule.get("values") != "classes":
        return [f"recode rewrites class codes; a {rule['prefix']}* layer holds "
                f"{rule.get('values') or 'values'}, not classes — drop recode"]
    try:
        rc = mapping(m)
    except ValueError as exc:
        return [str(exc)]
    problems = []
    decl = m.get("declared") if isinstance(m.get("declared"), dict) else {}
    dtype = str(decl.get("dtype") or "")
    off = sorted({b for b in rc.values() if dtype and not _fits(dtype, b)})
    if off:
        problems.append(f"recode writes {', '.join(map(str, off))}, which declared.dtype "
                        f"{dtype} cannot hold — declare the type the file lands as (e.g. uint8 "
                        "for codes 0-255)")
    try:
        legend = rasters._norm_legend(m.get("legend")) if isinstance(m.get("legend"), dict) else {}
    except (TypeError, ValueError):
        legend = {}
    nod = _int(decl.get("nodata"))
    outside = {_int(v) for v in (m.get("outside_values") or []) if _int(v) is not None}
    landing = {b for b in rc.values()}
    if legend:
        stray = sorted(b for b in landing if b not in legend and b != nod and b not in outside)
        if stray:
            as_lands = {rc.get(k, k): v for k, v in legend.items()}
            said_as = (f" — the legend names the codes as delivered; as it lands it is "
                       f"{rasters._legend_text(as_lands)}"
                       if set(legend) & set(rc) and len(as_lands) == len(legend) else "")
            problems.append(f"recode writes {', '.join(map(str, stray))}, which is no class of "
                            f"the legend {rasters._legend_text(legend)} (nor declared.nodata or "
                            f"an outside value): legend and declared describe the file AS IT "
                            f"LANDS, after the recode{said_as}")
    lo, hi = decl.get("valid_min"), decl.get("valid_max")
    if rasters._isnum(lo) and rasters._isnum(hi):
        classes = sorted(b for b in landing if b != nod and b not in outside)
        out = [b for b in classes if not float(lo) <= b <= float(hi)]
        if out:
            problems.append(f"declared valid_min/valid_max ({rasters._num(lo)}..{rasters._num(hi)}) "
                            f"are of the file as it lands: after the recode its classes are "
                            f"{', '.join(map(str, classes))} — declare valid_min {classes[0]}, "
                            f"valid_max {classes[-1]}")
    return problems


def run(ctx):
    """Rewrite ctx.path's cells, block by block (never the whole grid in memory), into a
    GeoTIFF on the same grid — the gate then converts, reprojects and verifies it."""
    import numpy as np
    import rasterio
    from rasterio.windows import Window
    from .. import rasters
    from . import PrepRefused, PreparedFile
    m = ctx.manifest
    rc = mapping(m)
    decl = m.get("declared") or {}
    out_dtype = np.dtype(str(decl["dtype"]))
    floats = out_dtype.kind == "f"
    dest = ctx.tmp(".tif", "recoded")
    counts = {a: 0 for a in rc}
    kept: dict = {}
    nodata_cells = 0
    with rasterio.Env(**rasters.gdal_env()):        # one driver, nothing over the network
        try:
            driver = rasters.driver_for(ctx.path)
            target, picked, driver = rasters._open_target(ctx.path, m.get("layer_name"), driver)
        except ValueError as exc:
            raise PrepRefused(str(exc)) from None
        with rasterio.open(target, driver=driver) as src:
            refusal, band_note = rasters._bands(src)
            if refusal:
                raise PrepRefused(refusal)
            try:
                crs, crs_note = rasters.crs_of(src)       # a CF lat/lon grid is read as WGS 84
            except ValueError as exc:
                raise PrepRefused(str(exc)) from None
            ctx.check_cells(src.width * src.height, "the raster to recode")
            src_dtype, src_nodata = src.dtypes[0], src.nodata
            nan_src = src_nodata is not None and np.isnan(src_nodata)
            declared_nodata = decl.get("nodata")
            if rasters._isnum(declared_nodata) or rasters._isnan(declared_nodata):
                out_nodata = float("nan") if rasters._isnan(declared_nodata) else float(declared_nodata)
                if not floats and (np.isnan(out_nodata) or not _fits(out_dtype.name, out_nodata)):
                    raise PrepRefused(f"declared.nodata {declared_nodata} cannot be stored as "
                                      f"{out_dtype.name}")
            elif src_nodata is not None and (floats or (not nan_src and _fits(out_dtype.name, src_nodata))):
                out_nodata = src_nodata
            else:
                out_nodata = None
            # the file's own nodata, unless the recode names it: a listed code is recoded
            nd_value = None if (src_nodata is None or (not nan_src and _int(src_nodata) in rc)) \
                else src_nodata
            profile = {"driver": "GTiff", "width": src.width, "height": src.height, "count": 1,
                       "dtype": out_dtype.name, "crs": crs, "transform": src.transform,
                       "nodata": out_nodata, "BIGTIFF": "IF_SAFER",
                       **rasters.packing(out_dtype.name, src.width, src.height)}
            info = np.iinfo(out_dtype) if out_dtype.kind in "iu" else None
            step = 1024
            with rasterio.open(dest, "w", **profile) as dst:
                for r in range(0, src.height, step):
                    ctx.check_time()
                    win = Window(0, r, src.width, min(step, src.height - r))
                    a = src.read(1, window=win)
                    out = np.empty(a.shape, dtype=out_dtype)
                    if nd_value is None:
                        nd = np.zeros(a.shape, dtype=bool)
                    elif nan_src:
                        nd = np.isnan(a)
                    else:
                        nd = a == nd_value
                    done = nd.copy()
                    for frm, to in rc.items():
                        sel = (a == frm) & ~nd
                        n = int(sel.sum())
                        if n:
                            out[sel] = to
                            counts[frm] += n
                            done |= sel
                    rest = ~done
                    if rest.any():
                        vals = a[rest]
                        if info is not None:          # an integer file: every kept value must fit
                            bad = (vals < info.min) | (vals > info.max)
                            if vals.dtype.kind == "f":
                                bad |= ~np.isfinite(vals) | (vals != np.round(vals))
                            if bad.any():
                                shown = sorted({str(_plain(v)) for v in np.unique(vals[bad])[:_SHOWN]})
                                raise PrepRefused(
                                    f"the file holds {', '.join(shown)}, which recode does not "
                                    f"list and {out_dtype.name} cannot hold — add each to recode "
                                    "(the code it lands as), or declare a dtype that holds it")
                        u, c = np.unique(vals, return_counts=True)
                        for v, n in zip(u.tolist(), c.tolist()):
                            if len(kept) < 256 or v in kept:
                                kept[v] = kept.get(v, 0) + int(n)
                        out[rest] = vals.astype(out_dtype)
                    k = int(nd.sum())
                    if k:
                        if out_nodata is None:
                            raise PrepRefused(
                                f"the file marks no data with {_plain(src_nodata)}, which "
                                f"{out_dtype.name} cannot hold — declare declared.nodata: the "
                                "value the recoded file marks no data with (one that is no class)")
                        nodata_cells += k
                        out[nd] = out_nodata
                    dst.write(out, 1, window=win)
    if not any(counts.values()):
        held = sorted(kept)[:_SHOWN]
        raise PrepRefused(f"recode changed no cell: the file holds none of the codes "
                          f"{', '.join(map(str, sorted(rc)))} — its values are "
                          f"{', '.join(str(_plain(v)) for v in held) or 'no data only'}; give "
                          "the codes as they are in the file")
    pairs = ", ".join(f"{a} -> {b} ({counts[a]:,} cells)" for a, b in sorted(rc.items()))
    note = f"recoded at ingest {pairs}"
    if out_dtype.name != src_dtype or _plain(out_nodata) != _plain(src_nodata):
        note += (f"; written as {out_dtype.name}, nodata {_plain(out_nodata)} (the file was "
                 f"{src_dtype}, nodata {_plain(src_nodata)}"
                 + (f", {nodata_cells:,} cells" if nodata_cells else "") + ")")
    notes = [note, *(n for n in (picked, crs_note) if n)]
    if band_note:
        notes.append(band_note)
    absent = [a for a in sorted(rc) if not counts[a]]
    if absent:
        notes.append(f"recode lists {', '.join(map(str, absent))}, which the file does not hold")
    if kept:
        shown = sorted(kept)[:_SHOWN]
        notes.append("values recode does not list, kept as they are: "
                     + ", ".join(f"{_plain(v)} ({kept[v]:,} cells)" for v in shown)
                     + (f" and {len(kept) - len(shown)} more" if len(kept) > len(shown) else ""))
    return PreparedFile(path=dest, notes=notes, provenance={"inputs": {
        "recode": {str(a): b for a, b in sorted(rc.items())},
        "from": {"dtype": src_dtype, "nodata": _plain(src_nodata)},
        "to": {"dtype": out_dtype.name, "nodata": _plain(out_nodata)},
        "cells": {str(a): counts[a] for a in sorted(rc)}}})


def _plain(v):
    """A JSON- and YAML-safe number: an int when whole, 'nan' for NaN, None for None."""
    import math
    if v is None:
        return None
    f = float(v)
    if math.isnan(f):
        return "nan"
    return int(f) if f.is_integer() else f


# --------------------------------------------------------------------------- suggestions

def _mangled(label) -> str:
    return " ".join(str(label).lower().split())


def _same(label) -> str:
    """A label as two legends are compared: case, accents, whitespace and punctuation
    aside ('Non-mangrove' is 'non mangrove', 'Bosque Húmedo' is 'bosque humedo')."""
    import re
    import unicodedata
    s = "".join(c for c in unicodedata.normalize("NFKD", str(label))
                if not unicodedata.combining(c))
    return " ".join(re.sub(r"[\W_]+", " ", s.casefold()).split())


def _by_label(have: dict, want: dict) -> dict | None:
    """{file code: stack code} when every label of the file's legend is one of the stack's
    (as _same compares them) and the two name the same classes; else None."""
    code_of = {_same(v): k for k, v in have.items()}
    rc = {k: code_of.get(_same(v)) for k, v in want.items()}
    if all(c is not None for c in rc.values()) and set(rc.values()) == set(have):
        return rc
    return None


def _landing(have: dict, want: dict, m: dict) -> dict:
    """The fields that land a year coded `want` as its stack's `have`: the recode, the
    stack's legend as the stack writes it, the declared range of the file as it lands."""
    rc = _by_label(have, want)
    moves = {a: b for a, b in rc.items() if a != b}
    out = {}
    if moves:
        out["recode"] = moves
    if m.get("legend") != have:
        out["legend"] = dict(sorted(have.items()))
    decl = m.get("declared") if isinstance(m.get("declared"), dict) else None
    if moves and decl is not None:
        landed = {**decl, "valid_min": min(have), "valid_max": max(have)}
        if landed != decl:
            out["declared"] = landed
    return out


def auto(situation: dict) -> dict | None:
    """A year whose legend names its stack's classes under other codes (the labels the
    same, case, accents, whitespace and punctuation aside): the recode, the stack's legend
    and the declared range as it lands — what each code means is the labels', no person's
    choice. Labels that differ stay a question (suggest)."""
    m = situation.get("manifest") or {}
    if (situation.get("kind") not in KINDS or situation.get("event") != "stack_legend_mismatch"
            or m.get("recode") is not None):
        return None
    have, want = situation["have"], situation["want"]
    rc = _by_label(have, want)
    if rc is None or len(rc) != len(have) or len({_same(v) for v in have.values()}) != len(have):
        return None                       # two codes for one class, here or in the stack: theirs
    fields = _landing(have, want, m)
    if not fields:
        return None
    return {"fields": fields, "restates": [f for f in ("legend", "declared") if f in fields],
            "why": f"the year's legend {_legend_json(want)} names the classes of the stack "
                   f"{situation.get('stack')!r}, {_legend_json(have)}, under other codes or "
                   "spellings, so the platform rewrites the cells to the stack's codes at "
                   "ingest; legend and declared describe the file as it lands"}


def _proposed_by_order(have: dict, want: dict) -> str | None:
    """When the labels differ but the classes are as many: the file's codes paired with
    the stack's — by label where one matches, else in code order — for the contributor to
    CONFIRM, never applied."""
    if len(want) != len(have):
        return None
    code_of = {_same(v): k for k, v in have.items()}
    pairs, used = {}, set()
    for k, v in sorted(want.items()):
        c = code_of.get(_same(v))
        if c is not None and c not in used:
            pairs[k] = c
            used.add(c)
    left = [c for c in sorted(have) if c not in used]
    for k in sorted(want):
        if k not in pairs:
            pairs[k] = left.pop(0)
    shown = ", ".join(f"{k} '{want[k]}' -> {c} '{have[c]}'" for k, c in sorted(pairs.items()))
    moves = {a: b for a, b in pairs.items() if a != b}
    return (f"the proposed mapping, to CONFIRM with the contributor before it is used: {shown} "
            f"— if they confirm it, resubmit the same link with recode: {_text(moves)}, legend: "
            f"{_legend_json(have)} and declared valid_min {min(have)}, valid_max {max(have)}"
            if moves else
            f"the proposed mapping, to CONFIRM with the contributor before it is used: {shown} "
            f"— if they confirm it, resubmit with the stack's legend {_legend_json(have)}")


def suggest(situation: dict) -> str | None:
    """The recode that resolves a decline, with the exact mapping — or, when what a code
    means is the contributor's to say, the question to ask them."""
    m = situation.get("manifest") or {}
    if situation.get("kind") not in KINDS or m.get("recode") is not None:
        return None                       # declared already: its own checks say what is wrong
    event = situation.get("event")
    if event == "stack_legend_mismatch":
        have, want = situation["have"], situation["want"]
        rc = _by_label(have, want)
        if rc is not None:
            moves = {a: b for a, b in rc.items() if a != b}
            lo, hi = min(have), max(have)
            return (f"resubmit the same link with recode: {_text(moves)}, legend: "
                    f"{_legend_json(have)} and declared valid_min {lo}, valid_max {hi} (the file "
                    "as it lands) — the platform rewrites the cells at ingest, nothing to do by "
                    "hand")
        said = (f"or resubmit the same link with recode: {{<code in the file>: <the stack's "
                f"code>, ...}} and the stack's legend {_legend_json(have)} — ASK the contributor "
                "which of the stack's classes each of the file's codes is (never guess); the "
                "platform rewrites the cells at ingest")
        order = _proposed_by_order(have, want)
        return f"{said}; {order}" if order else said
    if event == "duplicate_labels":
        legend = situation["legend"]
        first, rc = {}, {}
        for code in sorted(legend):
            key = _mangled(legend[code])
            if key in first:
                rc[code] = first[key]
            else:
                first[key] = code
        if not rc:
            return None
        kept = {c: legend[c] for c in sorted(legend) if c not in rc}
        return (f"or resubmit the same link with recode: {_text(rc)} and legend "
                f"{_legend_json(kept)} — the platform rewrites the cells at ingest, so one code "
                "means one class")
    if event == "class_range":
        lo, hi = situation["classes"]
        return (f"if the file holds a few class codes rather than a continuous value, resubmit "
                f"with recode: {{<code in the file>: <class {lo}-{hi}>, ...}} and declared "
                f"valid_min {lo}, valid_max {hi} — ASK the contributor which class each code is "
                "(never guess); the platform rewrites the cells at ingest")
    return None


def _legend_json(leg: dict) -> str:
    return "{" + ", ".join(f"{k}: {v}" for k, v in sorted(leg.items())) + "}"
