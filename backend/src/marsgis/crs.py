"""Mars coordinate reference systems.

Conventions used throughout marsgis (see docs/data-model.md):

* Body model: IAU 2015 Mars **sphere**, R = 3 396 190 m (IAU_2015:49900). This is what
  USGS Astrogeology analysis-ready products use, and it makes planetocentric latitude
  identical to geodetic latitude on the reference surface.
* Latitude: **planetocentric** (positive north). Planetographic latitude is only ever
  *derived* for display, using the IAU 2015 ellipsoid (IAU_2015:49901).
* Longitude: **east-positive**. Internally normalised to [-180, 180); 0–360 east and
  0–360 west are derived for display only.
* Elevation is *not* a CRS property here: rasters carry heights relative to a declared
  vertical datum (e.g. MOLA areoid), recorded in layer metadata.

Nothing in this module ever falls back to an Earth CRS. :func:`assert_mars_crs` is
called on every CRS that enters the system.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from pyproj import CRS, Transformer
from pyproj.crs import ProjectedCRS
from pyproj.crs.coordinate_operation import (
    EquidistantCylindricalConversion,
    LambertAzimuthalEqualAreaConversion,
    TransverseMercatorConversion,
)
from pyproj.crs.coordinate_system import Cartesian2DCS

from marsgis.config import RegionConfig

#: IAU 2015 Mars sphere, planetocentric, lon/lat degrees (east-positive).
MARS_SPHERE_CODE = "IAU_2015:49900"
#: IAU 2015 Mars ellipsoid, planetographic (used only to derive planetographic latitude).
MARS_OGRAPHIC_CODE = "IAU_2015:49901"

MARS_EQUATORIAL_RADIUS_M = 3_396_190.0
MARS_POLAR_RADIUS_M = 3_376_200.0

# Mars reference radii used by real products range from 3 389 500 m (mean, IAU 2009/2015 mean)
# to 3 396 190 m (equatorial). Anything outside this band is not a Mars body model.
_MARS_RADIUS_BAND_M = (3_370_000.0, 3_400_000.0)


class NotAMarsCRSError(ValueError):
    """Raised when a CRS is not defined on a Mars body model (e.g. an Earth CRS)."""


@lru_cache
def mars_sphere() -> CRS:
    return CRS.from_user_input(MARS_SPHERE_CODE)


@lru_cache
def mars_ographic() -> CRS:
    return CRS.from_user_input(MARS_OGRAPHIC_CODE)


def assert_mars_crs(crs: CRS | str | None, *, context: str = "") -> CRS:
    """Return ``crs`` as a pyproj CRS, or raise if it is missing or not a Mars CRS.

    The check is on the body model (ellipsoid radius) rather than on an authority
    code, because many planetary GeoTIFFs carry WKT without a top-level authority
    (verified: the USGS CTX DTM COGs report ``to_authority() == None``).
    """
    where = f" ({context})" if context else ""
    if crs is None or crs == "":
        raise NotAMarsCRSError(f"missing CRS{where}: refusing to assume one")
    crs = CRS.from_user_input(crs)
    ellipsoid = crs.ellipsoid
    if ellipsoid is None:
        raise NotAMarsCRSError(f"CRS has no ellipsoid{where}: {crs.name}")
    a = ellipsoid.semi_major_metre
    lo, hi = _MARS_RADIUS_BAND_M
    if not lo <= a <= hi:
        raise NotAMarsCRSError(
            f"CRS {crs.name!r}{where} has semi-major axis {a:.0f} m, not a Mars body model"
        )
    return crs


def normalize_lon_180(lon: float | np.ndarray) -> float | np.ndarray:
    """East-positive longitude to [-180, 180)."""
    return (lon + 180.0) % 360.0 - 180.0


def lon_east_360(lon_east: float) -> float:
    return lon_east % 360.0


def lon_west_360(lon_east: float) -> float:
    """East-positive longitude to the west-positive 0–360 convention (Viking/MDIM-era)."""
    return (-lon_east) % 360.0


def ocentric_to_ographic_lat(lat_ocentric_deg: float) -> float:
    """Planetocentric -> planetographic latitude on the IAU 2015 Mars ellipsoid.

    tan(φ_ographic) = (a/b)² · tan(φ_ocentric). Exact at the poles and equator.
    """
    if abs(lat_ocentric_deg) >= 90.0:
        return lat_ocentric_deg
    ratio = (MARS_EQUATORIAL_RADIUS_M / MARS_POLAR_RADIUS_M) ** 2
    return math.degrees(math.atan(ratio * math.tan(math.radians(lat_ocentric_deg))))


def ographic_to_ocentric_lat(lat_ographic_deg: float) -> float:
    if abs(lat_ographic_deg) >= 90.0:
        return lat_ographic_deg
    ratio = (MARS_POLAR_RADIUS_M / MARS_EQUATORIAL_RADIUS_M) ** 2
    return math.degrees(math.atan(ratio * math.tan(math.radians(lat_ographic_deg))))


def region_crs(region: RegionConfig) -> CRS:
    """Build the projected regional CRS on the IAU 2015 Mars sphere.

    A custom ProjectedCRS is used (rather than an IAU_2015:499xx code) because the
    IAU projected codes fix the projection centre (e.g. clon=0), which would put
    most regions far from the centre of the projection.
    """
    c = region.crs
    lon_0 = round(float(normalize_lon_180(c.lon_0)), 10)
    if c.projection == "tmerc":
        conversion = TransverseMercatorConversion(
            latitude_natural_origin=c.lat_0,
            longitude_natural_origin=lon_0,
            false_easting=0.0,
            false_northing=0.0,
            scale_factor_natural_origin=1.0,
        )
        label = "Transverse Mercator"
    elif c.projection == "eqc":
        conversion = EquidistantCylindricalConversion(
            latitude_first_parallel=c.lat_0,
            latitude_natural_origin=0.0,
            longitude_natural_origin=lon_0,
        )
        label = "Equirectangular"
    elif c.projection == "laea":
        conversion = LambertAzimuthalEqualAreaConversion(
            latitude_natural_origin=c.lat_0, longitude_natural_origin=lon_0
        )
        label = "Lambert Azimuthal Equal Area"
    else:  # pragma: no cover - guarded by the config Literal
        raise ValueError(f"unsupported projection {c.projection}")

    crs = ProjectedCRS(
        conversion=conversion,
        name=f"Mars (2015) - Sphere / Ocentric / {label} / region {region.id} "
        f"(lon_0={lon_0}, lat_0={c.lat_0})",
        geodetic_crs=mars_sphere(),
        cartesian_cs=Cartesian2DCS(),
    )
    return assert_mars_crs(crs, context=f"region {region.id}")


def crs_to_proj4(crs: CRS) -> str:
    """PROJ string for proj4js in the browser.

    pyproj warns that PROJ strings lose information; for our CRSs (sphere + one
    conversion) the round trip is exact, which is asserted in the tests.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return crs.to_proj4()


def same_crs(a: CRS | str, b: CRS | str) -> bool:
    return CRS.from_user_input(a).equals(CRS.from_user_input(b), ignore_axis_order=True)


@lru_cache(maxsize=64)
def _transformer(src_wkt: str, dst_wkt: str) -> Transformer:
    return Transformer.from_crs(CRS.from_wkt(src_wkt), CRS.from_wkt(dst_wkt), always_xy=True)


def transformer(src: CRS, dst: CRS) -> Transformer:
    """Cached, always_xy (lon, lat / x, y order) transformer."""
    return _transformer(src.to_wkt(), dst.to_wkt())


def xy_to_lonlat(crs: CRS, x: float, y: float) -> tuple[float, float]:
    """Projected region coordinates -> (lon east, lat planetocentric) on the Mars sphere."""
    lon, lat = transformer(crs, mars_sphere()).transform(x, y)
    return float(normalize_lon_180(lon)), float(lat)


def lonlat_to_xy(crs: CRS, lon: float, lat: float) -> tuple[float, float]:
    x, y = transformer(mars_sphere(), crs).transform(float(normalize_lon_180(lon)), lat)
    return float(x), float(y)
