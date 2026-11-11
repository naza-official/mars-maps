from __future__ import annotations

import os
from pathlib import Path

import pytest

# Tests run inside the backend container, where the repo's config/ is mounted at /config
# and data/ at /data. Outside the container, point at the repo checkout.
_REPO = Path(__file__).resolve().parents[2]
os.environ.setdefault("MARSGIS_CONFIG_DIR", "/config" if Path("/config/regions").exists()
                      else str(_REPO / "config"))
os.environ.setdefault("MARSGIS_DATA_DIR", "/data" if Path("/data").exists()
                      else str(_REPO / "data"))
os.environ.setdefault("MARSGIS_LOG_JSON", "false")


@pytest.fixture(scope="session")
def region():
    from marsgis.config import load_region

    return load_region("jezero")


@pytest.fixture(scope="session")
def grid(region):
    from marsgis.grid import region_grid

    return region_grid(region)


@pytest.fixture(scope="session")
def built_data():
    from marsgis.pipeline import is_built

    if not is_built("jezero"):
        pytest.skip("processed sample data not present (run `marsgis build`)")
    return True
