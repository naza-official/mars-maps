"""API integration tests against the real processed CTX sample (skipped if not built)."""

import io

import numpy as np
import pytest
import rasterio


@pytest.fixture(scope="module")
def client(built_data):
    from fastapi.testclient import TestClient

    from marsgis.api.app import app

    return TestClient(app)


def _cog(layer):
    import glob

    from marsgis.config import get_settings

    return glob.glob(str(get_settings().processed_dir / "jezero" / layer / "*" / f"{layer}.tif"))[0]


def test_region_detail(client):
    d = client.get("/api/regions/jezero").json()
    assert "+R=3396190" in d["crs"]["proj4"]
    assert d["tms"]["base_zoom"] == 5
    ids = [layer["id"] for layer in d["layers"]]
    assert {"elevation", "slope", "hillshade", "orthoimage"} <= set(ids)
    elev = next(layer for layer in d["layers"] if layer["id"] == "elevation")
    assert elev["units"] == "m" and elev["nodata"] == "nan"
    # Jezero floor/delta: heights roughly -3100..-1300 m relative to the MOLA areoid
    assert -3200 < elev["statistics"]["minimum"] < elev["statistics"]["maximum"] < -1000


def test_base_zoom_tile_is_bit_identical_to_grid(client):
    r = client.get("/api/cog/tiles/MarsJezeroTM/5/16/16.npy",
                   params={"region": "jezero", "layer": "elevation"})
    assert r.status_code == 200
    tile = np.load(io.BytesIO(r.content))[0]
    with rasterio.open(_cog("elevation")) as src:
        # tile (16,16) at z5 spans x [0, 5120], y [-5120, 0]
        row, col = src.index(0 + 10, 0 - 10)
        cog = src.read(1, window=((row, row + 256), (col, col + 256)))
    np.testing.assert_array_equal(tile, cog)


def test_inspect_matches_raster_and_titiler(client):
    d = client.get("/api/regions/jezero/inspect", params={"x": 1010, "y": -2010}).json()
    loc = d["location"]
    assert loc["lat_planetographic"] > loc["lat_planetocentric"]
    assert loc["lon_west_360"] == pytest.approx(360 - loc["lon_east_360"])
    elev = d["layers"]["elevation"]
    with rasterio.open(_cog("elevation")) as src:
        r, c = src.index(1010, -2010)
        assert elev["value"] == pytest.approx(float(src.read(1)[r, c]))
    t = client.get(f"/api/cog/point/{loc['lon_east_180']},{loc['lat_planetocentric']}",
                   params={"region": "jezero", "layer": "elevation",
                           "coord_crs": "IAU_2015:49900"}).json()
    assert t["values"][0] == pytest.approx(elev["value"])


def test_inspect_nodata_and_outside(client):
    # top-left corner of the frame is far outside the CTX footprint
    d = client.get("/api/regions/jezero/inspect", params={"x": -80000, "y": 80000}).json()
    assert d["layers"]["elevation"]["status"] == "outside"
    assert d["layers"]["elevation"]["value"] is None
    assert client.get("/api/regions/jezero/inspect",
                      params={"x": 1e7, "y": 0}).status_code == 422


def test_layer_addressing_is_restricted(client):
    r = client.get("/api/cog/info", params={"region": "jezero", "layer": "../../etc/passwd"})
    assert r.status_code == 404
    assert client.get("/api/cog/info", params={"region": "nope", "layer": "x"}).status_code == 404


def test_earth_tms_not_offered(client):
    r = client.get("/api/cog/tiles/WebMercatorQuad/0/0/0.png",
                   params={"region": "jezero", "layer": "elevation"})
    assert r.status_code == 422
