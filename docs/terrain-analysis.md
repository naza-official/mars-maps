# Terrain analysis

This document covers the terrain descriptors that marsgis derives from elevation data:
what they mean, how they are computed, where their limits lie, and how they are
validated. Every number below can be reproduced from the repository (the commands are
given), and every operator has an analytical test in `backend/tests/`.

Evidence labels follow `research.md`: **VERIFIED** means exercised here, **DOC** means
documented but not tested here, and **ASSUMPTION** marks a choice made by this project.

---

## 1. Input data

| Product | Source | Posting | Analysis grid | Role |
|---|---|---|---|---|
| CTX DTM | USGS ARD, controlled CTX stereo DTM (COG) | 20.16 m | 20 m regional grid | Regional context and slope at 20 m |
| **HiRISE DTM** | **HiRISE team PDS3 product `DTEEC_045994_1985_046060_1985_U01`** (original IMG) | 1.0097 m | **1 m nested grid** | Rover-scale descriptors and route planning |

Facts about the HiRISE product, all verified by reading the attached PDS3 label through
GDAL's PDS driver:

* **CRS**: equirectangular, `CENTER_LONGITUDE` 77.44° E, `CENTER_LATITUDE` (true scale)
  15° N, on a sphere whose radius is the *local* radius 3 394 839.8 m. Latitudes are
  planetocentric and longitudes east-positive. PROJ converts it to the IAU 2015 sphere
  with a "ballpark geographic offset", i.e. it keeps lat/lon unchanged. This is the
  correct semantics, because both CRSs are planetocentric on Mars. The label's corner
  latitude and longitude keywords agree with GDAL's georeferencing to within 2.2·10⁻⁵°
  (≈1.3 m, about one post), which is the known PDS3 half-pixel/extent-keyword ambiguity.
* **Heights**: metres above the Mars 2000 datum (the MOLA areoid; label `NOTE`),
  `SCALING_FACTOR` 1, `OFFSET` 0. `MISSING_CONSTANT` is `16#FF7FFFFB#`, which GDAL reads
  as −3.4028226550889045e38 and the warp turns into NaN.
* **Quality**: the HiRISE team gives "vertical precision in the tens of centimeters"
  for 1 m DTMs ([uahirise.org/dtm/about.php](https://www.uahirise.org/dtm/about.php)),
  and DTMs are produced at about 4× the image pixel scale, so the effective horizontal
  resolution is several metres. The product is tied to MOLA by bundle adjustment.

**Why the original PDS product and not the USGS ARD COG?** The ARD COG of the same DTM
was produced with `gdalwarp -r cubic -t_srs IAU_2015:49910` (its `provenance.txt`). Using
it would mean resampling twice and inheriting cubic overshoot next to scarps. The PDS3
IMG is the source of record and GDAL reads it natively, so ISIS is not needed (VERIFIED).

### Resolution policy (multiresolution data model)

* The 20 m and 1 m grids belong to the same grid family: same CRS and origin, with
  `20 / 1` an integer. Every 20 m cell is therefore an exact 20 × 20 block of 1 m cells
  (`GridSpec.at_resolution`, tested).
* HiRISE is warped onto 1 m, not onto 1.25 m (the nearest power-of-two zoom) and not
  onto 20 m. Because the projection changes, one resampling is unavoidable. A ~1:1
  bilinear resample keeps the native posting and does not overshoot.
* The two DTMs are **not mosaicked**. They are separate products that are each analysed
  at their own scale. Section 6 shows why fusing them needs vertical co-registration first.

---

## 2. Selected descriptors

All descriptors are **vehicle-independent**: they describe the terrain at a stated
spatial scale. Vehicle limits are applied later (`traversability.md`).

### 2.1 Slope (Horn 3 × 3) — `slope`, `hirise_slope`

| | |
|---|---|
| Definition | `θ = atan(√(∂z/∂x² + ∂z/∂y²))`, with gradients from Horn's (1981) weighted 3 × 3 finite differences |
| Inputs | Elevation on a metric grid (m) |
| Neighbourhood | 3 × 3 cells: a 3 m baseline at 1 m, 60 m at 20 m |
| Units / dtype | degrees, float32 |
| NoData | NaN if *any* of the 9 cells is missing, including the centre, which has zero weight in the kernel (bug found by a test and fixed in algorithm v2) |
| Resolution requirement | Measures slope at the stated baseline. A 20 m CTX slope underestimates slopes at rover scale. |
| Implementation | `processing.horn_gradients` (NumPy), strip-wise with a 1-cell halo |
| Validation | Exact on planes (`test_slope_of_known_plane`); agrees with the plane-fit gradient to 1e-6° (`test_tilt_direction_conventions_match_horn`) |
| Use | Reference and visualisation. At 1 m it is noise-sensitive and sub-footprint, so traversability uses 2.2. |

### 2.2 Footprint tilt (plane fit) — `hirise_tilt_5m`

| | |
|---|---|
| Definition | Inclination of the least-squares plane `z = a + b_x·u + b_y·v` fitted to the n × n patch centred on the cell: `tilt = atan(√(b_x² + b_y²))` |
| Origin | GESTALT, the JPL rover hazard evaluation: "merging the moment statistics from a rover-sized patch of surrounding cells and finding the best-fit plane … Pitch Hazard: compute the slope from the planar fit" (Goldberg, Maimone & Matthies 2002, [PDF](https://robotics.jpl.nasa.gov/media/documents/aero.pdf), doi:10.1109/AERO.2002.1035370). GESTALT (MER) and its successors on Curiosity treat the rover as a disc (heading-independent) ([JPL M2020 mobility](https://robotics.jpl.nasa.gov/what-we-do/flight-projects/mars-2020-rover/m2020mobility/)). |
| Window | **5 m** (5 × 5 cells at 1 m). It is the smallest odd number of cells covering the 4.04 m disc that circumscribes a 3.0 × 2.7 m rover footprint. The window is a feature parameter; the vehicle model checks that it matches its footprint. |
| Units / dtype | degrees, float32 |
| NoData | NaN unless all n² elevations are present: a partially observed patch is *unknown*, never extrapolated |
| Algorithm | For a complete symmetric patch the design is orthogonal, so `a = mean z`, `b_x = Σu z / S`, `b_y = Σv z / S`, with `S = n Σ u_j²`. These are three exact correlations (`scipy.ndimage.correlate`; no running sums, so no cancellation at z ≈ −2500 m). Implemented in `terrain.plane_fit`. |
| Cost | 3 correlations of n² taps plus n² vectorised residual passes. The 96 M-cell HiRISE grid takes ~25 s on 8 arm64 cores, strip-wise with an n//2 halo. |
| Validation | Exact on planes for n = 3, 5, 7 at z = −2500 m (error < 1e-9°); NaN propagation; strip/halo result identical to the in-memory result; the uncertainty formula is checked by Monte Carlo (§5) |

### 2.3 Footprint roughness (plane residual) — `hirise_roughness_5m`

| | |
|---|---|
| Definition | `max |z_i − (a + b_x u_i + b_y v_i)|` over the n × n patch: the largest bump or step relative to the plane the chassis would sit on |
| Origin | GESTALT's roughness hazard ("compute the residual from the planar fit") and step hazard. We use the **detrended** maximum instead of GESTALT's raw max − min elevation difference. At 1 m posting a raw relief over 5 m mixes in slope (a 5 m patch at 20° has 1.8 m of relief), and slope is already handled by tilt. |
| Window / units / dtype / NoData | Same as tilt: 5 m, metres, float32, NaN if the patch is incomplete |
| Validation | Analytic step case: one spike of height h in an n × n flat patch gives `h(1 − 1/n²)` exactly (`test_residual_of_a_step`) |
| Limitation | It measures metre-scale relief such as scarps, ridges, ripples and boulders larger than about 2–3 m. **Rocks below the DTM's effective resolution are not represented.** JPL's Mars 2020 analysis used a separate rock-abundance (CFA) map from HiRISE image shadows for those (Ono et al. 2016), and this project has no such map yet. |

### 2.4 Hillshade — `hillshade`, `hirise_hillshade`

A Lambertian hillshade (azimuth 315°, altitude 45°) from Horn gradients, uint8 1–255
with 0 as nodata. It is visual only and never used in analysis.

### 2.5 Aspect

Not published as a layer. The plane-fit gradient components already contain it, and no
current consumer needs aspect. Directional (heading-aware) costs would use
`(b_x, b_y)` directly (`path-planning.md` §6).

---

## 3. Descriptors evaluated and not selected (yet)

| Metric | Reference | Why not now |
|---|---|---|
| **TRI** (terrain ruggedness index) | Riley, DeGloria & Elliot 1999, *Intermountain J. Sci.* 5:23–27; `gdaldem TRI` ([GDAL](https://gdal.org/programs/gdaldem.html)) | Mean absolute difference to the 8 neighbours, so it **conflates slope and roughness**: a smooth 20° plane gets a high TRI. It is also tied to a 3 × 3 window, not the vehicle footprint. Rover practice separates tilt from residual roughness (GESTALT). |
| **VRM** (vector ruggedness measure) | Sappington et al. 2007, *J. Wildl. Manage.* 71:1419, doi:10.2193/2005-723 | Decouples slope better than TRI, but it is dimensionless (dispersion of unit normals), so it cannot be compared with a clearance in metres. The plane residual is in metres. |
| **TPI / local relief** | Weiss 2001; `gdaldem TPI`, `roughness` | A landform position or classification metric. Relief without detrending repeats the slope problem above. |
| **Curvature** (profile/plan) | Zevenbergen & Thorne 1987, doi:10.1002/esp.3290120107 | Second derivatives at 1 m amplify DTM noise. A useful mobility use (high-centring on crests, catching the belly pan) needs clearance modelling of the vehicle body, i.e. ACE-style conservative clearance bounds (Otsu et al., [arXiv:1808.00031](https://arxiv.org/abs/1808.00031)) on rover-scale DEMs. That is planned with a vehicle-geometry model, not as a generic curvature layer. |
| **Rock abundance (CFA)** | Golombek & Rapp 1997; Golombek et al. 2008/2012 (via Ono et al. 2016) | Needs rock detection from 25–30 cm HiRISE images, with shadow segmentation and a size-frequency fit. The data exist (HiRISE RED images), but this is a project of its own (§7). |
| **Terrain classes** (sand, bedrock, ripples…) | SPOC (Rothrock et al. 2016), used by Ono et al. 2016; AI4Mars (Swan et al. 2021) | ML on imagery. It would add the 10° sand slope limit and the "avoid ripples" rule. Deferred until the analytic baseline is in place (it now is). |

Library candidates for these metrics: `gdaldem` (C++, but not in our wheel-based image),
WhiteboxTools (Rust, 500+ tools including multiscale roughness), xarray-spatial and
RichDEM. Their compatibility findings are in `research.md` §11. The selected operators
need only SciPy's `ndimage.correlate` plus about 30 lines of NumPy, so they are
implemented directly and verified analytically. Every one of them is a planar operation
on a metric grid, so the body (Mars vs Earth) does not enter.

---

## 4. Metric grid validity

Pixel sizes are used as ground distances. This is valid because the region CRS is
Transverse Mercator with k0 = 1 on the Mars sphere: the point scale factor stays below
1 + 3·10⁻⁴ over the ±81.92 km frame (tested), and the HiRISE footprint lies within 8 km
of the central meridian (scale error < 3·10⁻⁶). Slopes, tilts and distances therefore
have < 0.03 % projection error. Heights are areoid heights, but over a 5 m patch the
areoid is planar to far below the DTM precision.

---

## 5. Uncertainty

Two kinds of DTM error matter, and they act at different scales:

| Error | Scale | Affects | Value used |
|---|---|---|---|
| Absolute (EP, control to MOLA) | product-wide / long wavelength | absolute heights, comparisons between products | 0.25 m (`vertical_precision`, *assumption* within the published "tens of cm") |
| **Relative**, between posts a few metres apart | patch (5 m) | tilt, roughness | **0.05 m** (`relative_precision`, *derived*, §6) |

**Propagation to tilt (analytic).** For independent height errors σ, least squares gives
`Var(b_x) = Var(b_y) = σ²/S` with `S = n Σu_j²` (S = 50 m² for 5 × 5 at 1 m), so
`σ_tilt ≈ deg(σ/√S)`. This is exact at zero slope and conservative elsewhere, because
`d atan(s)/ds ≤ 1`. With σ = 0.05 m, **σ_tilt = 0.41°**; with σ = 0.25 m it would be
2.03°. Verified by Monte Carlo: on pure noise the mean tilt equals the Rayleigh mean
`σ_tilt·√(π/2)` to within 3 % (`test_tilt_sigma_matches_monte_carlo`).

**Propagation to roughness.** A residual's error is ≈ σ (a single post against a plane
fitted to 25 posts), so σ_roughness = σ. The expected maximum residual of a perfectly
planar patch observed with noise σ is ≈ σ·√(2 ln n²) (`residual_noise_floor`, checked by
Monte Carlo), which is the detection floor of the roughness metric.

The uncertainties drive the *marginal* class in `traversability.md`: a cell whose
feature lies within kσ of a limit cannot be classified safe with confidence.

**Assumption to keep in view:** stereo-DTM errors are spatially correlated, both through
correlation-window smoothing and through SOCET "box" artefacts of 0.5–1 m
(uahirise.org). Correlated long-wavelength error largely cancels in a 5 m plane fit;
box artefacts do not, and they show up as spurious steps. No per-post quality map is
used yet: the HiRISE FOM is distributed only as a colour-coded browse product, and the
USGS ARD `DTFEC` COG is a 3-band uint8 rendering of it, not FOM values (VERIFIED by
reading it).

---

## 6. Validation on the real data

Reproduce with `docker compose run --rm init-data marsgis <command>`.

**Feature distributions** (`feature-stats --layer hirise_roughness_5m --above 0.2 --above 0.35`),
over 70.3 M valid cells:

| quantile | 1 % | 10 % | 25 % | 50 % | 75 % | 90 % | 98 % | 99.9 % |
|---|---|---|---|---|---|---|---|---|
| roughness (m) | 0.0085 | 0.017 | 0.024 | **0.037** | 0.058 | 0.085 | 0.144 | 0.345 |
| tilt (°) | – | – | – | 3.05 | – | 9.09 | 17.7 | 33.1 |

Fraction above 0.2 m roughness: 0.63 %. Above 0.35 m: 0.095 %. Tilt above 10°: 8.2 %;
above 20°: 1.4 %.

**Bound on the relative noise.** If the posts carried independent noise of σ = 0.25 m
(the absolute precision), the roughness of *every* patch would be at least ≈ 2σ ≈ 0.5 m
(the noise floor). The observed **median is 0.037 m and the 1st percentile 0.0085 m**,
so the uncorrelated relative error must be ≲ 0.02 m. SOCET's correlation window smooths
the DTM, which is consistent with the ~3–5 m effective resolution. We adopt
**σ_rel = 0.05 m** (2.5× the bound) as a conservative value, declared with
`basis: derived` in `config/sources.yaml`. The consequence: using the absolute EP for
patch features would have flagged 6 % of the observed terrain as "marginal" (measured
on a first build), whereas σ_rel flags 0.13 %.

**Cross-product check, HiRISE vs CTX** (`compare-dtm`). The 1 m HiRISE DTM was reduced
to exact 20 × 20 block means on the nested grid (no resampling; incomplete blocks
dropped) and differenced with the 20 m CTX DTM over 174 915 cells:

| mean | median | std | p05 | p95 |
|---|---|---|---|---|
| **−26.3 m** | −26.9 m | 8.6 m | −36.5 m | −14.3 m |

The offset matches the CTX product's own QA report (CTX sits on average 27.4 m above
the MOLA shots, σ 10.9 m; `qa_metrics.txt`), while the HiRISE DTM is tied to MOLA.
**The two DTMs are therefore not interchangeable in absolute height**, and
fusing them (mosaics, hierarchical planning across products) first requires vertical
co-registration, e.g. a plane or low-order surface fitted to the overlap differences.
Patch-scale descriptors are unaffected because they depend only on relative heights
within one product.

**Visual check.** The derived classes put hazards on the delta scarp and crater rims
and leave the crater floor nominal (`docs/img/traversability.png`), which matches the
known geology of the landing site.

---

## 7. Known limitations and next steps

1. **Sub-resolution rocks** are invisible to DTM descriptors. Next step: a rock-abundance
   (CFA) layer from HiRISE RED images (25 cm), detecting shadows and fitting the
   Golombek size-frequency model, as in the Mars 2020 traversability tools.
2. **No per-post quality layer.** Request FOM values (not the browse rendering) from the
   HiRISE team, or derive a quality proxy, e.g. local disagreement between the two
   stereo orthoimages.
3. **SOCET artefacts** (boxes, striping along the image lines) appear as roughness and
   tilt spikes. Horizontal striping is visible in the tilt-derived classes on the scarp.
   A detector for edges aligned with image lines would separate artefacts from terrain.
4. **Single footprint scale (5 m).** Additional scales such as 3 m (wheel) or 10 m (local
   landform) are configuration-only (`window_m`). Multi-scale stacks become useful once a
   vehicle model consumes them.
5. **Memory.** Strip processing bounds memory for every derivation. Statistics read
   strips too, and only the `feature-stats` diagnostic loads a whole layer.
