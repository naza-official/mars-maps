import pytest
from affine import Affine

from marsgis.tms import base_zoom, build_registry, region_tms, region_tms_id


def test_frame(grid, region):
    assert grid.frame_bounds == (-region.half_size_m, -region.half_size_m,
                                 region.half_size_m, region.half_size_m)
    assert grid.width == grid.height == 8192


def test_snap_window_is_outward_and_aligned(grid):
    w = grid.snap_window((-1001.0, -999.0, 1001.0, 1019.0))
    t = grid.window_transform(w)
    assert grid.is_aligned(t, grid.crs)
    left, top = t.c, t.f
    right, bottom = left + w.width * grid.resolution, top - w.height * grid.resolution
    assert left <= -1001.0 and right >= 1001.0 and bottom <= -999.0 and top >= 1019.0
    assert (left, right, bottom, top) == (-1020.0, 1020.0, -1000.0, 1020.0)


def test_snap_window_outside_frame(grid):
    with pytest.raises(ValueError):
        grid.snap_window((1e7, 1e7, 1e7 + 10, 1e7 + 10))


def test_alignment_detection(grid):
    assert grid.is_aligned(Affine(20, 0, -13040, 0, -20, 52600))
    assert not grid.is_aligned(Affine(20, 0, -13045, 0, -20, 52600))  # half-pixel shift
    assert not grid.is_aligned(Affine(20.16, 0, -13040, 0, -20.16, 52600))  # source res


def test_nested_grids(grid):
    fine = grid.at_resolution(5.0)
    assert fine.width == grid.width * 4
    assert fine.is_aligned(Affine(5, 0, -13040 + 5, 0, -5, 52600))
    with pytest.raises(ValueError):
        grid.at_resolution(15.0)


def test_region_tms_matches_grid(region, grid):
    tms = region_tms(region)
    z = base_zoom(region)
    assert z == 5
    m = tms.matrix(z)
    assert m.cellSize == pytest.approx(grid.resolution)
    assert tuple(m.pointOfOrigin) == (grid.origin_x, grid.origin_y)
    # tile edges at base zoom fall on grid lattice lines
    b = tms.xy_bounds(13, 5, z)
    assert grid.is_aligned(Affine(grid.resolution, 0, b.left, 0, -grid.resolution, b.top))
    # the TMS's geographic CRS is the Mars sphere, not WGS84
    assert "Mars" in tms.geographic_crs.name


def test_registry_is_mars_only(region):
    reg = build_registry()
    ids = reg.list()
    assert region_tms_id(region) in ids
    assert "MarsGeographicSphere" in ids
    assert "WebMercatorQuad" not in ids
    assert all(i.startswith("Mars") for i in ids)
