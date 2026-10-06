"""Fire detections for the land-cover pack: a derived, platform-owned point file.

The hub delivered 1.4 million MODIS active-fire detections as a shapefile. The
platform has no shapefile reader at request time and will not grow a second GDAL
for one layer, so the shapefile is read ONCE, with numpy alone, into a compact
`.npz` beside a passport that records what it came from (file hashes, counts, the
producer's own lineage note). Questions then cost milliseconds: a bounding-box
pre-filter and shapely's contains_xy inside the boundary.

    python -m app.land_cover.fire build            # every stack's registered point set
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import struct
import sys
import time
import zipfile

import numpy as np
from shapely import contains_xy

SATELLITES = {"Terra": 0, "Aqua": 1}


def _read_member(root, member):
    """Bytes of `member` under `root` — a directory, or a zip read in place."""
    if root.lower().endswith(".zip"):
        with zipfile.ZipFile(root) as z:
            return z.read(member)
    with open(os.path.join(root, member), "rb") as f:
        return f.read()


def read_dbf(raw):
    """A dBASE table as a dict of column -> bytes array (fixed width), plus counts."""
    n = struct.unpack("<I", raw[4:8])[0]
    hl, rl = struct.unpack("<HH", raw[8:12])
    fields = []
    for i in range(32, hl - 1, 32):
        d = raw[i:i + 32]
        if d[0] == 0x0D:
            break
        fields.append((d[:11].rstrip(b"\x00").decode("latin1"), chr(d[11]), d[16]))
    dtype = np.dtype([("_del", "S1")] + [(name, f"S{ln}") for name, _, ln in fields])
    if dtype.itemsize != rl:
        raise ValueError(f"dbf record length {rl} does not match its field widths {dtype.itemsize}")
    rec = np.frombuffer(raw, dtype=dtype, count=n, offset=hl)
    return {name: rec[name] for name, _, _ in fields}, n, fields


def read_shp_point_count(raw):
    """Record count of a point shapefile (type 1): every record is 28 bytes."""
    shape_type = struct.unpack("<i", raw[32:36])[0]
    if shape_type != 1:
        raise ValueError(f"shapefile type {shape_type} is not Point (1)")
    return (len(raw) - 100) // 28


def _num(col, kind=float):
    return np.char.strip(col.astype("U")).astype(kind)


def build(root, stem, out_npz, passport_path):
    """Convert <stem>.shp/.dbf under `root` into `out_npz` + a passport. Returns the passport."""
    t0 = time.time()
    dbf = _read_member(root, f"{stem}.dbf")
    shp = _read_member(root, f"{stem}.shp")
    cols, n, fields = read_dbf(dbf)
    n_shp = read_shp_point_count(shp)
    if n_shp != n:
        raise ValueError(f".shp holds {n_shp} points but .dbf {n} rows")
    lon = _num(cols["LONGITUDE"]); lat = _num(cols["LATITUDE"])
    date = _num(cols["ACQ_DATE"], np.int64)                       # YYYYMMDD
    y, m, d = date // 10000, (date // 100) % 100, date % 100
    days = ((y - 1970).astype("datetime64[Y]").astype("datetime64[M]")
            + (m - 1).astype("timedelta64[M]")).astype("datetime64[D]") \
        + (d - 1).astype("timedelta64[D]")
    version = cols["VERSION"].astype("U")
    sat = cols["SATELLITE"].astype("U")
    out = {
        "lon": lon, "lat": lat,
        "day": days.astype("datetime64[D]").astype("int32"),
        "year": _num(cols["ACQ_YEAR"], np.int16),
        "lc": _num(cols["LC_CODE"], np.int8),
        "confidence": _num(cols["CONFIDENCE"], np.int16),
        "frp": _num(cols["FRP"], np.float32),
        "night": np.char.strip(cols["DAYNIGHT"].astype("U")) == "N",
        "nrt": np.char.find(version, "NRT") >= 0,
        "sat": np.array([SATELLITES.get(s.strip(), 2) for s in sat], dtype="int8"),
    }
    os.makedirs(os.path.dirname(out_npz), exist_ok=True)
    np.savez_compressed(out_npz, **out)
    lineage = ""
    try:
        lineage = _read_member(root, f"{stem}.shp.xml").decode("utf-8", "replace")[:1200]
    except Exception:
        pass
    lc_counts = {int(k): int(v) for k, v in zip(*np.unique(out["lc"], return_counts=True))}
    passport = {
        "built_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "source": {"root": os.path.basename(root), "stem": stem,
                   "shp_sha256": hashlib.sha256(shp).hexdigest(),
                   "dbf_sha256": hashlib.sha256(dbf).hexdigest(),
                   "shp_bytes": len(shp), "dbf_bytes": len(dbf)},
        "records": int(n), "fields": [f[0] for f in fields],
        "date_range": [str(days.min()), str(days.max())],
        "years": [int(out["year"].min()), int(out["year"].max())],
        "land_cover_codes": lc_counts,
        "night_share": round(float(out["night"].mean()), 4),
        "nrt_share": round(float(out["nrt"].mean()), 4),
        "satellites": {name: int((out["sat"] == code).sum()) for name, code in SATELLITES.items()},
        "producer_lineage_xml_head": lineage,
        "build_seconds": round(time.time() - t0, 1),
    }
    with open(passport_path, "w") as f:
        json.dump(passport, f, indent=1)
    return passport


_CACHE: dict = {}


def load(npz_path):
    st = os.stat(npz_path)
    key = (npz_path, st.st_mtime, st.st_size)
    if _CACHE.get("key") != key:
        with np.load(npz_path) as z:
            _CACHE.update(key=key, data={k: z[k] for k in z.files})
    return _CACHE["data"]


def query(npz_path, boundary, legend):
    """Detections inside `boundary`: counts by year and by stamped land-cover class."""
    d = load(npz_path)
    minx, miny, maxx, maxy = boundary.bounds
    m = (d["lon"] >= minx) & (d["lon"] <= maxx) & (d["lat"] >= miny) & (d["lat"] <= maxy)
    idx = np.nonzero(m)[0]
    if idx.size:
        idx = idx[contains_xy(boundary, d["lon"][idx], d["lat"][idx])]
    n = int(idx.size)
    if not n:
        return {"n": 0}
    years, counts = np.unique(d["year"][idx], return_counts=True)
    lc, lcc = np.unique(d["lc"][idx], return_counts=True)
    day = d["day"][idx]
    epoch = np.datetime64("1970-01-01")
    return {
        "n": n,
        "by_year": {int(y): int(c) for y, c in zip(years, counts)},
        "by_class": {legend.get(int(k), f"class {int(k)}"): int(v) for k, v in zip(lc, lcc)},
        "first": str(epoch + np.timedelta64(int(day.min()), "D")),
        "last": str(epoch + np.timedelta64(int(day.max()), "D")),
        "night": int(d["night"][idx].sum()), "nrt": int(d["nrt"][idx].sum()),
        "median_frp": float(np.median(d["frp"][idx])),
        "confidence_median": float(np.median(d["confidence"][idx])),
    }


def _main(argv):
    from ..config import get_settings
    from ..graph.geo import stacks, tiffs
    if not argv or argv[0] != "build":
        print(__doc__)
        return 2
    settings = get_settings()
    built = 0
    for sid, row in stacks.rows().items():
        for pid, spec in (row.get("points") or {}).items():
            root = tiffs.stack_root(row)
            out = os.path.join(str(settings.cache_dir), "vectors", spec["derived"])
            passport = build(root, spec["file_pattern"], out, out + ".passport.json")
            print(f"{sid}/{pid}: {passport['records']:,} records {passport['date_range']} "
                  f"-> {out} ({passport['build_seconds']} s)")
            built += 1
    if not built:
        print("no stack registers a point set (tiffs.yml `points:`)")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
