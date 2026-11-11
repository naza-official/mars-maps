# Data model

This document defines how spatial data is represented inside marsgis. Every rule here is
enforced in code (module in brackets) and covered by tests unless stated otherwise.

## 1. Body model and coordinate conventions  [`marsgis/crs.py`]

| Aspect | Convention | Notes |
|---|---|---|
| Body | Mars | `assert_mars_crs()` rejects any CRS whose ellipsoid is not Mars-sized (3.37–3.40 Mm). Earth CRSs (EPSG:4326/3857/UTM…) raise `NotAMarsCRSError`. |
| Reference surface | **IAU 2015 Mars sphere**, R = 3 396 190 m — `IAU_2015:49900` | Same body model as the USGS Astrogeology analysis-ready products used as sources. |
| Latitude | **Planetocentric**, positive north | Planetographic latitude is *derived for display only*, on the IAU 2015 ellipsoid (a = 3 396 190 m, b = 3 376 200 m): `tan φg = (a/b)² tan φc`. At 18.45° N the two differ by ~0.20°. |
| Longitude | **East-positive**, stored in [−180, 180) | 0–360 °E and 0–360 °W (Viking/MDIM-era convention) are derived for display only. Jezero: 77.45 °E = 282.55 °W. |
| Heights | Metres above a **declared vertical datum** | Not a CRS property. The CTX DTM is relative to the **MOLA areoid** (GeoTIFF tag `GEOID=mola`). Never mix heights with different datums (areoid vs sphere vs ellipsoid) without an explicit conversion step. |
| Missing CRS | Error | A raster without a CRS is rejected; no CRS is ever assumed. |

### Why the sphere, not the ellipsoid?
The sources (USGS ARD CTX/HiRISE products) are on the sphere. Re-expressing them on the
ellipsoid would be a datum change with no new information, and a sphere makes
planetocentric latitude equal to geodetic latitude, removing a whole class of
ocentric/ographic mistakes. If a future NASA dataset is delivered on the ellipsoid
(`IAU_2015:49901`, planetographic), it must be transformed explicitly during ingestion
(PROJ supports this within the IAU_2015 authority).

## 2. Canonical regional CRS and grid  [`marsgis/grid.py`, `config/regions/*.yaml`]

Each **region** defines one *grid family*:

* **CRS**: Transverse Mercator on the Mars sphere, centred on the region
  (`lon_0`, `lat_0`), `k0 = 1`, no false easting/northing. Built as a custom
  `ProjectedCRS` (the IAU projected codes have fixed centres such as clon = 0).
  - Conformal → slope aspect/azimuths are correct.
  - Point scale factor < 1 + 3·10⁻⁴ across the whole ±81.92 km frame (tested), so
    pixel size in metres is ground distance to < 0.03 %. Slope, distance and cost
    calculations can use pixel units directly.
  - For regions far larger than ~300 km, or polar regions, choose `laea` (supported in
    config) or split into several regions.
* **Frame**: a square of side `2 × half_size_m` centred on (0, 0). For Jezero:
  163 840 m = 256 px × 20 m × 2⁵, so the regional TileMatrixSet's zoom 5 is exactly the
  20 m analysis grid.
* **Origin**: the frame's top-left corner (−81 920, +81 920).
* **Base resolution**: 20 m (≈ CTX DTM native 20.16 m; slight oversampling, no aliasing).
* **Nested resolutions**: a layer may use `base / 2^k` (or `base · 2^k`). All layers of a
  region are therefore pixel-aligned: every coarse pixel is an exact block of fine
  pixels. `GridSpec.at_resolution()` rejects any other resolution.

### Alignment rules
1. Output extents are the source footprint transformed with densified edges, snapped
   **outward** to the lattice and clipped to the frame (`GridSpec.snap_window`).
2. Every published raster is checked with `GridSpec.is_aligned()` (same CRS, same
   resolution, origin offset an integer number of pixels). Misaligned output is an error.
3. Consequence for consumers (cost surfaces, ML): layers of a region can be stacked by
   array slicing; no resampling is ever needed at analysis time.

## 3. Raster layer conventions  [`marsgis/processing.py`]

| Property | Convention |
|---|---|
| Storage | Cloud Optimized GeoTIFF, DEFLATE + predictor, 512×512 internal tiles, internal overviews (AVERAGE), BigTIFF if needed |
| Bands | One physical quantity per file (single band). Multiband only for genuinely joint quantities (e.g. RGB). |
| dtype | `float32` for physical quantities; `uint8` only for visual products (hillshade) |
| NoData (float) | **NaN**. Source sentinels (e.g. −32767) are converted to NaN during warping and never appear in processed data. |
| NoData (integer) | A declared value outside the valid range (hillshade: 0, valid 1–255) |
| Zero | Always a real value. Missing data is never written as 0 (tested). |
| Units | GDAL band unit (`units`) + band tag `UNITS` + STAC `raster:bands[].unit`. `1` = dimensionless. |
| Description | Band description (≤200 chars) + STAC `description` |
| Tags | `MARSGIS_REGION`, `MARSGIS_LAYER`, `MARSGIS_VERSION` |

### NoData propagation in derived layers
* Warping: GDAL excludes nodata from the bilinear kernel. Edge cells next to nodata may
  therefore be computed from fewer than 4 valid neighbours; they are not filled beyond
  the source footprint.
* Slope/hillshade (Horn 3×3): the result is NaN if **any** of the 9 cells is missing,
  including the centre (which has zero weight in Horn's kernel — this case was a bug
  caught by `test_slope_nan_propagation`, fixed in algorithm version 2).
* Statistics exclude nodata and non-finite values.

### Units in the current sample

| Layer | Units | Source of truth |
|---|---|---|
| elevation | m above MOLA areoid | Declared in `config/sources.yaml` (file carries no unit tag; `GEOID=mola` tag present) |
| orthoimage | `1` (relative brightness) | **Unknown physical unit.** Float32, values mostly 0–0.95, a few slightly negative (min −0.09). Possibly I/F but undocumented by the source, so it must not be used quantitatively. |
| slope | degrees | Derived: `atan(|∇z|)` with ∇z in m/m |
| hillshade | `1` (1–255 shading) | Visual only |

Conversions between units are not performed implicitly anywhere. A future conversion
(e.g. K ↔ °C, I/F ↔ radiance) must be a named processing step with its own algorithm
version and tests.

## 4. Storage layout and versioning  [`marsgis/pipeline.py`]

```
data/                                   (git-ignored; bind-mounted into containers)
  raw/<source-id>/                      immutable, verbatim downloads
    <original filename>.tif
    provenance.json                     url, sha256, size, ETag/Last-Modified, retrieved_at,
                                        licence, citation, declared units/datum, sidecars
    stac_item.json, qa_metrics.txt      upstream metadata kept verbatim
  processed/<region>/<layer>/<version>/<layer>.tif   COG, one directory per version
  catalog/<region>/catalog.json         STAC Catalog (root)
  catalog/<region>/<layer>/<layer>.json STAC Item of the *current* version
```

* **Raw data is immutable.** A re-fetch verifies the SHA-256 against `provenance.json`
  and fails if the file changed.
* **Versions are content-addressed**: `version = sha256(recipe, grid definition, input
  checksums or input layer versions, algorithm version, COG options)[:12]`. Unchanged
  inputs ⇒ same version ⇒ the build is a no-op. Any change (new source file, different
  resampling, algorithm fix, different grid) ⇒ new version directory; old versions stay
  on disk for comparison/rollback.
* Derived layers depend on their input's *version*, so a new elevation version
  automatically produces new slope/hillshade versions.
* Algorithm versions live in `processing.ALGORITHM_VERSIONS` and must be bumped whenever
  numerical output changes.

## 5. Metadata and provenance  [`marsgis/catalog.py`]

Each layer is described by a STAC Item (static catalog):

* `geometry`/`bbox`: footprint in **planetocentric east-positive degrees on the Mars
  sphere** (the convention of the USGS Astrogeology STAC API) — *not WGS84*; marked with
  `ssys:targets: ["Mars"]`.
* `proj:wkt2`, `proj:shape`, `proj:transform`, `proj:bbox` (region CRS); `proj:code` is
  `null` because a regional Mars CRS has no registered code.
* `raster:bands`: dtype, nodata (`"nan"` for NaN), unit, resolution, statistics
  (min/max/mean/std/p2/p98/valid %).
* `marsgis:version`, `marsgis:render` (default colormap/rescale for clients),
  `marsgis:provenance` (inputs with checksums, recipe, grid, software versions — GDAL,
  PROJ, rasterio, numpy — algorithm version, lineage sentence), `processing:lineage`.

## 6. Data representation for computation (route planning / ML)

The canonical analysis representation of a region is the stack of its aligned layers,
read as `float32` arrays with NaN for missing data:

* in code: `rasterio.open(item.assets.data.href).read(1)` per layer, or lazily via
  `rioxarray.open_rasterio(..., chunks=...)` → `xarray.Dataset` with one variable per
  layer and shared `x`/`y` coordinates (identical by construction);
* for ML training: windows/patches cut from the stack on the same lattice; the region CRS
  and transform are carried as metadata so predictions can be written back as layers;
* for large regions or many time steps a Zarr store with the same grid (chunk = 512×512)
  is the planned representation (see `decisions.md`, D6).

Pathfinding consumes a **cost surface** — a derived layer on the same grid — so routes
are sequences of grid cells (row, col) that convert to region x/y and lon/lat with the
grid transform and region CRS.
