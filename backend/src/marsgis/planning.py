"""Grid route planning on numerical cost rasters (independent of FastAPI/TiTiler/UI).

Movement model (docs/path-planning.md):

* nodes are the cell centres of the cost raster; 8-connected moves;
* a move between adjacent cells a, b of length L (res or res*sqrt 2) costs
  L * (c_a + c_b) / 2 - the standard raster least-cost-path discretisation (identical to
  scikit-image ``MCP_Geometric`` and to GIS cost-distance tools);
* cells whose cost is NaN/inf (hazard, unknown) are never entered;
* **no corner cutting**: a diagonal move is allowed only if both orthogonally adjacent
  cells are traversable (otherwise the vehicle footprint would sweep across the corner
  of an impassable cell).

Engines (both are established, tested implementations of Dijkstra's algorithm):

* ``dijkstra`` (default) - ``scipy.sparse.csgraph.dijkstra`` on an explicit graph built
  from the raster; enforces the corner-cutting rule.
* ``mcp`` - ``skimage.graph.MCP_Geometric``. Lower memory for large windows, but cannot
  forbid corner cutting; corner cuts in its result are counted and reported.

On graphs without corner-cutting opportunities both return the same optimal cost
(tests/test_planning.py), which cross-validates the implementations.
"""

from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
import rasterio
from pyproj import CRS
from rasterio.windows import Window
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from skimage.graph import MCP_Geometric

from marsgis.crs import mars_sphere, normalize_lon_180, transformer
from marsgis.traversability import CLASS_INFO, TClass

Engine = Literal["dijkstra", "mcp"]

#: Mars surface gravity, NASA Mars Fact Sheet (nssdc.gsfc.nasa.gov/planetary/factsheet/marsfact.html)
MARS_GRAVITY_M_S2 = 3.71

_NEIGHBOURS = ((0, 1), (1, 0), (1, 1), (1, -1))  # half of the 8-neighbourhood (undirected)


class PlanningError(ValueError):
    """A request that cannot be planned; ``code`` is machine-readable."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


@dataclass(frozen=True)
class PlanningLayers:
    cost: Path  # objective (s/m), NaN = not traversable
    time: Path  # physical traverse time (s/m)
    classes: Path  # traversability class (uint8)
    samples: dict[str, Path] = field(default_factory=dict)  # sampled along the route


@dataclass
class Window2D:
    window: Window
    transform: rasterio.Affine
    res: float
    crs: CRS
    raster_shape: tuple[int, int]

    def at_edge(self, r: int, c: int) -> bool:
        """True if (r, c) is on a window edge that is not also the raster edge."""
        w = self.window
        h_, w_ = int(w.height), int(w.width)
        rh, rw = self.raster_shape
        return ((r == 0 and w.row_off > 0) or (c == 0 and w.col_off > 0)
                or (r == h_ - 1 and w.row_off + h_ < rh) or (c == w_ - 1 and w.col_off + w_ < rw))


# ----------------------------------------------------------------------------- engines


def _graph(cost: np.ndarray, res: float) -> tuple[csr_matrix, np.ndarray]:
    """Undirected 8-connected graph over traversable cells, corner cutting excluded."""
    h, w = cost.shape
    valid = np.isfinite(cost) & (cost > 0)
    index = np.full((h, w), -1, dtype=np.int64)
    n = int(valid.sum())
    index[valid] = np.arange(n)
    rows, cols, weights = [], [], []
    for dr, dc in _NEIGHBOURS:
        rs, rt = slice(0, h - dr), slice(dr, h)
        cs, ct = slice(max(0, -dc), w - max(0, dc)), slice(max(0, dc), w - max(0, -dc))
        ok = valid[rs, cs] & valid[rt, ct]
        if dr and dc:  # diagonal: both cells sharing the corner must be traversable
            ok &= valid[rs, ct] & valid[rt, cs]
        length = math.hypot(dr, dc) * res
        wgt = length * 0.5 * (cost[rs, cs] + cost[rt, ct])
        rows.append(index[rs, cs][ok])
        cols.append(index[rt, ct][ok])
        weights.append(wgt[ok])
    g = csr_matrix((np.concatenate(weights), (np.concatenate(rows), np.concatenate(cols))),
                   shape=(n, n))
    return g, index


def _path_dijkstra(graph: csr_matrix, index: np.ndarray, start: tuple[int, int],
                   goal: tuple[int, int]) -> tuple[list[tuple[int, int]], float, int]:
    s, g = int(index[start]), int(index[goal])
    dist, pred = dijkstra(graph, directed=False, indices=s, return_predecessors=True)
    if not np.isfinite(dist[g]):
        return [], math.inf, int(np.isfinite(dist).sum())
    cells_of = np.argwhere(index >= 0)  # node id -> (r, c), same order as index assignment
    node, path = g, []
    while node != s:
        path.append(node)
        node = int(pred[node])
    path.append(s)
    path.reverse()
    return [tuple(map(int, cells_of[p])) for p in path], float(dist[g]), int(np.isfinite(dist).sum())


def _path_mcp(cost: np.ndarray, res: float, start: tuple[int, int], goal: tuple[int, int]
              ) -> tuple[list[tuple[int, int]], float, int]:
    c = np.where(np.isfinite(cost) & (cost > 0), cost, np.inf)
    mcp = MCP_Geometric(c, fully_connected=True)
    cum, _ = mcp.find_costs([start], [goal], find_all_ends=False)
    if not np.isfinite(cum[goal]):
        return [], math.inf, int(np.isfinite(cum).sum())
    path = [tuple(map(int, p)) for p in mcp.traceback(goal)]
    return path, float(cum[goal]) * res, int(np.isfinite(cum).sum())


# ----------------------------------------------------------------------------- planning


def _planning_window(src: rasterio.io.DatasetReader, pts: list[tuple[float, float]],
                     margin_m: float, max_cells: int) -> Window2D:
    rc = []
    for i, (x, y) in enumerate(pts):
        r, c = src.index(x, y, op=math.floor)
        if not (0 <= r < src.height and 0 <= c < src.width):
            raise PlanningError("outside_coverage",
                                f"waypoint {i} ({x:.1f}, {y:.1f}) is outside the coverage "
                                "of the planning layers", waypoint=i)
        rc.append((r, c))
    res = src.res[0]
    m = math.ceil(max(margin_m, 0.0) / res)
    rows, cols = [p[0] for p in rc], [p[1] for p in rc]
    r0, r1 = max(min(rows) - m, 0), min(max(rows) + m + 1, src.height)
    c0, c1 = max(min(cols) - m, 0), min(max(cols) + m + 1, src.width)
    if (r1 - r0) * (c1 - c0) > max_cells:
        raise PlanningError("window_too_large",
                            f"planning window {(c1 - c0)}x{(r1 - r0)} cells exceeds the limit "
                            f"of {max_cells}; reduce margin_m or plan shorter legs",
                            cells=(r1 - r0) * (c1 - c0), max_cells=max_cells)
    win = Window(c0, r0, c1 - c0, r1 - r0)
    return Window2D(window=win, transform=src.window_transform(win), res=res,
                    crs=CRS.from_wkt(src.crs.to_wkt()), raster_shape=(src.height, src.width))


def _class_label(k: int) -> str:
    return CLASS_INFO[TClass(k)][0] if k in {c.value for c in TClass} else "outside data"


def _read(path: Path, w: Window2D) -> np.ndarray:
    """Planning-window block of a layer that must share the cost raster's grid exactly."""
    with rasterio.open(path) as src:
        if (not src.window_transform(w.window).almost_equals(w.transform)
                or (src.height, src.width) != w.raster_shape):
            raise PlanningError("grid_mismatch", f"{path.name} is not on the planning grid")
        arr = src.read(1, window=w.window, masked=True)
    if np.issubdtype(arr.dtype, np.integer):
        return arr.filled(0)
    return arr.astype(np.float64).filled(np.nan)


def _sample(path: Path, xs: np.ndarray, ys: np.ndarray) -> list[float | None]:
    """Values of any aligned layer at the given cell-centre coordinates (no resampling)."""
    with rasterio.open(path) as src:
        rows, cols = rasterio.transform.rowcol(src.transform, xs, ys, op=math.floor)
        rows, cols = np.asarray(rows), np.asarray(cols)
        inside = (rows >= 0) & (rows < src.height) & (cols >= 0) & (cols < src.width)
        out: list[float | None] = [None] * len(xs)
        if inside.any():
            r0, r1 = rows[inside].min(), rows[inside].max() + 1
            c0, c1 = cols[inside].min(), cols[inside].max() + 1
            block = src.read(1, window=Window(c0, r0, c1 - c0, r1 - r0), masked=True)
            for i in np.flatnonzero(inside):
                v = block[rows[i] - r0, cols[i] - c0]
                out[i] = None if np.ma.is_masked(v) or not np.isfinite(v) else float(v)
    return out


def _runs(classes: np.ndarray, along: np.ndarray) -> list[dict[str, Any]]:
    """Contiguous stretches of equal class along the route (explainability)."""
    runs, start = [], 0
    for i in range(1, len(classes) + 1):
        if i == len(classes) or classes[i] != classes[start]:
            c = int(classes[start])
            runs.append({"class": c, "label": _class_label(c),
                         "from_m": float(along[start]),
                         "to_m": float(along[min(i, len(classes) - 1)])})
            start = i
    return runs


def plan_route(layers: PlanningLayers, waypoints_xy: list[tuple[float, float]], *,
               engine: Engine = "dijkstra", margin_m: float = 300.0,
               max_cells: int = 8_000_000, vehicle_mass_kg: float | None = None
               ) -> dict[str, Any]:
    """Minimum-cost route through ``waypoints_xy`` (region CRS metres), leg by leg.

    Returns a JSON-serialisable dict: geometry (region CRS and lon/lat), totals, per-leg
    results, per-vertex profiles (distance, cumulative cost/time, class, sampled layers),
    class runs, and warnings. Raises :class:`PlanningError` for unplannable requests.
    """
    if len(waypoints_xy) < 2:
        raise PlanningError("too_few_waypoints", "need at least a start and a destination")
    t0 = _time.perf_counter()
    with rasterio.open(layers.cost) as src:
        w = _planning_window(src, waypoints_xy, margin_m, max_cells)
    cost = _read(layers.cost, w)
    tsec = _read(layers.time, w)
    cls = _read(layers.classes, w)
    inv = ~w.transform
    pts_rc = []
    for i, (x, y) in enumerate(waypoints_xy):
        col, row = inv @ (x, y)
        r, c = int(math.floor(row)), int(math.floor(col))
        if not np.isfinite(cost[r, c]):
            k = int(cls[r, c])
            raise PlanningError("waypoint_not_traversable",
                                f"waypoint {i} is on non-traversable terrain ({_class_label(k)})",
                                waypoint=i, terrain_class=k)
        pts_rc.append((r, c))

    graph = index = None
    if engine == "dijkstra":
        graph, index = _graph(cost, w.res)
    path: list[tuple[int, int]] = []
    legs = []
    for i in range(len(pts_rc) - 1):
        a, b = pts_rc[i], pts_rc[i + 1]
        if engine == "dijkstra":
            leg, leg_cost, settled = _path_dijkstra(graph, index, a, b)
        elif engine == "mcp":
            leg, leg_cost, settled = _path_mcp(cost, w.res, a, b)
        else:
            raise PlanningError("bad_engine", f"unknown engine {engine!r}")
        if not leg:
            raise PlanningError("no_path",
                                f"no traversable route for leg {i} within the planning window "
                                f"(margin {margin_m:.0f} m); the destination may be enclosed "
                                "by hazards/unknown terrain, or a detour lies outside the "
                                "window", leg=i, margin_m=margin_m)
        legs.append({"leg": i, "objective_cost_s": leg_cost, "cells_settled": settled,
                     "start_index": len(path) - 1 if path else 0})
        path.extend(leg if not path else leg[1:])
        legs[-1]["end_index"] = len(path) - 1

    rr = np.array([p[0] for p in path])
    cc = np.array([p[1] for p in path])
    xs, ys = w.transform @ (cc + 0.5, rr + 0.5)
    xs, ys = np.asarray(xs), np.asarray(ys)
    step_len = np.hypot(np.diff(rr), np.diff(cc)) * w.res
    c_node, t_node = cost[rr, cc], tsec[rr, cc]
    step_cost = step_len * 0.5 * (c_node[:-1] + c_node[1:])
    step_time = step_len * 0.5 * (t_node[:-1] + t_node[1:])
    along = np.concatenate([[0.0], np.cumsum(step_len)])
    cum_cost = np.concatenate([[0.0], np.cumsum(step_cost)])
    cum_time = np.concatenate([[0.0], np.cumsum(step_time)])
    classes = cls[rr, cc].astype(int)

    diag = (np.abs(np.diff(rr)) == 1) & (np.abs(np.diff(cc)) == 1)
    corner_cuts = 0
    for k in np.flatnonzero(diag):
        if not (np.isfinite(cost[rr[k], cc[k + 1]]) and np.isfinite(cost[rr[k + 1], cc[k]])):
            corner_cuts += 1

    samples = {name: _sample(p, xs, ys) for name, p in layers.samples.items()}
    lon, lat = transformer(w.crs, mars_sphere()).transform(xs, ys)
    lon = normalize_lon_180(np.asarray(lon))

    warnings = []
    if any(w.at_edge(int(r), int(c)) for r, c in path):
        warnings.append("route touches the planning-window boundary: a cheaper route may "
                        "exist outside it; increase margin_m to check")
    if corner_cuts:
        warnings.append(f"{corner_cuts} diagonal moves cut the corner of a non-traversable "
                        "cell (engine 'mcp' cannot forbid this; use 'dijkstra')")

    length_by_class: dict[str, float] = {}
    mid_class = np.where(classes[:-1] == classes[1:], classes[:-1],
                         np.maximum(classes[:-1], classes[1:]))  # worse class of the two ends
    for k in np.unique(mid_class):
        length_by_class[_class_label(int(k))] = float(step_len[mid_class == k].sum())

    summary: dict[str, Any] = {
        "distance_m": float(along[-1]),
        "straight_line_m": float(sum(math.dist(waypoints_xy[i], waypoints_xy[i + 1])
                                     for i in range(len(waypoints_xy) - 1))),
        "objective_cost_s": float(cum_cost[-1]),
        "estimated_drive_time_s": float(cum_time[-1]),
        "estimated_drive_time_h": float(cum_time[-1] / 3600.0),
        "length_by_class_m": length_by_class,
        "corner_cuts": corner_cuts,
        "vertices": len(path),
    }
    elev = samples.get("elevation")
    if elev and all(v is not None for v in elev):
        z = np.array(elev)
        dz = np.diff(z)
        summary.update({
            "surface_distance_m": float(np.sum(np.hypot(step_len, dz))),
            "elevation_gain_m": float(dz[dz > 0].sum()),
            "elevation_loss_m": float(-dz[dz < 0].sum()),
            "elevation_min_m": float(z.min()),
            "elevation_max_m": float(z.max()),
        })
        if vehicle_mass_kg:
            e = vehicle_mass_kg * MARS_GRAVITY_M_S2 * summary["elevation_gain_m"]
            summary["potential_energy_climb_wh"] = e / 3600.0
    for name in ("tilt", "roughness", "slope_1m"):
        vals = [v for v in samples.get(name, []) if v is not None]
        if vals:
            summary[f"{name}_max"] = float(max(vals))
            summary[f"{name}_mean"] = float(np.mean(vals))

    return {
        "engine": engine,
        "resolution_m": w.res,
        "planning_window": {"col_off": int(w.window.col_off), "row_off": int(w.window.row_off),
                            "width": int(w.window.width), "height": int(w.window.height),
                            "margin_m": margin_m},
        "geometry_xy": {"type": "LineString", "coordinates": np.c_[xs, ys].round(3).tolist()},
        "geometry_lonlat": {"type": "LineString",
                            "coordinates": np.c_[lon, np.asarray(lat)].round(8).tolist()},
        "summary": summary,
        "legs": legs,
        "profile": {
            "distance_m": along.round(3).tolist(),
            "cumulative_objective_s": cum_cost.round(3).tolist(),
            "cumulative_time_s": cum_time.round(3).tolist(),
            "class": classes.tolist(),
            **{name: [None if v is None else round(v, 4) for v in vals]
               for name, vals in samples.items()},
        },
        "class_runs": _runs(classes, along),
        "warnings": warnings,
        "compute_ms": round((_time.perf_counter() - t0) * 1000, 1),
    }
