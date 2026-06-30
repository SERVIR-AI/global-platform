"""Bring any 1-5 raster onto one AOI reference grid (the clipped hazard grid), so the
Layer-2 combine can do cell-by-cell math. Vulnerability layers arrive at different
resolutions/extents (30 m / 100 m / coarse); align_to resamples each onto the hazard
grid with NEAREST neighbour — categorical 1-5 classes, never interpolated.

Memory-safe: each layer is windowed-clipped to the AOI first (ingest.hazard_clip reads
only the AOI window — sources are up to 182301x147780), then the small clip is
reprojected onto the reference grid. Cells the layer doesn't cover are filled with 0
(= no class), so a layer that falls short of the AOI yields nodata there, not a crash.
"""
import os
import warnings

warnings.filterwarnings("ignore")
import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject

from . import ingest, trace, verify


def reference_grid(aoi, hazard):
    """The AOI's reference grid = the clipped hazard raster: its transform/crs/shape."""
    clip = ingest.hazard_clip(aoi, hazard)
    with rasterio.open(clip) as src:
        return {"path": clip, "transform": src.transform, "crs": src.crs,
                "width": src.width, "height": src.height}


def _resample_onto(src_arr, src_transform, src_crs, ref, src_nodata=None, fill=0):
    """Nearest-resample `src_arr` onto the reference grid `ref`; return the dst array.
    Pure (no IO) so the resampling can be unit-tested on synthetic grids."""
    dst = np.full((ref["height"], ref["width"]), fill, dtype=src_arr.dtype)
    reproject(source=src_arr, destination=dst,
              src_transform=src_transform, src_crs=src_crs,
              dst_transform=ref["transform"], dst_crs=ref["crs"],
              resampling=Resampling.nearest, src_nodata=src_nodata, dst_nodata=fill)
    return dst


def align_to(ref, layer, aoi, verify_first=True):
    """Resample `layer` onto the reference grid `ref`; write & return
    <aoi>/<layer>__aligned.tif. Verifies the layer against its schema first."""
    if verify_first:
        report = verify.verify_raster(layer)
        if not report.ok:
            raise ValueError(f"{layer} failed verification: {report.mismatches}")
    clip = ingest.hazard_clip(aoi, layer)            # windowed clip on the layer's native grid
    adir = os.path.dirname(aoi["admin"])
    out = os.path.join(adir, f"{layer}__aligned.tif")
    with rasterio.open(clip) as src:
        dst = _resample_onto(src.read(1), src.transform, src.crs, ref, src_nodata=src.nodata)
        prof = {"driver": "GTiff", "height": ref["height"], "width": ref["width"],
                "count": 1, "dtype": dst.dtype, "crs": ref["crs"],
                "transform": ref["transform"], "compress": "lzw", "nodata": 0}
    with rasterio.open(out, "w", **prof) as d:
        d.write(dst, 1)
    trace.emit({"kind": "compute", "op": "align", "layer": layer, "dest": out, "was_cached": False})
    return out
