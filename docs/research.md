# Research report

Date of investigation: 2026-11-11. Versions are those current on PyPI/npm on that date.

Evidence levels used throughout:

* **VERIFIED** — exercised in this repository (tests or recorded experiments).
* **DOC** — supported by documentation, not tested here.
* **THEORY** — expected to work from first principles; neither documented nor tested.
* **INCOMPATIBLE** — known/observed failure or missing functionality.

---

## 1. Planetary data and coordinate systems

### Sources and archives
* **NASA PDS** (PDS3 legacy, PDS4 current). GDAL reads PDS3 (`PDS` driver), PDS4
  (`PDS4`, read/write, tables as OGR layers) and ISIS3 cubes (`ISIS3`, read/write, label
  as JSON metadata). — DOC: [PDS4](https://gdal.org/drivers/raster/pds4.html),
  [PDS3](https://gdal.org/drivers/raster/pds.html),
  [ISIS3](https://gdal.org/drivers/raster/isis3.html). The rasterio wheels used here
  bundle GDAL 3.12.2 with these drivers (DOC; not exercised with real PDS files yet).
* **USGS Astrogeology Analysis Ready Data** — STAC API at
  <https://stac.astrogeology.usgs.gov/api/> with collections
  `mro_ctx_controlled_usgs_dtms`, `mro_hirise_socet_dtms`, `mo_themis_controlled_mosaics`,
  `mro_hirise_uncontrolled_observations`; assets are COGs on the public
  `astrogeo-ard` S3 bucket, licence CC0. — VERIFIED (queried, downloaded, processed).
  [HiRISE DTM docs](https://stac.astrogeology.usgs.gov/docs/data/mars/hirise_dtms/).
* **Global DEMs**: MOLA–HRSC blended DEM, 200 m, ~11 GB plain GeoTIFF (not COG) —
  [USGS](https://astrogeology.usgs.gov/search/map/mars_mgs_mola_mex_hrsc_blended_dem_global_200m).
  Too large for a first slice; useful later for regional context (convert to COG or read
  windows).

### ISIS3
USGS's planetary image processing suite (camera models, map projection of level-1
images). Distributed via conda; on Apple Silicon the official route is an **osx-64
environment under Rosetta**, native ARM builds are described as not reliable —
[install guide](https://astrogeology.usgs.gov/docs/how-to-guides/environment-setup-and-maintenance/installing-isis-via-anaconda),
[ARM notes](https://astrogeology.usgs.gov/docs/how-to-guides/isis-developer-guides/developing-mac-m1-arm-systems).
In Docker on Apple Silicon it would run as linux/amd64 under emulation (THEORY: slow).
**Not needed** while inputs are map-projected products (ARD COGs, DTMs, mosaics);
re-evaluate if raw EDR/level-0 data arrives.

### CRS facts (VERIFIED with PROJ 9.8.1)
* PROJ ships the **IAU_2015** authority: `IAU_2015:49900` (Mars 2015 sphere,
  ocentric, R = 3 396 190 m), `IAU_2015:49901` (ellipsoid, ographic, 1/f = 169.894),
  `IAU_2015:49910` (sphere equirectangular, clon = 0), etc.
  Reference: Hare et al., [doi:10.1007/s10569-017-9805-5](https://doi.org/10.1007/s10569-017-9805-5) (IAU WGCCRE 2015).
* The USGS CTX COGs carry WKT1 with authority only on the GEOGCS; `to_authority()` on the
  projected CRS returns `None` → CRS identity must be checked by equivalence, not codes.
* PROJ refuses EPSG:4326 → Mars transformations ("Cannot find coordinate operations") —
  useful as a safety net against Earth defaults in libraries.
* A custom TM `ProjectedCRS` on the Mars sphere round-trips through GeoTIFF keys
  (GDAL 3.12) as an equivalent CRS.

### Conventions and risks when combining datasets
| Risk | Example | Mitigation here |
|---|---|---|
| Planetocentric vs planetographic latitude | 18.45°c = 18.65°g at Jezero (~12 km apart) | Store ocentric; derive ographic for display only |
| East vs west longitude, 0–360 vs ±180 | Jezero 77.45°E = 282.55°W; tile names "18N282W" | Store east ±180; explicit converters |
| Sphere vs ellipsoid vs older radii (3 396 000 m MOLA, 3 389 500 m mean) | Older products | Mars-radius band check; explicit transformation on ingest |
| Vertical datum | MOLA areoid vs sphere vs ellipsoid heights differ by km | Declared per source; never mixed implicitly |
| Map projection distortion | Equirectangular clon=0 at 18°N: +5 % E–W | Regional TM (<0.03 %) |
| Co-registration between missions | CTX DTM vs MOLA: mean 27.4 m, σ 10.9 m vertical offset (source QA file) | Recorded; future uncertainty layers |
| Resolution mismatch | CTX 20 m vs HiRISE 1 m vs MOLA 463 m | Nested power-of-two grids; explicit resampling per layer |
| Missing metadata | CTX COGs have no unit tags; orthoimage unit undocumented | Declared in `sources.yaml`; unknowns flagged |

---

## 2. Raster processing and storage

| Tool | Version | Role | Notes |
|---|---|---|---|
| GDAL | 3.12.2 (in rasterio wheel) | I/O, warping, COG driver | VERIFIED |
| PROJ | 9.8.1 (rasterio and pyproj wheels — same version) | CRS | VERIFIED; IAU_2015 present |
| rasterio | 1.5.2 | Python GDAL binding | VERIFIED; **wheel needs `libexpat1`** on `python:*-slim` (ImportError otherwise) |
| pyproj | 3.8.0 | CRS objects, transforms, scale factors | VERIFIED |
| NumPy | 2.3.5 | Terrain derivatives | VERIFIED |
| rioxarray / xarray | 0.23.0 / current | Labelled stacks | DOC; planned (decisions D6) |
| Zarr | v3 | ML stacks, multidimensional | DOC; planned |
| NetCDF | — | Exchange format | Not selected for serving |

Findings:
* **COG** is the right processed format for 2-D layers: tiled + overviews give efficient
  partial reads locally and over HTTP (VERIFIED against USGS S3 with range reads).
* **Warping**: `WarpedVRT` with explicit destination grid, block-wise writes → memory
  independent of raster size (VERIFIED). Use bilinear for continuous fields, average for
  downsampling, nearest for categorical; avoid cubic for DTMs (overshoot).
* **NoData**: source sentinel −32767 → NaN (VERIFIED by test); GDAL's bilinear kernel
  ignores nodata cells; Horn's kernel needs an explicit centre-cell check (bug found and
  fixed via tests).

---

## 3. Backend and geospatial APIs

| Component | Version | Licence | Verdict |
|---|---|---|---|
| FastAPI | 0.162.4 | MIT | Selected |
| titiler.core | 2.4.0 | MIT | Selected (embedded factories) |
| rio-tiler | 9.4.13 | BSD-3 | via titiler; Mars datasets VERIFIED with Mars TMS |
| morecantile | 7.1.0 | MIT | Custom Mars TMS VERIFIED |
| planetcantile | 0.6.0 | BSD-3 | 24 Mars TMSs VERIFIED loading; one tile in `MarsGeographicSphere` rendered (200) |
| pystac | 1.16.0 | Apache-2.0 | Static catalog |
| STAC API (stac-fastapi-pgstac), PostGIS, MinIO | — | MIT/GPL/AGPL | Deferred (decisions D9) |

### Does TiTiler support Mars without modification?
* **Tiles in Mars TMSs: yes**, configuration only (`supported_tms`). VERIFIED: tiles at the
  regional TMS base zoom are **bit-identical** to the analysis grid (elevation and slope,
  max |Δ| = 0, NaN masks equal).
* **Default TMS registry is Earth-only** (WebMercatorQuad etc.). planetcantile injects its
  sets into `morecantile.tms` **only if imported before morecantile** (VERIFIED by reading
  `planetcantile/defaults.py` and observing an empty Mars list when imported after).
  → build an explicit `TileMatrixSets` registry.
* **`/point` defaults `coord_crs` to EPSG:4326** → HTTP 500 on Mars datasets (VERIFIED);
  works with `coord_crs=IAU_2015:49900` (VERIFIED, matches our `/inspect`).
* **Built-in viewer** assumes Web Mercator/Leaflet → disabled.
* `tilejson`/`info` geographic bounds use the TMS's geographic CRS; with our TMSs that is
  the Mars sphere (morecantile `geographic_crs` VERIFIED).
* USGS operates a public TiTiler with planetcantile TMSs
  ([tutorial](https://stac.astrogeology.usgs.gov/docs/tutorials/mosaic_json_analysis)), and
  NASA AMMOS MMGIS uses TiTiler + planetcantile
  ([docs](https://nasa-ammos.github.io/MMGIS/configure/projections/planetcantile)) — the
  stack choice has operational precedent.

### Relevant projects
* **planetcantile** — <https://github.com/AndrewAnnex/planetcantile>: IAU TMS JSONs for
  all bodies; small project (~50 commits), no tagged GitHub releases, PyPI 0.6.0 → pin.
* **MMGIS** — <https://github.com/NASA-AMMOS/MMGIS>: NASA mission GIS (Leaflet 2-D,
  Cesium-based 3-D, Node, PostGIS, TiTiler, STAC, Docker). Too heavy to adopt for a
  prototype, but the reference implementation for mission-grade planetary web GIS; worth
  revisiting for operations-style features (layers config, time, drawing).
* **rio-tiler non-Earth support** (since v3: `geographic_crs`/TMS-based bounds) —
  [rio-tiler docs](https://cogeotiff.github.io/rio-tiler/); older issues about non-Earth
  errors predate v3.
* **morecantile** — <https://developmentseed.org/morecantile/>: `TileMatrixSet.custom()`
  with any pyproj CRS.
* **TiTiler non-Web-Mercator TMS** —
  [notebook](https://developmentseed.org/titiler/examples/notebooks/Working_with_nonWebMercatorTMS),
  [custom TMS](https://developmentseed.org/titiler/examples/code/tiler_with_custom_tms).

---

## 4. Frontend visualisation

| Library | Version | Licence | Custom CRS | Notes |
|---|---|---|---|---|
| **OpenLayers** | 10.11.0 | BSD-2 | Yes (proj4js, any `TileGrid`) | **Selected**. VERIFIED rendering in Mars TM with 3 Mars-specific fixes (below) |
| proj4js | 2.22.0 | MIT | — | VERIFIED with workarounds |
| MapLibre GL JS | 6.17.0 | BSD-3 | **No** — Web Mercator / globe / vertical perspective only; arbitrary CRS is an open roadmap item ([roadmap](https://maplibre.org/roadmap/maplibre-gl-js/non-mercator-projection), [discussion #163](https://github.com/maplibre/maplibre/discussions/163)) | INCOMPATIBLE for regional analysis grids |
| CesiumJS | 1.147.0 | Apache-2.0 | 3-D globe; `Ellipsoid.MARS` since 1.133 ([release notes](https://cesium.com/blog/2025/09/02/cesium-releases-in-september-2025/)) | DOC; candidate for future 3-D context view |
| Leaflet + Proj4Leaflet | — | BSD-2 | Yes (used by MMGIS) | Viable alternative; weaker vector/WebGL story |

### OpenLayers/proj4js on Mars — issues found (all VERIFIED)
1. **Scale is computed on Earth.** OpenLayers' `getPointResolution` for non-degree
   projections transforms to EPSG:4326 and measures on the Earth sphere → ScaleLine
   wrong by ~1.9×. Fix: `Projection({ getPointResolution: r => r })` (TM, k0 = 1).
2. **proj4js spherical tmerc throws** "Incorrect elliptical usage" (exact `etmerc`
   requires an ellipsoid). Fix: `+approx`.
3. **proj4js spherical inverse TM latitude-sign bug** when `lat_0 ≠ 0` and `y < 0`
   (mirrored latitude south of the projection centre). Found by the click-time
   browser-vs-server cross-check. Fix: equivalent definition `lat_0=0, y_0=−R·φ0`.
   After the fix: agreement ≤ 2e-9 m at all tested points across the frame.
   Worth reporting upstream to proj4js.

---

## 5. Scientific computing and route planning (investigated, not implemented)

* **Raster pathfinding**: `scikit-image` `graph.MCP` / `MCP_Geometric` (Dijkstra on cost
  rasters, handles diagonal step lengths) — simplest correct baseline; `networkx`/custom
  A* for heuristics and constraints; SciPy `ndimage` for neighbourhood statistics
  (roughness = local std/TRI).
* **Traversability**: slope thresholds (vehicle-specific; values must come from the
  challenge's mission constraints, not be assumed), roughness at wheel scale (needs HiRISE 1 m DTMs, available for Jezero in the USGS STAC),
  rock abundance, hazard masks.
* **Multi-objective**: weighted sum first; Pareto (label-setting) once objectives are
  known (distance, energy, exposure, risk).
* **ML**: PyTorch on windows of the aligned stack; only after cost-surface baselines exist.
* Precondition, satisfied by the current design: all layers aligned on a metric grid with
  NaN = unknown/impassable.

---

## 6. Compatibility matrix (Apple Silicon, Docker Desktop/OrbStack, linux/arm64)

| Item | Evidence | Result |
|---|---|---|
| `python:3.12-slim-trixie` arm64 + rasterio 1.5.2 wheel | VERIFIED | Works after `apt install libexpat1` |
| rasterio GDAL 3.12.2 / PROJ 9.8.1 vs pyproj PROJ 9.8.1 | VERIFIED | Same PROJ version → consistent CRS behaviour |
| IAU_2015 CRS codes in pyproj and rasterio | VERIFIED | Available |
| Read USGS CTX COGs over HTTPS | VERIFIED | Works (range reads) |
| Custom Mars TM CRS → GeoTIFF → reopen equivalence | VERIFIED | Equivalent |
| morecantile 7.1.0 custom TMS on Mars CRS, `geographic_crs` Mars | VERIFIED | Works |
| planetcantile 0.6.0 TMS registration | VERIFIED | Works only with explicit registry (import-order pitfall) |
| titiler.core 2.4.0 tiles in Mars TMS | VERIFIED | Bit-identical to grid at base zoom |
| titiler `/point` default coord_crs | INCOMPATIBLE | 500 on Mars data; pass `coord_crs` |
| titiler built-in viewer | INCOMPATIBLE (by design) | Web Mercator only; disabled |
| Node 24 + Vite 8.3.8 + OL 10.11 + proj4 2.22 + TS 5.9.3 | VERIFIED | Builds, typechecks, renders (headless Chromium via Playwright 1.65) |
| proj4js spherical TM | INCOMPATIBLE as-is | Workarounds (§4) VERIFIED |
| MapLibre custom CRS | INCOMPATIBLE (DOC) | Not selected |
| CesiumJS Mars ellipsoid | DOC | Not tested |
| ISIS3 on Apple Silicon | DOC | osx-64 via Rosetta / amd64 emulation; not needed yet |
| GDAL PDS3/PDS4/ISIS3 drivers in wheel | DOC | Not exercised (no such files yet) |
| PostGIS with IAU SRIDs | THEORY | Requires manual `spatial_ref_sys` entries |
| MinIO / `/vsis3/` | DOC | Standard GDAL feature; not tested |
| Docker BuildKit inside the Claude Code sandbox | Environment-specific | Registry token fetch failed (TLS interception); classic builder used for verification. Not an issue on a normal host |

---

## 7. Reusable existing solutions (adopted or recommended)

* Adopted: GDAL/rasterio (all raster I/O), PROJ IAU_2015 database, titiler.core
  factories, morecantile custom TMS, planetcantile TMS definitions, pystac, OpenLayers,
  USGS ARD COGs + STAC.
* Recommended next: rioxarray/xarray/Zarr (stacks), scikit-image `graph` (pathfinding
  baseline), stac-fastapi-pgstac + titiler-pgstac (many items), CesiumJS (3-D), MMGIS
  (reference for mission-ops features).

## 8. Identified limitations and technical risks

1. NASA data formats/CRSs unknown — mitigated by declarative sources + strict validation.
2. Small upstream projects (planetcantile) — pinned; TMS JSONs could be vendored.
3. proj4js correctness for planetary projections — mitigated by server-authoritative
   inspection and runtime cross-check; consider computing all displayed coordinates
   server-side for non-TM projections until each is verified.
4. Whole-array terrain derivation — fine for ~10⁷ cells; needs halo-windowed processing
   for large regions.
5. Unit/datum metadata often missing in planetary products — never inferred silently;
   documented as declared or unknown.
6. Single-region CRS — cross-region products need reprojection; polar regions need a
   different projection choice.
