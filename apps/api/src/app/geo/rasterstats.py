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


def windowed_stats(path, sample=1024, keep_values=(), skip_values=(), driver=None):
    """Profile + a decimated-sample min/max/distinct for `path`. Never full-loads.
    `keep_values`: values that count even when they equal the nodata tag — a binary
    extent map tags 0 as nodata and names it a class in the same legend.
    `skip_values`: values that are not readings — the layer's declared outside values
    and nodata (255 over open sea) — so a declared range is checked on readings only.
    `driver`: the one GDAL driver to open with (the contribution gate names it)."""
    with rasterio.open(path, driver=driver) as src:
        full_w, full_h = src.width, src.height
        dtype = src.dtypes[0]
        nodata = src.nodata
        crs_epsg = src.crs.to_epsg() if src.crs else None
        crs = src.crs.to_string() if src.crs else None
        pixel = abs(src.transform.a)
        out_h, out_w = min(sample, full_h), min(sample, full_w)
        # A decimated read (`out_shape`) is only cheap when the file has overviews; a
        # tiled file without them makes GDAL touch every block and a 1 GB file took
        # 5 GB of memory to sample. Sample whole BLOCKS instead: every k-th block,
        # k chosen so about sample*sample cells are read, one block in memory at a
        # time. A small file (under the budget) is read in full, so its min/max/
        # distinct are exact, not sampled.
        keep = {float(v) for v in keep_values}
        drop = [float(v) for v in skip_values if float(v) not in keep]
        if nodata is not None and float(nodata) not in keep:
            drop.append(float(nodata))
        blocks = list(src.block_windows(1))
        # Read about 256 blocks spread evenly over the file (a 1 GB tiled float32
        # file: 6% of it), or all of them when there are fewer.
        want = max(16, min(len(blocks), 256))
        k = max(1, int(np.ceil(len(blocks) / float(want)))) if blocks else 1
        smin = smax = None
        distinct: set = set()
        _DISTINCT_CAP = 100_000             # a continuous raster has no useful distinct count
        for i, (_, win) in enumerate(blocks):
            if i % k:
                continue
            blk = src.read(1, window=win).astype("float64").ravel()
            blk = blk[~np.isnan(blk)]
            if drop:
                blk = blk[~np.isin(blk, np.array(drop, dtype="float64"))]
            if not blk.size:
                continue
            bmin, bmax = float(blk.min()), float(blk.max())
            smin = bmin if smin is None else min(smin, bmin)
            smax = bmax if smax is None else max(smax, bmax)
            if len(distinct) < _DISTINCT_CAP:
                distinct.update(np.unique(blk).tolist())
    ndist = int(len(distinct))
    return {
        "dtype": dtype, "nodata": nodata, "crs": crs, "crs_epsg": crs_epsg,
        "pixel_size_deg": pixel, "width": full_w, "height": full_h,
        "sampled_min": smin, "sampled_max": smax, "sampled_distinct": ndist,
        "sample_shape": (out_h, out_w),
    }
