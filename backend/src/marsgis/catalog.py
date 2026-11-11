"""Dataset metadata and discovery: a static STAC catalog per region.

Layout (all under data/catalog/<region>/):
    catalog.json                    STAC Catalog (root)
    <layer>/<layer>.json            STAC Item for the *current* version of the layer

Each Item records the projection (`proj:*`), raster band info (`raster:bands`), Mars
target (`ssys:targets`), the content-addressed version and full provenance. STAC
geometry/bbox are planetocentric east-positive degrees on the Mars sphere — the
convention used by the USGS Astrogeology STAC API — *not* WGS84.

A static catalog keeps the prototype dependency-free; the Items are valid input for
stac-fastapi-pgstac if/when a searchable STAC API is needed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pystac
import rasterio
from pyproj import CRS

from marsgis.crs import transformer, mars_sphere

log = logging.getLogger(__name__)

STAC_EXTENSIONS = [
    "https://stac-extensions.github.io/projection/v2.0.0/schema.json",
    "https://stac-extensions.github.io/raster/v1.1.0/schema.json",
    "https://stac-extensions.github.io/ssys/v1.1.0/schema.json",
    "https://stac-extensions.github.io/processing/v1.2.0/schema.json",
]


def footprint_lonlat(path: Path) -> tuple[list[float], dict[str, Any]]:
    """Footprint of a raster as a lon/lat polygon on the Mars sphere (densified edges)."""
    with rasterio.open(path) as src:
        left, bottom, right, top = src.bounds
        crs = CRS.from_wkt(src.crs.to_wkt())
    n = 16
    ring_xy = (
        [(left + (right - left) * i / n, top) for i in range(n)]
        + [(right, top - (top - bottom) * i / n) for i in range(n)]
        + [(right - (right - left) * i / n, bottom) for i in range(n)]
        + [(left, bottom + (top - bottom) * i / n) for i in range(n)]
    )
    tr = transformer(crs, mars_sphere())
    ring = [list(tr.transform(x, y)) for x, y in ring_xy]
    ring.append(ring[0])
    lons, lats = [p[0] for p in ring], [p[1] for p in ring]
    return [min(lons), min(lats), max(lons), max(lats)], {"type": "Polygon", "coordinates": [ring]}


def build_item(*, region_id: str, layer_cfg: Any, cog_path: Path, version: str,
               stats: dict[str, Any], provenance: dict[str, Any], data_root: Path) -> pystac.Item:
    bbox, geometry = footprint_lonlat(cog_path)
    with rasterio.open(cog_path) as src:
        crs_wkt = CRS.from_wkt(src.crs.to_wkt()).to_wkt()  # pyproj default: WKT2_2019
        proj = {
            "proj:code": None,  # no registered code for a regional Mars CRS
            "proj:wkt2": crs_wkt,
            "proj:shape": [src.height, src.width],
            "proj:transform": list(src.transform)[:6],
            "proj:bbox": list(src.bounds),
        }
        nodata = src.nodata
        dtype = src.dtypes[0]
        res = src.res[0]
    nodata_json: Any = "nan" if nodata is not None and nodata != nodata else nodata
    item = pystac.Item(
        id=f"{region_id}-{layer_cfg.id}",
        geometry=geometry,
        bbox=bbox,
        datetime=None,
        properties={
            "title": layer_cfg.title,
            "description": layer_cfg.description,
            "start_datetime": "1970-01-01T00:00:00Z",  # acquisition time unknown/irrelevant for derived mosaics
            "end_datetime": datetime.now(UTC).isoformat(),
            "ssys:targets": ["Mars"],
            **proj,
            "gsd": res,
            "marsgis:region": region_id,
            "marsgis:layer": layer_cfg.id,
            "marsgis:version": version,
            "marsgis:units": layer_cfg.units,
            "marsgis:render": layer_cfg.render.model_dump(),
            "processing:lineage": provenance.get("lineage"),
            "processing:software": provenance.get("software"),
            "marsgis:provenance": provenance,
        },
        stac_extensions=STAC_EXTENSIONS,
    )
    rel = cog_path.relative_to(data_root)
    item.add_asset(
        "data",
        pystac.Asset(
            href=f"/data/{rel.as_posix()}",
            media_type=pystac.MediaType.COG,
            roles=["data"],
            extra_fields={
                "raster:bands": [{
                    "data_type": dtype,
                    "nodata": nodata_json,
                    "unit": layer_cfg.units,
                    "spatial_resolution": res,
                    "statistics": stats,
                }]
            },
        ),
    )
    return item


def write_item(catalog_dir: Path, region_id: str, item: pystac.Item) -> Path:
    region_dir = catalog_dir / region_id
    layer_id = item.properties["marsgis:layer"]
    path = region_dir / layer_id / f"{layer_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    d = item.to_dict(include_self_link=False, transform_hrefs=False)
    path.write_text(json.dumps(d, indent=2))
    _write_root(region_dir, region_id)
    return path


def _write_root(region_dir: Path, region_id: str) -> None:
    links = [{"rel": "root", "href": "./catalog.json", "type": "application/json"}]
    for p in sorted(region_dir.glob("*/*.json")):
        links.append({"rel": "item", "href": f"./{p.relative_to(region_dir).as_posix()}",
                      "type": "application/geo+json"})
    root = {
        "type": "Catalog",
        "stac_version": pystac.get_stac_version(),
        "id": f"marsgis-{region_id}",
        "description": f"Processed Mars layers for region {region_id}",
        "links": links,
    }
    (region_dir / "catalog.json").write_text(json.dumps(root, indent=2))


def load_items(catalog_dir: Path, region_id: str) -> dict[str, dict[str, Any]]:
    """Current Items of a region keyed by layer id (plain dicts, cheap to serve)."""
    out: dict[str, dict[str, Any]] = {}
    for p in sorted((catalog_dir / region_id).glob("*/*.json")):
        d = json.loads(p.read_text())
        out[d["properties"]["marsgis:layer"]] = d
    return out
