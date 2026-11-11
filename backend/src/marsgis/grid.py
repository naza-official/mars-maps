"""Canonical analysis grids.

A region defines one *grid family*: a fixed CRS, a fixed origin (top-left corner of the
region frame) and a base resolution. Every layer uses resolution = base / 2^k, so any
two layers in a region are pixel-aligned (each coarse pixel is an exact block of fine
pixels). This is what makes multi-layer stacking for cost surfaces and ML trivial and
loss-free: no layer ever needs re-resampling to be combined with another.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from affine import Affine
from pyproj import CRS
from rasterio.windows import Window

from marsgis.config import RegionConfig
from marsgis.crs import region_crs

_EPS = 1e-6  # relative tolerance for floating-point lattice checks


@dataclass(frozen=True)
class GridSpec:
    crs: CRS
    origin_x: float  # left edge of the region frame
    origin_y: float  # top edge of the region frame
    resolution: float
    width: int  # full-frame size in pixels
    height: int

    @property
    def frame_bounds(self) -> tuple[float, float, float, float]:
        return (
            self.origin_x,
            self.origin_y - self.height * self.resolution,
            self.origin_x + self.width * self.resolution,
            self.origin_y,
        )

    @property
    def frame_transform(self) -> Affine:
        return Affine(self.resolution, 0.0, self.origin_x, 0.0, -self.resolution, self.origin_y)

    def at_resolution(self, resolution: float) -> GridSpec:
        """A nested grid of the same family. ``resolution`` must be base / 2^k or base * 2^k."""
        ratio = self.resolution / resolution
        k = math.log2(ratio)
        if abs(k - round(k)) > _EPS:
            raise ValueError(
                f"resolution {resolution} is not base {self.resolution} times a power of two"
            )
        factor = 2.0 ** round(k)
        return GridSpec(
            crs=self.crs,
            origin_x=self.origin_x,
            origin_y=self.origin_y,
            resolution=resolution,
            width=round(self.width * factor),
            height=round(self.height * factor),
        )

    def snap_window(self, bounds: tuple[float, float, float, float]) -> Window:
        """Smallest whole-pixel window of this grid covering ``bounds``, clipped to the frame."""
        left, bottom, right, top = bounds
        r = self.resolution
        col0 = math.floor((left - self.origin_x) / r + _EPS)
        col1 = math.ceil((right - self.origin_x) / r - _EPS)
        row0 = math.floor((self.origin_y - top) / r + _EPS)
        row1 = math.ceil((self.origin_y - bottom) / r - _EPS)
        col0, row0 = max(col0, 0), max(row0, 0)
        col1, row1 = min(col1, self.width), min(row1, self.height)
        if col1 <= col0 or row1 <= row0:
            raise ValueError(f"bounds {bounds} do not intersect the region frame")
        return Window(col0, row0, col1 - col0, row1 - row0)

    def window_transform(self, window: Window) -> Affine:
        return self.frame_transform @ Affine.translation(window.col_off, window.row_off)

    def is_aligned(self, transform: Affine, crs: CRS | None = None) -> bool:
        """True if a raster with ``transform`` lies exactly on this grid's lattice."""
        if crs is not None and not self.crs.equals(crs, ignore_axis_order=True):
            return False
        if transform.b != 0 or transform.d != 0:
            return False
        r = self.resolution
        if abs(transform.a - r) > _EPS * r or abs(-transform.e - r) > _EPS * r:
            return False
        cols = (transform.c - self.origin_x) / r
        rows = (self.origin_y - transform.f) / r
        return abs(cols - round(cols)) < 1e-4 and abs(rows - round(rows)) < 1e-4


def region_grid(region: RegionConfig) -> GridSpec:
    """Base grid of a region: square frame centred on the projection origin (0, 0)."""
    h = region.half_size_m
    n = 2 * h / region.base_resolution_m
    if abs(n - round(n)) > _EPS:
        raise ValueError("2 * half_size_m must be a multiple of base_resolution_m")
    return GridSpec(
        crs=region_crs(region),
        origin_x=-h,
        origin_y=h,
        resolution=region.base_resolution_m,
        width=round(n),
        height=round(n),
    )
