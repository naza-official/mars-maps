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
from marsgis.config import (
    TERRAIN_OPS,
    TRAVERSABILITY_OPS,
    LayerConfig,
    RegionConfig,
    Settings,
    SourceConfig,
    get_settings,
    load_region,
    load_sources,
    load_vehicle,
)
from marsgis.grid import GridSpec, region_grid
from marsgis.ingest import RawSource, fetch_source, validate_source
from marsgis.processing import (
    ALGORITHM_VERSIONS,
    COG_OPTIONS,
    LayerRaster,
    band_statistics,
    derive_terrain,
    derive_traversability,
    warp_to_grid,
)
from marsgis.terrain import tilt_sigma_deg, window_cells
from marsgis.traversability import Uncertainty

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
    """Topological order: warp layers first, then derived layers whose inputs are built."""
    done: set[str] = set()
    ordered: list[LayerConfig] = []
    pending = list(region.layers)
    while pending:
        progressed = False
        for layer in list(pending):
            if all(d in done for d in layer.dependencies):
                ordered.append(layer)
                done.add(layer.id)
                pending.remove(layer)
                progressed = True
        if not progressed:
            raise ValueError(f"unresolvable layer dependencies: {[p.id for p in pending]}")
    return ordered


def _published_version(item_path) -> str | None:
    """Version the catalog currently points at for a layer (None if unpublished)."""
    if not item_path.exists():
        return None
    return json.loads(item_path.read_text())["properties"].get("marsgis:version")


def _root_source(region: RegionConfig, layer: LayerConfig,
                 sources: dict[str, SourceConfig]) -> SourceConfig:
    """The single source a layer ultimately derives from (multi-source lineage is refused
    for uncertainty propagation until a fusion model exists)."""
    roots = set()
    stack = [layer]
    while stack:
        cur = stack.pop()
        if cur.op == "warp":
            roots.add(cur.source)
        stack.extend(region.layer(d) for d in cur.dependencies)
    if len(roots) != 1:
        raise ValueError(f"layer {layer.id} derives from {sorted(roots)}; expected one source")
    return sources[roots.pop()]


def _feature_uncertainty(region: RegionConfig, layer: LayerConfig,
                         sources: dict[str, SourceConfig],
                         resolution: float) -> tuple[Uncertainty, dict[str, Any]]:
    """Propagate the source's declared vertical precision to the feature uncertainties:
    sigma_tilt analytically (least-squares plane), sigma_roughness ~ sigma_z."""
    src = _root_source(region, layer, sources)
    declared = src.relative_precision or src.vertical_precision
    if declared is None:
        raise ValueError(f"source {src.id} declares no relative_precision/vertical_precision; "
                         f"required for uncertainty-aware traversability ({layer.id})")
    sigma_z = declared.value
    tilt_layer = region.layer(layer.inputs["tilt"])
    rough_layer = region.layer(layer.inputs["roughness"])
    if tilt_layer.params.get("window_m") != rough_layer.params.get("window_m"):
        raise ValueError(f"{layer.id}: tilt and roughness must use the same window")
    window_m = float(tilt_layer.params["window_m"])
    n = window_cells(window_m, resolution)
    sigma = Uncertainty(tilt_deg=tilt_sigma_deg(sigma_z, n, resolution), roughness_m=sigma_z)
    info = {"source": src.id, "sigma_z_m": sigma_z, "sigma_z_basis": declared.basis,
            "sigma_z_kind": "relative" if src.relative_precision else "absolute",
            "window_m": window_m, "sigma_tilt_deg": sigma.tilt_deg,
            "sigma_roughness_m": sigma.roughness_m}
    return sigma, info


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
            parent = built[layer.dependencies[0]]
            with rasterio.open(parent["path"]) as src:
                grid = base.at_resolution(src.res[0])
            if layer.input:
                inputs = {"layer": layer.input, "version": parent["version"]}
            else:
                inputs = {name: {"layer": lid, "version": built[lid]["version"]}
                          for name, lid in layer.inputs.items()}

        extra: dict[str, Any] = {}
        if layer.op in TRAVERSABILITY_OPS:
            vehicle = load_vehicle(layer.vehicle, settings.config_dir)
            sigma, sigma_info = _feature_uncertainty(region, layer, sources, grid.resolution)
            extra = {"vehicle": vehicle.model_dump(), "uncertainty": sigma_info}

        recipe = layer.model_dump(exclude={"render", "title", "description"})
        version = _hash({"recipe": recipe, "grid": _grid_signature(grid), "inputs": inputs,
                         "algorithm": ALGORITHM_VERSIONS[layer.op],
                         "cog_options": COG_OPTIONS, **extra})
        out = settings.processed_dir / region.id / layer.id / version / f"{layer.id}.tif"
        built[layer.id] = {"version": version, "path": out}

        if only and layer.id not in only:
            continue
        item_path = settings.catalog_dir / region.id / layer.id / f"{layer.id}.json"
        if out.exists() and _published_version(item_path) == version:
            log.info("layer up to date", extra={"layer": layer.id, "version": version})
            continue
        if out.exists():
            log.info("layer output exists; republishing", extra={"layer": layer.id,
                                                                 "version": version})
        else:
            nodata: float | int = float("nan") if layer.dtype.startswith("float") else 0
            meta = LayerRaster(
                layer_id=layer.id, units=layer.units, description=layer.description,
                dtype=layer.dtype, nodata=nodata,
                # categorical layers must never be averaged in overviews
                overview_resampling="MODE" if layer.categories else "AVERAGE",
                tags={"region": region.id, "layer": layer.id, "version": version},
            )
            if layer.op == "warp":
                warp_to_grid(raw.path, grid, out, meta, layer.resampling)
            elif layer.op in TERRAIN_OPS:
                derive_terrain(built[layer.input]["path"], grid, out, meta, layer.op,
                               layer.params)
            else:
                derive_traversability(
                    {name: built[lid]["path"] for name, lid in layer.inputs.items()},
                    grid, out, meta, layer.op, vehicle, sigma)

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
                f"{layer.op} of {inputs.get('source') or inputs.get('layer') or sorted(layer.inputs.values())} "
                f"onto region {region.id} grid at {grid.resolution} m "
                f"({layer.resampling if layer.op == 'warp' else 'n/a'})"
            ),
            **extra,
        }
        item = build_item(region_id=region.id, layer_cfg=layer, cog_path=out, version=version,
                          stats=stats, provenance=provenance, data_root=settings.data_dir)
        write_item(settings.catalog_dir, region.id, item)
        log.info("layer published", extra={"layer": layer.id, "version": version,
                                           "valid_percent": stats["valid_percent"]})
    return {k: v["version"] for k, v in built.items()}


def is_built(region_id: str, settings: Settings | None = None) -> bool:
    """True if every configured layer has a published catalog item (any version)."""
    settings = settings or get_settings()
    region = load_region(region_id, settings.config_dir)
    cat = settings.catalog_dir / region_id
    return all((cat / layer.id / f"{layer.id}.json").exists() for layer in region.layers)


__all__ = ["build_region", "is_built"]
