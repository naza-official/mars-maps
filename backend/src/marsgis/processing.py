"""Scientific preprocessing: warp sources onto the canonical grid, derive terrain layers,
write Cloud Optimized GeoTIFFs.

Conventions (docs/data-model.md):
* float layers use NaN as nodata (never 0, never a magic number in processed data);
* integer layers declare an explicit nodata value that is outside their valid range;
* every output is checked to lie exactly on the region grid before it is published.
"""

from __future__ import annotations

import logging
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
from rasterio.warp import transform_bounds

from marsgis.crs import assert_mars_crs
from marsgis.grid import GridSpec

log = logging.getLogger(__name__)

#: Bump when an algorithm's numerical output changes; part of every layer version hash.
ALGORITHM_VERSIONS = {
    "warp": "1",
    "slope": "2",  # 2: centre-cell nodata now propagates (was silently filled in 1)
    "hillshade": "2",
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


def derive_terrain(input_path: Path, grid: GridSpec, out: Path, meta: LayerRaster, op: str,
                   params: dict[str, Any]) -> None:
    """Derive slope/hillshade from an elevation layer that already lies on ``grid``.

    Pixel sizes are taken as true ground distances. This is valid because the region
    CRS is Transverse Mercator with k0 = 1: within ±82 km of the central meridian the
    point scale factor is < 1 + 3e-4 (asserted in tests), i.e. < 0.03 % slope error.
    """
    with rasterio.open(input_path) as src:
        if not grid.is_aligned(src.transform, src.crs and assert_mars_crs(src.crs.to_wkt())):
            raise ValueError(f"{input_path} is not aligned with the region grid")
        z = src.read(1, masked=True).filled(np.nan).astype(np.float64)
        res_x, res_y = src.res
        if op == "slope":
            data = slope_degrees(z, res_x, res_y)
        elif op == "hillshade":
            data = hillshade(z, res_x, res_y, **params)
        else:
            raise ValueError(f"unknown terrain op {op}")
        profile = _base_profile(grid, src.transform, src.width, src.height, meta)

    tmp = out.with_suffix(".tmp.tif")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(data, 1)
        _annotate(dst, meta)
    _finalize_cog(tmp, out, meta)
    log.info("derived layer", extra={"layer": meta.layer_id, "op": op, "out": str(out)})


# -------------------------------------------------------------------------- statistics


def band_statistics(path: Path) -> dict[str, float | None]:
    """Statistics over valid pixels only (nodata/NaN excluded)."""
    with rasterio.open(path) as src:
        arr = src.read(1, masked=True)
    valid = arr.compressed().astype(np.float64)
    valid = valid[np.isfinite(valid)]
    total = arr.size
    if valid.size == 0:
        return {"valid_percent": 0.0, "minimum": None, "maximum": None, "mean": None,
                "stddev": None, "p02": None, "p98": None}
    p02, p98 = np.percentile(valid, [2, 98])
    return {
        "valid_percent": round(100.0 * valid.size / total, 3),
        "minimum": float(valid.min()),
        "maximum": float(valid.max()),
        "mean": float(valid.mean()),
        "stddev": float(valid.std()),
        "p02": float(p02),
        "p98": float(p98),
    }
