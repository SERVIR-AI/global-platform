"""Contributed methods, run for every pack at the assemble seam (mcp/assemble.py).

Each approved method bound to the pack, and the caller's own staged ones, reads the rows
of its inputs that are about the pack's area, is computed by the platform
(contrib/methods.py) and is cited with its steps, its source and its contributor. So a
figure an answer turns on is the platform's arithmetic, checkable by the gate, never the
LLM's. The step never asks which pack it is in: a pack supplies its area (`_area` in its
stats, or the caller's geometry) and, through its target, its country.
"""

from __future__ import annotations

import re

MAX_METHODS = 20          # methods computed per answer; the rest are named in a gap

_LAT_KEYS = ("lat", "latitude", "decimallatitude")
_LON_KEYS = ("lon", "lng", "long", "longitude", "decimallongitude")
_COUNTRY_KEYS = ("country", "country_name", "countryname", "pais", "país", "nation")


def _key_of(recs, names):
    keys = {str(k).lower(): k for r in recs[:50] for k in r}
    return next((keys[n] for n in names if n in keys), None)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _true_count(res) -> int:
    m = re.match(r"\s*([\d,]+)\s+(?:rows|records)\b", str(res.get("summary") or ""))
    read = len(res.get("records") or [])
    return max(read, int(m.group(1).replace(",", ""))) if m else read


def place(recs: list, spec: dict, boundary, countries: list) -> tuple:
    """(rows about the area, how they were placed, placeable) — the rule every pack's
    bound tables follow: by coordinates inside the boundary, else by a country column,
    else every row when the source declares the area's country. (None, why, placeable)
    when no rule applies."""
    from ..rag import filters
    lat_k, lon_k = _key_of(recs, _LAT_KEYS), _key_of(recs, _LON_KEYS)
    cty_k = _key_of(recs, _COUNTRY_KEYS)
    declared = [str(c) for c in (spec.get("countries") or []) if str(c).strip()]
    if lat_k and lon_k and boundary is not None:
        import numpy as np
        import shapely
        xs = np.array([_num(r.get(lon_k)) if _num(r.get(lon_k)) is not None else np.nan
                       for r in recs], dtype=float)
        ys = np.array([_num(r.get(lat_k)) if _num(r.get(lat_k)) is not None else np.nan
                       for r in recs], dtype=float)
        ok = np.isfinite(xs) & np.isfinite(ys)
        hit = np.zeros(len(recs), dtype=bool)
        if ok.any():
            hit[ok] = shapely.intersects_xy(boundary, xs[ok], ys[ok])
        return [r for r, h in zip(recs, hit) if h], f"their {lat_k}/{lon_k} fall inside the boundary", True
    if cty_k and countries:
        rows = [r for r in recs if str(r.get(cty_k) or "").strip()
                and filters.speaks_to([str(r[cty_k])], countries)]
        return rows, f"their {cty_k} is {', '.join(countries)}", True
    if declared and countries and filters.speaks_to(declared, countries):
        return list(recs), f"the source declares {', '.join(declared)}, where this area is", True
    if lat_k and lon_k:
        return None, ("its rows are placed by coordinates and this pack resolved no boundary "
                      "(pass `geometry`)"), False
    return None, "it has no coordinate or country column and declares no country", False


def attach(pack: str, target: dict, stats: dict, citations: list, gaps: list,
           trace: list, extras: dict) -> dict | None:
    """Compute the pack's contributed methods over its area; append a citation per result
    (numbered after the last) and a gap per failure. Returns the `methods` block — the
    results by method — or None when the pack has none that apply."""
    from ..contrib import methods
    from . import feeds
    try:
        rows = methods.for_pack(pack)
    except Exception as exc:                       # the pack never fails on its methods
        gaps.append(f"contributed methods could not be read for this pack: "
                    f"{type(exc).__name__}: {exc}")
        return None
    if not rows:
        return None
    area = stats.get("_area") if isinstance(stats, dict) else None
    if not (isinstance(area, dict) and area.get("geometry")):
        from . import layers as _layers
        area = _layers._caller_area(target, extras)
    boundary = None
    if area and area.get("geometry"):
        from shapely.geometry import shape
        try:
            boundary = shape(area["geometry"])
        except Exception:
            boundary = None
    countries = [str(c) for c in ((area or {}).get("countries") or []) if str(c).strip()]
    if not countries and str(target.get("country") or "").strip():
        countries = [str(target["country"]).strip()]
    name = (area or {}).get("name") or target.get("place") or target.get("country") or "this area"
    n = max((int(c.get("n") or 0) for c in citations), default=0)
    names = sorted(rows)
    skipped, names = names[MAX_METHODS:], names[:MAX_METHODS]
    block, unplaced = {}, []
    for mname in names:
        row = rows[mname]
        title = row.get("title") or mname
        holds = [str(c) for c in (row.get("countries") or []) if str(c).strip()]
        if holds:
            from ..rag import filters
            if not countries:
                gaps.append(f"{title} ({mname}), a contributed method that holds for "
                            f"{', '.join(holds)}, was not run: the platform could not tell which "
                            f"country {name} is in")
                continue
            if not filters.speaks_to(holds, countries):
                trace.append(f"method[{mname}] not run: it holds for {', '.join(holds)}, "
                             f"not {', '.join(countries)}")
                continue
        used, problem, quiet = {}, None, False
        for alias, ref in (row.get("inputs") or {}).items():
            ds = str(next(iter(ref.values())))
            spec = methods.input_spec(ds)
            if spec is None:
                problem = f"its input {ds!r} is no longer served by the platform"
                break
            miss = feeds.scope_miss(spec, countries, boundary.bounds if boundary is not None else None)
            if miss:
                trace.append(f"method[{mname}] not run: its input {ds} is scoped to {miss[0]} "
                             f"({miss[1]})")
                quiet = True
                break
            res = feeds.query(ds, {"limit": methods.MAX_ROWS})
            if res.get("status") != "ok":
                problem = f"its input {ds} did not answer: {res.get('note') or res.get('status')}"
                break
            recs = res.get("records") or []
            total = _true_count(res)
            placed, how, placeable = place(recs, spec, boundary, countries)
            if placed is None:
                if not placeable:
                    unplaced.append(f"{title} ({mname}): {how}")
                quiet = True
                break
            if not placed and not (spec.get("countries") or spec.get("bbox")):
                # a source that declares no coverage says nothing by having no row here
                trace.append(f"method[{mname}] not run: none of the {total:,} rows of {ds} is "
                             f"about {name} ({how.replace('their', 'placed by their')})")
                quiet = True
                break
            used[alias] = {"dataset": ds, "title": spec.get("title") or ds, "rows": placed,
                           "read": len(recs), "total": total, "how": how}
        if problem:
            gaps.append(f"{title} ({mname}), a contributed method, was not computed for {name}: "
                        f"{problem}. Not a zero: nothing was computed.")
            continue
        if quiet:
            continue
        try:
            out = methods.evaluate(row, {a: v["rows"] for a, v in used.items()})
        except methods.MethodError as exc:
            gaps.append(f"{title} ({mname}), a contributed method, could not be computed for "
                        f"{name}: {exc}. Not a zero: nothing was computed.")
            continue
        n += 1
        citations.append(_citation(n, mname, row, used, out, name, holds))
        block[mname] = {"n": n, "results": {o: {"value": v, "unit": ((row.get("outputs") or {}).get(o) or {}).get("unit")}
                                            for o, v in out.items()},
                        "inputs": {a: {"dataset": v["dataset"], "rows_about_area": len(v["rows"]),
                                       "rows_total": v["total"]} for a, v in used.items()}}
        trace.append(f"method[{mname}] computed over "
                     + ", ".join(f"{len(v['rows']):,} rows of {v['dataset']}" for v in used.values()))
    if unplaced:
        gaps.append(f"{len(unplaced)} contributed method(s) of this pack were not computed for "
                    f"{name} because their inputs' rows cannot be placed here: " + "; ".join(unplaced))
    if skipped:
        gaps.append(f"{len(skipped)} more contributed method(s) are bound to this pack but were not "
                    f"run (at most {MAX_METHODS} per answer): {', '.join(skipped)}")
    return block or None


def _citation(n: int, mname: str, row: dict, used: dict, out: dict, name: str, holds) -> dict:
    from ..contrib import methods
    outs = row.get("outputs") or {}
    results = "; ".join(f"{o} {methods.fmt(v)} {(outs.get(o) or {}).get('unit') or ''}".strip()
                        for o, v in out.items())
    inputs = "; ".join(
        f"{v['title']} ({v['dataset']}), {len(v['rows']):,} of its {v['total']:,} row(s) about "
        f"{name}, as {v['how']}"
        + (f"; only the last {v['read']:,} rows were read" if v["read"] < v["total"] else "")
        for v in used.values())
    params = methods.render_params(row)
    review = row.get("review")
    text = (f"{row.get('title') or mname}: a contributed method ({mname}, by "
            f"{row.get('contributor_label') or 'a contributor'}; source: "
            f"{row.get('source') or 'unstated'}; licence {row.get('license') or 'unstated'}), "
            f"computed by the platform for {name}. Inputs: {inputs}."
            + (f" Parameters: {params}." if params else "")
            + f" Steps: {methods.render_steps(row)}. Results: {results}."
            + (f" Holds for: {', '.join(holds)}." if holds else "")
            + (f" Contributor's note: {row['usage_notes']}" if row.get("usage_notes") else "")
            + (f" Review: {review}." if review else ""))
    cit = {"n": n, "kind": "method_result", "retrieval": "computed-at-pack-time",
           "source": f"contributed method {mname} ({row.get('source') or 'source unstated'})",
           "title": f"{row.get('title') or mname}, {name}",
           "pub_date": row.get("vintage"),
           "validation": ("contributed method; its worked examples were re-run by the gate"
                          + (f"; {review}" if review else "")),
           "method": mname, "contribution_id": row.get("contribution_id"),
           "results": {o: {"value": v, "unit": (outs.get(o) or {}).get("unit")} for o, v in out.items()},
           "text": text}
    if row.get("license"):
        cit["license"] = row["license"]
    if row.get("staged_by"):
        cit["staged_by"] = row["staged_by"]
    return cit
