"""Coordinate-aware pixel inspection across all layers of a region.

Values are read at full resolution from the layer COGs (no overviews, no resampling):
the returned value is exactly the analysis-grid cell containing the query point.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import Window

from marsgis.crs import (
    lon_east_360,
    lon_west_360,
    ocentric_to_ographic_lat,
    xy_to_lonlat,
)
from marsgis.grid import GridSpec


def coordinate_report(grid: GridSpec, x: float, y: float) -> dict[str, Any]:
    lon, lat = xy_to_lonlat(grid.crs, x, y)
    return {
        "x_m": x,
        "y_m": y,
        "lon_east_180": lon,
        "lon_east_360": lon_east_360(lon),
        "lon_west_360": lon_west_360(lon),
        "lat_planetocentric": lat,
        "lat_planetographic": ocentric_to_ographic_lat(lat),
        "crs": grid.crs.name,
    }


def sample_layer(path: Path, x: float, y: float) -> dict[str, Any]:
    with rasterio.open(path) as src:
        row, col = src.index(x, y, op=math.floor)
        base = {"row": int(row), "col": int(col), "resolution_m": src.res[0]}
        if not (0 <= row < src.height and 0 <= col < src.width):
            return {**base, "status": "outside", "value": None}
        val = src.read(1, window=Window(col, row, 1, 1), masked=True)
        cell_x, cell_y = src.xy(row, col)  # cell centre
        base.update({"cell_center_x_m": cell_x, "cell_center_y_m": cell_y})
        if np.ma.is_masked(val[0, 0]) or not np.isfinite(val[0, 0]):
            return {**base, "status": "nodata", "value": None}
        return {**base, "status": "ok", "value": float(val[0, 0])}
