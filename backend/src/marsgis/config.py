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


class SourceConfig(BaseModel):
    id: str
    title: str
    kind: str
    url: str
    stac_item: str | None = None
    sidecars: dict[str, str] = Field(default_factory=dict)
    license: str
    citation: str
    expected: SourceExpectations
    units: str
    vertical_datum: str | None = None


# --------------------------------------------------------------------------- regions


class RegionCRSConfig(BaseModel):
    projection: Literal["tmerc", "eqc", "laea"]
    lon_0: float = Field(ge=-180, le=360, description="Degrees east, planetocentric")
    lat_0: float = Field(ge=-90, le=90, description="Degrees north, planetocentric")


class RenderConfig(BaseModel):
    colormap: str | None = None
    rescale: Literal["auto"] | tuple[float, float] = "auto"


class LayerConfig(BaseModel):
    id: str
    title: str
    op: Literal["warp", "slope", "hillshade"]
    source: str | None = None  # for op=warp
    input: str | None = None  # for derived ops
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
        if self.op != "warp" and not self.input:
            raise ValueError(f"layer {self.id}: derived op={self.op} requires input")
        return self


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


def list_regions(config_dir: Path | None = None) -> list[str]:
    config_dir = config_dir or get_settings().config_dir
    return sorted(p.stem for p in (config_dir / "regions").glob("*.yaml"))
