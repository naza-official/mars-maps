"""Rover-scale terrain descriptors on synthetic surfaces with analytic answers.

Synthetic arrays here are test inputs only; they are never published as data layers.
"""

import math

import numpy as np
import pytest
import rasterio
from affine import Affine

from marsgis.grid import region_grid
from marsgis.processing import LayerRaster, derive_terrain, slope_degrees
from marsgis.terrain import (
    complete_windows,
    plane_fit,
    residual_noise_floor,
    tilt_sigma_deg,
    window_cells,
)


def _plane(n=40, res=1.0, gx=0.1, gy=-0.05, z0=-2500.0):
    """z = z0 + gx * east + gy * north on a grid whose rows go south."""
    rows, cols = np.mgrid[0:n, 0:n]
    east, north = cols * res, -rows * res
    return z0 + gx * east + gy * north


def test_window_cells():
    assert window_cells(5, 1.0) == 5
    assert window_cells(7.5, 2.5) == 3
    with pytest.raises(ValueError):
        window_cells(4, 1.0)  # even
    with pytest.raises(ValueError):
        window_cells(5, 1.25)  # not a whole number of cells
    with pytest.raises(ValueError):
        window_cells(1, 1.0)  # too small


@pytest.mark.parametrize("n", [3, 5, 7])
def test_tilt_of_exact_plane(n):
    gx, gy = 0.1, -0.05
    tilt, resid = plane_fit(_plane(gx=gx, gy=gy), 1.0, n)
    k = n // 2
    core = (slice(k, -k), slice(k, -k))
    expected = math.degrees(math.atan(math.hypot(gx, gy)))
    assert np.nanmax(np.abs(tilt[core] - expected)) < 1e-9
    assert np.nanmax(resid[core]) < 1e-9  # large absolute z (-2500 m): no cancellation
    # incomplete patches at the raster edge are unknown, not extrapolated
    assert np.isnan(tilt[:k]).all() and np.isnan(resid[:, -k:]).all()


def test_tilt_direction_conventions_match_horn():
    """Plane-fit gradient and Horn's slope agree on a plane (same east/north conventions)."""
    z = _plane(gx=0.2, gy=0.3, res=2.0)
    tilt, _ = plane_fit(z, 2.0, 3)
    horn = slope_degrees(z, 2.0, 2.0)
    assert np.nanmax(np.abs(tilt[2:-2, 2:-2] - horn[2:-2, 2:-2])) < 1e-6


def test_residual_of_a_step():
    """A single raised cell of height h inside a flat patch: max residual is known exactly.

    With the orthogonal design, the plane fitted to an n x n patch containing one spike
    of height h at offset (u, v) is a = h/n^2, b_x = h u/S, b_y = h v/S. For the patch
    centred on the spike (u = v = 0) the plane is flat at h/n^2, so the max residual is
    h (1 - 1/n^2).
    """
    n, h = 5, 0.8
    z = np.zeros((21, 21))
    z[10, 10] = h
    _, resid = plane_fit(z, 1.0, n)
    assert resid[10, 10] == pytest.approx(h * (1 - 1 / n**2), abs=1e-12)
    # patches that do not contain the spike are perfectly flat
    assert resid[10, 10 + n // 2 + 1] == pytest.approx(0.0, abs=1e-12)


def test_missing_data_inside_patch_gives_unknown():
    z = _plane()
    z[20, 20] = np.nan
    tilt, resid = plane_fit(z, 1.0, 5)
    assert np.isnan(tilt[18:23, 18:23]).all() and np.isnan(resid[18:23, 18:23]).all()
    assert np.isfinite(tilt[15, 15]) and np.isfinite(tilt[25, 25])
    assert complete_windows(z, 5)[15, 15] and not complete_windows(z, 5)[22, 22]


def test_tilt_sigma_matches_monte_carlo():
    rng = np.random.default_rng(0)
    sigma_z, n = 0.25, 5
    z = rng.normal(0.0, sigma_z, size=(400, 400))
    tilt, _ = plane_fit(z, 1.0, n)
    # on a flat surface the fitted gradient components are N(0, s) with s = sigma/sqrt(S);
    # tilt = |gradient| is Rayleigh-distributed with mean s*sqrt(pi/2)
    s_deg = tilt_sigma_deg(sigma_z, n, 1.0)
    assert s_deg == pytest.approx(math.degrees(0.25 / math.sqrt(50.0)))
    mean_tilt = np.nanmean(tilt)
    assert mean_tilt == pytest.approx(s_deg * math.sqrt(math.pi / 2), rel=0.03)


def test_residual_noise_floor_order_of_magnitude():
    rng = np.random.default_rng(1)
    z = rng.normal(0.0, 0.25, size=(300, 300))
    _, resid = plane_fit(z, 1.0, 5)
    floor = residual_noise_floor(0.25, 5)
    assert 0.6 * floor < np.nanmean(resid) < 1.2 * floor


def test_strip_processing_equals_whole_array(tmp_path, region, monkeypatch):
    """The strip/halo driver must reproduce the in-memory operator exactly."""
    import marsgis.processing as proc

    grid = region_grid(region).at_resolution(1.0)
    rng = np.random.default_rng(2)
    h, w = 300, 170
    z = (np.cumsum(rng.normal(0, 0.3, (h, w)), axis=0) - 2500.0).astype(np.float32)
    z[100:104, 50:53] = np.nan
    src = tmp_path / "z.tif"
    transform = Affine(1.0, 0, 500.0, 0, -1.0, 700.0)
    with rasterio.open(src, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
                       crs=proc.rio_crs(grid), transform=transform, nodata=float("nan")) as d:
        d.write(z, 1)
    monkeypatch.setattr(proc, "STRIP_ROWS", 64)  # force many strips with halos
    for op, idx in (("plane_tilt", 0), ("plane_roughness", 1)):
        out = tmp_path / f"{op}.tif"
        meta = LayerRaster(layer_id=op, units="x", description="t", dtype="float32",
                           nodata=float("nan"))
        derive_terrain(src, grid, out, meta, op, {"window_m": 5})
        with rasterio.open(out) as ds:
            got = ds.read(1)
        want = plane_fit(z.astype(np.float64), 1.0, 5)[idx].astype(np.float32)
        np.testing.assert_array_equal(np.isnan(got), np.isnan(want))
        assert np.nanmax(np.abs(got - want)) < 1e-5
