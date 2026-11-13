# Traversability model

Traversability is a property of a **vehicle on a terrain**, not of the terrain alone.
marsgis keeps five representations separate. Each is its own published layer or result,
with its own provenance:

| Level | What | Where | Vehicle-dependent? |
|---|---|---|---|
| 1. Raw observations | DTM as delivered (PDS3 IMG, CTX COG) | `data/raw/` | no |
| 2. Terrain features | elevation on the grid, slope, footprint tilt, footprint roughness | `terrain.py`, layers `hirise_*` | no (scale-dependent only) |
| 3. Terrain classification | nominal / slow / marginal / hazard / unknown | `traversability.py`, `trav_class_<vehicle>` | **yes** |
| 4. Traverse time | expected seconds per metre | `time_<vehicle>` | **yes** |
| 5. Planning cost | objective per metre: time × explicit risk weight | `cost_<vehicle>` | **yes** (+ risk preference) |

Vehicle parameters live in `config/vehicles/<id>.yaml`, never in terrain code. Adding a
vehicle means adding a YAML file and three layer recipes. The API exposes every parameter
together with its basis: `GET /api/vehicles/perseverance`.

---

## 1. Precedent: how JPL estimated traversability for Mars 2020

The model follows the structure of the Mars 2020 Traversability Tools used during
Mars 2020 landing-site selection (including Jezero), described in **Ono et al. 2016**,
"Data-driven surface traversability analysis for Mars 2020 landing site selection",
IEEE Aerospace Conf., doi:10.1109/AERO.2016.7500597
([PDF](https://www-robotics.jpl.nasa.gov/media/documents/Data-driven%20surface%20traversability%20analysis%20for%20Mars%202020%20landing%20site%20selection.pdf)).
Read for this work, it states:

* inputs: a **slope map** from HiRISE stereo DEMs, a **rock abundance (CFA)** map from
  automated shadow-based rock detection, a **terrain class** map (CNN classifier, 11
  classes) and a manual **hazard** map;
* a *binary* model (traversable if CFA and slope are below thresholds, with hazard and
  untraversable classes excluded) for distance-optimal planning, and a *continuous*
  model that maps terrain to **driving speed**, with cost per pixel = **1 / speed**, for
  time-optimal planning (Dijkstra);
* MSL-heritage capability: "able to climb slopes up to approximately **20 degrees**" in
  bedrock or cohesive regolith, "approximately **10 degrees**" on sand dunes, and ripple
  fields with amplitude ≥ 10 cm should be avoided;
* AutoNav rate "approximately **80 m/hr**" for CFA < 7 %, "approximately **60 m/hr**"
  for CFA 7–15 %, and CFA > 15 % "should be avoided";
* when terrain class is unknown, the smooth-terrain model is used. **We deliberately
  differ here** (§4): unknown *elevation* coverage is impassable.

Onboard, rovers evaluate a rover-sized patch: a plane fit, then tilt, residual
roughness, step and "border" (unknown-neighbour) hazards, with unknown cells given zero
certainty (GESTALT; Goldberg, Maimone & Matthies 2002). Perseverance's ENav adds
heading-dependent, conservative clearance evaluation (ACE; Otsu et al.,
[arXiv:1808.00031](https://arxiv.org/abs/1808.00031)).

What we can reproduce from public data: the slope/tilt and roughness parts, the speed
model, and time-optimal planning. What we cannot yet reproduce: CFA and terrain classes
(`terrain-analysis.md` §3, §7).

---

## 2. Vehicle parameters (`config/vehicles/perseverance.yaml`)

**Status: EXPERIMENTAL.** These are public-source planning-analysis values, not the
flight team's onboard limits.

| Parameter | Value | Basis | Source / derivation |
|---|---|---|---|
| footprint diameter | 4.04 m | derived | √(3.0² + 2.7²), footprint from the [JPL Mars 2020 press kit](https://www.jpl.nasa.gov/news/press_kits/mars_2020/launch/quick_facts/) |
| mass | 1025 kg | published | JPL press kit |
| max tilt | 20° | published | Ono et al. 2016 (MSL heritage, cohesive terrain) |
| nominal tilt | 10° | assumption | speed reduction starts here |
| speed factor at max tilt | 0.5 | assumption | linear slow-down nominal → max tilt (slip, caution) |
| max step (roughness) | 0.35 m | assumption | order of magnitude: AutoNav avoided a 35 cm rock ([PIA26073](https://www.jpl.nasa.gov/images/pia26073-perseverance-autonav-avoids-a-boulder)); wheel Ø 52.5 cm. *Not* a published limit (a secondary-source "wheel-drop" claim could not be confirmed). |
| slow step | 0.20 m | assumption | analogue of the CFA 7–15 % band |
| nominal speed | 80 m/h | published | Ono et al. 2016 (JPL reports ~110 m/h achieved with ENav, [JPL](https://robotics.jpl.nasa.gov/what-we-do/flight-projects/mars-2020-rover/m2020mobility/)) |
| slow speed | 60 m/h | published | Ono et al. 2016 |
| uncertainty k | 1 | assumption | width of the marginal band, in σ |
| marginal cost multiplier | 2 | assumption | risk aversion (dimensionless) |
| unknown policy | forbid | design rule | §4 |

The model validates parameter consistency on load (e.g. nominal < max tilt,
slow step < max step).

---

## 3. Classification and speed model

Inputs per cell: footprint tilt *t* (°) and roughness *r* (m) at the vehicle's footprint
scale (5 m), plus their 1σ uncertainties σ_t and σ_r propagated from the source's
declared relative precision (`terrain-analysis.md` §5: σ_t = 0.41°, σ_r = 0.05 m).

```
UNKNOWN  (5)  t or r missing (footprint not fully observed)
HAZARD   (4)  t > max_tilt  or  r > max_step
MARGINAL (3)  not HAZARD and ( t > max_tilt − k·σ_t  or  r > max_step − k·σ_r )
SLOW     (2)  t > nominal_tilt  or  r > slow_step
NOMINAL  (1)  otherwise
(0 = outside the raster, nodata)
```

```
base speed  = speed_nominal          if r ≤ slow_step  else speed_slow        [m/h]
tilt factor = 1                      for t ≤ nominal_tilt
            → linear to speed_factor_at_max_tilt at t = max_tilt
time        = 3600 / (base × factor)                                       [s/m]
cost        = time × marginal_cost_multiplier   on MARGINAL cells, = time otherwise
time, cost  = NaN on HAZARD and UNKNOWN cells (never entered by the planner)
```

Properties tested in `tests/test_traversability.py`: class boundaries, the speed formula,
monotonic time with tilt, marginal weighting, and that unknown and hazard cells are never
traversable.

### Why "marginal" exists
A threshold applied to an uncertain measurement has an uncertain outcome. Cells whose
feature lies within k·σ of a hard limit cannot be confirmed safe. They stay traversable
(the measurement says safe), but the planner pays an explicit, configurable penalty to
avoid them. This is a deterministic approximation of a probabilistic hazard. A
probabilistic alternative, P(t > limit) = 1 − Φ((limit − t)/σ_t) turned into expected
risk, is listed in §7.

### Result on the Jezero HiRISE DTM
Over the 70.3 M observed cells: **nominal 91.6 %, slow 6.9 %, marginal 0.13 %,
hazard 1.4 %**. A further 25.2 M cells in the raster rectangle are unknown (outside the
DTM strip or with incomplete footprints). Hazards concentrate on the delta scarp and
crater rims (`docs/img/traversability.png`).

---

## 4. Unknown and low-quality terrain

* **Unknown is never safe.** A cell is UNKNOWN when any elevation in its footprint is
  missing. The cost is NaN, and the planner never enters it (tested:
  `test_unknown_policy_unknown_cells_block`). This is stricter than Ono et al. 2016 (who
  fall back to a default class), and matches GESTALT's zero-certainty treatment of
  unknown cells.
* **Insufficient quality** is handled through the uncertainty band (marginal class). A
  per-post quality map (HiRISE FOM values) is not available in usable form
  (`terrain-analysis.md` §5). When it is, posts with suspicious correlation should be
  treated as missing for footprint features, turning their cells UNKNOWN.
* **Outside the product's coverage**, routes are refused (`outside_coverage`) rather than
  silently planned on the coarser CTX product.

---

## 5. Sensitivity analysis

`marsgis sensitivity <lon,lat> <lon,lat>` re-evaluates the vehicle model inside the
planning window for one-at-a-time parameter changes and re-plans with the same engine.
It does not rebuild published layers. Route: Perseverance landing site
(77.4508° E, 18.4447° N) → delta front (77.4150° E, 18.4650° N), 4.7 M-cell window,
straight line 2345 m.

| parameter | value | distance (m) | drive time (h) | route shift mean / max (m) | length by class (m) |
|---|---|---|---|---|---|
| **baseline** | — | 2555.4 | 32.89 | 0 / 0 | nominal 2294, slow 261 |
| max_tilt | 15° | **no route**: the delta front cannot be reached below 15° within the window | | | |
| max_tilt | 25° | 2541.4 | 32.41 | 6 / 36 | nominal 2305, slow 237 |
| nominal_tilt | 5° | 2553.9 | 33.58 | 37 / 70 | nominal 2073, slow 481 |
| nominal_tilt | 15° | 2555.4 | 32.17 | 43 / 82 | nominal 2427, slow 129 |
| speed_factor_at_max_tilt | 0.25 | 2561.2 | 33.76 | 2 / 22 | nominal 2293, slow 269 |
| speed_factor_at_max_tilt | 1.0 | 2552.1 | 31.90 | 62 / 102 | nominal 2141, slow 411 |
| max_step | 0.25 / 0.5 m | 2555.4 | 32.89 | 0 / 0 | unchanged |
| slow_step | 0.10 m | 2559.6 | 33.00 | 14 / 35 | nominal 2249, slow 311 |
| slow_step | 0.30 m | 2555.4 | 32.89 | 5 / 35 | nominal 2307, slow 248 |
| marginal_cost_multiplier | 1 | 2555.4 | 32.89 | 0 / 1 | + marginal 3 |
| marginal_cost_multiplier | 4 | 2555.4 | 32.89 | 0 / 0 | unchanged |
| uncertainty_k | 0 / 2 | 2555.4 | 32.89 | 0 / 1 | ± marginal 2–3 |
| σ_z (relative) | 0.02 m | 2555.4 | 32.89 | 0 / 1 | unchanged |
| σ_z | 0.25 m (absolute EP) | 2561.3 | 33.04 | 40 / 74 | + marginal 3 |

Reading:
* On this route the estimated **drive time is robust to −3 % / +3 %** across all
  variants. It is dominated by distance at nominal speed (80 m/h), so the
  **speed_nominal assumption** is the largest single source of time uncertainty: using
  the ~110 m/h JPL reports for ENav instead of 80 m/h would cut every time by 27 %.
* **Route geometry is most sensitive to the tilt-related speed assumptions**
  (nominal_tilt, speed factor): the corridor across the floor moves by 40–60 m on
  average. All variants cross the delta scarp at the same gap.
* **max_tilt is the decisive feasibility parameter.** At 15° the destination becomes
  unreachable, which is consistent with the delta-front ascent needing 15–20° locally.
* Step and roughness parameters do not matter here, because the crater floor is smooth
  at 5 m scale (98th percentile roughness 0.14 m). They would matter on rough units.
* These are single-route results. A proper study would sample many origin–destination
  pairs and report distributions, as Ono et al. 2016 did with landing ellipses.

---

## 6. Limitations

1. Parameters are public-source approximations. Any operational use needs the flight
   team's mobility envelope (slip models, suspension limits, ACE clearance).
2. No terrain classes: sand (10° limit), ripples (avoid) and other terrain-dependent
   speeds are not modelled, so slow or impassable sand looks nominal.
3. No rock abundance: rock fields below DTM resolution look nominal. This is the main
   reason the time estimate is optimistic (Ono et al. add +30 % for obstacle avoidance
   and opportunistic science, based on Curiosity odometry).
4. Heading-independent (disc) evaluation: cross-slope and pitch are not distinguished.
5. No energy or thermal state: potential energy of climbs is only reported.

## 7. Next steps (ordered by expected value)

1. **CFA rock-abundance layer** from HiRISE RED imagery, fed into the speed model as in
   Ono et al. 2016.
2. **Probabilistic hazards**: `P(hazard) = 1 − Φ((limit − x)/σ)` per feature, with
   cost `= time × (1 + λ·P)` or a chance constraint on cumulative risk along the route
   (Ono et al. cite chance-constrained planning [2–3]).
3. **Heading-aware tilt**: decompose the plane gradient into pitch and roll per heading
   (16 headings), feeding a state lattice (`path-planning.md` §6).
4. **Terrain classes** (ML, e.g. SPOC/AI4Mars-style) once labelled data for Jezero are
   available. Perseverance traverse data could serve as validation labels.
5. **Validation against the actual Perseverance traverse**: the rover's driven path in
   Jezero is public (MMGIS / PDS localisation products). Compare planned corridors and
   class statistics along the real path.
