"""One-at-a-time sensitivity of a planned route to the experimental vehicle parameters.

The published feature layers (tilt, roughness) are fixed; for each variant the vehicle
model is re-evaluated on the planning window only (in memory, written to a temporary
directory), and the route is re-planned with the same engine. Reported per variant:
distance, drive time, objective, length per class, and how far the route moved
(mean / max distance of the variant's vertices from the baseline route).
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.spatial import cKDTree

from marsgis.catalog import layer_path, load_items
from marsgis.config import DeclaredValue, VehicleConfig, get_settings, load_region, load_vehicle
from marsgis.planning import PlanningError, PlanningLayers, plan_route
from marsgis.routing import waypoint_xy
from marsgis.terrain import tilt_sigma_deg, window_cells
from marsgis.traversability import Uncertainty, evaluate

#: (parameter, values) varied one at a time around the configured baseline
VARIANTS: list[tuple[str, list[float]]] = [
    ("max_tilt", [15.0, 25.0]),
    ("nominal_tilt", [5.0, 15.0]),
    ("speed_factor_at_max_tilt", [0.25, 1.0]),
    ("max_step", [0.25, 0.5]),
    ("slow_step", [0.1, 0.3]),
    ("marginal_cost_multiplier", [1.0, 4.0]),
    ("uncertainty_k", [0.0, 2.0]),
    ("sigma_z", [0.02, 0.25]),  # relative precision; 0.25 = absolute EP used instead
]


def _with(vehicle: VehicleConfig, name: str, value: float) -> VehicleConfig:
    old: DeclaredValue = getattr(vehicle, name)
    return vehicle.model_copy(update={name: old.model_copy(update={"value": value})})


def _write(path: Path, arr: np.ndarray, profile: dict[str, Any], dtype: str, nodata) -> Path:
    with rasterio.open(path, "w", **{**profile, "dtype": dtype, "nodata": nodata}) as d:
        d.write(arr.astype(dtype), 1)
    return path


def run_sensitivity(region_id: str, vehicle_id: str,
                    lonlats: list[tuple[float, float]]) -> dict[str, Any]:
    region = load_region(region_id)
    cfg = region.planning[vehicle_id]
    base_vehicle = load_vehicle(vehicle_id)
    items = load_items(get_settings().catalog_dir, region_id)
    unc = items[cfg.cost]["properties"]["marsgis:provenance"]["uncertainty"]
    tilt_p, rough_p = layer_path(region_id, cfg.samples["tilt"]), \
        layer_path(region_id, cfg.samples["roughness"])
    pts = [waypoint_xy(region_id, {"lon": lo, "lat": la}) for lo, la in lonlats]

    with rasterio.open(tilt_p) as src:
        res = src.res[0]
        rc = [src.index(x, y, op=math.floor) for x, y in pts]
        m = math.ceil(cfg.default_margin_m / res)
        r0 = max(min(r for r, _ in rc) - m, 0)
        r1 = min(max(r for r, _ in rc) + m + 1, src.height)
        c0 = max(min(c for _, c in rc) - m, 0)
        c1 = min(max(c for _, c in rc) + m + 1, src.width)
        win = Window(c0, r0, c1 - c0, r1 - r0)
        tilt = src.read(1, window=win, masked=True).astype(np.float64).filled(np.nan)
        profile = {"driver": "GTiff", "width": int(win.width), "height": int(win.height),
                   "count": 1, "crs": src.crs, "transform": src.window_transform(win)}
    with rasterio.open(rough_p) as src:
        rough = src.read(1, window=win, masked=True).astype(np.float64).filled(np.nan)

    n = window_cells(float(unc["window_m"]), res)

    def run(vehicle: VehicleConfig, sigma_z: float, tmp: Path) -> dict[str, Any]:
        sigma = Uncertainty(tilt_deg=tilt_sigma_deg(sigma_z, n, res), roughness_m=sigma_z)
        ev = evaluate(tilt, rough, vehicle, sigma)
        layers = PlanningLayers(
            cost=_write(tmp / "cost.tif", ev.cost_s_per_m, profile, "float32", float("nan")),
            time=_write(tmp / "time.tif", ev.time_s_per_m, profile, "float32", float("nan")),
            classes=_write(tmp / "cls.tif", ev.classes, profile, "uint8", 0),
            samples={})
        # the temporary rasters *are* the planning window (bbox + default margin), so the
        # planner's own window is clipped to them: same search space as the API baseline
        return plan_route(layers, pts, margin_m=cfg.default_margin_m, max_cells=cfg.max_cells)

    out: dict[str, Any] = {"region": region_id, "vehicle": vehicle_id,
                           "waypoints_lonlat": lonlats, "window_cells": int(win.width * win.height),
                           "baseline": {"sigma_z": unc["sigma_z_m"],
                                        **{name: getattr(base_vehicle, name).value
                                           for name, _ in VARIANTS if name != "sigma_z"}},
                           "variants": []}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        base = run(base_vehicle, unc["sigma_z_m"], tmp)
        tree = cKDTree(np.array(base["geometry_xy"]["coordinates"]))

        def describe(name: str, value: Any, r: dict[str, Any] | None, err: str | None = None):
            if r is None:
                return {"parameter": name, "value": value, "error": err}
            d, _ = tree.query(np.array(r["geometry_xy"]["coordinates"]))
            s = r["summary"]
            return {"parameter": name, "value": value,
                    "distance_m": round(s["distance_m"], 1),
                    "drive_time_h": round(s["estimated_drive_time_h"], 3),
                    "objective_s": round(s["objective_cost_s"], 1),
                    "length_by_class_m": {k: round(v, 1)
                                          for k, v in s["length_by_class_m"].items()},
                    "route_shift_mean_m": round(float(d.mean()), 1),
                    "route_shift_max_m": round(float(d.max()), 1)}

        out["variants"].append(describe("baseline", None, base))
        for name, values in VARIANTS:
            for value in values:
                try:
                    if name == "sigma_z":
                        r = run(base_vehicle, value, tmp)
                    else:
                        r = run(_with(base_vehicle, name, value), unc["sigma_z_m"], tmp)
                    out["variants"].append(describe(name, value, r))
                except (PlanningError, ValueError) as e:  # e.g. waypoint becomes hazard
                    out["variants"].append(describe(name, value, None, str(e)))
    return out
