# Research report

Date of investigation: 2026-11-11 (§1–§8, visualisation prototype) and 2026-11-13
(§9–§15, terrain analysis, traversability and route planning). Versions are those
current on PyPI/npm on those dates.

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

## 5. Scientific computing and route planning (initial notes, superseded by §9–§14)

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

(Item 4 was resolved on 2026-11-13: all derivations now run strip-wise with halos; see §12.)

---

# Part II: terrain analysis, traversability and route planning (2026-11-13)

## 9. Higher-resolution Mars terrain data for Jezero

### 9.1 What exists (USGS STAC search, VERIFIED)

A STAC search of `mro_hirise_socet_dtms` over 77.2–77.8° E, 18.2–18.8° N returns four
HiRISE stereo DTMs, each with a DTM COG, a hillshade, two or four orthoimages, a
"FOM" COG and a `provenance.txt`:

| Item | Extent (° E / ° N) | Posting | Size (DTM COG) | Covers |
|---|---|---|---|---|
| **DTEEC_045994_1985_046060_1985_U01** | 77.383–77.501 / 18.306–18.548 | 1.0166 m (ARD) / 1.0097 m (PDS) | 157 MB (ARD COG), 381 MB (PDS IMG) | **crater floor, landing site, delta front** (selected) |
| DTEEC_048842_1985_048908_1985_U01 | 77.462–77.584 / 18.337–18.588 | 1.017 m | – | eastern floor |
| DTEEC_023524_1985_023379_1985_U01 | 77.453–77.598 / 18.160–18.521 | 1.015 m | – | southern floor |
| DTEEC_023247_1985_022957_1985_U01 | 77.549–77.690 / 18.180–18.521 | 1.015 m | – | eastern floor and rim |

The selected product was a candidate-site DTM ("Candidate landing site for 2020 mission
in Jezero Crater", label `RATIONALE_DESC`) and contains the Octavia E. Butler landing
point. Licence: NASA PDS archive data (no use restrictions); the USGS ARD copies are CC0.

### 9.2 Original PDS product vs USGS ARD COG (decision D11)

| Aspect | ARD COG (`astrogeo-ard`) | PDS3 IMG (`hirise-pds.lpl.arizona.edu`) |
|---|---|---|
| Processing | `gdalwarp -r cubic -t_srs IAU_2015:49910` from the IMG (from its `provenance.txt`) | as produced by SOCET Set, no resampling |
| CRS | equirectangular clon = 0 on the IAU 2015 sphere | equirectangular clon = 77.44, lat_ts = 15 on the *local-radius* sphere 3 394 839.8 m |
| Readable by our stack | yes (COG) | yes: GDAL PDS driver in the rasterio 1.5.2 wheel; CRS, `MISSING_CONSTANT`, scaling and offset read correctly (VERIFIED over `/vsicurl/` and after download) |
| Metadata | STAC item (gsd, proj, bbox) | full PDS label (datum note, valid range, map projection, source images) |
| Inconsistencies noticed | `provenance.txt` translates the DTM with `-ot UInt16`, yet the COG is float32 (the log does not match the product); the "FOM" asset is a 3-band uint8 colour rendering, not FOM values | label extent keywords differ from GDAL's georeferencing by ≈1.3 m (half-pixel convention) |

**Selected: the PDS3 IMG.** It is the source of record, it avoids a second (cubic)
resampling, and it needs no planetary toolchain. ISIS3 is unnecessary because the
product is already map-projected.

### 9.3 CRS and datum verification (VERIFIED)

* The PDS sphere radius (local radius at the centre latitude) differs from the IAU 2015
  sphere. PROJ transforms between them with a *ballpark geographic offset* (identity on
  planetocentric lat/lon). That is correct for planetocentric coordinates on two Mars
  spheres, and `assert_mars_crs` accepts the 3 394 839.8 m radius (inside the Mars band).
* The label's MAX/MIN latitude and longitude match the transformed GDAL corners to
  2.2·10⁻⁵° ≈ 1.3 m (about one post).
* Heights: Mars 2000 datum (MOLA areoid), the same datum as the CTX DTM. A measured
  **−26.3 m mean offset (σ 8.6 m)** separates HiRISE from CTX in the overlap. It is
  explained by the CTX product's own MOLA offset (+27.4 m, CTX QA report) and recorded
  in `terrain-analysis.md` §6.

### 9.4 Precision and quality metadata

* The HiRISE team says "vertical precision in the tens of centimeters", with DTMs derived
  at about 4× image pixel scale ([uahirise.org/dtm/about.php](https://www.uahirise.org/dtm/about.php)).
  Kirk et al. 2008 (doi:10.1029/2007JE003000) define the expected precision
  EP = ρ·GSD/(p/h); this product's label does not give EP.
* FOM classes in the HiRISE DTM README ([example](https://hirise.lpl.arizona.edu/PDS/EXTRAS/DTM/ESP/ORB_026400_026499/ESP_026402_1605_ESP_026257_1605/README.TXT)):
  1 no data; 2 shadow; 3, 5–20, 28, 31–39 suspicious; 4, 30 interpolated; 21 saturated;
  22–27, 29 manually edited; 40–59 low good correlation; 60–99 good. The distributed FOM
  is a *colour-coded* map draped on shaded relief. The ARD `DTFEC` COG is 3-band uint8
  (VERIFIED), so per-post FOM values are not available to us. That is a gap for
  quality-aware masking.
* Measured relative noise: ≲ 0.02 m (from the roughness distribution;
  `terrain-analysis.md` §6).

### 9.5 Other datasets considered

| Dataset | Resolution | Use | Status |
|---|---|---|---|
| HiRISE RED orthoimages (`*_RED_C_01_ORTHO`, `*_RED_A_01_ORTHO`) | ~1 m / 0.25 m | context imagery; rock detection (CFA) at 0.25 m | DOC; A-products are 0.4–1 GB each; JP2 read by GDAL's JP2OpenJPEG driver (driver present, not exercised) |
| MOLA MEGDR / MOLA–HRSC blended DEM | 463 m / 200 m | regional context, areoid | §1 |
| CTX ASP DTMs (USGS, [docs](https://stac.astrogeology.usgs.gov/docs/data/mars/ctxdtms)) | ~20 m | alternative regional DTMs | DOC |
| Perseverance traverse and localisation (PDS, MMGIS) | rover scale | validation of planned corridors | future (traversability.md §7) |
| THEMIS thermal inertia | 100 m | proxy for loose material vs rock | future; would support terrain classes |

---

## 10. Rover traversability: what flight and mission teams do

| Source | What it establishes | Used for |
|---|---|---|
| **Goldberg, Maimone & Matthies 2002**, GESTALT, IEEE Aerospace, doi:10.1109/AERO.2002.1035370 ([PDF](https://robotics.jpl.nasa.gov/media/documents/aero.pdf)), read in full | rover-sized patch plane fit from moment statistics; step (max elevation difference vs clearance), roughness (residual vs clearance fraction), pitch (plane slope vs max pitch) and border (unknown neighbour) hazards; obstacles grow by half the vehicle width; unknown cells get zero certainty | footprint tilt and roughness definitions, configuration-space evaluation, unknown handling |
| **Ono et al. 2016**, Mars 2020 Traversability Tools, doi:10.1109/AERO.2016.7500597 ([PDF](https://www-robotics.jpl.nasa.gov/media/documents/Data-driven%20surface%20traversability%20analysis%20for%20Mars%202020%20landing%20site%20selection.pdf)), read in full | slope + CFA + terrain class + hazard → binary and speed models; cost = 1/speed; Dijkstra on an 8-connected grid; slope ~20° (cohesive) / ~10° (sand); 80 / 60 m/h by CFA band; 8-connected distance −5.4 % correction; +30 % for obstacle avoidance | model structure, published thresholds and speeds, metrication note |
| JPL M2020 mobility page ([link](https://robotics.jpl.nasa.gov/what-we-do/flight-projects/mars-2020-rover/m2020mobility/)) | wheel speed 4.2 cm/s (~150 m/h); Perseverance autonomous rate ~110 m/h; GESTALT treats the rover as a disc; ENav is orientation-sensitive | disc footprint, speed range |
| Otsu et al., ACE ([arXiv:1808.00031](https://arxiv.org/abs/1808.00031)) | conservative closed-form bounds on clearance, attitude and suspension angles; planned for Mars 2020 onboard planning | future body-clearance model (instead of generic curvature) |
| Carsten et al. 2007, Global path planning on MER ([PDF](https://www-robotics.jpl.nasa.gov/media/documents/IEEEAC-Carsten-1125.pdf)) | Field D* (interpolated, any-angle) global planner combined with GESTALT arcs; uploaded to MER in 2006 | any-angle planning as the next planner step |
| NASA/JPL PIA26073 (2023), [link](https://www.jpl.nasa.gov/images/pia26073-perseverance-autonav-avoids-a-boulder) | AutoNav routed around a 35 cm rock | order of magnitude for the step limit (**not** a published limit; a secondary-source "wheel drop 35 cm" claim was not confirmed) |
| JPL Mars 2020 press kit ([quick facts](https://www.jpl.nasa.gov/news/press_kits/mars_2020/launch/quick_facts/)) | 3.0 × 2.7 × 2.2 m, ~1025 kg | footprint, mass |
| Heverly et al. 2013 (JFR), Arvidson et al. (cited by Ono et al. [5], [6]) | MSL traverse performance, sand and ripple mobility | not read directly; cited through Ono et al. |
| Golombek & Rapp 1997; Golombek et al. (cited by Ono et al. [7], [11], [14]) | rock size-frequency model, CFA | future CFA layer |

Conclusion: traversability must be **vehicle-specific, footprint-scale and
uncertainty-aware**, with unknown terrain excluded. Slope or TRI alone does not meet
that bar. All three (vehicle-specific, footprint-scale, uncertainty-aware) are now
implemented; CFA and terrain classes are the documented gaps.

---

## 11. Terrain-analysis libraries (VERIFIED on linux/arm64 in our image)

| Library | Version | Licence | arm64 | Planetary compatibility | Verdict |
|---|---|---|---|---|---|
| **SciPy `ndimage`** | 1.18.1 | BSD-3 | wheel ✓ | body-agnostic array operators | **Selected** for windowed operators (exact correlations, min-filters) |
| NumPy | 2.3.5 | BSD-3 | ✓ | agnostic | Selected |
| xarray-spatial | 0.10.20 | MIT | wheel ✓ | `slope(method='planar')` is agnostic, but **`method='geodesic'` hard-codes WGS84** (`slope.py` imports `WGS84_A2, WGS84_B2`; `utils.py` uses 6378137) | Not selected: the Earth-geodesic path is a trap for Mars, it adds numba and dask, and the planar operators duplicate a few lines of NumPy |
| WhiteboxTools (`whitebox` PyPI) | 2.3.6 / WBT 2.4.0 | MIT | runs ✓ | file-based CLI, agnostic | Not selected: the Python package **downloads the binary at runtime** (not reproducible inside our pinned image) and works through intermediate files. Good as an independent cross-check tool later. |
| RichDEM | 0.3.4 | GPL-3 | **no arm64 wheel** (pip `--only-binary` fails) | agnostic | Rejected (build from source; GPL; low maintenance) |
| GDAL `gdaldem` | 3.12 | MIT | CLI not in our wheel-based image (D7) | agnostic (planar; `-s` scale for geographic CRSs) | Not needed: its TRI/TPI/roughness were not selected (`terrain-analysis.md` §3) |
| ISIS3 | – | public domain | osx-64 / amd64 only (§1) | native planetary | Not needed (map-projected input) |

## 12. Planning libraries (VERIFIED where stated)

| Library | Version | arm64 | Notes | Verdict |
|---|---|---|---|---|
| **SciPy `sparse.csgraph.dijkstra`** | 1.18.1 | ✓ | Dijkstra on CSR graphs, directed or undirected, predecessors; explicit graph lets us forbid corner cutting and later add directional costs | **Selected** (default engine) |
| **scikit-image `graph.MCP_Geometric`** | 0.26.0 | ✓ | Cython Dijkstra on cost arrays; `(c_a + c_b)/2 · L` semantics; infinite costs impassable; no corner-cutting control | **Selected** as second engine and cross-check |
| pyastar2d | 1.1.4 | wheel ✓ | C++ A* on numpy weights, 8-connected | Candidate if A* speed-up is needed; small project |
| NetworkX | 3.7 | ✓ (pure Python) | too slow for 10⁶–10⁷ nodes | Rejected for grids |
| scikit-fmm | 2025.6.23 | **no arm64 cp312 wheel** | fast marching, continuous geodesics | Rejected for now (§path-planning 1) |
| OMPL (Python wheels) | 2.0.1 | wheel ✓, import ✓ | sampling-based planners, SE(2) spaces | Future: local kinematic refinement |
| Nav2 Smac planner | ROS 2 | via ROS images | Hybrid A* / state lattice | Algorithmic reference; not a dependency |

Measured on the real data: both engines return the identical optimum; dijkstra 2.8 s
and 1.5 GB for a 4.7 M-cell window, mcp 2.5 s and 0.56 GB (`path-planning.md` §5).

## 13. Data management for multi-resolution analysis

| Option | Assessment | Decision |
|---|---|---|
| Nested grids in one region CRS (integer resolution ratios) | exact block alignment (20 m = 20×20 × 1 m), lossless stacking, exact block-mean comparisons (used for HiRISE vs CTX) | **Adopted** (D10) |
| Resample HiRISE to 20 m | discards the information that matters for mobility | Rejected |
| Resample HiRISE to 1.25 m (power-of-two zoom) | tiles bit-identical at z9, but drops below the native 1.0097 m posting | Rejected (1 m kept; tiles are resampled for display only) |
| Mosaic CTX + HiRISE into one DTM | needs vertical co-registration first (−26 m offset) and a blending rule | Deferred; products stay separate |
| Zarr / xarray datacube | right for many layers × time, and for ML patches | Still planned (D6); not needed by the 2-D pipeline |
| STAC API (pgstac), PostGIS | no search or vector persistence need yet | Still deferred (D9) |

Provenance carried per layer: source checksum and URL, recipe, grid, software versions,
algorithm version, and for vehicle-specific layers the full vehicle parameter set and
the uncertainty values used. Routes return the versions of every input layer.

## 14. Compatibility matrix additions (2026-11-13)

| Item | Evidence | Result |
|---|---|---|
| GDAL PDS driver (rasterio 1.5.2 wheel) reading a HiRISE PDS3 DTM | VERIFIED (remote `/vsicurl/` and local) | CRS, nodata, scale/offset correct |
| PROJ local-radius Mars sphere → IAU 2015 sphere | VERIFIED | ballpark (identity on lat/lon), as intended |
| GDAL drivers present: PDS, PDS4, ISIS3, VICAR, JP2OpenJPEG | VERIFIED (driver list) | available; only PDS exercised |
| scipy 1.18.1 / scikit-image 0.26.0 arm64 wheels | VERIFIED (installed in image, tests pass) | OK |
| xarray-spatial geodesic slope on Mars | VERIFIED by source reading | INCOMPATIBLE (WGS84 constants) |
| WhiteboxTools 2.4.0 on arm64 | VERIFIED runs | runtime binary download |
| RichDEM 0.3.4, scikit-fmm 2025.6.23 arm64 wheels | VERIFIED absent | source build required |
| OMPL 2.0.1 Python wheel arm64 | VERIFIED import | OK (not used) |
| TiTiler categorical `colormap` JSON on uint8 class COG | VERIFIED (UI) | OK; overviews use MODE |
| Strip-wise derivation equals whole-array derivation | VERIFIED (test) | identical |

## 15. ML: assessment

No ML in this milestone, by design. Where ML adds concrete value later:
1. **Terrain classification** (sand, ripples, bedrock) from HiRISE imagery. Ono et al.
   2016 used a CNN (SPOC). Labels: AI4Mars (rover imagery, Swan et al. 2021) or manual
   orbital labels. It would enable the 10° sand limit and the ripple rule.
2. **Rock detection** for CFA: shadow segmentation is classical, and learned detectors
   are optional.
3. **Learned slip or speed models** trained on Perseverance drive logs (PDS), replacing
   the assumed speed factors.

Each output would be a versioned layer on the same grid, with the model version as its
algorithm version, consumed by the existing traversability model.
