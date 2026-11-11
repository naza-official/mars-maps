"""Data ingestion: fetch raw source files verbatim, record provenance, validate metadata.

Raw files are immutable: they are written once under ``data/raw/<source id>/`` together
with ``provenance.json`` (URL, SHA-256, size, HTTP validators, retrieval time, licence).
A re-run verifies the checksum instead of re-downloading.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import rasterio

from marsgis.config import SourceConfig, get_settings
from marsgis.crs import assert_mars_crs

log = logging.getLogger(__name__)


class SourceValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RawSource:
    config: SourceConfig
    path: Path
    sha256: str


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(client: httpx.Client, url: str, dest: Path) -> dict[str, Any]:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with client.stream("GET", url) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
        headers = {k: r.headers.get(k) for k in ("etag", "last-modified", "content-length")}
    tmp.rename(dest)
    return headers


def fetch_source(source: SourceConfig, raw_dir: Path | None = None) -> RawSource:
    raw_dir = (raw_dir or get_settings().raw_dir) / source.id
    raw_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urlparse(source.url).path).name
    dest = raw_dir / filename
    prov_path = raw_dir / "provenance.json"

    if dest.exists() and prov_path.exists():
        prov = json.loads(prov_path.read_text())
        digest = _sha256(dest)
        if digest != prov["sha256"]:
            raise SourceValidationError(
                f"{dest} checksum {digest} differs from provenance {prov['sha256']}; "
                "raw data must not change — delete the directory to re-fetch deliberately"
            )
        log.info("raw source present", extra={"source": source.id, "sha256": digest})
        return RawSource(source, dest, digest)

    log.info("downloading source", extra={"source": source.id, "url": source.url})
    with httpx.Client(timeout=httpx.Timeout(60.0, read=300.0), follow_redirects=True) as client:
        headers = _download(client, source.url, dest)
        sidecars = {}
        for name, url in {**source.sidecars, **({"stac_item": source.stac_item}
                                               if source.stac_item else {})}.items():
            side = raw_dir / (f"{name}.json" if name == "stac_item" else Path(urlparse(url).path).name)
            try:
                _download(client, url, side)
                sidecars[name] = {"url": url, "file": side.name, "sha256": _sha256(side)}
            except httpx.HTTPError as e:  # sidecars are informative, not required
                log.warning("sidecar download failed", extra={"source": source.id, "url": url,
                                                              "error": str(e)})

    digest = _sha256(dest)
    prov = {
        "source_id": source.id,
        "title": source.title,
        "url": source.url,
        "file": dest.name,
        "sha256": digest,
        "size_bytes": dest.stat().st_size,
        "http": headers,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "license": source.license,
        "citation": source.citation,
        "declared_units": source.units,
        "declared_vertical_datum": source.vertical_datum,
        "sidecars": sidecars,
    }
    prov_path.write_text(json.dumps(prov, indent=2))
    log.info("downloaded source", extra={"source": source.id, "sha256": digest,
                                        "bytes": prov["size_bytes"]})
    return RawSource(source, dest, digest)


def describe_raster(path: Path | str) -> dict[str, Any]:
    """Structured metadata summary of a raster (used by `marsgis inspect` and validation)."""
    with rasterio.open(path) as src:
        crs = src.crs
        info: dict[str, Any] = {
            "path": str(path),
            "driver": src.driver,
            "width": src.width,
            "height": src.height,
            "count": src.count,
            "dtypes": list(src.dtypes),
            "nodata": src.nodata,
            "crs_name": None,
            "crs_wkt": crs.to_wkt() if crs else None,
            "crs_authority": None,
            "transform": list(src.transform)[:6],
            "resolution": src.res,
            "bounds": list(src.bounds),
            "block_shapes": src.block_shapes,
            "overviews": src.overviews(1),
            "compression": src.compression.value if src.compression else None,
            "units": list(src.units),
            "scales": list(src.scales),
            "offsets": list(src.offsets),
            "descriptions": list(src.descriptions),
            "tags": src.tags(),
            "band_tags": [src.tags(i) for i in range(1, src.count + 1)],
            "is_tiled": src.profile.get("tiled", False),
        }
        if crs:
            from pyproj import CRS

            pcrs = CRS.from_wkt(crs.to_wkt())
            info["crs_name"] = pcrs.name
            info["crs_authority"] = pcrs.to_authority(min_confidence=70)
            info["ellipsoid"] = {
                "name": pcrs.ellipsoid.name,
                "semi_major_m": pcrs.ellipsoid.semi_major_metre,
                "inverse_flattening": pcrs.ellipsoid.inverse_flattening,
            }
    return info


def validate_source(raw: RawSource) -> dict[str, Any]:
    """Check the raw file against what sources.yaml declares. Fail loudly on mismatch."""
    info = describe_raster(raw.path)
    exp = raw.config.expected
    if info["crs_wkt"] is None:
        raise SourceValidationError(f"{raw.config.id}: raster has no CRS; refusing to assume one")
    assert_mars_crs(info["crs_wkt"], context=raw.config.id)
    if exp.crs_name_contains not in (info["crs_name"] or ""):
        raise SourceValidationError(
            f"{raw.config.id}: CRS {info['crs_name']!r} does not contain {exp.crs_name_contains!r}"
        )
    if exp.dtype and info["dtypes"][0] != exp.dtype:
        raise SourceValidationError(f"{raw.config.id}: dtype {info['dtypes'][0]} != {exp.dtype}")
    if exp.nodata is not None and info["nodata"] != exp.nodata:
        raise SourceValidationError(f"{raw.config.id}: nodata {info['nodata']} != {exp.nodata}")
    log.info("source validated", extra={"source": raw.config.id, "crs": info["crs_name"],
                                       "res": info["resolution"], "dtype": info["dtypes"][0]})
    return info
