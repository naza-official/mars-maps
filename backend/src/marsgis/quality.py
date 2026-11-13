"""Data-quality diagnostics that back documented numbers (reproducible from the CLI).

* :func:`feature_stats` - exact quantiles of a layer (used to bound DTM relative noise).
* :func:`compare_dtms` - vertical comparison of a fine DTM against a coarse DTM on the
  nested grid family: the fine layer is reduced to *exact* block means of whole coarse
  cells (no resampling; incomplete blocks are dropped), then differenced.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import Window

QUANTILES = (0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.98, 0.999)


def feature_stats(path: Path, thresholds: tuple[float, ...] = ()) -> dict[str, Any]:
    with rasterio.open(path) as src:
        a = src.read(1, masked=True)
        units = src.units[0]
    v = a.compressed().astype(np.float64)
    v = v[np.isfinite(v)]
    return {
        "layer": path.stem,
        "units": units,
        "valid_cells": int(v.size),
        "quantiles": {str(q): float(np.quantile(v, q)) for q in QUANTILES},
        "fraction_above": {str(t): float((v > t).mean()) for t in thresholds},
    }


def compare_dtms(fine: Path, coarse: Path) -> dict[str, Any]:
    """Statistics of (block mean of fine) - coarse over fully covered coarse cells."""
    with rasterio.open(fine) as f, rasterio.open(coarse) as c:
        ratio = c.res[0] / f.res[0]
        n = round(ratio)
        if abs(ratio - n) > 1e-9 or n < 1:
            raise ValueError("coarse resolution must be an integer multiple of the fine one")
        # fine raster origin expressed in coarse cells (both on the same lattice family)
        col0 = (f.transform.c - c.transform.c) / c.res[0]
        row0 = (c.transform.f - f.transform.f) / c.res[0]
        cc0, cr0 = math.ceil(col0 - 1e-9), math.ceil(row0 - 1e-9)
        dc, dr = round((cc0 - col0) * n), round((cr0 - row0) * n)
        nh, nw = (f.height - dr) // n, (f.width - dc) // n
        nh, nw = min(nh, c.height - cr0), min(nw, c.width - cc0)
        z = f.read(1, window=Window(dc, dr, nw * n, nh * n), masked=True)
        z = z.astype(np.float64).filled(np.nan)
        cz = c.read(1, window=Window(cc0, cr0, nw, nh), masked=True)
        cz = cz.astype(np.float64).filled(np.nan)
    blocks = z.reshape(nh, n, nw, n)
    complete = np.isfinite(blocks).all(axis=(1, 3))
    mean = np.where(complete, np.nansum(blocks, axis=(1, 3)) / (n * n), np.nan)
    d = mean - cz
    d = d[np.isfinite(d)]
    if d.size == 0:
        raise ValueError("the DTMs do not overlap on complete coarse cells")
    return {
        "fine": fine.stem,
        "coarse": coarse.stem,
        "block": f"{n}x{n} fine cells per coarse cell (exact block mean)",
        "cells_compared": int(d.size),
        "mean_m": float(d.mean()),
        "median_m": float(np.median(d)),
        "std_m": float(d.std()),
        "p05_m": float(np.quantile(d, 0.05)),
        "p95_m": float(np.quantile(d, 0.95)),
        "interpretation": "fine minus coarse; a non-zero mean is a vertical datum/control "
                          "offset between the products, which must be removed before fusion",
    }
