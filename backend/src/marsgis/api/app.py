"""HTTP API: region/layer discovery, pixel inspection and tile serving.

Tile serving reuses TiTiler (titiler.core factories) with two Mars-specific changes:
* an explicit TileMatrixSet registry (planetcantile Mars sets + regional TMSs), so
  no Earth TMS is offered and nothing depends on morecantile import order;
* datasets are addressed by ``region`` + ``layer`` ids resolved through the catalog,
  never by arbitrary paths/URLs (prevents reading arbitrary files or SSRF).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from titiler.core.errors import DEFAULT_STATUS_CODES, add_exception_handlers
from titiler.core.factory import ColorMapFactory, TilerFactory, TMSFactory

import marsgis
from marsgis.catalog import item_asset_path, load_items
from marsgis.config import get_settings, list_regions, list_vehicles, load_region, load_vehicle
from marsgis.crs import crs_to_proj4, lonlat_to_xy, mars_sphere
from marsgis.grid import region_grid
from marsgis.inspect import coordinate_report, sample_layer
from marsgis.logs import configure_logging
from marsgis.planning import PlanningError
from marsgis.routing import plan_for_region
from marsgis.tms import base_zoom, build_registry, region_tms_id

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)
log = logging.getLogger("marsgis.api")

app = FastAPI(
    title="marsgis",
    version=marsgis.__version__,
    description="Mars geospatial prototype API (IAU 2015 Mars CRSs; no Earth CRS assumed).",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                   allow_methods=["GET", "POST"],
                   allow_headers=["*"])
add_exception_handlers(app, DEFAULT_STATUS_CODES)


@app.middleware("http")
async def access_log(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    log.info("request", extra={"method": request.method, "path": request.url.path,
                               "status": response.status_code,
                               "ms": round((time.perf_counter() - t0) * 1000, 1)})
    return response


# ----------------------------------------------------------------------- helpers


def _items(region_id: str) -> dict[str, dict[str, Any]]:
    if region_id not in list_regions():
        raise HTTPException(404, f"unknown region {region_id!r}")
    return load_items(settings.catalog_dir, region_id)


def _asset_path(item: dict[str, Any]) -> Path:
    try:
        return item_asset_path(item, settings.data_dir)
    except ValueError as e:
        raise HTTPException(500, str(e)) from e


def LayerPathParams(
    region: Annotated[str, Query(description="Region id, e.g. 'jezero'")],
    layer: Annotated[str, Query(description="Layer id, e.g. 'elevation'")],
) -> str:
    """TiTiler path dependency: (region, layer) -> local COG path from the catalog."""
    items = _items(region)
    if layer not in items:
        raise HTTPException(404, f"unknown layer {layer!r} in region {region!r}")
    return str(_asset_path(items[layer]))


# ----------------------------------------------------------------------- titiler

registry = build_registry()
cog = TilerFactory(
    router_prefix="/api/cog",
    path_dependency=LayerPathParams,
    supported_tms=registry,
    add_viewer=False,  # TiTiler's viewer assumes Web Mercator / Leaflet
    add_part=False,
)
app.include_router(cog.router, prefix="/api/cog", tags=["Tiles (TiTiler)"])
app.include_router(TMSFactory(supported_tms=registry).router, prefix="/api",
                   tags=["Tile Matrix Sets"])
app.include_router(ColorMapFactory().router, prefix="/api", tags=["Colormaps"])


# ----------------------------------------------------------------------- marsgis


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, Any]:
    return {"status": "ok", "version": marsgis.__version__, "regions": list_regions()}


@app.get("/api/regions", tags=["regions"])
def regions() -> list[dict[str, Any]]:
    out = []
    for rid in list_regions():
        r = load_region(rid)
        out.append({"id": r.id, "name": r.name, "body": r.body,
                    "layers": sorted(load_items(settings.catalog_dir, rid))})
    return out


@app.get("/api/regions/{region_id}", tags=["regions"])
def region_detail(region_id: str) -> dict[str, Any]:
    """Everything a client needs to render a region in its native CRS."""
    items = _items(region_id)
    region = load_region(region_id)
    grid = region_grid(region)
    layers = []
    for layer_cfg in region.layers:
        item = items.get(layer_cfg.id)
        if item is None:
            continue
        band = item["assets"]["data"]["raster:bands"][0]
        layers.append({
            "id": layer_cfg.id,
            "title": layer_cfg.title,
            "description": layer_cfg.description,
            "units": layer_cfg.units,
            "version": item["properties"]["marsgis:version"],
            "bounds_xy": item["properties"]["proj:bbox"],
            "resolution_m": band["spatial_resolution"],
            "dtype": band["data_type"],
            "nodata": band["nodata"],
            "statistics": band["statistics"],
            "render": layer_cfg.render.model_dump(),
            "categories": [c.model_dump() for c in layer_cfg.categories],
            "footprint_lonlat": item["geometry"],
        })
    return {
        "id": region.id,
        "name": region.name,
        "body": region.body,
        "crs": {
            "name": grid.crs.name,
            "wkt2": grid.crs.to_wkt(),
            "proj4": crs_to_proj4(grid.crs),
            "geographic_proj4": crs_to_proj4(mars_sphere()),
            "conventions": {"latitude": "planetocentric", "longitude": "east-positive",
                            "body_model": "IAU 2015 Mars sphere R=3396190 m (IAU_2015:49900)"},
        },
        "frame_xy": list(grid.frame_bounds),
        "center_lonlat": [region.crs.lon_0, region.crs.lat_0],
        "base_resolution_m": grid.resolution,
        "tms": {"id": region_tms_id(region), "base_zoom": base_zoom(region),
                "max_zoom": region.max_zoom, "tile_size": region.tile_size},
        "layers": layers,
        "planning": {vid: cfg.model_dump() for vid, cfg in region.planning.items()},
    }


@app.get("/api/regions/{region_id}/layers/{layer_id}/stac", tags=["regions"])
def layer_stac(region_id: str, layer_id: str) -> dict[str, Any]:
    items = _items(region_id)
    if layer_id not in items:
        raise HTTPException(404, f"unknown layer {layer_id!r}")
    return items[layer_id]


@app.get("/api/regions/{region_id}/inspect", tags=["regions"])
def inspect(
    region_id: str,
    x: Annotated[float | None, Query(description="Easting in the region CRS (m)")] = None,
    y: Annotated[float | None, Query(description="Northing in the region CRS (m)")] = None,
    lon: Annotated[float | None, Query(description="Longitude, deg east (planetocentric)")] = None,
    lat: Annotated[float | None, Query(description="Latitude, deg planetocentric")] = None,
) -> dict[str, Any]:
    """Values of every layer at one location, with the location in several conventions.

    Give either (x, y) in the region CRS or (lon, lat) planetocentric east-positive.
    """
    items = _items(region_id)
    grid = region_grid(load_region(region_id))
    if x is not None and y is not None:
        pass
    elif lon is not None and lat is not None:
        if not -90 <= lat <= 90:
            raise HTTPException(422, "lat must be within [-90, 90] (planetocentric)")
        x, y = lonlat_to_xy(grid.crs, lon, lat)
    else:
        raise HTTPException(422, "provide x & y (region CRS metres) or lon & lat")
    left, bottom, right, top = grid.frame_bounds
    if not (left <= x <= right and bottom <= y <= top):
        raise HTTPException(422, "location is outside the region frame")
    region = load_region(region_id)
    values = {}
    for layer_cfg in region.layers:
        if layer_cfg.id in items:
            values[layer_cfg.id] = {
                "units": layer_cfg.units,
                **sample_layer(_asset_path(items[layer_cfg.id]), x, y),
            }
    return {"region": region_id, "location": coordinate_report(grid, x, y), "layers": values}


# ----------------------------------------------------------------------- vehicles / routes


@app.get("/api/vehicles", tags=["routes"])
def vehicles() -> list[dict[str, Any]]:
    return [{"id": v.id, "name": v.name, "status": v.status}
            for v in (load_vehicle(vid) for vid in list_vehicles())]


@app.get("/api/vehicles/{vehicle_id}", tags=["routes"])
def vehicle_detail(vehicle_id: str) -> dict[str, Any]:
    """All mobility parameters with value, unit, basis (published/derived/assumption), source."""
    if vehicle_id not in list_vehicles():
        raise HTTPException(404, f"unknown vehicle {vehicle_id!r}")
    return load_vehicle(vehicle_id).model_dump()


#: Route planning is CPU- and memory-heavy (dijkstra: ~320 B/cell, up to ~2.5 GB per
#: request at the 8 M-cell cap). Bound concurrent plans so parallel requests cannot
#: exhaust the container's memory; excess requests get 503 instead of queueing forever.
_PLANNING_SLOTS = threading.BoundedSemaphore(2)


class Waypoint(BaseModel):
    lon: float | None = Field(None, description="deg east, planetocentric")
    lat: float | None = Field(None, description="deg, planetocentric")
    x: float | None = Field(None, description="region CRS easting (m)")
    y: float | None = Field(None, description="region CRS northing (m)")


class RouteRequest(BaseModel):
    waypoints: list[Waypoint] = Field(min_length=2, max_length=20,
                                      description="start, optional via points, destination")
    vehicle: str = "perseverance"
    engine: Literal["dijkstra", "mcp"] = "dijkstra"
    margin_m: float | None = Field(None, ge=0, le=5000,
                                   description="planning window margin around the waypoints")


@app.post("/api/regions/{region_id}/routes", tags=["routes"])
def plan(region_id: str, req: RouteRequest) -> dict[str, Any]:
    """Minimum-cost route on the vehicle's numerical cost raster (never on rendered tiles).

    Returns geometry (region CRS and lon/lat), totals (distance, risk-weighted objective,
    estimated drive time, elevation gain/loss, max tilt/roughness), per-leg results,
    per-vertex profiles, class runs, warnings and the versions of every input layer.
    """
    if region_id not in list_regions():
        raise HTTPException(404, f"unknown region {region_id!r}")
    wps = [{k: v for k, v in w.model_dump().items() if v is not None} for w in req.waypoints]
    if not _PLANNING_SLOTS.acquire(timeout=30):
        raise HTTPException(503, "route planner busy; retry shortly")
    try:
        return plan_for_region(region_id, req.vehicle, wps, engine=req.engine,
                               margin_m=req.margin_m)
    except PlanningError as e:
        status = 404 if e.code in {"unknown_vehicle", "layers_missing"} else 422
        raise HTTPException(status, {"code": e.code, "message": str(e),
                                     **{k: v for k, v in e.details.items()}}) from e
    finally:
        _PLANNING_SLOTS.release()
