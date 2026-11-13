# Path planning

The planner (`backend/src/marsgis/planning.py`) computes minimum-cost routes on
**numerical cost rasters**. It never reads rendered tiles, and it imports nothing from
FastAPI, TiTiler or the frontend. `routing.py` resolves catalog layers and coordinates
for the API and the CLI. The planner itself takes file paths and region-CRS coordinates.

---

## 1. Candidate algorithms and libraries

| Approach | Implementations examined | Optimality | Fits our problem? | Verdict |
|---|---|---|---|---|
| **Dijkstra on an 8-connected grid graph** | `scipy.sparse.csgraph.dijkstra` (SciPy 1.18.1, C), `skimage.graph.MCP_Geometric` (scikit-image 0.26.0, Cython), NetworkX (pure Python) | exact on the graph | yes: the same discretisation as Ono et al. 2016's time-optimal planner and GIS cost-distance | **Selected** (SciPy default, scikit-image as independent engine) |
| A* | pyastar2d 1.1.4 (C++, 4/8-connected, arm64 wheel VERIFIED); NetworkX `astar_path` | exact with an admissible heuristic | yes; admissible heuristic = straight-line distance × min cost per metre (45 s/m) | Deferred: Dijkstra over a 4.7 M-cell window takes ~2.5 s. Revisit when windows grow. |
| Fast marching (continuous eikonal) | scikit-fmm (no linux/arm64 cp312 wheel, VERIFIED by `pip --dry-run`, needs a source build); `skimage.graph.MCP` is the discrete analogue | first-order accurate continuous geodesics; removes the 8-connected metrication bias | partly: gives a smoother path, but the result is a field, needs back-tracing, and is harder to explain | Not selected; the metrication bias is documented instead (§4) |
| Any-angle (Theta*, Field D*) | Field D* ran on board the MER rovers as the global planner (Carsten et al. 2007, IEEE Aerospace, [PDF](https://www-robotics.jpl.nasa.gov/media/documents/IEEEAC-Carsten-1125.pdf)) | near-optimal, interpolated | yes; the best fix for 45° zig-zags | Next step (§6). No maintained Python implementation found. |
| Sampling-based (RRT*, PRM*, …) | **OMPL 2.0.1** (BSD; PyPI wheels, import on arm64 VERIFIED) | asymptotically optimal | designed for continuous configuration spaces with collision checkers; our cost field is dense and gridded | Not for global raster planning; useful later for local kinematic refinement |
| Hybrid A* / state lattice | Nav2 Smac Planner (ROS 2, C++; [docs](https://docs.nav2.org/configuration/packages/configuring-smac-planner.html)) | resolution-optimal over (x, y, θ) | heading and turning-radius constraints, exactly what skid-steer and rocker-bogie rovers need locally | Rejected as a dependency (needs a ROS 2 runtime, ROS costmap 0–255 semantics, Earth-agnostic but robot-frame oriented); its algorithm is the model for §6 |
| Multi-objective (Pareto label-setting) | no maintained Python package found | exact Pareto set | needed when distance, time, risk and exposure must be traded off, not weighted | Future, once more objectives exist |

Robotics frameworks were checked for planetary assumptions. OMPL and Nav2 are
body-agnostic: they work in a local metric frame, so a Mars TM frame works as is. Nav2's
costmaps assume 0–255 occupancy-cost semantics and a ROS TF tree, which do not fit a
scientific raster pipeline. None of these libraries reads planetary CRSs, which is why
the pipeline converts everything to the metric region grid first.

---

## 2. Selected implementation

**Engine `dijkstra` (default): SciPy `csgraph.dijkstra`** on an explicit undirected
graph:

* nodes: traversable cells (finite, positive cost) of the planning window;
* edges: 8-neighbourhood, built vectorised from four half-offsets;
* weight of edge (a, b) = `L · (c_a + c_b) / 2`, with L = res or res·√2. The cost per
  metre is integrated with the trapezoidal rule between cell centres, which is the
  `MCP_Geometric` and GIS cost-distance definition;
* **corner cutting is forbidden**: a diagonal edge exists only if both cells sharing the
  corner are traversable;
* path reconstruction from the predecessor array.

**Engine `mcp`: scikit-image `MCP_Geometric`.** It is independent code with the same
cost semantics and less memory (no explicit edge list). It **cannot** forbid corner
cutting, so the planner counts corner cuts in its result and reports them as a warning.

**Cross-validation:** on random cost fields without corner-cutting opportunities both
engines return the same optimum to 1e-9 relative (`test_engines_agree_on_random_costs`).
On the real Jezero route they agree to the last digit (118 411.75 s, 2555.4 m; the
API test `test_engines_agree_on_real_data`).

Why not write A*/Dijkstra ourselves: two maintained, tested library implementations
exist. Writing our own would add the most error-prone part (the priority queue and
relaxation) to code that only needs to build the graph.

---

## 3. Movement model and cost interpretation

| Aspect | Model |
|---|---|
| Space | cells of the 1 m HiRISE analysis grid in the region TM CRS (metric to < 3·10⁻⁶ here) |
| Connectivity | 8-connected, no corner cutting |
| Vehicle | a point at the cell centre. The footprint is already accounted for, because every feature is evaluated over the rover-sized patch around the cell (configuration-space evaluation, as in GESTALT). |
| Edge cost | `L · (c_a + c_b)/2` with `c` = `cost_<vehicle>` (s/m, risk-weighted time) |
| Objective | **risk-weighted traverse time** (s). Physical time is reported separately from `time_<vehicle>`. |
| Impassable | NaN cost (HAZARD and UNKNOWN classes) |
| Direction dependence | none (isotropic). Uphill and downhill cost the same (§6). |
| Multiple waypoints | sequential legs (start → via … → destination), each optimal; the concatenation is optimal for the fixed waypoint order |
| Search space | the waypoints' bounding box plus `margin_m` (default 300 m), clipped to the raster. If the route touches the window edge, a warning says a cheaper detour may exist outside. |

**Quantities are not silently mixed.** The optimiser minimises one quantity in seconds.
Risk enters only as an explicit, dimensionless, documented multiplier on marginal cells.
The result reports every physical quantity separately: planimetric and surface distance
(m), estimated drive time (s), objective (s), elevation gain and loss (m), potential
energy of climbs (Wh = m·g·Δh⁺/3600 with g = 3.71 m/s², NASA Mars Fact Sheet), max and
mean tilt (°) and roughness (m), and length by class (m). Radiation and other exposure
objectives would enter as their own reported integrals (rate × time), never folded into
time without a stated conversion.

---

## 4. Optimality and limitations

* **Optimal on the graph**: Dijkstra with non-negative weights is exact. "Optimal" means
  minimum cost among 8-connected cell paths inside the planning window.
* **Metrication bias**: an 8-connected path between two points on uniform terrain is
  octile, not Euclidean. Straight lines at 22.5° are 8.2 % too long (on average about
  5.4 %, the correction Ono et al. 2016 applied), and paths show 45° and 0° segments
  (visible in `docs/img/route.png`). The fix is any-angle planning (§6), not smoothing:
  smoothing a path changes its cost without re-checking the cells it crosses.
* **Window optimality**: the planning window is bounded. A warning is raised when the
  route touches the window edge. `margin_m` is a request parameter, capped by
  `max_cells` (8 M) per request.
* **Feasibility under uncertainty**: a route through nominal cells is safe only to the
  extent of the terrain model (`traversability.md` §6). Corridors, not lines, are the
  appropriate interpretation at orbital resolution.

---

## 5. Results and performance (real data)

Landing site → delta front (2345 m straight line), 4.7 M-cell window, 8 arm64 cores in
Docker:

| engine | distance | objective / time | cells settled | compute |
|---|---|---|---|---|
| dijkstra | 2555.4 m | 118 412 s (32.9 h) | 4.60 M | 2.8 s |
| mcp | 2555.4 m | 118 412 s | 4.39 M | 2.5 s |

Both include reading the layers, graph construction, search and diagnostics. Through the
API the same request takes 5–6 s (thread pool, cold page cache).

Measured peak memory (process RSS, including Python and libraries):

| engine | 4.7 M cells | 7.7 M cells |
|---|---|---|
| dijkstra (explicit graph, ~320 B/cell) | 1.50 GB | 2.36 GB (4.5 s) |
| mcp (implicit graph, ~120 B/cell) | 0.56 GB | – |

The per-request cap is therefore `max_cells: 8 000 000`. Doubling the margin to 600 m
(7.7 M cells) returns the identical route, which confirms that the default 300 m window
did not constrain it. For larger windows use the `mcp` engine (about 3× less memory, and
corner cuts are reported) or the hierarchical approach in §6.

The route result (`POST /api/regions/{id}/routes`) contains:
* `geometry_xy` (region CRS) and `geometry_lonlat` (Mars sphere, planetocentric, °E);
* `summary`: totals as listed in §3 plus corner cuts and the vertex count;
* `legs`: objective, settled cells and vertex index range per leg;
* `profile`: per-vertex distance along the route, cumulative objective and time, class,
  and the sampled values of elevation, tilt, roughness and 1 m slope. These are read
  from the published rasters at the exact cells; the test compares them with
  `rasterio.sample`;
* `class_runs`: contiguous stretches by class (explainability);
* `inputs`: layer ids and content-addressed versions of every input (reproducibility);
* `warnings`, `notes`, `units`, `compute_ms`.

Errors are structured (`422`/`404` with a `code`): `outside_coverage`,
`waypoint_not_traversable` (with the terrain class), `no_path`, `window_too_large`,
`unknown_vehicle`, `layers_missing`.

---

## 6. Next steps

1. **Any-angle planning** (Theta* or Field D*-style interpolation) to remove the 45°
   artefacts and the octile distance bias. The cost semantics stay the same.
2. **Direction-dependent costs** with a directed graph (`csgraph` supports it): pitch and
   roll from `(b_x, b_y)` projected on the edge direction, uphill/downhill asymmetry, and
   an energy objective `m g Δh⁺ + rolling resistance`. Memory doubles (directed edges).
3. **Heading/kinematic planning**: a state lattice over (x, y, θ) with 16 headings and
   turn-in-place costs, either at coarse resolution or as local refinement of the global
   corridor. Nav2 Smac and OMPL are the algorithmic references.
4. **Hierarchical / multi-resolution**: a coarse plan on the 20 m CTX product across the
   region, refined on 1 m HiRISE where available. This needs CTX–HiRISE vertical
   co-registration first (−26 m offset, `terrain-analysis.md` §6), plus a rule for which
   product governs in overlaps.
5. **Multi-objective**: Pareto fronts of (time, risk, exposure) instead of a weighted sum,
   once exposure layers exist.
6. **Many-pairs analysis** (Ono et al.'s sequential Dijkstra): one Dijkstra from a
   destination gives the cost-to-go from every cell, which suits ellipse-wide or
   corridor statistics.
