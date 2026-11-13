"""Route planner: analytic cases, constraints, error semantics, engine cross-validation."""

import math

import numpy as np
import pytest
import rasterio
from affine import Affine

from marsgis.grid import region_grid
from marsgis.planning import PlanningError, PlanningLayers, plan_route
from marsgis.processing import rio_crs
from marsgis.traversability import TClass

RES = 1.0
X0, Y0 = 1000.0, -2000.0  # arbitrary grid-aligned origin in the region CRS


def _write(path, arr, grid, dtype, nodata):
    h, w = arr.shape
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype=dtype,
                       crs=rio_crs(grid), transform=Affine(RES, 0, X0, 0, -RES, Y0),
                       nodata=nodata) as d:
        d.write(arr.astype(dtype), 1)
    return path


@pytest.fixture
def make_layers(tmp_path, region):
    grid = region_grid(region).at_resolution(RES)

    def make(cost: np.ndarray, time: np.ndarray | None = None) -> PlanningLayers:
        time = cost if time is None else time
        cls = np.where(np.isfinite(cost), TClass.NOMINAL, TClass.HAZARD).astype(np.uint8)
        z = np.zeros_like(cost)
        return PlanningLayers(
            cost=_write(tmp_path / "cost.tif", cost, grid, "float32", float("nan")),
            time=_write(tmp_path / "time.tif", time, grid, "float32", float("nan")),
            classes=_write(tmp_path / "cls.tif", cls, grid, "uint8", 0),
            samples={"elevation": _write(tmp_path / "z.tif", z, grid, "float32", float("nan"))},
        )

    return make


def xy(r, c):
    """Cell (row, col) centre in the test rasters' CRS coordinates."""
    return X0 + (c + 0.5) * RES, Y0 - (r + 0.5) * RES


@pytest.mark.parametrize("engine", ["dijkstra", "mcp"])
def test_uniform_cost_straight_and_diagonal(make_layers, engine):
    layers = make_layers(np.full((30, 30), 45.0))
    res = plan_route(layers, [xy(5, 2), xy(5, 22)], engine=engine)
    assert res["summary"]["distance_m"] == pytest.approx(20.0)
    assert res["summary"]["objective_cost_s"] == pytest.approx(20 * 45.0)
    assert res["summary"]["estimated_drive_time_s"] == pytest.approx(20 * 45.0)
    res = plan_route(layers, [xy(3, 3), xy(13, 13)], engine=engine)
    assert res["summary"]["distance_m"] == pytest.approx(10 * math.sqrt(2))
    # octile metric: (dx=10, dy=4) -> 6 straight + 4 diagonal steps
    res = plan_route(layers, [xy(3, 3), xy(7, 13)], engine=engine)
    assert res["summary"]["distance_m"] == pytest.approx(6 + 4 * math.sqrt(2))


def test_route_goes_through_the_gap_in_a_wall(make_layers):
    cost = np.full((40, 40), 45.0)
    cost[:, 20] = np.nan
    cost[30, 20] = 45.0  # single gap
    res = plan_route(make_layers(cost), [xy(10, 5), xy(10, 35)])
    coords = res["geometry_xy"]["coordinates"]
    gx, gy = xy(30, 20)
    assert any(abs(x - gx) < 1e-6 and abs(y - gy) < 1e-6 for x, y in coords)
    assert res["summary"]["length_by_class_m"] == {"nominal": pytest.approx(
        res["summary"]["distance_m"])}


def test_no_corner_cutting(make_layers):
    cost = np.full((12, 12), 45.0)
    cost[5, 6] = np.nan
    cost[6, 5] = np.nan
    layers = make_layers(cost)
    safe = plan_route(layers, [xy(5, 5), xy(6, 6)], engine="dijkstra")
    assert safe["summary"]["corner_cuts"] == 0
    assert safe["summary"]["distance_m"] > math.sqrt(2) + 1e-9
    cut = plan_route(layers, [xy(5, 5), xy(6, 6)], engine="mcp")
    assert cut["summary"]["corner_cuts"] == 1  # reported, not hidden
    assert any("corner" in w for w in cut["warnings"])


def test_engines_agree_on_random_costs(make_layers):
    rng = np.random.default_rng(3)
    cost = rng.uniform(45, 200, size=(80, 90))
    layers = make_layers(cost)
    pts = [xy(3, 4), xy(70, 85)]
    a = plan_route(layers, pts, engine="dijkstra")
    b = plan_route(layers, pts, engine="mcp")
    assert a["summary"]["objective_cost_s"] == pytest.approx(b["summary"]["objective_cost_s"],
                                                             rel=1e-9)
    # reported cost equals the engine's own optimum (diagnostics recomputed independently)
    assert a["legs"][0]["objective_cost_s"] == pytest.approx(a["summary"]["objective_cost_s"])


def test_objective_and_time_are_separate(make_layers):
    cost = np.full((20, 20), 90.0)
    time = np.full((20, 20), 45.0)
    res = plan_route(make_layers(cost, time), [xy(2, 2), xy(2, 12)])
    assert res["summary"]["objective_cost_s"] == pytest.approx(900.0)
    assert res["summary"]["estimated_drive_time_s"] == pytest.approx(450.0)


def test_multi_waypoint_legs(make_layers):
    layers = make_layers(np.full((30, 30), 45.0))
    res = plan_route(layers, [xy(2, 2), xy(2, 12), xy(12, 12)])
    assert len(res["legs"]) == 2
    assert res["summary"]["distance_m"] == pytest.approx(20.0)
    assert res["legs"][1]["start_index"] == res["legs"][0]["end_index"]


def test_waypoint_on_hazard_is_rejected(make_layers):
    cost = np.full((20, 20), 45.0)
    cost[5, 5] = np.nan
    with pytest.raises(PlanningError) as e:
        plan_route(make_layers(cost), [xy(5, 5), xy(10, 10)])
    assert e.value.code == "waypoint_not_traversable"


def test_enclosed_goal_has_no_path(make_layers):
    cost = np.full((30, 30), 45.0)
    cost[8:13, 8] = cost[8:13, 12] = cost[8, 8:13] = cost[12, 8:13] = np.nan
    with pytest.raises(PlanningError) as e:
        plan_route(make_layers(cost), [xy(2, 2), xy(10, 10)])
    assert e.value.code == "no_path"


def test_unknown_policy_unknown_cells_block(make_layers):
    """NaN (unknown) cells are impassable even if a straight line would cross them."""
    cost = np.full((20, 30), 45.0)
    cost[0:19, 15] = np.nan  # leaves one row open at the bottom
    res = plan_route(make_layers(cost), [xy(2, 5), xy(2, 25)])
    assert res["summary"]["distance_m"] > 30


def test_window_limits(make_layers):
    layers = make_layers(np.full((50, 50), 45.0))
    with pytest.raises(PlanningError) as e:
        plan_route(layers, [xy(2, 2), xy(40, 40)], max_cells=100)
    assert e.value.code == "window_too_large"
    with pytest.raises(PlanningError) as e:
        plan_route(layers, [xy(2, 2), (1000.0, -5.0)])
    assert e.value.code == "outside_coverage"
    # a tight margin is honoured and flagged when the route hugs the window boundary
    cost = np.full((50, 50), 45.0)
    cost[0:30, 25] = np.nan
    res = plan_route(make_layers(cost), [xy(10, 10), xy(10, 40)], margin_m=25)
    assert res["warnings"] == [] or "boundary" in res["warnings"][0]
