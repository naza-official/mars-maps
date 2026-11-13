"""Region-level route planning service: resolves published layers and coordinates, then
calls the framework-independent planner (planning.py). Used by the API and the CLI."""

from __future__ import annotations

from typing import Any

from marsgis.catalog import item_asset_path, load_items
from marsgis.config import get_settings, load_region, load_vehicle
from marsgis.crs import lonlat_to_xy
from marsgis.grid import region_grid
from marsgis.planning import Engine, PlanningError, PlanningLayers, plan_route


def planning_layers(region_id: str, vehicle_id: str) -> tuple[PlanningLayers, dict[str, Any]]:
    """Paths of the planning layers for (region, vehicle) and their versions (provenance)."""
    settings = get_settings()
    region = load_region(region_id)
    if vehicle_id not in region.planning:
        raise PlanningError("unknown_vehicle",
                            f"no planning configuration for vehicle {vehicle_id!r} in region "
                            f"{region_id!r} (available: {sorted(region.planning)})")
    cfg = region.planning[vehicle_id]
    items = load_items(settings.catalog_dir, region_id)
    needed = {"cost": cfg.cost, "time": cfg.time, "classes": cfg.classes, **cfg.samples}
    missing = [lid for lid in needed.values() if lid not in items]
    if missing:
        raise PlanningError("layers_missing", f"planning layers not built: {missing}")

    def path(lid: str):
        return item_asset_path(items[lid], settings.data_dir)

    layers = PlanningLayers(cost=path(cfg.cost), time=path(cfg.time),
                            classes=path(cfg.classes),
                            samples={name: path(lid) for name, lid in cfg.samples.items()})
    inputs = {role: {"layer": lid, "version": items[lid]["properties"]["marsgis:version"]}
              for role, lid in needed.items()}
    return layers, inputs


def waypoint_xy(region_id: str, wp: dict[str, float]) -> tuple[float, float]:
    """{'x','y'} in the region CRS or {'lon','lat'} planetocentric east-positive degrees."""
    if "x" in wp and "y" in wp:
        return float(wp["x"]), float(wp["y"])
    if "lon" in wp and "lat" in wp:
        if not -90 <= wp["lat"] <= 90:
            raise PlanningError("bad_waypoint", "lat must be within [-90, 90] (planetocentric)")
        grid = region_grid(load_region(region_id))
        return lonlat_to_xy(grid.crs, wp["lon"], wp["lat"])
    raise PlanningError("bad_waypoint", "each waypoint needs x,y (region CRS m) or lon,lat")


def plan_for_region(region_id: str, vehicle_id: str, waypoints: list[dict[str, float]], *,
                    engine: Engine = "dijkstra", margin_m: float | None = None
                    ) -> dict[str, Any]:
    region = load_region(region_id)
    layers, inputs = planning_layers(region_id, vehicle_id)
    cfg = region.planning[vehicle_id]
    vehicle = load_vehicle(vehicle_id)
    pts = [waypoint_xy(region_id, wp) for wp in waypoints]
    res = plan_route(layers, pts, engine=engine,
                     margin_m=cfg.default_margin_m if margin_m is None else margin_m,
                     max_cells=cfg.max_cells, vehicle_mass_kg=vehicle.mass.value)
    res.update({
        "region": region_id,
        "vehicle": {"id": vehicle.id, "name": vehicle.name, "status": vehicle.status},
        "crs": region_grid(region).crs.name,
        "waypoints_xy": [list(p) for p in pts],
        "inputs": inputs,
        "units": {"distance": "m", "objective_cost": "s (risk-weighted time)",
                  "time": "s", "elevation": "m above MOLA areoid", "tilt": "deg",
                  "roughness": "m", "energy": "Wh (potential energy of climbs only)"},
        "notes": [
            "8-connected grid paths: distances are octile approximations (straight lines "
            "at 22.5 deg are overestimated by up to 8.2 %).",
            "Vehicle parameters are experimental (see /api/vehicles); time is a model "
            "estimate, not an operational drive-time prediction.",
            "Terrain features come from an orbital DTM with ~3-5 m effective resolution; "
            "sub-metre rocks are not represented.",
        ],
    })
    return res
