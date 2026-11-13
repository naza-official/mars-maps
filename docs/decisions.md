# Technical decisions

Format: context → decision → alternatives → consequences. "Verified" means tested in
this repository (see `docs/research.md` §6 for the evidence).

---

## D1. Modular monolith, one Python image for API and pipeline

**Context.** Ingestion, processing, catalog, tiling and inspection share the same CRS,
grid and catalog code. The team is small and the data volume is currently tens of MB.

**Decision.** One Python package (`marsgis`) with clear module boundaries
(`crs`, `grid`, `ingest`, `processing`, `catalog`, `pipeline`, `tms`, `inspect`, `api`),
one Docker image, run as two Compose services (`init-data` one-shot pipeline, `api`).

**Alternatives.** Stock TiTiler container + separate pipeline container; eoAPI
(stac-fastapi-pgstac + titiler-pgstac + tipg); MMGIS.

**Consequences.** One dependency lock, one GDAL/PROJ version, no network hops. Module
boundaries are the seams for later extraction (e.g. a worker service for heavy jobs).
Revisit when processing jobs exceed request timescales or need horizontal scaling.

---

## D2. Canonical CRS: IAU 2015 Mars sphere, planetocentric, east-positive; regional TM

**Context.** Mars data mixes planetocentric/planetographic latitudes, east/west longitudes,
0–360/±180 ranges, sphere/ellipsoid models and different projections. Analysis needs
metric distances and slopes.

**Decision.** Body model `IAU_2015:49900`; per-region Transverse Mercator (k0 = 1) centred
on the region; frame and grid defined in `config/regions/<id>.yaml`. Details:
`docs/data-model.md` §1–2.

**Alternatives.**
* Keep source equirectangular `IAU_2015:49910` (clon = 0): at 18° N the E–W scale error
  is 1/cos 18° ≈ +5 % → wrong slopes and distances unless corrected everywhere.
* Equirectangular with `lat_ts` = region latitude: E–W scale still varies ±0.5 % across
  the region, not conformal.
* Lambert azimuthal equal-area: equal-area (good for area statistics) but not conformal;
  available as `projection: laea` for polar or large regions.
* Planetographic ellipsoid `IAU_2015:49901`: mismatches the USGS sources.

**Consequences.** Slopes/distances are metric to < 0.03 % (tested). Each region is its
own CRS, so cross-region analysis needs reprojection (acceptable: routes are regional).
Browser-side transforms needed proj4js workarounds (D5).

---

## D3. Storage formats

| Purpose | Format | Why |
|---|---|---|
| Raw sources | Verbatim as delivered (+ provenance.json) | Reproducibility; never mutate inputs |
| Processed 2-D layers | **COG** (DEFLATE+predictor, 512 tiles, overviews) | Windowed/range reads, overviews for display, served by TiTiler directly, readable by every GIS tool, S3-ready |
| Metadata/discovery | **Static STAC** Items/Catalog | Standard, matches USGS Astrogeology STAC; ingestible by stac-fastapi-pgstac later |
| ML / multidimensional (planned) | **Zarr** (v3) on the same grid, 512×512 chunks | Many layers × time steps, parallel writes, xarray-native |
| Vectors (planned) | GeoJSON over the API (region CRS or lon/lat on Mars sphere); PostGIS when querying is needed | Small volumes today |
| Not used | NetCDF (fine for exchange, weaker for cloud partial reads), MBTiles (Web Mercator by spec), GeoPackage (no need yet) | |

---

## D4. Tile serving: titiler.core embedded, explicit Mars TMS registry

**Context.** Standard XYZ/Web Mercator tiling is inappropriate for Mars and for regional
analysis grids. TiTiler supports any OGC TileMatrixSet through morecantile.

**Decision.** Embed `titiler.core` factories in the FastAPI app with:
* `supported_tms` = explicit registry: one **regional TMS per region** (morecantile
  `TileMatrixSet.custom` in the region CRS, `geographic_crs` = Mars sphere) + the 24
  **planetcantile** Mars TMSs (global interoperability with USGS/MMGIS). No Earth TMS.
* `path_dependency` resolving `region` + `layer` ids through the catalog (no arbitrary
  file paths or URLs → no path traversal/SSRF).
* TiTiler viewer disabled (it assumes Web Mercator/Leaflet).

**Verified.** Base-zoom tiles are bit-identical to the analysis grid; tiles in
`MarsGeographicSphere` render; Earth TMS ids are rejected (422).

**Caveats found.** `/point` defaults `coord_crs` to EPSG:4326 → HTTP 500 for Mars
datasets (PROJ refuses Earth→Mars). Clients must pass `coord_crs=IAU_2015:49900`, or use
`/api/regions/{id}/inspect`. planetcantile only patches `morecantile.tms` if imported
before morecantile — we never rely on that global.

**Alternatives.** Pre-rendered static tiles (gdal2tiles supports custom profiles but
loses dynamic styling); planetcantile's own app (fine, but we need catalog-based
addressing and our own endpoints anyway); MMGIS (complete app, Node/Leaflet, heavier
than needed now — but its use of TiTiler + planetcantile validates this choice).

---

## D5. Frontend: OpenLayers 10 + proj4js, rendering in the region CRS

**Context.** Needs custom planetary projections, custom tile grids, raster + vector
layers, pixel inspection, route rendering.

**Decision.** OpenLayers with the region CRS registered via proj4js and a `TileGrid`
built from the region TMS (origin top-left, resolutions 640 m / 2^z). Map scale uses a
custom `getPointResolution` (identity) because OpenLayers otherwise measures on the
**Earth** sphere via EPSG:4326 (would overstate distances by ~1.9×).

**proj4js 2.22 defects found and worked around (browser only):**
1. `+proj=tmerc` on a sphere throws "Incorrect elliptical usage" → use `+approx`.
2. Spherical inverse TM flips latitude sign when projected y < 0 (tests sign of y
   instead of `lat_0 + y/R`) → rewrite as `lat_0=0, y_0=−R·φ0` (mathematically
   identical). A click-time cross-check against the server's PROJ result guards this
   (agreement ~1e-9 m).

**Alternatives.** MapLibre GL JS — Web Mercator/globe only, custom CRS still an open
roadmap item → rejected. Leaflet + Proj4Leaflet — works (MMGIS uses Leaflet) but weaker
for multi-CRS vector work and WebGL rendering. CesiumJS — `Ellipsoid.MARS` since 1.133;
the right tool for a later 3-D view, not for an analysis-grid 2-D map.

---

## D6. Scientific stack: rasterio + NumPy now; xarray/rioxarray/Zarr when stacking

**Decision.** Pipeline uses rasterio (WarpedVRT block-wise warping, COG driver) and NumPy.
`rioxarray`/`xarray`/`dask` are introduced with the first multi-layer computation (cost
surface) — not before, to keep the dependency set minimal.

**Consequences.** Terrain derivatives currently load the whole layer into memory (fine for
~10⁷ cells). For larger regions: windowed processing with a 1-pixel halo, or
`xarray.map_overlap` with dask.

---

## D7. Dependencies from wheels, not system GDAL

**Decision.** `python:3.12-slim-trixie` + rasterio/pyproj manylinux wheels (GDAL 3.12.2,
PROJ 9.8.1, IAU_2015 database included) + `libexpat1`. Transitive lock generated with
`uv pip compile --universal` (works on arm64 and amd64).

**Alternatives.** `ghcr.io/osgeo/gdal` image (system GDAL + build rasterio from source:
slower builds, two places for PROJ data); conda-forge (heavier, but needed if ISIS is
ever co-installed).

**Consequence.** The `gdal*` CLI tools are not in the image; `marsgis inspect` and
`rio` (rasterio CLI) cover inspection. If CLI tools are needed, add a `gdal` tools
service from `ghcr.io/osgeo/gdal` with the same PROJ major version.

---

## D8. Sample dataset: USGS ARD CTX DTM + orthoimage, Jezero crater

USGS Astrogeology analysis-ready COGs (`mro_ctx_controlled_usgs_dtms`, item
`P18_007925_1987_XN_18N282W__P19_008650_1987_XI_18N282W`), CC0, ~43 MB total, real mission
data, already controlled and orthorectified, discoverable through STAC. Jezero is the
Perseverance landing site, so later validation data (rover traverse, HiRISE DTMs at 1 m)
exist for the same region.

---

## D9. No PostGIS / MinIO / STAC API yet

Not needed for one region with four layers. The design keeps the seams:
* COG hrefs are paths today; GDAL reads `s3://`/`/vsis3/` identically → MinIO is a config
  change (+ `AWS_S3_ENDPOINT`).
* STAC Items are standard → bulk-load into pgstac when search across many items is needed.
* PostGIS when vector data (routes, waypoints, hazard polygons) needs persistence and
  spatial queries. Note: PostGIS `spatial_ref_sys` has no IAU entries; planetary SRIDs
  must be inserted (unverified here).

Re-evaluated 2026-11-13 with routing in place: routes are computed on demand and
returned as JSON with input versions, and nothing needs to persist or query them
spatially yet. The decision stands.

---

## D10. Multi-resolution: integer-nested grids, products kept separate (2026-11-13)

**Context.** HiRISE DTMs (1 m) cover small parts of the 20 m CTX region. The prompt and
the science both require keeping native resolution and source provenance.

**Decision.** Generalise grid nesting from `base / 2^k` to `base / n` for an integer `n`.
HiRISE is warped once onto the **1 m** member of the region grid family, close to its
1.0097 m posting. Every 20 m cell is an exact 20 × 20 block of 1 m cells. CTX and HiRISE
remain **separate products**; each analysis declares which one it uses (planning uses
HiRISE, and refuses waypoints outside its coverage).

**Alternatives.** 1.25 m (power-of-two zoom, bit-identical tiles at z9: rejected, since
it falls below native posting). Resampling to 20 m (rejected: destroys the information).
Mosaicking (deferred: a measured −26.3 m vertical offset between the products must be
co-registered first).

**Consequences.** Tiles of 1 m layers are resampled by TiTiler for display (z10 =
0.625 m). Exact block statistics across resolutions became trivial (`marsgis
compare-dtm`).

## D11. Ingest the original PDS3 HiRISE DTM, not the USGS ARD COG

**Evidence.** The ARD COG was made with `gdalwarp -r cubic` from the PDS IMG (its own
`provenance.txt`). GDAL's PDS driver in our wheel reads the IMG correctly (CRS,
`MISSING_CONSTANT`, scaling verified). **Decision:** the PDS IMG is the raw source,
with SHA-256 provenance and the ARD `provenance.txt` kept as a sidecar. The cost is a
380 MB download instead of 157 MB, once.

## D12. Rover-footprint plane-fit descriptors (GESTALT-style); TRI/VRM not used

**Decision.** Terrain features for mobility are the tilt and maximum residual of a plane
fitted to a rover-footprint patch (5 m), following GESTALT. Implemented with SciPy
`ndimage.correlate` (exact sums) and validated analytically. **Rejected:** TRI (mixes
slope and roughness), VRM (dimensionless, cannot be compared with clearance), raw
curvature (amplifies noise; body clearance needs an ACE-type model), xarray-spatial
(WGS84-geodesic path, heavy dependencies), WhiteboxTools (runtime binary download),
RichDEM (no arm64 wheel, GPL). Details: `terrain-analysis.md` §3, `research.md` §11.

## D13. Vehicle-specific traversability with declared parameter provenance

**Decision.** Separate layers for features → classes → time → cost. Vehicle YAML where
every value has `value, unit, basis (published | derived | assumption), source`. Model
structure from Ono et al. 2016 (thresholds plus a speed model, cost = 1/speed). Unknown
terrain is impassable. Uncertainty enters as a k-σ "marginal" band with an explicit
risk multiplier. A one-at-a-time sensitivity analysis is a CLI command and its results
are documented. **Rejected:** a weighted sum of normalised slope, roughness etc. with
invented weights. It has no physical unit, cannot be checked, and is exactly what the
requirements warn against.

**Correction made during review:** `max_step = 0.35 m` was first labelled "published"
(as a Perseverance wheel-drop limit). NASA's primary source (PIA26073) only shows
AutoNav avoiding a 35 cm rock, so it is now labelled `assumption`.

## D14. Planner: SciPy csgraph Dijkstra (default) + scikit-image MCP (second engine)

**Decision.** Both are established Dijkstra implementations. SciPy on an explicit graph
forbids corner cutting and allows directional costs later. scikit-image's
`MCP_Geometric` uses ~3× less memory and serves as an independent cross-check (identical
optimum on random fields and on the real route). The planner module is
framework-independent. Planning windows are capped at 8 M cells (measured 2.4 GB
peak), and the API allows 2 concurrent plans. **Rejected or deferred:** custom
A* (no need; libraries exist), NetworkX (too slow), scikit-fmm (no arm64 wheel), OMPL
and Nav2 (continuous or ROS-oriented; references for future kinematic planning).
Details: `path-planning.md`.

## D15. DTM relative vs absolute precision

**Evidence.** With σ = 0.25 m (absolute precision), white noise would put every 5 m
patch's max residual near 0.5 m, but the measured median is 0.037 m. **Decision:**
sources declare `vertical_precision` (absolute) and `relative_precision` (patch scale,
here 0.05 m, *derived* from the measured distribution with 2.5× margin). Patch features
use the relative value. This changed the marginal fraction from 6.1 % to 0.13 % of the
observed terrain.

---

## Unresolved questions

1. **NASA dataset formats** (PDS3/PDS4/ISIS cubes/NetCDF/HDF?), CRS, resolution, units
   and vertical datums are unknown. Ingestion is adapter-based (`sources.yaml` +
   validation); each new format needs an adapter and expectations.
2. **ISIS3** is only needed if we receive *unprojected* (level-0/1) instrument data.
   It runs on Apple Silicon only via osx-64/Rosetta (conda) or as an amd64 Docker image
   under emulation — evaluate only if such data arrives.
3. **Orthoimage units**: the USGS CTX orthoimage is float32 0–0.95 without unit metadata.
   Ask USGS/NASA or read the product SIS before using it quantitatively.
4. **Vertical datum harmonisation**: MOLA areoid heights vs any future sphere/ellipsoid
   heights; requires an areoid model (e.g. MOLA MEGDR areoid) if mixed.
5. **Region size/CRS for the challenge region**: TM is ideal up to a few hundred km and
   away from the poles; polar regions → `laea` or polar stereographic.
6. **Temporal layers** (temperature, radiation): time dimension → Zarr/xarray; STAC
   `datetime` semantics for model outputs vs observations.
7. ~~**Uncertainty**: whether/how to propagate DTM uncertainty into traversability is
   open.~~ Addressed for patch-scale features (D15, `traversability.md`). Still open:
   per-post quality (HiRISE FOM values are only distributed as a colour rendering) and
   CTX–HiRISE vertical co-registration before any fusion (measured −26.3 ± 8.6 m).
8. **Rock abundance and terrain classes** are the largest missing inputs to the
   traversability model. Both need imagery-based analysis (HiRISE RED at 0.25 m).
9. **Validation data**: Perseverance's actual drive path through this DTM is public and
   should become the first validation dataset for the planner and classes.
