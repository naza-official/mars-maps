import math

import pytest
from pyproj import CRS

from marsgis import crs as mcrs


def test_iau_codes_available_in_bundled_proj():
    sphere = mcrs.mars_sphere()
    assert sphere.ellipsoid.semi_major_metre == 3_396_190.0
    assert sphere.ellipsoid.inverse_flattening == 0.0  # sphere
    og = mcrs.mars_ographic()
    assert og.ellipsoid.semi_minor_metre == pytest.approx(3_376_200.0, abs=1.0)


@pytest.mark.parametrize("code", ["EPSG:4326", "EPSG:3857", "EPSG:32633"])
def test_earth_crs_rejected(code):
    with pytest.raises(mcrs.NotAMarsCRSError):
        mcrs.assert_mars_crs(code)


def test_missing_crs_rejected():
    with pytest.raises(mcrs.NotAMarsCRSError):
        mcrs.assert_mars_crs(None)


def test_no_earth_to_mars_transformation_exists():
    """PROJ must refuse a cross-body transform; this is what protects us from silent
    Earth assumptions in third-party code (e.g. TiTiler's default coord_crs)."""
    from pyproj import Transformer
    from pyproj.exceptions import ProjError

    with pytest.raises(ProjError):
        Transformer.from_crs("EPSG:4326", mcrs.MARS_SPHERE_CODE, always_xy=True).transform(1, 1,
                                                                                         errcheck=True)


def test_region_crs_centre_and_roundtrip(region):
    c = mcrs.region_crs(region)
    assert c.geodetic_crs.equals(mcrs.mars_sphere())
    x, y = mcrs.lonlat_to_xy(c, region.crs.lon_0, region.crs.lat_0)
    assert abs(x) < 1e-6 and abs(y) < 1e-6
    for lon, lat in [(77.0, 18.0), (78.5, 19.5), (76.2, 17.2)]:
        x, y = mcrs.lonlat_to_xy(c, lon, lat)
        lon2, lat2 = mcrs.xy_to_lonlat(c, x, y)
        assert lon2 == pytest.approx(lon, abs=1e-9) and lat2 == pytest.approx(lat, abs=1e-9)


def test_region_metric_distances_match_great_circle(region):
    """1 km northward in the region CRS is 1 km on the Mars sphere (TM, k0=1)."""
    c = mcrs.region_crs(region)
    lon0, lat0 = mcrs.xy_to_lonlat(c, 0, 0)
    lon1, lat1 = mcrs.xy_to_lonlat(c, 0, 1000)
    arc = math.radians(lat1 - lat0) * mcrs.MARS_EQUATORIAL_RADIUS_M
    assert arc == pytest.approx(1000.0, rel=1e-6)


def test_tm_scale_factor_within_frame(region):
    """Point scale factor across the whole frame stays < 1 + 3e-4 (slopes valid in metres)."""
    from pyproj import Proj

    c = mcrs.region_crs(region)
    p = Proj(c)
    h = region.half_size_m
    for x, y in [(h, 0), (-h, h), (h, -h), (0, h)]:
        lon, lat = mcrs.xy_to_lonlat(c, x, y)
        f = p.get_factors(lon, lat)
        assert abs(f.meridional_scale - 1) < 3e-4
        assert abs(f.parallel_scale - 1) < 3e-4
        assert abs(f.angular_distortion) < 1e-9  # conformal


def test_proj4_export_is_lossless(region):
    c = mcrs.region_crs(region)
    again = CRS.from_proj4(mcrs.crs_to_proj4(c))
    for lon, lat in [(77.0, 18.0), (78.6, 19.7)]:
        a = mcrs.lonlat_to_xy(c, lon, lat)
        b = mcrs.lonlat_to_xy(again, lon, lat)
        assert a[0] == pytest.approx(b[0], abs=1e-6) and a[1] == pytest.approx(b[1], abs=1e-6)


def test_ocentric_ographic_latitude():
    assert mcrs.ocentric_to_ographic_lat(0.0) == 0.0
    assert mcrs.ocentric_to_ographic_lat(90.0) == 90.0
    # planetographic latitude is always poleward of planetocentric on an oblate body
    og = mcrs.ocentric_to_ographic_lat(45.0)
    assert 45.3 < og < 45.4
    assert mcrs.ographic_to_ocentric_lat(og) == pytest.approx(45.0, abs=1e-12)


def test_longitude_conventions():
    assert mcrs.normalize_lon_180(282.55) == pytest.approx(-77.45)
    assert mcrs.lon_east_360(-77.45) == pytest.approx(282.55)
    # Jezero: 77.45 E == 282.55 W
    assert mcrs.lon_west_360(77.45) == pytest.approx(282.55)
    assert mcrs.lon_west_360(0.0) == 0.0
