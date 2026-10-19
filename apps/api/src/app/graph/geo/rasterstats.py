"""Cheap raster stats WITHOUT loading the whole array.

These rasters are continent-scale (bldDensity is 182301x147780, hazard_flood
54691x44335) — a full `read(1)` OOM-kills the process. dtype/nodata/crs/pixel come
straight from the profile (no pixel read); min/max/distinct come from a single
decimated overview read (`out_shape`), which uses the COG overviews when present.
This is the one shared helper every verify/align/reclass path reuses (rule R1).
"""
import warnings

warnings.filterwarnings("ignore")
import numpy as np
import rasterio


def windowed_stats(path, sample=1024, keep_values=(), skip_values=()):
    """Profile + a decimated-sample min/max/distinct for `path`. Never full-loads.
    `keep_values`: values that count even when they equal the nodata tag — a binary
    extent map tags 0 as nodata and names it a class in the same legend.
    `skip_values`: values that are not readings — the layer's declared outside values
    and nodata (255 over open sea) — so a declared range is checked on readings only."""
    with rasterio.open(path) as src:
        full_w, full_h = src.width, src.height
        dtype = src.dtypes[0]
        nodata = src.nodata
        crs_epsg = src.crs.to_epsg() if src.crs else None
        crs = src.crs.to_string() if src.crs else None
        pixel = abs(src.transform.a)
        out_h, out_w = min(sample, full_h), min(sample, full_w)
        arr = src.read(1, out_shape=(out_h, out_w))   # decimated; uses overviews if present

    a = arr.astype("float64").ravel()
    a = a[~np.isnan(a)]
    if nodata is not None and float(nodata) not in {float(v) for v in keep_values}:
        a = a[a != nodata]
    skip = [float(v) for v in skip_values if float(v) not in {float(k) for k in keep_values}]
    if skip:
        a = a[~np.isin(a, np.array(skip, dtype="float64"))]
    if a.size:
        smin, smax, ndist = float(a.min()), float(a.max()), int(np.unique(a).size)
    else:
        smin = smax = None
        ndist = 0
    return {
        "dtype": dtype, "nodata": nodata, "crs": crs, "crs_epsg": crs_epsg,
        "pixel_size_deg": pixel, "width": full_w, "height": full_h,
        "sampled_min": smin, "sampled_max": smax, "sampled_distinct": ndist,
        "sample_shape": (out_h, out_w),
    }
