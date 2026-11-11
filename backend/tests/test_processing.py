"""Processing tests on *synthetic* rasters with analytically known values.

These fixtures are test inputs only; they are never published as data layers.
"""

import math

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS as RioCRS

from marsgis.crs import MARS_EQUATORIAL_RADIUS_M, lonlat_to_xy, xy_to_lonlat
from marsgis.processing import LayerRaster, hillshade, slope_degrees, warp_to_grid

EQC = "IAU_2015:49910"  # source-like CRS: Mars sphere equirectangular, clon=0
NODATA = -32767.0


def _latitude_ramp(path, nodata_block=True):
    """Raster in IAU_2015:49910 whose value equals planetocentric latitude (degrees)."""
    res = 100.0
    deg_per_m = 180.0 / (math.pi * MARS_EQUATORIAL_RADIUS_M)
    x0 = 76.9 / deg_per_m
    y0 = 19.0 / deg_per_m
    w, h = 600, 600
    rows = np.arange(h)
    lat = (y0 - (rows + 0.5) * res) * deg_per_m
    data = np.repeat(lat[:, None], w, axis=1).astype(np.float32)
    if nodata_block:
        data[200:260, 300:360] = NODATA
    profile = dict(driver="GTiff", width=w, height=h, count=1, dtype="float32",
                   crs=RioCRS.from_user_input(EQC), transform=Affine(res, 0, x0, 0, -res, y0),
                   nodata=NODATA)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


def _meta():
    return LayerRaster(layer_id="t", units="deg", description="test", dtype="float32",
                       nodata=float("nan"))


def test_warp_preserves_geolocation_and_nodata(tmp_path, grid):
    src = _latitude_ramp(tmp_path / "src.tif")
    out = tmp_path / "out.tif"
    warp_to_grid(src, grid, out, _meta(), "bilinear")
    with rasterio.open(out) as ds:
        assert grid.is_aligned(ds.transform)
        assert ds.nodata != ds.nodata  # NaN
        assert ds.units == ("deg",)
        arr = ds.read(1)
        # value == latitude of the cell centre (linear field, bilinear => near exact)
        for row, col in [(800, 300), (1500, 1000), (2000, 700)]:
            x, y = ds.xy(row, col)
            _, lat = xy_to_lonlat(grid.crs, x, y)
            if np.isfinite(arr[row, col]):
                assert arr[row, col] == pytest.approx(lat, abs=2e-4)
        # source nodata became NaN, never 0 and never the sentinel
        assert not np.any(arr == NODATA)
        assert not np.any(arr == 0.0)
        assert np.isnan(arr).any()


def test_warp_nodata_hole_lands_where_expected(tmp_path, grid):
    src = _latitude_ramp(tmp_path / "src.tif")
    out = tmp_path / "out.tif"
    warp_to_grid(src, grid, out, _meta(), "bilinear")
    # centre of the nodata block in source coordinates -> lon/lat -> region xy
    deg_per_m = 180.0 / (math.pi * MARS_EQUATORIAL_RADIUS_M)
    lon = (76.9 / deg_per_m + 330 * 100.0) * deg_per_m
    lat = (19.0 / deg_per_m - 230 * 100.0) * deg_per_m
    x, y = lonlat_to_xy(grid.crs, lon, lat)
    with rasterio.open(out) as ds:
        r, c = ds.index(x, y)
        assert np.isnan(ds.read(1)[r, c])


def test_warp_rejects_earth_crs(tmp_path, grid):
    p = tmp_path / "earth.tif"
    with rasterio.open(p, "w", driver="GTiff", width=4, height=4, count=1, dtype="float32",
                       crs="EPSG:4326", transform=Affine(1, 0, 0, 0, -1, 4), nodata=-1) as d:
        d.write(np.zeros((4, 4), np.float32), 1)
    from marsgis.crs import NotAMarsCRSError

    with pytest.raises(NotAMarsCRSError):
        warp_to_grid(p, grid, tmp_path / "o.tif", _meta(), "bilinear")


def test_slope_of_known_plane():
    res = 20.0
    yy, xx = np.mgrid[0:50, 0:50]
    # rises 0.1 m per metre eastward  => slope = atan(0.1)
    z = (xx * res * 0.1).astype(np.float64)
    s = slope_degrees(z, res, res)
    assert np.nanmax(np.abs(s[1:-1, 1:-1] - math.degrees(math.atan(0.1)))) < 1e-5
    # edges have incomplete 3x3 windows -> NaN, not a fabricated value
    assert np.isnan(s[0]).all() and np.isnan(s[:, -1]).all()


def test_slope_nan_propagation():
    z = np.zeros((10, 10))
    z[5, 5] = np.nan
    s = slope_degrees(z, 20, 20)
    assert np.isnan(s[4:7, 4:7]).all()
    assert s[2, 2] == 0.0


def test_hillshade_orientation():
    res = 20.0
    yy, xx = np.mgrid[0:20, 0:20]
    facing_nw = (xx - (19 - yy)) * res * 0.3  # rises to the SE, descends toward NW
    facing_se = -facing_nw
    hs_nw = hillshade(facing_nw.astype(float), res, res)[5, 5]
    hs_se = hillshade(facing_se.astype(float), res, res)[5, 5]
    assert hs_nw > hs_se  # light from azimuth 315 (NW)
    flat = hillshade(np.zeros((5, 5)), res, res)[2, 2]
    assert flat == round(1 + 254 * math.cos(math.radians(45)))
