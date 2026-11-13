"""Scientific preprocessing: warp sources onto the canonical grid, derive terrain layers,
write Cloud Optimized GeoTIFFs.

Conventions (docs/data-model.md):
* float layers use NaN as nodata (never 0, never a magic number in processed data);
* integer layers declare an explicit nodata value that is outside their valid range;
* every output is checked to lie exactly on the region grid before it is published.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import rasterio.shutil
from affine import Affine
from rasterio.crs import CRS as RioCRS
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from pyproj import CRS
from rasterio.warp import transform_bounds
from rasterio.windows import Window

from marsgis.config import VehicleConfig
from marsgis.crs import assert_mars_crs
from marsgis.grid import GridSpec
from marsgis.terrain import plane_fit, window_cells
from marsgis.traversability import Uncertainty, evaluate

log = logging.getLogger(__name__)

#: Bump when an algorithm's numerical output changes; part of every layer version hash.
ALGORITHM_VERSIONS = {
    "warp": "1",
    "slope": "2",  # 2: centre-cell nodata now propagates (was silently filled in 1)
    "hillshade": "2",
    "plane_tilt": "1",
    "plane_roughness": "1",
    "traversability_class": "1",
    "traversal_time": "1",
    "traversal_cost": "1",
}

COG_OPTIONS: dict[str, Any] = {
    "COMPRESS": "DEFLATE",
    "PREDICTOR": "YES",  # floating-point predictor for float32, horizontal for ints
    "BLOCKSIZE": 512,
    "OVERVIEWS": "AUTO",
    "BIGTIFF": "IF_SAFER",
    "NUM_THREADS": "ALL_CPUS",
}


@dataclass
class LayerRaster:
    """Description of a layer raster about to be written."""

    layer_id: str
    units: str
    description: str
    dtype: str
    nodata: float | int
    overview_resampling: str = "AVERAGE"
    tags: dict[str, str] = field(default_factory=dict)


def rio_crs(grid: GridSpec) -> RioCRS:
    return RioCRS.from_wkt(grid.crs.to_wkt())


def _base_profile(grid: GridSpec, transform: Affine, width: int, height: int,
                  meta: LayerRaster) -> dict[str, Any]:
    return {
        "driver": "GTiff",
        "dtype": meta.dtype,
        "count": 1,
        "width": width,
        "height": height,
        "crs": rio_crs(grid),
        "transform": transform,
        "nodata": meta.nodata,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
    }


def _finalize_cog(tmp: Path, out: Path, meta: LayerRaster) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    rasterio.shutil.copy(
        tmp, out, driver="COG", OVERVIEW_RESAMPLING=meta.overview_resampling, **COG_OPTIONS
    )
    tmp.unlink()


def _annotate(dst: rasterio.io.DatasetWriter, meta: LayerRaster) -> None:
    dst.units = (meta.units,)
    dst.descriptions = (meta.description[:200],)
    dst.update_tags(**{f"MARSGIS_{k.upper()}": str(v) for k, v in meta.tags.items()})
    dst.update_tags(1, LAYER=meta.layer_id, UNITS=meta.units)


def warp_to_grid(src_path: Path, grid: GridSpec, out: Path, meta: LayerRaster,
                 resampling: str) -> dict[str, Any]:
    """Reproject/resample a source raster onto the region grid, block by block.

    The output extent is the source footprint (transformed with a densified edge),
    snapped *outward* to the grid lattice and clipped to the region frame.
    """
    with rasterio.open(src_path) as src:
        if src.crs is None:
            raise ValueError(f"{src_path} has no CRS; refusing to assume one")
        assert_mars_crs(src.crs.to_wkt(), context=str(src_path))
        if src.nodata is None:
            raise ValueError(f"{src_path} declares no nodata value; declare it explicitly")
        dst_bounds = transform_bounds(src.crs, rio_crs(grid), *src.bounds, densify_pts=51)
        window = grid.snap_window(dst_bounds)
        transform = grid.window_transform(window)
        width, height = int(window.width), int(window.height)

        tmp = out.with_suffix(".tmp.tif")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with WarpedVRT(
            src,
            crs=rio_crs(grid),
            transform=transform,
            width=width,
            height=height,
            resampling=Resampling[resampling],
            src_nodata=src.nodata,
            nodata=meta.nodata,
            dtype=meta.dtype,
        ) as vrt, rasterio.open(tmp, "w", **_base_profile(grid, transform, width, height,
                                                           meta)) as dst:
            for _, win in dst.block_windows(1):
                dst.write(vrt.read(1, window=win), 1, window=win)
            _annotate(dst, meta)
    _finalize_cog(tmp, out, meta)
    log.info("warped layer", extra={"layer": meta.layer_id, "out": str(out), "width": width,
                                    "height": height, "resampling": resampling})
    return {"window": [window.col_off, window.row_off, width, height]}


# ----------------------------------------------------------------------------- terrain


def horn_gradients(z: np.ndarray, res_x: float, res_y: float) -> tuple[np.ndarray, np.ndarray]:
    """Horn (1981) 3x3 finite differences.

    Returns (dz/dx east-positive, dz/dy north-positive). Any NaN in the 3x3 window
    yields NaN: missing data is never filled with zeros or neighbours.
    """
    p = np.pad(z.astype(np.float64), 1, mode="constant", constant_values=np.nan)
    a, b, c = p[:-2, :-2], p[:-2, 1:-1], p[:-2, 2:]
    d, f = p[1:-1, :-2], p[1:-1, 2:]
    g, h, i = p[2:, :-2], p[2:, 1:-1], p[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8.0 * res_x)
    # rows increase southward, so (top - bottom) is the north-positive difference
    dzdy = ((a + 2 * b + c) - (g + 2 * h + i)) / (8.0 * res_y)
    # Horn's kernel has zero weight on the centre cell; a missing centre must still
    # produce a missing result, otherwise nodata cells would be silently filled.
    centre_missing = ~np.isfinite(p[1:-1, 1:-1])
    dzdx[centre_missing] = np.nan
    dzdy[centre_missing] = np.nan
    return dzdx, dzdy


def slope_degrees(z: np.ndarray, res_x: float, res_y: float) -> np.ndarray:
    dzdx, dzdy = horn_gradients(z, res_x, res_y)
    return np.degrees(np.arctan(np.hypot(dzdx, dzdy))).astype(np.float32)


def hillshade(z: np.ndarray, res_x: float, res_y: float, azimuth_deg: float = 315.0,
              altitude_deg: float = 45.0) -> np.ndarray:
    """Lambertian hillshade, uint8 1..255 with 0 = nodata."""
    dzdx, dzdy = horn_gradients(z, res_x, res_y)
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(-dzdy, -dzdx)  # direction of steepest descent, math angle
    zenith = np.radians(90.0 - altitude_deg)
    az = np.radians(90.0 - azimuth_deg)  # compass -> math angle (illumination direction)
    shade = np.cos(zenith) * np.cos(slope) + np.sin(zenith) * np.sin(slope) * np.cos(az - aspect)
    out = np.clip(np.round(1 + 254 * np.clip(shade, 0, 1)), 1, 255)
    return np.where(np.isfinite(shade), out, 0).astype(np.uint8)


STRIP_ROWS = 512


def _read_strip(src: rasterio.io.DatasetReader, row0: int, row1: int, halo: int) -> np.ndarray:
    """Rows [row0 - halo, row1 + halo) as float64 with NaN for nodata *and* for rows/cols
    outside the raster (outside = unobserved, never zero)."""
    h, w = src.height, src.width
    r0, r1 = max(row0 - halo, 0), min(row1 + halo, h)
    data = src.read(1, window=Window(0, r0, w, r1 - r0), masked=True)
    data = data.astype(np.float64).filled(np.nan)
    top, bottom = r0 - (row0 - halo), (row1 + halo) - r1
    return np.pad(data, ((top, bottom), (halo, halo)), constant_values=np.nan)


def derive_layer(inputs: dict[str, Path], grid: GridSpec, out: Path, meta: LayerRaster,
                 fn: Callable[[dict[str, np.ndarray], float], np.ndarray],
                 halo: int) -> None:
    """Compute a derived layer strip by strip (bounded memory for any raster size).

    ``fn`` receives float64 arrays (NaN = missing) of shape (strip + 2*halo, width + 2*halo)
    for every named input plus the cell size, and returns an array of the same shape;
    the halo is cropped before writing. Because the halo is at least the operator radius
    and out-of-raster cells are NaN, the result is identical to processing the whole
    raster at once (asserted in tests/test_terrain.py).

    All inputs must lie on the same grid window (same transform and shape) - they are
    never resampled here.
    """
    srcs = {name: rasterio.open(path) for name, path in inputs.items()}
    try:
        ref = next(iter(srcs.values()))
        for name, src in srcs.items():
            assert_mars_crs(src.crs.to_wkt(), context=f"{meta.layer_id}:{name}")
            if not grid.is_aligned(src.transform, CRS.from_wkt(src.crs.to_wkt())):
                raise ValueError(f"input {name} of {meta.layer_id} is not on the region grid")
            if src.transform != ref.transform or src.shape != ref.shape:
                raise ValueError(f"inputs of {meta.layer_id} are not on the same grid window")
        res_x, res_y = ref.res
        if abs(res_x - res_y) > 1e-9 * res_x:
            raise ValueError("non-square cells are not supported by terrain operators")
        profile = _base_profile(grid, ref.transform, ref.width, ref.height, meta)
        tmp = out.with_suffix(".tmp.tif")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(tmp, "w", **profile) as dst:
            for row0 in range(0, ref.height, STRIP_ROWS):
                row1 = min(row0 + STRIP_ROWS, ref.height)
                arrays = {n: _read_strip(s, row0, row1, halo) for n, s in srcs.items()}
                result = fn(arrays, res_x)
                core = result[halo:halo + (row1 - row0), halo:halo + ref.width]
                if np.issubdtype(np.dtype(meta.dtype), np.integer):
                    core = np.where(np.isfinite(core), core, meta.nodata)
                dst.write(core.astype(meta.dtype), 1,
                          window=Window(0, row0, ref.width, row1 - row0))
            _annotate(dst, meta)
    finally:
        for src in srcs.values():
            src.close()
    _finalize_cog(tmp, out, meta)
    log.info("derived layer", extra={"layer": meta.layer_id, "out": str(out)})


def terrain_operator(op: str, params: dict[str, Any], res: float
                     ) -> tuple[Callable[[dict[str, np.ndarray], float], np.ndarray], int]:
    """(function, halo) for a single-input terrain operator on an elevation layer."""
    if op == "slope":
        return (lambda a, r: slope_degrees(a["z"], r, r)), 1
    if op == "hillshade":
        return (lambda a, r: hillshade(a["z"], r, r, **params).astype(np.float64)), 1
    if op in ("plane_tilt", "plane_roughness"):
        n = window_cells(float(params["window_m"]), res)
        idx = 0 if op == "plane_tilt" else 1
        return (lambda a, r: plane_fit(a["z"], r, n)[idx]), n // 2
    raise ValueError(f"unknown terrain op {op}")


def derive_terrain(input_path: Path, grid: GridSpec, out: Path, meta: LayerRaster, op: str,
                   params: dict[str, Any]) -> None:
    """Derive a terrain layer from an elevation layer that already lies on ``grid``.

    Pixel sizes are taken as true ground distances. This is valid because the region
    CRS is Transverse Mercator with k0 = 1: within ±82 km of the central meridian the
    point scale factor is < 1 + 3e-4 (asserted in tests), i.e. < 0.03 % slope error.
    """
    with rasterio.open(input_path) as src:
        res = src.res[0]
    fn, halo = terrain_operator(op, params, res)
    derive_layer({"z": input_path}, grid, out, meta, fn, halo)


def derive_traversability(inputs: dict[str, Path], grid: GridSpec, out: Path,
                          meta: LayerRaster, op: str, vehicle: VehicleConfig,
                          sigma: Uncertainty) -> None:
    """Traversability class / traverse time / planning cost from feature layers (no halo)."""
    field = {"traversability_class": "classes", "traversal_time": "time_s_per_m",
             "traversal_cost": "cost_s_per_m"}[op]

    def fn(a: dict[str, np.ndarray], _res: float) -> np.ndarray:
        ev = evaluate(a["tilt"], a["roughness"], vehicle, sigma)
        out_arr = getattr(ev, field).astype(np.float64)
        # cells outside the raster (NaN padding only) stay nodata for the class layer
        return out_arr

    derive_layer(inputs, grid, out, meta, fn, halo=0)


# -------------------------------------------------------------------------- statistics


def band_statistics(path: Path, max_sample: int = 2_000_000) -> dict[str, Any]:
    """Statistics over valid pixels only (nodata/NaN excluded), computed strip by strip.

    min/max/mean/stddev/valid_percent are exact; p02/p98 come from a regular subsample of
    at most ``max_sample`` valid values (stated in ``percentile_sample``). Integer
    (categorical) layers also get a per-value histogram.
    """
    with rasterio.open(path) as src:
        total = src.width * src.height
        step = max(1, int(math.sqrt(total / max_sample)))
        n = 0
        s1 = s2 = 0.0
        vmin, vmax = math.inf, -math.inf
        sample: list[np.ndarray] = []
        hist: dict[int, int] = {}
        is_int = np.issubdtype(np.dtype(src.dtypes[0]), np.integer)
        for row0 in range(0, src.height, STRIP_ROWS):
            h = min(STRIP_ROWS, src.height - row0)
            arr = src.read(1, window=Window(0, row0, src.width, h), masked=True)
            valid = arr.compressed().astype(np.float64)
            valid = valid[np.isfinite(valid)]
            if valid.size:
                n += valid.size
                s1 += float(valid.sum())
                s2 += float((valid**2).sum())
                vmin, vmax = min(vmin, float(valid.min())), max(vmax, float(valid.max()))
                if is_int:
                    vals, counts = np.unique(valid.astype(np.int64), return_counts=True)
                    for val, cnt in zip(vals.tolist(), counts.tolist(), strict=True):
                        hist[val] = hist.get(val, 0) + cnt
            sub = arr[(-row0) % step::step, ::step]
            sub = sub.compressed().astype(np.float64)
            sample.append(sub[np.isfinite(sub)])
    if n == 0:
        return {"valid_percent": 0.0, "minimum": None, "maximum": None, "mean": None,
                "stddev": None, "p02": None, "p98": None}
    mean = s1 / n
    smp = np.concatenate(sample)
    p02, p98 = np.percentile(smp, [2, 98]) if smp.size else (vmin, vmax)
    out: dict[str, Any] = {
        "valid_percent": round(100.0 * n / total, 3),
        "minimum": vmin,
        "maximum": vmax,
        "mean": mean,
        "stddev": math.sqrt(max(s2 / n - mean**2, 0.0)),
        "p02": float(p02),
        "p98": float(p98),
        "percentile_sample": {"step": step, "count": int(smp.size)},
    }
    if is_int:
        out["histogram"] = {str(k): v for k, v in sorted(hist.items())}
    return out
