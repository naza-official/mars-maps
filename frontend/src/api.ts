// Typed client for the marsgis API. All geometry is in the region CRS (metres)
// unless a field name says lonlat (planetocentric, east-positive, Mars sphere).

export interface LayerStats {
  valid_percent: number;
  minimum: number | null;
  maximum: number | null;
  mean: number | null;
  stddev: number | null;
  p02: number | null;
  p98: number | null;
}

export interface LayerInfo {
  id: string;
  title: string;
  description: string;
  units: string;
  version: string;
  bounds_xy: [number, number, number, number];
  resolution_m: number;
  dtype: string;
  nodata: number | "nan" | null;
  statistics: LayerStats;
  render: { colormap: string | null; rescale: "auto" | [number, number] };
  /** non-empty for categorical layers (classes): value -> label/colour */
  categories: Category[];
  footprint_lonlat: { type: "Polygon"; coordinates: number[][][] };
}

export interface Category {
  value: number;
  label: string;
  color: string;
  description: string;
}

export interface PlanningInfo {
  cost: string;
  time: string;
  classes: string;
  samples: Record<string, string>;
  max_cells: number;
  default_margin_m: number;
}

export interface RegionInfo {
  id: string;
  name: string;
  body: string;
  crs: {
    name: string;
    wkt2: string;
    proj4: string;
    geographic_proj4: string;
    conventions: Record<string, string>;
  };
  frame_xy: [number, number, number, number];
  center_lonlat: [number, number];
  base_resolution_m: number;
  tms: { id: string; base_zoom: number; max_zoom: number; tile_size: number };
  layers: LayerInfo[];
  planning: Record<string, PlanningInfo>;
}

export interface LayerSample {
  units: string;
  status: "ok" | "nodata" | "outside";
  value: number | null;
  row: number;
  col: number;
  resolution_m: number;
}

export interface InspectResult {
  region: string;
  location: {
    x_m: number;
    y_m: number;
    lon_east_180: number;
    lon_east_360: number;
    lon_west_360: number;
    lat_planetocentric: number;
    lat_planetographic: number;
    crs: string;
  };
  layers: Record<string, LayerSample>;
}

export interface Waypoint {
  x: number;
  y: number;
}

export interface RouteResult {
  engine: string;
  resolution_m: number;
  vehicle: { id: string; name: string; status: string };
  geometry_xy: { type: "LineString"; coordinates: [number, number][] };
  summary: {
    distance_m: number;
    straight_line_m: number;
    objective_cost_s: number;
    estimated_drive_time_s: number;
    estimated_drive_time_h: number;
    length_by_class_m: Record<string, number>;
    corner_cuts: number;
    vertices: number;
    surface_distance_m?: number;
    elevation_gain_m?: number;
    elevation_loss_m?: number;
    potential_energy_climb_wh?: number;
    tilt_max?: number;
    roughness_max?: number;
  };
  legs: { leg: number; objective_cost_s: number; cells_settled: number }[];
  profile: {
    distance_m: number[];
    cumulative_objective_s: number[];
    cumulative_time_s: number[];
    class: number[];
    [sample: string]: (number | null)[];
  };
  class_runs: { class: number; label: string; from_m: number; to_m: number }[];
  warnings: string[];
  notes: string[];
  inputs: Record<string, { layer: string; version: string }>;
  compute_ms: number;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: unknown,
  ) {
    super(typeof detail === "object" && detail && "message" in detail
      ? String((detail as { message: unknown }).message)
      : `HTTP ${status}: ${JSON.stringify(detail)}`);
  }
}

async function getJSON<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}: ${await r.text()}`);
  return (await r.json()) as T;
}

export const api = {
  region: (id: string) => getJSON<RegionInfo>(`/api/regions/${encodeURIComponent(id)}`),
  inspect: (id: string, x: number, y: number) =>
    getJSON<InspectResult>(`/api/regions/${encodeURIComponent(id)}/inspect?x=${x}&y=${y}`),
  route: async (id: string, waypoints: Waypoint[], vehicle: string, engine: string) => {
    const r = await fetch(`/api/regions/${encodeURIComponent(id)}/routes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ waypoints, vehicle, engine }),
    });
    const body = await r.json();
    if (!r.ok) throw new ApiError(r.status, body.detail ?? body);
    return body as RouteResult;
  },
  colormap: (name: string) =>
    getJSON<Record<string, [number, number, number, number]>>(
      `/api/colorMaps/${encodeURIComponent(name)}`,
    ),
  tileUrl(tmsId: string, region: string, layer: string, params: Record<string, string>): string {
    const q = new URLSearchParams({ region, layer, ...params });
    return `/api/cog/tiles/${tmsId}/{z}/{x}/{y}.png?${q.toString()}`;
  },
};
