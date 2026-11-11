"""Reproducible, idempotent layer builds.

Each layer output is content-addressed: its version is a hash of
(layer recipe, region grid definition, input versions/checksums, marsgis version).
Outputs live at ``data/processed/<region>/<layer>/<version>/<layer>.tif``; old
versions are kept, and the catalog Item points at the current one. Re-running a build
with unchanged inputs is a no-op.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import numpy as np
import rasterio

import marsgis
from marsgis.catalog import build_item, write_item
from marsgis.config import LayerConfig, RegionConfig, Settings, get_settings, load_region, load_sources
from marsgis.grid import GridSpec, region_grid
from marsgis.ingest import RawSource, fetch_source, validate_source
from marsgis.processing import (
    ALGORITHM_VERSIONS,
    COG_OPTIONS,
    LayerRaster,
    band_statistics,
    derive_terrain,
    warp_to_grid,
)

log = logging.getLogger(__name__)


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _software() -> dict[str, str]:
    import pyproj

    return {
        "marsgis": marsgis.__version__,
        "rasterio": rasterio.__version__,
        "gdal": rasterio.__gdal_version__,
        "proj": pyproj.proj_version_str,
        "numpy": np.__version__,
    }


def _grid_signature(grid: GridSpec) -> dict[str, Any]:
    return {"crs": grid.crs.to_wkt(), "origin": [grid.origin_x, grid.origin_y],
            "resolution": grid.resolution, "size": [grid.width, grid.height]}


def _layer_order(region: RegionConfig) -> list[LayerConfig]:
    """Topological order: warp layers first, then derived layers whose input is built."""
    done: set[str] = set()
    ordered: list[LayerConfig] = []
    pending = list(region.layers)
    while pending:
        progressed = False
        for layer in list(pending):
            if layer.op == "warp" or layer.input in done:
                ordered.append(layer)
                done.add(layer.id)
                pending.remove(layer)
                progressed = True
        if not progressed:
            raise ValueError(f"unresolvable layer dependencies: {[p.id for p in pending]}")
    return ordered


def build_region(region_id: str, settings: Settings | None = None,
                 only: list[str] | None = None) -> dict[str, str]:
    settings = settings or get_settings()
    region = load_region(region_id, settings.config_dir)
    sources = load_sources(settings.config_dir)
    base = region_grid(region)
    built: dict[str, dict[str, Any]] = {}  # layer id -> {"version", "path"}
    raws: dict[str, RawSource] = {}

    for layer in _layer_order(region):
        if layer.op == "warp":
            if layer.source not in sources:
                raise KeyError(f"layer {layer.id}: unknown source {layer.source}")
            if layer.source not in raws:
                raw = fetch_source(sources[layer.source], settings.raw_dir)
                validate_source(raw)
                raws[layer.source] = raw
            raw = raws[layer.source]
            grid = base.at_resolution(layer.resolution_m)
            inputs = {"source": raw.config.id, "sha256": raw.sha256, "url": raw.config.url}
        else:
            parent = built[layer.input]
            with rasterio.open(parent["path"]) as src:
                grid = base.at_resolution(src.res[0])
            inputs = {"layer": layer.input, "version": parent["version"]}

        recipe = layer.model_dump(exclude={"render", "title", "description"})
        version = _hash({"recipe": recipe, "grid": _grid_signature(grid), "inputs": inputs,
                         "algorithm": ALGORITHM_VERSIONS[layer.op],
                         "cog_options": COG_OPTIONS})
        out = settings.processed_dir / region.id / layer.id / version / f"{layer.id}.tif"
        built[layer.id] = {"version": version, "path": out}

        if only and layer.id not in only:
            continue
        if out.exists():
            log.info("layer up to date", extra={"layer": layer.id, "version": version})
        else:
            nodata: float | int = float("nan") if layer.dtype.startswith("float") else 0
            meta = LayerRaster(
                layer_id=layer.id, units=layer.units, description=layer.description,
                dtype=layer.dtype, nodata=nodata,
                tags={"region": region.id, "layer": layer.id, "version": version},
            )
            if layer.op == "warp":
                warp_to_grid(raw.path, grid, out, meta, layer.resampling)
            else:
                derive_terrain(built[layer.input]["path"], grid, out, meta, layer.op,
                               layer.params)

        with rasterio.open(out) as check:
            if not grid.is_aligned(check.transform, grid.crs):
                raise RuntimeError(f"{out} is not aligned with the region grid")

        stats = band_statistics(out)
        provenance = {
            "inputs": inputs,
            "recipe": recipe,
            "grid": {k: v for k, v in _grid_signature(grid).items() if k != "crs"},
            "software": _software(),
            "algorithm_version": ALGORITHM_VERSIONS[layer.op],
            "lineage": (
                f"{layer.op} of {inputs.get('source') or inputs.get('layer')} onto region "
                f"{region.id} grid at {grid.resolution} m ({layer.resampling if layer.op == 'warp' else 'n/a'})"
            ),
        }
        item = build_item(region_id=region.id, layer_cfg=layer, cog_path=out, version=version,
                          stats=stats, provenance=provenance, data_root=settings.data_dir)
        write_item(settings.catalog_dir, region.id, item)
        log.info("layer published", extra={"layer": layer.id, "version": version,
                                           "valid_percent": stats["valid_percent"]})
    return {k: v["version"] for k, v in built.items()}


def is_built(region_id: str, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    region = load_region(region_id, settings.config_dir)
    cat = settings.catalog_dir / region_id
    return all((cat / layer.id / f"{layer.id}.json").exists() for layer in region.layers)


__all__ = ["build_region", "is_built"]
