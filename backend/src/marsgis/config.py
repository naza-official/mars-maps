"""Configuration: runtime settings (env vars) and declarative region/source configs (YAML)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, overridable with MARSGIS_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="MARSGIS_")

    data_dir: Path = Path("/data")
    config_dir: Path = Path("/config")
    log_level: str = "INFO"
    log_json: bool = True
    cors_origins: list[str] = ["http://localhost:5173"]

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def catalog_dir(self) -> Path:
        return self.data_dir / "catalog"


@lru_cache
def get_settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------- sources


class SourceExpectations(BaseModel):
    """Assertions checked against the raw file before any processing."""

    crs_name_contains: str
    dtype: str | None = None
    nodata: float | None = None


class DeclaredValue(BaseModel):
    """A number that is *declared* (not read from the data) together with its basis.

    ``basis`` makes the epistemic status explicit:
    * ``published``  - taken from a cited primary source (mission/instrument documentation);
    * ``derived``    - computed from published values (the derivation is in ``source``);
    * ``assumption`` - an engineering assumption of this project, to be replaced/validated.
    """

    value: float
    unit: str
    basis: Literal["published", "derived", "assumption"]
    source: str


class SourceConfig(BaseModel):
    id: str
    title: str
    kind: str
    url: str
    format: str = "GeoTIFF"
    stac_item: str | None = None
    sidecars: dict[str, str] = Field(default_factory=dict)
    license: str
    citation: str
    expected: SourceExpectations
    units: str
    vertical_datum: str | None = None
    #: native posting of the source raster (informative; the file's transform is authoritative)
    native_resolution_m: float | None = None
    #: 1-sigma absolute vertical precision of an elevation source (long-wavelength error)
    vertical_precision: DeclaredValue | None = None
    #: 1-sigma *relative* height error between posts a few metres apart: the error that
    #: affects patch-scale features (tilt, roughness). Falls back to vertical_precision.
    relative_precision: DeclaredValue | None = None


# --------------------------------------------------------------------------- regions


class RegionCRSConfig(BaseModel):
    projection: Literal["tmerc", "eqc", "laea"]
    lon_0: float = Field(ge=-180, le=360, description="Degrees east, planetocentric")
    lat_0: float = Field(ge=-90, le=90, description="Degrees north, planetocentric")


class RenderConfig(BaseModel):
    colormap: str | None = None
    rescale: Literal["auto"] | tuple[float, float] = "auto"


TERRAIN_OPS = ("slope", "hillshade", "plane_tilt", "plane_roughness")
TRAVERSABILITY_OPS = ("traversability_class", "traversal_time", "traversal_cost")


class Category(BaseModel):
    value: int
    label: str
    color: str  # "#rrggbb"
    description: str = ""


class LayerConfig(BaseModel):
    id: str
    title: str
    op: Literal["warp", "slope", "hillshade", "plane_tilt", "plane_roughness",
                "traversability_class", "traversal_time", "traversal_cost"]
    source: str | None = None  # for op=warp
    input: str | None = None  # for single-input derived ops (terrain)
    inputs: dict[str, str] = Field(default_factory=dict)  # named inputs (traversability ops)
    vehicle: str | None = None  # for traversability ops: config/vehicles/<id>.yaml
    categories: list[Category] = Field(default_factory=list)  # for categorical layers
    resolution_m: float | None = None  # derived layers inherit the input's grid
    resampling: Literal["nearest", "bilinear", "cubic", "average"] = "bilinear"
    dtype: Literal["float32", "uint8", "uint16", "int16"]
    units: str
    description: str
    params: dict[str, Any] = Field(default_factory=dict)
    render: RenderConfig = Field(default_factory=RenderConfig)

    @model_validator(mode="after")
    def _check_inputs(self) -> LayerConfig:
        if self.op == "warp" and not (self.source and self.resolution_m):
            raise ValueError(f"layer {self.id}: op=warp requires source and resolution_m")
        if self.op in TERRAIN_OPS and not self.input:
            raise ValueError(f"layer {self.id}: derived op={self.op} requires input")
        if self.op in TRAVERSABILITY_OPS:
            if not self.vehicle:
                raise ValueError(f"layer {self.id}: op={self.op} requires vehicle")
            missing = {"tilt", "roughness"} - set(self.inputs)
            if missing:
                raise ValueError(f"layer {self.id}: op={self.op} requires inputs {missing}")
        return self

    @property
    def dependencies(self) -> list[str]:
        """Ids of the layers this layer is computed from."""
        if self.op == "warp":
            return []
        return [self.input] if self.input else list(self.inputs.values())


class PlanningConfig(BaseModel):
    """Which published layers a route planner consumes for one vehicle in one region."""

    cost: str  # objective, s/m (risk-weighted time), NaN = not traversable
    time: str  # physical traverse-time estimate, s/m
    classes: str  # traversability class (uint8)
    #: layers sampled along the route for diagnostics (name -> layer id)
    samples: dict[str, str] = Field(default_factory=dict)
    max_cells: int = 8_000_000  # largest planning window (cells) accepted per request
    default_margin_m: float = 300.0


class RegionConfig(BaseModel):
    id: str
    name: str
    body: Literal["mars"]
    crs: RegionCRSConfig
    half_size_m: float
    base_resolution_m: float
    tile_size: int = 256
    max_zoom: int = 9
    layers: list[LayerConfig]
    #: vehicle id -> the layers the route planner uses for that vehicle
    planning: dict[str, PlanningConfig] = Field(default_factory=dict)

    @field_validator("layers")
    @classmethod
    def _unique_ids(cls, v: list[LayerConfig]) -> list[LayerConfig]:
        ids = [layer.id for layer in v]
        if len(ids) != len(set(ids)):
            raise ValueError("layer ids must be unique")
        return v

    def layer(self, layer_id: str) -> LayerConfig:
        for layer in self.layers:
            if layer.id == layer_id:
                return layer
        raise KeyError(f"unknown layer {layer_id!r} in region {self.id!r}")


def _load_yaml(path: Path) -> Any:
    with path.open() as f:
        return yaml.safe_load(f)


def load_sources(config_dir: Path | None = None) -> dict[str, SourceConfig]:
    config_dir = config_dir or get_settings().config_dir
    raw = _load_yaml(config_dir / "sources.yaml")
    return {s["id"]: SourceConfig.model_validate(s) for s in raw["sources"]}


def load_region(region_id: str, config_dir: Path | None = None) -> RegionConfig:
    config_dir = config_dir or get_settings().config_dir
    path = config_dir / "regions" / f"{region_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"region config not found: {path}")
    return RegionConfig.model_validate(_load_yaml(path))


# --------------------------------------------------------------------------- vehicles


class VehicleConfig(BaseModel):
    """Vehicle mobility parameters. Every number carries its basis and source.

    The terrain features are vehicle-independent; this model is what turns them into
    vehicle-specific traversability (docs/traversability.md).
    """

    id: str
    name: str
    status: Literal["experimental", "validated"]
    description: str
    #: diameter of the disc that circumscribes the vehicle footprint (heading-independent)
    footprint_diameter: DeclaredValue
    mass: DeclaredValue
    max_tilt: DeclaredValue  # deg: above -> hazard
    nominal_tilt: DeclaredValue  # deg: above -> speed reduction starts
    speed_factor_at_max_tilt: DeclaredValue  # 1: speed multiplier reached at max_tilt
    max_step: DeclaredValue  # m: plane-fit residual above -> hazard
    slow_step: DeclaredValue  # m: plane-fit residual above -> reduced speed
    speed_nominal: DeclaredValue  # m/h
    speed_slow: DeclaredValue  # m/h
    uncertainty_k: DeclaredValue  # 1: k-sigma band below a limit -> "marginal"
    marginal_cost_multiplier: DeclaredValue  # 1: objective weight on marginal cells
    unknown_policy: Literal["forbid"] = "forbid"

    @model_validator(mode="after")
    def _check(self) -> VehicleConfig:
        if not 0 < self.nominal_tilt.value < self.max_tilt.value < 90:
            raise ValueError("require 0 < nominal_tilt < max_tilt < 90 deg")
        if not 0 < self.slow_step.value < self.max_step.value:
            raise ValueError("require 0 < slow_step < max_step")
        if not 0 < self.speed_slow.value <= self.speed_nominal.value:
            raise ValueError("require 0 < speed_slow <= speed_nominal")
        if not 0 < self.speed_factor_at_max_tilt.value <= 1:
            raise ValueError("speed_factor_at_max_tilt must be in (0, 1]")
        if self.marginal_cost_multiplier.value < 1:
            raise ValueError("marginal_cost_multiplier must be >= 1")
        return self


def load_vehicle(vehicle_id: str, config_dir: Path | None = None) -> VehicleConfig:
    config_dir = config_dir or get_settings().config_dir
    path = config_dir / "vehicles" / f"{vehicle_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"vehicle config not found: {path}")
    return VehicleConfig.model_validate(_load_yaml(path))


def list_vehicles(config_dir: Path | None = None) -> list[str]:
    config_dir = config_dir or get_settings().config_dir
    return sorted(p.stem for p in (config_dir / "vehicles").glob("*.yaml"))


def list_regions(config_dir: Path | None = None) -> list[str]:
    config_dir = config_dir or get_settings().config_dir
    return sorted(p.stem for p in (config_dir / "regions").glob("*.yaml"))
