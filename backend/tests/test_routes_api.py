"""Route planning, vehicles and the HiRISE products through the API, on the real data
(skipped if the processed data are not built)."""

import math

import numpy as np
import pytest
import rasterio

LANDING = {"lon": 77.4508, "lat": 18.4447}  # Perseverance landing site (Octavia E. Butler)
DELTA_FRONT = {"lon": 77.4150, "lat": 18.4650}


@pytest.fixture(scope="module")
def client(built_data):
    from fastapi.testclient import TestClient

    from marsgis.api.app import app

    return TestClient(app)


@pytest.fixture(scope="module")
def route(client):
    r = client.post("/api/regions/jezero/routes",
                    json={"waypoints": [LANDING, DELTA_FRONT], "vehicle": "perseverance"})
    assert r.status_code == 200, r.text
    return r.json()


def test_vehicle_parameters_exposed(client):
    assert client.get("/api/vehicles").json()[0]["id"] == "perseverance"
    v = client.get("/api/vehicles/perseverance").json()
    assert v["status"] == "experimental"
    assert v["max_tilt"]["basis"] in {"published", "derived", "assumption"}
    assert client.get("/api/vehicles/nope").status_code == 404


def test_hirise_layers_are_nested_on_the_region_grid(client):
    from marsgis.catalog import layer_path
    from marsgis.config import load_region
    from marsgis.grid import region_grid

    grid = region_grid(load_region("jezero"))
    with rasterio.open(layer_path("jezero", "hirise_elevation")) as fine, \
            rasterio.open(layer_path("jezero", "elevation")) as coarse:
        assert fine.res == (1.0, 1.0) and coarse.res == (20.0, 20.0)
        assert grid.at_resolution(1.0).is_aligned(fine.transform)
        # every 20 m cell is an exact 20x20 block of 1 m cells
        assert ((fine.transform.c - coarse.transform.c) / 1.0) % 1 == 0
    d = client.get("/api/regions/jezero").json()
    cls = next(layer for layer in d["layers"] if layer["id"] == "trav_class_perseverance")
    assert [c["value"] for c in cls["categories"]] == [1, 2, 3, 4, 5]
    assert "perseverance" in d["planning"]


def test_route_result_structure_and_consistency(route):
    s = route["summary"]
    assert route["engine"] == "dijkstra" and route["resolution_m"] == 1.0
    # path length is at least the straight line, and plausibly close to it on this floor
    assert s["straight_line_m"] <= s["distance_m"] < 1.5 * s["straight_line_m"]
    assert s["corner_cuts"] == 0
    # the route never enters hazard/unknown cells and respects the tilt limit
    assert set(route["profile"]["class"]) <= {1, 2, 3}
    assert s["tilt_max"] <= 20.0
    assert s["roughness_max"] <= 0.35
    # totals equal the last profile entries; time at 80 m/h is the lower bound
    assert s["objective_cost_s"] == pytest.approx(route["profile"]["cumulative_objective_s"][-1],
                                                  abs=0.01)
    assert s["estimated_drive_time_s"] >= s["distance_m"] * 3600 / 80 - 1e-6
    # geometry endpoints are the waypoint cells; lon/lat round-trips through the region CRS
    lon0, lat0 = route["geometry_lonlat"]["coordinates"][0]
    assert math.hypot((lon0 - LANDING["lon"]) * math.cos(math.radians(lat0)),
                      lat0 - LANDING["lat"]) * math.pi / 180 * 3396190 < 1.0
    # provenance: every input layer and its content-addressed version
    assert {"cost", "time", "classes", "elevation", "tilt"} <= set(route["inputs"])


def test_route_values_come_from_the_rasters(route):
    """Profile samples equal the published raster values at the route's cells."""
    from marsgis.catalog import layer_path

    xs = np.array([c[0] for c in route["geometry_xy"]["coordinates"]][::97])
    ys = np.array([c[1] for c in route["geometry_xy"]["coordinates"]][::97])
    tilt = route["profile"]["tilt"][::97]
    with rasterio.open(layer_path("jezero", "hirise_tilt_5m")) as src:
        vals = [v[0] for v in src.sample(zip(xs, ys, strict=True))]
    np.testing.assert_allclose(tilt, vals, atol=1e-4)


def test_engines_agree_on_real_data(client, route):
    r = client.post("/api/regions/jezero/routes",
                    json={"waypoints": [LANDING, DELTA_FRONT], "engine": "mcp"}).json()
    assert r["summary"]["objective_cost_s"] == pytest.approx(
        route["summary"]["objective_cost_s"], rel=1e-9)


def test_route_errors_are_explicit(client):
    outside = client.post("/api/regions/jezero/routes",
                          json={"waypoints": [LANDING, {"lon": 77.0, "lat": 18.0}]})
    assert outside.status_code == 422 and outside.json()["detail"]["code"] == "outside_coverage"
    unknown_vehicle = client.post("/api/regions/jezero/routes",
                                  json={"waypoints": [LANDING, DELTA_FRONT], "vehicle": "x"})
    assert unknown_vehicle.status_code == 404
    one = client.post("/api/regions/jezero/routes", json={"waypoints": [LANDING]})
    assert one.status_code == 422
