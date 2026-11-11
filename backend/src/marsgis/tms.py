"""OGC TileMatrixSets for Mars.

Two families are served:

* One **regional TMS per region**, built in the region's projected CRS so that at
  zoom ``z_base`` tiles map 1:1 onto the canonical 20 m analysis grid. The frontend
  renders in this CRS, so what you see is exactly the analysis grid.
* The **planetcantile** IAU Mars TMSs (e.g. ``MarsGeographicSphere``), for global
  context and interoperability with USGS / MMGIS tooling.

Note on planetcantile (verified with 0.6.0): it only injects its TMSs into
``morecantile.tms`` if it is imported *before* morecantile; otherwise the default
registry silently stays Earth-only. We therefore never rely on ``morecantile.tms``
and build an explicit registry instead.
"""

from __future__ import annotations

import math
from functools import lru_cache

import morecantile
from morecantile import TileMatrixSet, TileMatrixSets

from marsgis.config import RegionConfig, list_regions, load_region
from marsgis.crs import mars_sphere, region_crs


def region_tms_id(region: RegionConfig) -> str:
    return f"Mars{region.id.capitalize()}TM"


def region_tms(region: RegionConfig) -> TileMatrixSet:
    h = region.half_size_m
    tms = TileMatrixSet.custom(
        extent=[-h, -h, h, h],
        crs=region_crs(region),
        tile_width=region.tile_size,
        tile_height=region.tile_size,
        minzoom=0,
        maxzoom=region.max_zoom,
        id=region_tms_id(region),
        title=f"Mars 2015 sphere, regional TM grid for {region.name}",
        geographic_crs=mars_sphere(),
    )
    return tms


def base_zoom(region: RegionConfig) -> int:
    """Zoom level whose cell size equals the region's base resolution."""
    z = math.log2(2 * region.half_size_m / region.tile_size / region.base_resolution_m)
    if abs(z - round(z)) > 1e-9:
        raise ValueError("region frame is not a power-of-two multiple of tile_size * resolution")
    return round(z)


@lru_cache
def build_registry() -> TileMatrixSets:
    """Explicit TMS registry: Mars planetcantile sets + one regional TMS per region."""
    from planetcantile.defaults import planetcantile_tms_paths

    mars_sets = {p.stem: p for p in planetcantile_tms_paths if p.stem.startswith("Mars")}
    regional = {}
    for rid in list_regions():
        region = load_region(rid)
        regional[region_tms_id(region)] = region_tms(region)
    # Keep WebMercatorQuad out on purpose: it is an Earth (EPSG:3857) grid.
    return TileMatrixSets({**mars_sets, **regional})


__all__ = ["base_zoom", "build_registry", "morecantile", "region_tms", "region_tms_id"]
